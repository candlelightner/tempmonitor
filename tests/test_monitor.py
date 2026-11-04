import asyncio
import logging
import threading
import time

import pytest

from tempmonitor.models import Settings, default_curve
from tempmonitor.monitor import Monitor
from tempmonitor.store import SampleWriter, Store


class MemoryStore:
    def __init__(self, release=None):
        self.release, self.records, self.saved = release, [], []
        self.entered = threading.Event()

    def curves(self):
        return [default_curve(i) for i in range(1, 5)]

    def save_curve(self, channel, curve):
        self.saved.append(channel)

    def record(self, sample, retention_days, at=None):
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(10)
        self.records.append((at, sample))


class Stop(BaseException):
    pass


@pytest.mark.asyncio
async def test_telemetry_continues_while_sample_store_is_stalled(monkeypatch):
    release = threading.Event()
    store = MemoryStore(release)
    monitor = Monitor(Settings(), store)
    sent = []
    telemetry = monitor.device.telemetry

    async def record_telemetry(value, valid):
        sent.append(valid)
        await telemetry(value, valid)

    monitor.device.telemetry = record_telemetry
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0.001)

    monkeypatch.setattr("tempmonitor.monitor.asyncio.sleep", fast_sleep)
    await monitor.start()
    try:
        assert await asyncio.to_thread(store.entered.wait, 5)
        for _ in range(500):
            if len(sent) >= 20:
                break
            await real_sleep(0.01)
        # The writer is still stuck inside its first SQLite call while telemetry keeps flowing.
        assert len(sent) >= 20 and all(sent) and not store.records
    finally:
        release.set()
        await monitor.stop()
    assert store.records and not monitor.writer.thread.is_alive()


class FlakyDevice:
    def __init__(self, outcomes):
        self.outcomes, self.connected = list(outcomes), False
        self.disconnects, self.curves = [], []

    async def connect(self):
        new, self.connected = not self.connected, True
        return new

    async def telemetry(self, value, valid):
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def set_curve(self, channel, curve):
        self.curves.append(channel)

    async def disconnect(self, force=False):
        self.connected = False
        self.disconnects.append(force)

    def snapshot(self):
        return {"connected": self.connected}


@pytest.mark.asyncio
async def test_failed_poll_retries_quickly_and_keeps_session_on_rejection(monkeypatch):
    monitor = Monitor(Settings(), MemoryStore())
    monitor.device = FlakyDevice([ConnectionError(), ValueError("rejected"), TimeoutError(), None,
                                  ConnectionError(), None])
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 6:
            raise Stop

    monkeypatch.setattr("tempmonitor.monitor.asyncio.sleep", sleep)
    with pytest.raises(Stop):
        await monitor.run()
    assert delays[:3] == [1, 2, 4] and delays[4] == 1
    assert 6 < delays[3] <= 7 and 6 < delays[5] <= 7
    # Transport failures force-drop the session; the controller-side rejection does not.
    assert monitor.device.disconnects == [True, True, True]
    # Curves owed to the reconnect are pushed once telemetry succeeds, even after a rejection in between.
    assert monitor.device.curves == [1, 2, 3, 4, 1, 2, 3, 4]
    assert monitor.error is None and len(monitor.writer.pending) == 6


def test_stalled_writer_drops_oldest_samples_with_one_warning(caplog):
    store = MemoryStore()
    writer = SampleWriter(store, 7, limit=3)
    with caplog.at_level(logging.WARNING, "tempmonitor.store"):
        for i in range(6):
            writer.submit({"i": i})
    assert [s["i"] for _, s in writer.pending] == [3, 4, 5]
    assert len([r for r in caplog.records if "dropped" in r.message]) == 1
    writer.start()
    writer.close(5)
    assert [s["i"] for _, s in store.records] == [3, 4, 5]
    assert all(at is not None for at, _ in store.records)


def test_writer_thread_persists_capture_time_to_sqlite(tmp_path, monkeypatch):
    store = Store(tmp_path / "data" / "tempmonitor.db")
    writer = SampleWriter(store, 7)
    now = time.time()
    monkeypatch.setattr("tempmonitor.store.time.time", lambda: now - 30)
    writer.submit({"gpu_stale": False})
    monkeypatch.undo()
    writer.start()
    writer.close(5)
    assert store.history() == [{"at": int(now - 30), "gpu_stale": False}]
