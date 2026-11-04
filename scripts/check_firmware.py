#!/usr/bin/env python3
"""Validate or compile an isolated copy using disposable, non-production secrets. Never flash."""
import argparse
import base64
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile", action="store_true")
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--external", action="store_true", help="Also validate the optional SHT31-D package")
    profile.add_argument("--homeserver", action="store_true", help="Validate the installed 0x38 sensor profile")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "esphome"
    with tempfile.TemporaryDirectory(prefix="tempmonitor-firmware-") as temporary:
        target = Path(temporary)
        for path in source.iterdir():
            if path.is_file() and path.name != "secrets.yaml":
                shutil.copy(path, target / path.name)
        (target / "secrets.yaml").write_text(
            'wifi_ssid: "validation-only"\nwifi_password: "validation-only-not-a-real-password"\n'
            f'api_encryption_key: "{base64.b64encode(secrets.token_bytes(32)).decode()}"\n'
            'ota_password: "validation-only-not-for-deployment"\n')
        (target / "secrets.yaml").chmod(0o600)
        config = target / ("homeserver.yaml" if args.homeserver else "fancontroller.yaml")
        if args.external:
            config.write_text(config.read_text().replace("# external: !include", "external: !include"))
        return subprocess.run([sys.executable, "-m", "esphome", "compile" if args.compile else "config",
                               str(config)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
