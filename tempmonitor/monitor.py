import asyncio
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from .hardware import ESPDevice, MockDevice, POLL_SECONDS, STALE_SECONDS, gpu_max, read_gpus
from .models import Curve, Override, Settings
from .store import SampleWriter, Store

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.device = MockDevice() if settings.mock else ESPDevice(settings)
        self.gpus = []
        self.last_sample = None
        self.error = None
        self.curves = store.curves()
        self.push_curves = False
        self.writer = SampleWriter(store, settings.retention_days)
        # NVML gets its own thread: web handlers stuck on a stalled disk can exhaust the default executor.
        self.gpu_executor = ThreadPoolExecutor(1, thread_name_prefix="nvml")
        self.lock = asyncio.Lock()
        self.curve_lock = asyncio.Lock()
        self.task = None
        self.gpu_task = None

    async def start(self):
        self.writer.start()
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.gpu_task:
            self.gpu_task.cancel()
        await self.device.disconnect()
        await asyncio.to_thread(self.writer.close, 5)
        self.gpu_executor.shutdown(wait=False, cancel_futures=True)

    async def sample(self):
        if self.settings.mock:
            self.gpus = [{"uuid": "mock-p40", "name": "NVIDIA Tesla P40 · simulated",
                          "temperature": round(46 + math.sin(time.monotonic() / 60) * 9, 1), "error": None}]
        else:
            if self.gpu_task is None:
                self.gpu_task = asyncio.get_running_loop().run_in_executor(
                    self.gpu_executor, read_gpus, self.settings.gpu_uuids)
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
            if await self.device.connect():
                self.push_curves = True
            # Send health first: persisted curves must never delay the telemetry watchdog.
            await self.device.telemetry(value, valid)
            # A rejection keeps the session open, so remember curves still owed to a fresh connection.
            if self.push_curves:
                for channel, curve in enumerate(self.curves, 1):
                    await self.device.set_curve(channel, curve)
                self.push_curves = False
        self.error = None

    async def run(self):
        failures, warned = 0, 0.0
        while True:
            started = time.monotonic()
            try:
                await self.sample()
                if failures:
                    log.info("Hardware poll recovered after %d failed attempts", failures)
                failures = 0
            except Exception as exc:
                failures += 1
                self.error = "Controller connection or telemetry failed; check device health and local DNS"
                if failures == 1 or started - warned >= 60:
                    warned = started
                    # Log the exception type only: transport exceptions can include secret connection parameters.
                    log.warning("Hardware poll failed (%s, attempt %d)", type(exc).__name__, failures)
                # Controller-side rejections arrive over a working session; only drop broken transports.
                if not isinstance(exc, ValueError):
                    with suppress(Exception):
                        await self.device.disconnect(force=True)
            # Queue only: SQLite runs on the writer thread, so a stalled disk cannot delay the next update.
            self.writer.submit(self.snapshot())
            if failures:
                # Retry 1 s, 2 s, 4 s ... so a single lost update cannot run out the 22 s firmware watchdog.
                await asyncio.sleep(min(POLL_SECONDS, 2 ** (failures - 1)))
            else:
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
        async with self.curve_lock:
            async with self.lock:
                # Persist only acknowledged device settings; the UI must not claim an offline update succeeded.
                await self.device.set_curve(channel, curve)
                self.curves[channel - 1] = curve
            # Save outside the telemetry lock: a stalled disk must not hold back the next gpu_update.
            await asyncio.to_thread(self.store.save_curve, channel, curve)

    async def override(self, channel: int, override: Override):
        if channel not in range(1, 5):
            raise ValueError("Channel must be between 1 and 4")
        async with self.lock:
            await self.device.override(channel, override)
