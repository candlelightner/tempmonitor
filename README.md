# Tempmonitor

A self-hosted temperature dashboard and fan controller for an Ubuntu Server 24.04 NVIDIA host. Python 3.12, NiceGUI/FastAPI, Pydantic and SQLite; one application container. Its stone, teal and copper interface follows Dirigentenstab's visual style, with no runtime, code or account dependency on Dirigentenstab.

The ESP32 owns the control loop. The server samples NVML approximately every seven seconds and sends the maximum GPU temperature through the **encrypted ESPHome native API**. There is no Home Assistant, MQTT, cloud control or HTTP fan-control interface on the new firmware.

## Start with simulated hardware

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec app cat /data/setup-token
```

Open `http://localhost:8090`, enter that setup token, and create the single administrator. This is a **mock** deployment: it cannot contact the physical controller. The generated token disappears after setup. No administrator password is shipped or written to logs.

The application binds to **127.0.0.1:8090** and sends Secure, HttpOnly, SameSite=Strict session cookies. Use HTTPS for remote access. Browsers that reject Secure cookies on plain HTTP localhost need HTTPS, or the explicit loopback-only mock development setting below. Production mode refuses insecure cookies.

For Python development:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
TM_MOCK=true TM_SECURE_COOKIES=false .venv/bin/tempmonitor
```

The responsive dashboard includes GPU/environment temperatures, humidity, live temperature/RPM history, four PWM channels, four editable curves, expiring manual overrides, sensor freshness, controller health and hotspot/client status. History lives in SQLite with seven-day retention by default. Charts offer one, six and twenty-four hours. Invalid/stale readings appear as unavailable, never as zero temperatures.

Display labels are configurable in Docker through `.env`: `TM_GPU_LABEL`, `TM_UPPER_LABEL`, `TM_LOWER_LABEL` and `TM_CHANNEL_LABELS` (exactly four comma-separated names). Recreate only the app with the same Compose files to apply them. Labels do not affect sensor IDs, curves, accounts or the SQLite volume. After commissioning, `COMPOSE_FILE=compose.yaml:compose.hardware.yaml` in `.env` keeps subsequent Compose commands in real-hardware mode.

## Preserve and adopt the existing hotspot

**Use the existing `homeserver-2g`; do not create a replacement AP.** Host tooling is deliberately limited to adopting the host's existing `hotspot-hostapd.service` and `hotspot-dhcp.service`. It reads the installed hostapd config and DHCP unit, rather than storing a second copy of the Wi-Fi password or network configuration.

Run on the Ubuntu host, outside Docker:

```bash
sudo python3 scripts/hotspot.py preflight
sudo python3 scripts/hotspot.py install
sudo python3 scripts/hotspot.py verify
sudo python3 scripts/hotspot.py status
# Recovery starts only stopped known services; it never restarts a working service:
sudo python3 scripts/hotspot.py recover
```

Preflight checks Wi-Fi AP capability, the existing fixed 2.4 GHz WPA2 configuration, private addressing, DHCP binding, missing interfaces, overlapping subnets, bridge membership, default-route conflicts, forwarding isolation and boot enablement. Unknown/unsafe configuration fails closed. It does not stop NetworkManager/networkd, edit netplan, alter an uplink, flush nftables, enable IP forwarding, or touch SSH, Cloudflare, Tailscale or CloudStack.

Installation is idempotent. Working AP/DHCP files, credentials, running services and existing isolation rules are preserved. Missing boot enablement is added with `systemctl enable`, without restarting the AP. If forwarding isolation is missing, a dedicated `inet tempmonitor_hotspot_guard` table blocks **both directions**, with a boot-time unit; only this application's own table can be replaced. There is no NAT, bridge, Internet sharing or forwarding exception. A stopped hotspot can be recovered only from its already installed, validated configuration.

The read-only host publisher writes `/run/tempmonitor-hotspot/status.json` every 15 seconds. Its oneshot systemd service has no HTTP listener or command endpoint. The application mounts this directory **read-only**, and treats status older than 45 seconds as unavailable. The only root work is the host installer/publisher; the application never receives sudo, a Docker socket, net-admin capabilities, host filesystems or network configuration credentials.

## Real hardware deployment

Complete the mock/browser tests and firmware checks first. Keep the current controller untouched while it is being moved. Do not run an upload until it is ready and the fan wiring and independent 12 V supply have been checked.

1. Inspect the current hotspot with `preflight` and install the read-only publisher as above.
2. Create local secrets using the **existing** `homeserver-2g` password. The command prompts without echoing it and refuses to overwrite existing secrets:

   ```bash
   python3 scripts/init_secrets.py
   ```

   This creates `secrets/esp32_api_key` and `esphome/secrets.yaml`, both mode 0600 and gitignored. The generated API key is identical in both files. Reuse an existing API key instead when adopting a previously provisioned device; do not silently rotate it. The container runs as UID/GID 1000, matching this host's ordinary user; on another host, adjust the image/user and secret-file ownership together.

3. Install the firmware tool and validate **without** contacting a device:

   ```bash
   .venv/bin/pip install '.[firmware]'
   .venv/bin/python scripts/check_firmware.py
   .venv/bin/python scripts/check_firmware.py --external
   .venv/bin/python scripts/check_firmware.py --compile
   # Also validate the real local secrets, without publishing them:
   .venv/bin/esphome config esphome/homeserver.yaml
   ```

   `check_firmware.py` uses an isolated temporary copy and disposable credentials, then deletes its artifacts. It never uploads. Firmware is standard ESPHome YAML plus a small tested curve/safety header, with local hardware definitions for Arthofer Rev. 3.3. The hardware package contains all four 25 kHz PWM outputs and four one-second tachometer counters.

4. **When the controller is ready**, compile the real configuration, save a copy of the existing known-good firmware/configuration, and perform the initial upload over USB:

   ```bash
   .venv/bin/esphome compile esphome/homeserver.yaml
   # Replace this with the verified controller USB port, not another USB device:
   .venv/bin/esphome upload esphome/homeserver.yaml --device /dev/ttyACM0
   ```

   There is no automatic flash/upload command in application startup, Compose or the hotspot installer. OTA is password protected; no fallback AP or web server is enabled. Maintain independent fan power while commissioning.

5. Enable the real hardware overlay. It mounts the host's existing NVML library and two NVIDIA device nodes; no NVIDIA Container Toolkit installation or Docker daemon restart is required:

   ```bash
   docker compose -f compose.yaml -f compose.hardware.yaml config --quiet
   docker compose -f compose.yaml -f compose.hardware.yaml up -d --build
   docker compose logs --tail=50 app
   ```

   Real mode never falls back to simulated values on errors. Missing keys, unencrypted factory firmware, the wrong MAC/project, failed NVML readings, lost DNS and missing controller actions are reported as unhealthy.

### Local DNS, without static leases

The firmware retains the current hostname, `fancontroller-r3-3-0667a0`, and uses the existing DHCP server. The hardware Compose overlay sets **the container's resolver only** to `TM_HOTSPOT_DNS=192.168.77.1`. It connects to `TM_DEVICE_HOST=fancontroller-r3-3-0667a0`; it does not pin the current lease `192.168.77.85`. The resolver is consulted again on reconnection. `TM_DEVICE_MAC` also pins the expected device identity.

After the controller has obtained a lease, verify inside the app container:

```bash
docker compose -f compose.yaml -f compose.hardware.yaml exec app \
  python -c "import socket; print(socket.gethostbyname('fancontroller-r3-3-0667a0'))"
