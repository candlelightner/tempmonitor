import math
import time
from itertools import pairwise

import aioesphomeapi as esp

from .models import Curve, Override, Settings, default_curve

POLL_SECONDS = 7
STALE_SECONDS = 22


def read_gpus(uuids: list[str]) -> list[dict]:
    import pynvml as nv

    initialized = False
    try:
        nv.nvmlInit()
        initialized = True
        handles = ([nv.nvmlDeviceGetHandleByUUID(uuid) for uuid in uuids] if uuids else
                   [nv.nvmlDeviceGetHandleByIndex(i) for i in range(nv.nvmlDeviceGetCount())])
        result = []
        for handle in handles:
            uuid = nv.nvmlDeviceGetUUID(handle)
            name = nv.nvmlDeviceGetName(handle)
            try:
                temperature = nv.nvmlDeviceGetTemperature(handle, nv.NVML_TEMPERATURE_GPU)
                if not math.isfinite(temperature) or not 0 <= temperature <= 110:
                    raise ValueError("Unsafe GPU sensor reading")
                result.append({"uuid": uuid, "name": name, "temperature": temperature, "error": None})
            except (nv.NVMLError, ValueError):
                result.append({"uuid": uuid, "name": name, "temperature": None, "error": "GPU sensor unavailable"})
        return result
    except nv.NVMLError:
        return [{"uuid": uuid, "name": "NVIDIA GPU", "temperature": None, "error": "NVML unavailable"}
                for uuid in (uuids or ["unknown"])]
    finally:
        if initialized:
            nv.nvmlShutdown()


def gpu_max(gpus: list[dict]) -> tuple[float, bool]:
    values = [g.get("temperature") for g in gpus]
    valid = bool(values) and all(isinstance(t, (int, float)) and math.isfinite(t) and 0 <= t <= 110
                                 for t in values)
    return (max(values), True) if valid else (0.0, False)


class ESPDevice:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: esp.APIClient | None = None
        self.entities = {}
        self.services = {}
        self.values = {}
        self.info = {}

    @property
    def connected(self):
        return self.client is not None and self.client.is_connected

    async def connect(self):
        if self.connected:
            return False
        await self.disconnect()
        self.values.clear()
        self.client = esp.APIClient(self.settings.device_host, 6053,
                                   noise_psk=self.settings.encryption_key(),
                                   expected_mac=self.settings.device_mac, client_info="tempmonitor/0.1")
        try:
            await self.client.connect(login=True, log_errors=False)
            info = await self.client.device_info()
            if info.project_name != "candlelightner.tempmonitor":
                raise ValueError("Controller needs the validated tempmonitor ESPHome configuration")
            entities, services = await self.client.list_entities_services()
            self.entities = {e.key: e for e in entities}
            self.services = {s.name: s for s in services}
            if not {"gpu_update", "set_curve", "manual_override"} <= self.services.keys():
                raise ValueError("Controller is missing required control actions")
            self.info = {"name": info.name, "model": info.model, "firmware": info.esphome_version,
                         "mac": info.mac_address}
            self.client.subscribe_states(self.on_state)
            return True
        except BaseException:
            await self.disconnect()
            raise

    def on_state(self, state):
        entity = self.entities.get(state.key)
        if entity is None:
            return
        value = state.state
        if getattr(state, "missing_state", False) or isinstance(value, float) and not math.isfinite(value):
            value = None
        self.values[entity.object_id] = {
            "name": entity.name, "value": value, "unit": getattr(entity, "unit_of_measurement", ""),
            "received": time.monotonic(),
        }

    async def action(self, name: str, data: dict):
        if not self.connected:
            raise ConnectionError("Controller disconnected; autonomous fallback remains on the ESP32")
        response = await self.client.execute_service(self.services[name], data, return_response=True, timeout=4)
        if response is None or not response.success:
            raise ValueError("Controller rejected the control update")

    async def telemetry(self, value: float, valid: bool):
        await self.action("gpu_update", {"temperature": float(value), "valid": valid})

    async def set_curve(self, channel: int, curve: Curve):
        await self.action("set_curve", {"channel": channel,
                                       "minimum_duty": curve.minimum_duty,
                                       **{f"t{i}": float(p.temperature) for i, p in enumerate(curve.points)},
                                       **{f"d{i}": p.duty for i, p in enumerate(curve.points)}})

    async def override(self, channel: int, override: Override):
        await self.action("manual_override", {"channel": channel, "duty": override.duty,
                                             "seconds": override.seconds})

    def snapshot(self):
        now = time.monotonic()
        sensors = {}
        for key, item in self.values.items():
            age = round(now - item["received"], 1)
            stale = not self.connected or age > STALE_SECONDS
            sensors[key] = {k: v for k, v in item.items() if k != "received"}
            sensors[key].update(age=age, stale=stale)
            if stale:
                sensors[key]["value"] = None
        return {"connected": self.connected, "encrypted": True, "info": self.info, "sensors": sensors}

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            finally:
                self.client = None


