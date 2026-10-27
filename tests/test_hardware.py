import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import aioesphomeapi as esp
import pytest

from tempmonitor.hardware import ESPDevice
from tempmonitor.models import Curve, Override, Settings


@pytest.mark.asyncio
async def test_encrypted_identity_bound_client_and_acknowledged_actions(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text(base64.b64encode(b"a" * 32).decode())
    calls = []

    class Client:
        is_connected = False

        def __init__(self, host, port, **kwargs):
            assert host == "fancontroller-r3-3-0667a0" and port == 6053
            assert kwargs["noise_psk"] == key.read_text()
            assert kwargs["expected_mac"] == "14:63:93:06:67:A0"

        async def connect(self, **_kwargs):
            self.is_connected = True

        async def device_info(self):
            return SimpleNamespace(project_name="candlelightner.tempmonitor", name="controller", model="ESP32-S2",
                                   esphome_version="2026.10.0", mac_address="14:63:93:06:67:A0")

        async def list_entities_services(self):
            return [esp.SensorInfo(key=1, name="Environment Temperature", object_id="environment_temperature")], [
                esp.UserService(name=name, key=i) for i, name in enumerate(("gpu_update", "set_curve", "manual_override"))]

        def subscribe_states(self, callback):
            callback(esp.SensorState(key=1, state=27.5))

        async def execute_service(self, service, data, **kwargs):
            assert kwargs["return_response"] is True
            calls.append((service.name, data))
            return SimpleNamespace(success=True)

        async def disconnect(self):
            self.is_connected = False

    monkeypatch.setattr("tempmonitor.hardware.esp.APIClient", Client)
    device = ESPDevice(Settings(device_key_file=key))
    assert await device.connect()
    assert not await device.connect()
    assert device.snapshot()["sensors"]["environment_temperature"]["value"] == 27.5
    await device.telemetry(42, True)
    await device.set_curve(1, Curve())
    await device.override(4, Override(duty=70, seconds=300))
    assert calls[0] == ("gpu_update", {"temperature": 42.0, "valid": True})
    assert calls[1][1] == {"channel": 1, "minimum_duty": 35, "t0": 35.0, "t1": 45.0, "t2": 60.0, "t3": 75.0,
                           "d0": 0, "d1": 40, "d2": 75, "d3": 100}
    assert calls[2] == ("manual_override", {"channel": 4, "duty": 70, "seconds": 300})
    await device.disconnect()
    assert device.snapshot()["sensors"]["environment_temperature"]["value"] is None
    with pytest.raises(ConnectionError):
        await device.override(1, Override(duty=100))


def test_secret_files_are_private_consistent_and_never_overwritten(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/init_secrets.py"
    spec = importlib.util.spec_from_file_location("init_secrets", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "esphome").mkdir()
    module.create_files(tmp_path, "fixture-only-wifi-password")
    key = tmp_path / "secrets/esp32_api_key"
    assert key.stat().st_mode & 0o777 == 0o600
    firmware = tmp_path / "esphome/secrets.yaml"
    assert firmware.stat().st_mode & 0o777 == 0o600
    import json

    assert json.loads(firmware.read_text())["api_encryption_key"] == key.read_text().strip()
    before = key.read_bytes()
    with pytest.raises(FileExistsError):
        module.create_files(tmp_path, "different-password")
    assert key.read_bytes() == before
