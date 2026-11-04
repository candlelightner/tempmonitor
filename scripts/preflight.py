#!/usr/bin/env python3
"""Read-only application checks; never connect to or flash a controller."""
import json
import os
import shutil

from tempmonitor.hardware import gpu_max, read_gpus
from tempmonitor.models import Settings


def main():
    settings = Settings.from_env()
    checks = {"mock": settings.mock, "bind": settings.bind, "port": settings.port,
              "public_origin": settings.public_origin, "device_dns_name": settings.device_host,
              "docker": bool(shutil.which("docker"))}
    if not settings.mock:
        settings.encryption_key()
        checks["encryption_key_valid"] = True
        gpus = read_gpus(settings.gpu_uuids)
        checks["gpu_readings_valid"] = gpu_max(gpus)[1]
        checks["gpu_count"] = len(gpus)
        checks["host_status_file"] = settings.hotspot_status.exists()
    parent = settings.data_dir
    while not parent.exists():
        parent = parent.parent
    checks["data_parent_writable"] = os.access(parent, os.W_OK)
    print(json.dumps(checks, indent=2))
    return 0 if checks["data_parent_writable"] and checks.get("gpu_readings_valid", True) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Preflight failed ({type(exc).__name__}); check local configuration and secret files") from None
