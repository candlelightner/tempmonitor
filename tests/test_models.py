import base64
import math
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from tempmonitor.hardware import MockDevice, gpu_max
from tempmonitor.models import Curve, Override, Point, Settings, default_curve


def test_model_boundaries(tmp_path):
    assert Settings().bind == "127.0.0.1"
    assert Settings().secure_cookies
    for fields in ({"public_origin": "http://public.example"}, {"public_origin": "https://a.example/path"},
                   {"secure_cookies": False, "mock": False}):
        with pytest.raises(ValidationError):
            Settings(**fields)
    for points in (
        [(35, 0), (35, 35), (60, 70), (75, 100)],
        [(35, 0), (45, 70), (60, 35), (75, 100)],
        [(35, 0), (45, 35), (60, 70), (75, 90)],
        [(math.nan, 0), (45, 35), (60, 70), (75, 100)],
    ):
        with pytest.raises(ValidationError):
            Curve(points=[Point(temperature=t, duty=d) for t, d in points])
    with pytest.raises(ValidationError):
        Override(duty=100, seconds=901)
    with pytest.raises(ValidationError):
        Override(duty=True)
    key = tmp_path / "key"
    key.write_text(base64.b64encode(b"a" * 32).decode())
    assert Settings(device_key_file=key).encryption_key() == key.read_text()
    key.write_text("invalid")
    with pytest.raises(ValueError):
        Settings(device_key_file=key).encryption_key()


def test_gpu_max_does_not_mask_failed_or_missing_gpus():
    assert gpu_max([{"temperature": 45}, {"temperature": 63}]) == (63, True)
    for values in ([], [None], [math.nan], [math.inf], [111], [45, None]):
        assert gpu_max([{"temperature": v} for v in values]) == (0, False)


@pytest.mark.asyncio
async def test_mock_watchdog_thermal_floor_and_override_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("tempmonitor.hardware.time.monotonic", lambda: now[0])
    device = MockDevice()
    await device.connect()
    assert device.snapshot()["sensors"]["fan_1_duty"]["value"] == 100
    await device.telemetry(30, True)
    assert device.snapshot()["sensors"]["fan_1_duty"]["value"] == 0
    await device.override(1, Override(duty=70, seconds=60))
    assert device.snapshot()["sensors"]["fan_1_duty"]["value"] == 100
    now[0] += 2
    assert device.snapshot()["sensors"]["fan_1_duty"]["value"] == 70
    now[0] += 61
    await device.telemetry(30, True)
    assert device.snapshot()["sensors"]["fan_1_duty"]["value"] == 0
    await device.telemetry(80, True)
    await device.override(1, Override(duty=0, seconds=60))
    assert device.snapshot()["sensors"]["fan_1_duty"]["value"] == 100
    await device.telemetry(30, True)
    now[0] += 23
    assert device.snapshot()["sensors"]["fan_1_duty"]["value"] == 100


def test_gpu_and_cabinet_default_profiles():
    gpu, cabinet = default_curve(1), default_curve(4)
    assert gpu.minimum_duty == 35 and cabinet.minimum_duty == 25
    assert [(p.temperature, p.duty) for p in gpu.points] == [(35, 0), (45, 40), (60, 75), (75, 100)]
    assert [(p.temperature, p.duty) for p in cabinet.points] == [(35, 0), (45, 30), (60, 50), (75, 100)]


def test_actual_firmware_safety_functions(tmp_path):
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "control-test"
    subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(root / "tests/test_control.cpp"),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