```

This preserves the existing DHCP settings and local DNS records. No host-wide resolver changes, static lease reservations, router advertisements or port forwards are needed. For a Python process outside Docker, configure an appropriate local resolver yourself or use the controller's `.local` mDNS hostname through `TM_DEVICE_HOST`; preflight never rewrites the host resolver.

### Container network and privileges

The accepted deployment uses **unprivileged host networking** so the application can directly reach the isolated ESPHome device without NAT, routing exceptions or a TCP proxy. It still runs as a non-root user, drops every Linux capability, has a read-only root filesystem and uses `no-new-privileges`. NVML uses the existing `/dev/nvidiactl`, `/dev/nvidia0` and a read-only bind of `TM_NVML_LIBRARY` (normally `/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1`). No driver, GPU runtime or Docker daemon configuration changes are needed. The library and device nodes permit GPU queries; they are not a security boundary against all GPU operations. The remaining mounts are `/data`, temporary memory storage, the API key and the read-only hotspot status directory. The app never mounts the Docker socket or host `/etc`, and never creates/manages an AP from inside Docker.

Host networking shares reachability with host processes; it is not a network sandbox. Public exposure is controlled by the application's loopback bind and your existing authenticated HTTPS access path.

## Control policy and commissioning

GPU and cabinet channels ship with separate conservative linear profiles:

| Maximum valid temperature | GPU pair (channel 1) | Cabinet pair (channel 4) |
| --- | --- | --- |
| ≤35°C | 0% | 0% |
| 45°C | 40% | 30% |
| 60°C | 75% | 50% |
| 75°C | 100% | 100% |

Nonzero demand is clamped to a running minimum of **35% for the GPU pair**, **25% for the cabinet pair**. A stopped channel receives a **two-second 100% startup boost** before settling to its curve. These values are editable with each curve; they are starting points that still need physical calibration. The fan manufacturer specifies [P14 Pro PST stop below 5% PWM](https://www.arctic.de/en/P14-Pro-PST/ACFAN00314A); the higher initial minima provide margin for the actual installed fan pairs and airflow restriction.

Curves require four increasing temperatures and nondecreasing PWM, reaching 100% by 80°C. The ESP32 validates requests independently of Pydantic. Updated curves are acknowledged before the app saves them, retained in ESP32 preferences, and reapplied from SQLite on reconnect. Flash preference writes are batched for one minute; the server retains the acknowledged settings immediately.

The maximum includes the required NVML GPU reading(s), the onboard HDC1080 and any enabled external temperature sensor. If any configured GPU reading is unavailable or out of range, NVML sends an invalid-health update rather than ignoring the failed GPU. Configure `TM_GPU_UUIDS` to pin the required devices on hosts with changing GPU inventories.

Current wiring: **channel 1** drives both GPU fans through a splitter; **channel 4** drives the cabinet inlet/exhaust fans in a daisy chain. Channels 2 and 3 are unused. A shared channel normally returns only one fan's tachometer signal; RPM cannot independently confirm that both fans are turning. Check both physically and respect the controller/channel current limits.

**100% autonomous fallback** applies on boot, missing/invalid telemetry, more than 22 seconds without a GPU update, Wi-Fi/API disconnection, unavailable/stale required environment data (20 seconds), invalid restored curves, OTA, or temperatures ≥80°C. The controller checks at one-second intervals; the watchdog acts within about 23 seconds of the last update. Keeping another API client connected cannot bypass the telemetry timeout.

Manual overrides last up to 15 minutes and can raise duty above the curve. They cannot lower the thermal floor, disable fallback or survive a controller reboot. The ESP32 expires them without the server. Zero duty is a PWM request, not removal of 12 V: actual fan stop/minimum speed depends on the connected fan. RPM is monitored, but this initial version does not infer a stalled fan from unused channels or automatically cut GPU power.

The installed lower-compartment sensor responds to the **AHT20-compatible protocol at `0x38`**, confirmed with live temperature/humidity readings. `esphome/homeserver.yaml` includes the base controller and `external-aht20.yaml`. The lower compartment contains the laptop and disks; the onboard HDC1080 at `0x40` measures the upper GPU/enclosure compartment. Build/upload `homeserver.yaml` for this installation; `fancontroller.yaml` remains the generic onboard-only configuration. The optional `external-sht31.example.yaml` is for a separately verified SHT31-D at `0x44`; select a driver from the responding device and protocol rather than a product-name assumption. Both profiles make external sensor health mandatory even if it never produces a reading. The dashboard labels both compartments separately.

Before relying on automatic cooling, physically verify all fitted channels, fan startup at low duty, tachometer scaling, temperature placement and power. Test server shutdown, Wi-Fi loss, invalid telemetry, sensor unplugging, override expiry and ESP32 reboot. Confirm 100% fallback with independent fan power and without a GPU workload. Software/compile tests are not hardware acceptance tests.

## Existing HTTPS access

Set `TM_PUBLIC_ORIGIN` to the exact browser origin, for example `https://tempmonitor.example.net` (no path/trailing slash), and restart only this app. Do not expose port 8090 publicly.