class MockDevice:
    """Simulated firmware for development, never a substitute on NVML/ESP errors."""

    def __init__(self):
        self.connected = False
        self.curves = [default_curve(i) for i in range(1, 5)]
        self.overrides = {}
        self.running = [False] * 4
        self.startup_until = [0.0] * 4
        self.gpu_temperature = 0.0
        self.gpu_valid = False
        self.received = None

    async def connect(self):
        new = not self.connected
        self.connected = True
        return new

    async def telemetry(self, value: float, valid: bool):
        self.gpu_temperature, self.gpu_valid, self.received = value, valid, time.monotonic()

    async def set_curve(self, channel: int, curve: Curve):
        self.curves[channel - 1] = curve

    async def override(self, channel: int, override: Override):
        self.overrides[channel] = (override.duty, time.monotonic() + override.seconds)

    def snapshot(self):
        now = time.monotonic()
        stale = self.received is None or now - self.received > STALE_SECONDS
        unsafe = not self.connected or stale or not self.gpu_valid or not math.isfinite(self.gpu_temperature)
        temp = max(self.gpu_temperature, 27.2) if not unsafe else 27.2
        fallback = unsafe or temp >= 80
        sensors = {}

        def sensor(key, name, value, unit=""):
            sensors[key] = {"name": name, "value": value, "unit": unit, "age": 0, "stale": False}

        sensor("environment_temperature", "Environment", 27.2, "°C")
        sensor("environment_humidity", "Humidity", 48.0, "%")
        sensor("control_temperature", "Control temperature", temp, "°C")
        sensor("control_mode", "Control mode", "Failsafe · 100%" if fallback else "Automatic")
        sensor("failsafe", "Failsafe", fallback)
        for i, curve in enumerate(self.curves, 1):
            duty = curve.points[-1].duty
            if temp <= curve.points[0].temperature:
                duty = curve.points[0].duty
            else:
                for a, b in pairwise(curve.points):
                    if temp <= b.temperature:
                        duty = math.ceil(a.duty + (b.duty - a.duty) *
                                         (temp - a.temperature) / (b.temperature - a.temperature))
                        break
            override, until = self.overrides.get(i, (0, 0))
            remaining = max(0, math.ceil(until - now))
            duty = 100 if fallback else max(duty, override if remaining else 0)
            if duty > 0:
                duty = max(duty, curve.minimum_duty)
                if not self.running[i - 1]:
                    self.startup_until[i - 1] = now + 2
                if now < self.startup_until[i - 1]:
                    duty = 100
            self.running[i - 1] = duty > 0
            sensor(f"fan_{i}_duty", f"Fan {i} Duty", duty, "%")
            sensor(f"fan_{i}_speed", f"Fan {i} Speed", duty * 25 if duty else 0, "RPM")
            sensor(f"fan_{i}_override_remaining", f"Fan {i} Override remaining", remaining, "s")
        return {"connected": self.connected, "encrypted": False,
                "info": {"name": "Mock FanController", "model": "ESP32-S2 simulator", "firmware": "mock"},
                "sensors": sensors}

    async def disconnect(self):
        self.connected = False
