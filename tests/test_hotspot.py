import importlib.util
import ipaddress
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("hotspot", Path(__file__).resolve().parents[1] / "scripts/hotspot.py")
hotspot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hotspot)


def nft_rules(ap="wlap0"):
    rules = [{"chain": {"family": "inet", "table": "hotspot", "name": "forward", "hook": "forward"}}]
    for direction in ("iifname", "oifname"):
        rules.append({"rule": {"family": "inet", "table": "hotspot", "chain": "forward", "expr": [
            {"match": {"op": "==", "left": {"meta": {"key": direction}}, "right": ap}}, {"drop": None}]}})
    return {"nftables": rules}


@pytest.fixture
def existing(monkeypatch, tmp_path):
    config = tmp_path / "existing-hostapd.conf"
    config.write_text("interface=wlap0\nssid=homeserver-2g\nhw_mode=g\nchannel=6\nwpa=2\n"
                      "wpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\nwpa_passphrase=fixture-only-password\n")
    firewall = tmp_path / "existing.nft"
    firewall.write_text('iifname "wlap0" drop\noifname "wlap0" drop\n')
    ap_unit = f"ExecStartPre=/usr/sbin/nft -f {firewall}\nExecStart=/usr/sbin/hostapd {config}"
    dhcp_unit = ("ExecStart=/usr/sbin/dnsmasq --interface=wlap0 --bind-interfaces "
                 "--dhcp-range=192.168.77.50,192.168.77.150,255.255.255.0,12h --dhcp-option=option:router")
    interfaces = [{"ifname": "wlap0", "addr_info": [{"family": "inet", "local": "192.168.77.1", "prefixlen": 24}]},
                  {"ifname": "enp0", "addr_info": [{"family": "inet", "local": "10.0.0.2", "prefixlen": 24}]}]
    state = {"calls": [], "config": config, "ap_unit": ap_unit, "dhcp_unit": dhcp_unit,
             "interfaces": interfaces, "routes": [{"dst": "default", "dev": "enp0"}],
             "enabled": {hotspot.AP_SERVICE, hotspot.DHCP_SERVICE}, "inactive": set(), "rules": nft_rules()}

    def command(*args, check=True, input=None):
        state["calls"].append((args, input))
        if args[:2] == ("systemctl", "cat"):
            return state["ap_unit"] if args[2] == hotspot.AP_SERVICE else state["dhcp_unit"]
        if args[:2] == ("systemctl", "is-enabled"):
            return "enabled" if args[2] in state["enabled"] else "disabled"
        if args[:2] == ("systemctl", "is-active"):
            return "inactive" if args[2] in state["inactive"] else "active"
        if args[:2] == ("systemctl", "enable"):
            state["enabled"].add(args[-1])
            return ""
        if args[:2] == ("systemctl", "start"):
            state["inactive"].discard(args[-1])
            return ""
        if args == ("iw", "list"):
            return "Supported interface modes:\n * AP\n"
        if args[:3] == ("ip", "-j", "address"):
            return json.dumps(state["interfaces"])
        if args[:3] == ("ip", "-j", "route"):
            return json.dumps(state["routes"])
        if args == ("nft", "-j", "list", "ruleset"):
            return json.dumps(state["rules"])
        return ""

    monkeypatch.setattr(hotspot, "command", command)
    monkeypatch.setattr(hotspot.shutil, "which", lambda _: "/fake/tool")
    monkeypatch.setattr(hotspot, "INSTALL_DIR", tmp_path / "installed")
    monkeypatch.setattr(hotspot, "UNIT_DIR", tmp_path / "units")
    hotspot.UNIT_DIR.mkdir()
    return state


def test_preflight_is_read_only_and_redacts_credentials(existing):
    state = hotspot.preflight()
    assert state["ssid"] == "homeserver-2g"
    assert state["isolated"] and state["isolation_persistent"]
    assert "fixture-only-password" not in json.dumps(state)
    assert not any(args[:2] in (("systemctl", "start"), ("systemctl", "enable"))
                   for args, _ in existing["calls"])


def test_install_is_idempotent_and_never_restarts_or_rewrites_working_network(existing):
    original = existing["config"].read_bytes()
    hotspot.install()
    hotspot.install()
    assert existing["config"].read_bytes() == original
    for args, _ in existing["calls"]:
        assert args[0] not in ("hostapd", "dnsmasq")
        assert not (args[0] == "ip" and "-j" not in args)
        assert not (args[0] == "nft" and "-f" in args)
        assert not (args[0] == "systemctl" and args[1] in ("stop", "restart"))
        if args[0] == "systemctl" and args[1] == "start":
            assert args[-1] == "tempmonitor-hotspot-status.service"


def test_only_missing_boot_enablement_and_failed_units_are_changed(existing):
    existing["enabled"].remove(hotspot.DHCP_SERVICE)
    hotspot.install()
    assert hotspot.DHCP_SERVICE in existing["enabled"]
    existing["inactive"].add(hotspot.DHCP_SERVICE)
    existing["calls"].clear()
    hotspot.recover()
    starts = [args[-1] for args, _ in existing["calls"] if args[:2] == ("systemctl", "start")]
    assert starts == [hotspot.DHCP_SERVICE]


def test_subnet_uplink_bridge_and_ssid_conflicts_refuse_mutation(existing):
    existing["routes"] = [{"dst": "default", "dev": "wlap0"}]
    with pytest.raises(RuntimeError, match="uplink"):
        hotspot.preflight()
    existing["routes"] = []
    existing["interfaces"][0]["master"] = "bridge0"
    with pytest.raises(RuntimeError, match="bridged"):
        hotspot.preflight()
    del existing["interfaces"][0]["master"]
    existing["interfaces"][1]["addr_info"][0]["local"] = "192.168.77.2"
    with pytest.raises(RuntimeError, match="conflicts"):
        hotspot.preflight()
    existing["config"].write_text(existing["config"].read_text().replace("homeserver-2g", "other-network"))
    with pytest.raises(RuntimeError, match="SSID"):
        hotspot.preflight()


def test_isolation_requires_both_directions_and_no_hidden_accepts():
    rules = nft_rules()
    assert hotspot.isolated("wlap0", rules)
    rules["nftables"].append({"rule": {"family": "inet", "table": "hotspot", "chain": "forward", "expr": [{"accept": None}]}})
    assert not hotspot.isolated("wlap0", rules)
    assert not hotspot.isolated("wlap0", {"nftables": nft_rules()["nftables"][:2]})
    assert not hotspot.isolated("other0", nft_rules())
    hotspot.assert_no_conflicts("wlap0", ipaddress.ip_network("192.168.77.0/24"), [], [])