For Cloudflare Tunnel, insert a new hostname rule into the **existing** ingress configuration pointing to `http://127.0.0.1:8090`, preserving every current rule and catch-all. NiceGUI needs WebSockets to pass through. The app does not trust forwarded identity headers; its own local login still applies.

For Tailscale HTTPS, inspect `tailscale serve status` first and choose an unused HTTPS listener/service mapping to `http://127.0.0.1:8090`; set `TM_PUBLIC_ORIGIN` to that resulting HTTPS origin. Preserve existing Serve mappings and ACLs. No Tailscale, Cloudflare or SSH configuration is changed by this repository.

## Authentication and API

One local administrator, no registration. Passwords use Argon2id; opaque random sessions are stored as SHA-256 token digests in SQLite with seven-day expiration and survive app restarts. Logout revokes a session; password changes revoke all sessions, including existing dashboard callbacks. Login attempts are limited per peer and globally, and first-run setup requires a private filesystem token.

Monitoring APIs accept either a read-only viewing session or an administrator session. Control APIs require an administrator session. Mutations additionally require the exact configured `Origin` and the `X-CSRF-Token` returned by authenticated `GET /api/session`. The same session and origin protection applies to NiceGUI's Socket.IO handshake, and every control callback rechecks session validity. There is no registration, bearer key, external identity provider or Cloudflare-header login bypass.

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Public process liveness only |
| `GET /api/session` | Current user and session CSRF token |
| `GET /api/status` | GPU, device, sensor and hotspot state |
| `GET /api/history?hours=1` | Bounded chart data; 1/6/24 hours |
| `PUT /api/channels/1/curve` | `{"points":[{"temperature":35,"duty":0},...]}`; exactly four points |
| `PUT /api/channels/1/override` | `{"duty":70,"seconds":300}`; zero seconds restores auto |

