import asyncio
import json
import logging
import math
import time
from contextlib import suppress

from .hardware import ESPDevice, MockDevice, POLL_SECONDS, STALE_SECONDS, gpu_max, read_gpus
from .models import Curve, Override, Settings
from .store import Store

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.device = MockDevice() if settings.mock else ESPDevice(settings)
        self.gpus = []
        self.last_sample = None
        self.error = None
        self.curves = store.curves()
        self.lock = asyncio.Lock()
        self.task = None
        self.gpu_task = None

    async def start(self):
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.gpu_task:
            self.gpu_task.cancel()
        await self.device.disconnect()

    async def sample(self):
        if self.settings.mock:
            self.gpus = [{"uuid": "mock-p40", "name": "NVIDIA Tesla P40 · simulated",
                          "temperature": round(46 + math.sin(time.monotonic() / 60) * 9, 1), "error": None}]
        else:
            if self.gpu_task is None:
                self.gpu_task = asyncio.create_task(asyncio.to_thread(read_gpus, self.settings.gpu_uuids))
            try:
                self.gpus = await asyncio.wait_for(asyncio.shield(self.gpu_task), timeout=3)
                self.gpu_task = None
            except TimeoutError:
                self.gpus = [{"uuid": "unavailable", "name": "NVIDIA GPU", "temperature": None,
                              "error": "NVML read timed out"}]
            except Exception:
                self.gpu_task = None
                self.gpus = [{"uuid": "unavailable", "name": "NVIDIA GPU", "temperature": None,
                              "error": "NVML read failed"}]
        self.last_sample = time.time()
        value, valid = gpu_max(self.gpus)
        async with self.lock:
            new = await self.device.connect()
            # Send health first: persisted curves must never delay the telemetry watchdog.
            await self.device.telemetry(value, valid)
            if new:
                for channel, curve in enumerate(self.curves, 1):
                    await self.device.set_curve(channel, curve)
        self.error = None

    async def run(self):
        while True:
            started = time.monotonic()
            try:
                await self.sample()
            except Exception as exc:
                self.error = "Controller connection or telemetry failed; check device health and local DNS"
                # Log the exception type only: transport exceptions can include secret connection parameters.
                log.warning("Hardware poll failed (%s)", type(exc).__name__)
                await self.device.disconnect()
            try:
                await asyncio.to_thread(self.store.record, self.snapshot(), self.settings.retention_days)
            except Exception:
                log.exception("Could not persist monitoring sample")
            await asyncio.sleep(max(0.2, POLL_SECONDS - (time.monotonic() - started)))

    def hotspot(self):
        try:
            status = json.loads(self.settings.hotspot_status.read_text())
            if not isinstance(status.get("sampled_at"), (float, int)):
                raise ValueError
            status["stale"] = time.time() - status["sampled_at"] > 45
            return status
        except (OSError, ValueError, TypeError):
            return {"available": False, "stale": True, "ssid": "homeserver-2g", "clients": [],
                    "message": "Host status publisher has not supplied a current sample"}

    def snapshot(self):
        device = self.device.snapshot()
        age = time.time() - self.last_sample if self.last_sample else None
        stale = age is None or age > STALE_SECONDS
        gpus = [{**g, "temperature": None if stale else g["temperature"], "stale": stale} for g in self.gpus]
        return {"mock": self.settings.mock, "sampled_at": self.last_sample, "gpu_stale": stale,
                "gpus": gpus, "device": device, "hotspot": self.hotspot(), "error": self.error,
                "curves": [c.model_dump() for c in self.curves], "channel_names": self.settings.channel_labels}

    async def set_curve(self, channel: int, curve: Curve):
        if channel not in range(1, 5):
            raise ValueError("Channel must be between 1 and 4")
        async with self.lock:
            # Persist only acknowledged device settings; the UI must not claim an offline update succeeded.
            await self.device.set_curve(channel, curve)
            await asyncio.to_thread(self.store.save_curve, channel, curve)
            self.curves[channel - 1] = curve

    async def override(self, channel: int, override: Override):
        if channel not in range(1, 5):
            raise ValueError("Channel must be between 1 and 4")
        async with self.lock:
            await self.device.override(channel, override)
