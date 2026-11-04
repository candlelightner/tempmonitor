#!/usr/bin/env python3
"""Create ignored local secret files. Never overwrite existing controller credentials."""
import base64
import getpass
import json
import os
from pathlib import Path
import secrets


def create_files(root: Path, wifi_password: str):
    key_file = root / "secrets/esp32_api_key"
    firmware_file = root / "esphome/secrets.yaml"
    if key_file.exists() or firmware_file.exists():
        raise FileExistsError("Local secrets already exist; refusing to rotate or overwrite them")
    if not (8 <= len(wifi_password) <= 63 or len(wifi_password) == 64 and
            all(c in "0123456789abcdefABCDEF" for c in wifi_password)):
        raise ValueError("Use the existing WPA2 password (8–63 characters or a 64-character hex PSK)")
    key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_file.parent.chmod(0o700)
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    data = {"wifi_ssid": "homeserver-2g", "wifi_password": wifi_password,
            "api_encryption_key": key, "ota_password": secrets.token_urlsafe(32)}
    # JSON is also valid YAML and safely escapes every character in the existing password.
    for path, content in ((key_file, key + "\n"), (firmware_file, json.dumps(data, indent=2) + "\n")):
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
            f.write(content)


if __name__ == "__main__":
    try:
        create_files(Path(__file__).resolve().parents[1], getpass.getpass("Existing homeserver-2g password: "))
        print("Created secrets/esp32_api_key and esphome/secrets.yaml (private, gitignored).")
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