Secure backups should include the SQLite database and its WAL using SQLite's backup API, plus the ignored firmware/application secrets. Keep backups private: they contain password hashes, active session digests and device credentials. Restore the same secret files when restoring the database; do not overwrite a working controller with a different key accidentally.

## Checks

```bash
.venv/bin/pip install '.[test,browser,firmware]'
.venv/bin/playwright install chromium
.venv/bin/ruff check tempmonitor tests scripts
.venv/bin/pytest -q -m 'not browser'
.venv/bin/pytest -q -m browser
.venv/bin/python scripts/check_firmware.py
.venv/bin/python scripts/check_firmware.py --external
.venv/bin/python scripts/check_firmware.py --compile
docker compose config --quiet
sudo python3 scripts/hotspot.py preflight
```

Tests cover auth/session revocation, origin/CSRF protection, mocked hardware and timeout behavior, real C++ curve/safety functions, hotspot adoption without disruption, secret generation, and the desktop/mobile browser flow. Browser tests start isolated mock instances, use disposable accounts and save screenshots under ignored `test-results/`. They never contact or flash a controller, change network state, or run the host installer against live services.

## Shared viewing and legal information

The default dashboard prompts for the shared viewing password `fanclub`. Viewers see live readings,
charts and fan curves without mutation controls. The top-right **Log in as admin** link opens `/login`,
which also works directly without the viewing password. The existing administrator account is unchanged.
Viewer cookies and server-side hashed sessions expire after seven days; admin and viewer tokens are separate.
`/legal` is accessible before login and contains the Austrian operator disclosure and privacy/essential-cookie notice.
Only necessary authentication cookies are used, so there is no cookie-consent banner.

## Automatic updates on this host

`scripts/update.sh` fetches GitHub and fast-forwards the current branch to its upstream. It rebuilds only
this Compose app, waits for health, and retains the existing database volume, secrets and `.env` hardware mode.
It preserves local edits and stops if Git cannot fast-forward or would overwrite an edit. Failed builds are
retried on the next run. It never flashes the controller. The systemd units in `scripts/systemd/` run as
`emil` every five minutes; Docker commands use the host's existing passwordless sudo access.

```bash
sudo install -m 644 scripts/systemd/tempmonitor-update.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tempmonitor-update.timer
journalctl -u tempmonitor-update.service
```

Updates follow `origin/feat/initial-monitor` on this deployment. Pushes to another branch do not deploy here.
