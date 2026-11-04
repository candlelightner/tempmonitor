#!/usr/bin/env python3
"""Adopt the existing homeserver-2g hotspot. Never create or rewrite an AP or DHCP config."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

AP_SERVICE = "hotspot-hostapd.service"
DHCP_SERVICE = "hotspot-dhcp.service"
STATUS_DIR = Path("/run/tempmonitor-hotspot")
INSTALL_DIR = Path("/usr/local/lib/tempmonitor")
UNIT_DIR = Path("/etc/systemd/system")
GUARD_UNIT = "tempmonitor-hotspot-isolation.service"


def command(*args, check=True, input=None):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=10, check=False)
    if check and result.returncode:
        # Never echo hostapd or dnsmasq output: it may contain private configuration.
        raise RuntimeError(f"{args[0]} failed ({result.returncode})")
    return result.stdout.strip()


def enabled(unit):
    return command("systemctl", "is-enabled", unit, check=False) == "enabled"


def active(unit):
    return command("systemctl", "is-active", unit, check=False) == "active"


def unit_text(unit):
    text = command("systemctl", "cat", unit)
    if not text:
        raise RuntimeError(f"Existing unit {unit} was not found; refusing to create an access point")
    return text


def config_path(text):
    # A real hostapd config must already exist, and its contents are never returned to the app.
    match = re.search(r"^ExecStart=(?:\S*/)?hostapd\s+(?:-\S+\s+)*(/[^\s]+)", text, re.M)
    if not match:
        raise RuntimeError("Cannot identify the existing hostapd configuration")
    return Path(match[1])


def parse_hostapd(text):
    result = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def addresses():
    return json.loads(command("ip", "-j", "address", "show"))


def assert_no_conflicts(ap, subnet, interfaces, routes):
    for route in routes:
        if route.get("dst") == "default" and route.get("dev") == ap:
            raise RuntimeError("The AP is an uplink; refusing to change anything")
    for interface in interfaces:
        if interface["ifname"] == ap:
            if interface.get("master"):
                raise RuntimeError("The AP is bridged; refusing to replace the existing network")
            continue
        for address in interface.get("addr_info", []):
            if address["family"] == "inet":
                network = ipaddress.ip_network(f"{address['local']}/{address['prefixlen']}", strict=False)
                if subnet.overlaps(network):
                    raise RuntimeError(f"Hotspot subnet conflicts with interface {interface['ifname']}")


def isolated(ap, rules):
    chains = {(x["chain"]["family"], x["chain"]["table"], x["chain"]["name"]): x["chain"]
              for x in rules.get("nftables", []) if "chain" in x}
    drops = {}
    for entry in rules.get("nftables", []):
        rule = entry.get("rule", {})
        key = (rule.get("family"), rule.get("table"), rule.get("chain"))
        chain = chains.get(key, {})
        if key[0] != "inet" or chain.get("hook") != "forward":
            continue
        expr = rule.get("expr", [])
        for direction in ("iifname", "oifname"):
            matches = any(x.get("match", {}).get("left") == {"meta": {"key": direction}} and
                          x["match"].get("op") == "==" and x["match"].get("right") == ap for x in expr)
            if matches and any("drop" in x for x in expr):
                drops.setdefault(key, set()).add(direction)
    # A dedicated forward chain with only the two drop rules cannot hide an earlier accept exception.
    for key, directions in drops.items():
        related = [x["rule"] for x in rules["nftables"] if "rule" in x and
                   (x["rule"].get("family"), x["rule"].get("table"), x["rule"].get("chain")) == key]
        if len(directions) == 2 and len(related) == 2:
            return True
    return False


def preflight(*, allow_stopped=False):
    if sys.platform != "linux":
        raise RuntimeError("This installer requires Linux")
    for tool in ("iw", "ip", "nft", "systemctl", "dnsmasq", "hostapd"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing {tool}; install the Ubuntu hostapd/dnsmasq/nftables/iw packages first")
    ap_unit, dhcp_unit = unit_text(AP_SERVICE), unit_text(DHCP_SERVICE)
    config = parse_hostapd(config_path(ap_unit).read_text())
    ap = config.get("interface", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", ap):
        raise RuntimeError("Invalid existing AP interface")
    if config.get("ssid") != "homeserver-2g":
        raise RuntimeError("Expected the existing homeserver-2g hotspot; refusing to adopt another SSID")
    if config.get("hw_mode") != "g" or not 1 <= int(config.get("channel", "0")) <= 11:
        raise RuntimeError("The existing hotspot must use a fixed 2.4 GHz channel (1–11)")
    if config.get("wpa") != "2" or "WPA-PSK" not in config.get("wpa_key_mgmt", ""):
        raise RuntimeError("The existing hotspot does not declare WPA2-PSK")
    if "CCMP" not in config.get("rsn_pairwise", ""):
        raise RuntimeError("The existing hotspot does not declare WPA2 CCMP encryption")
    # Read-only capability check. Never take over the managed radio or stop a network manager.
    if not re.search(r"\* AP\s*$", command("iw", "list"), re.M):
        raise RuntimeError("Wi-Fi hardware does not advertise AP capability")
    match = re.search(r"--dhcp-range=([\d.]+),([\d.]+),([\d.]+)", dhcp_unit)
    if not match or f"--interface={ap}" not in dhcp_unit or "--bind-interfaces" not in dhcp_unit:
        raise RuntimeError("Cannot verify the existing DHCP scope and interface binding")
    start, end, mask = match.groups()
    subnet = ipaddress.ip_network(f"{start}/{mask}", strict=False)
    private_ranges = [ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    if not any(subnet.subnet_of(x) for x in private_ranges) or ipaddress.ip_address(end) not in subnet:
        raise RuntimeError("Existing DHCP scope is not a private local subnet")
    if not re.search(r"--dhcp-option=(?:option:)?router(?:\s|$)", dhcp_unit):
        raise RuntimeError("DHCP advertises a router; refusing to change an existing DHCP configuration")
    interfaces = addresses()
    routes = json.loads(command("ip", "-j", "route", "show", "table", "all"))
    assert_no_conflicts(ap, subnet, interfaces, routes)
    info = next((x for x in interfaces if x["ifname"] == ap), None)
    if info is None and not allow_stopped:
        raise RuntimeError("The configured AP interface is unavailable; run recovery after reviewing preflight")
    ipv4 = [a for a in (info or {}).get("addr_info", []) if a["family"] == "inet"]
    if info and not any(ipaddress.ip_address(a["local"]) in subnet for a in ipv4):
        raise RuntimeError("Existing AP address does not match its DHCP scope")
    rules = json.loads(command("nft", "-j", "list", "ruleset"))
    isolation = isolated(ap, rules)
    persistent = False
    for nft_path in re.findall(r"\bnft\s+-f\s+(/\S+)", ap_unit):
        try:
            text = Path(nft_path).read_text()
            persistent = all(re.search(rf'{direction}\s+"?{re.escape(ap)}"?\s+drop', text)
                             for direction in ("iifname", "oifname"))
        except OSError:
            pass
    persistent = persistent or enabled(GUARD_UNIT)
    return {"ssid": "homeserver-2g", "interface": ap, "subnet": str(subnet),
            "address": ipv4[0]["local"] if ipv4 else None,
            "ap_active": active(AP_SERVICE), "dhcp_active": active(DHCP_SERVICE),
            "boot_enabled": enabled(AP_SERVICE) and enabled(DHCP_SERVICE),
            "isolated": isolation, "isolation_persistent": persistent}


def status():
    try:
        state = preflight(allow_stopped=True)
        stations = command("iw", "dev", state["interface"], "station", "dump", check=False)
        associated = set(re.findall(r"^Station ([0-9a-f:]{17})", stations, re.M))
        leases = []
        lease_file = Path("/var/lib/misc/dnsmasq.leases")
        if lease_file.exists():
            for line in lease_file.read_text().splitlines():
                fields = line.split()
                if len(fields) >= 4 and fields[1].lower() in associated:
                    leases.append({"mac": fields[1], "ip": fields[2], "hostname": fields[3]})
        known = {x["mac"].lower() for x in leases}
        leases += [{"mac": mac, "ip": None, "hostname": "DHCP pending"} for mac in associated - known]
        state.update(available=True, clients=leases)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        state = {"available": False, "ssid": "homeserver-2g", "clients": [],
                 "message": "Host hotspot checks failed; run sudo python3 scripts/hotspot.py preflight"}
    return {**state, "sampled_at": time.time()}


def publish():
    STATUS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = STATUS_DIR / "status.json.tmp"
    temporary.write_text(json.dumps(status()))
    temporary.chmod(0o644)
    temporary.replace(STATUS_DIR / "status.json")


def write_if_changed(path, content, mode=0o644):
    if not path.exists() or path.read_text() != content:
        path.write_text(content)
        path.chmod(mode)


def install_guard(ap):
    path = Path("/etc/tempmonitor-hotspot")
    path.mkdir(mode=0o755, exist_ok=True)
    guard = path / "isolation.nft"
    write_if_changed(guard, guard_rules(ap))
    write_if_changed(UNIT_DIR / GUARD_UNIT, f'''[Unit]
Description=Missing isolation for the existing homeserver-2g hotspot
Before={AP_SERVICE}
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 /usr/local/lib/tempmonitor/hotspot.py isolate
[Install]
WantedBy=multi-user.target
''')
    command("systemctl", "daemon-reload")
    command("systemctl", "enable", GUARD_UNIT)
    if not isolated(ap, json.loads(command("nft", "-j", "list", "ruleset"))):
        isolate()


def guard_rules(ap):
    return f'''table inet tempmonitor_hotspot_guard {{
  chain forward {{
    type filter hook forward priority -110; policy accept;
    iifname "{ap}" drop
    oifname "{ap}" drop
  }}
}}
'''


def isolate():
    state = preflight(allow_stopped=True)
    # Replace only our dedicated table, atomically; never flush or edit existing host rules.
    existing = command("nft", "list", "table", "inet", "tempmonitor_hotspot_guard", check=False)
    rules = ("delete table inet tempmonitor_hotspot_guard\n" if existing else "") + guard_rules(state["interface"])
    command("nft", "-f", "-", input=rules)


def install():
    state = preflight()
    if not state["ap_active"] or not state["dhcp_active"]:
        raise RuntimeError("Existing hotspot is not running; review preflight and run recover first")
    INSTALL_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    write_if_changed(INSTALL_DIR / "hotspot.py", Path(__file__).read_text(), 0o755)
    if not state["isolated"] or not state["isolation_persistent"]:
        install_guard(state["interface"])
    for unit in (AP_SERVICE, DHCP_SERVICE):
        if not enabled(unit):
            command("systemctl", "enable", unit)
    write_if_changed(UNIT_DIR / "tempmonitor-hotspot-status.service", '''[Unit]
Description=Read-only status of the existing isolated Wi-Fi hotspot
After=hotspot-hostapd.service hotspot-dhcp.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/tempmonitor/hotspot.py publish
RuntimeDirectory=tempmonitor-hotspot
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN
RestrictAddressFamilies=AF_UNIX AF_NETLINK
''')
    write_if_changed(UNIT_DIR / "tempmonitor-hotspot-status.timer", '''[Unit]
Description=Publish local hotspot health every 15 seconds
[Timer]
OnBootSec=10
OnUnitActiveSec=15
AccuracySec=1
[Install]
WantedBy=timers.target
''')
    command("systemctl", "daemon-reload")
    command("systemctl", "enable", "--now", "tempmonitor-hotspot-status.timer")
    command("systemctl", "start", "tempmonitor-hotspot-status.service")
    return verify()


def verify():
    state = preflight()
    if not all(state[x] for x in ("ap_active", "dhcp_active", "boot_enabled", "isolated", "isolation_persistent")):
        raise RuntimeError("Hotspot persistence or isolation is incomplete; run preflight before installation")
    return state


def recover():
    state = preflight(allow_stopped=True)
    if not state["isolated"] or not state["isolation_persistent"]:
        INSTALL_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        write_if_changed(INSTALL_DIR / "hotspot.py", Path(__file__).read_text(), 0o755)
        install_guard(state["interface"])
    # Start only missing services. Never restart, stop, rewrite, or re-provision a working AP.
    for unit in (AP_SERVICE, DHCP_SERVICE):
        if not active(unit):
            command("systemctl", "start", unit)
    return preflight()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "install", "verify", "recover", "status", "publish", "isolate"))
    args = parser.parse_args()
    if args.action in ("install", "recover", "publish", "isolate") and os.geteuid() != 0:
        parser.error("This action must run as root on the host")
    try:
        result = globals()[args.action]()
        if result is not None:
            print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
