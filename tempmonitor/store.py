import json
import logging
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from .models import Curve, default_curve

log = logging.getLogger(__name__)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self.path = path
        with self.transaction() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS admin (
                    id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS viewers (token TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (peer TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attempt_time ON attempts(at);
                CREATE TABLE IF NOT EXISTS curves (channel INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples (at INTEGER PRIMARY KEY, value TEXT NOT NULL);
            """)
        path.chmod(0o600)

    @contextmanager
    def transaction(self, *, immediate=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def curves(self) -> list[Curve]:
        with self.transaction() as db:
            rows = {r["channel"]: r["value"] for r in db.execute("SELECT * FROM curves")}
        return [Curve.model_validate_json(rows[i]) if i in rows else default_curve(i) for i in range(1, 5)]

    def save_curve(self, channel: int, curve: Curve):
        with self.transaction() as db:
            db.execute("INSERT INTO curves VALUES (?,?) ON CONFLICT(channel) DO UPDATE SET value=excluded.value",
                       (channel, curve.model_dump_json()))

    def record(self, sample: dict, retention_days: int, at: float | None = None):
        now = int(time.time() if at is None else at)
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO samples VALUES (?,?)", (now, json.dumps(sample, allow_nan=False)))
            db.execute("DELETE FROM samples WHERE at < ?", (now - retention_days * 86400,))

    def history(self, hours: int = 1) -> list[dict]:
        # Keep chart responses bounded; the full-resolution history remains in SQLite.
        with self.transaction() as db:
            rows = db.execute("SELECT at,value FROM samples WHERE at>=? ORDER BY at",
                              (time.time() - hours * 3600,)).fetchall()
        stride = max(1, (len(rows) + 599) // 600)
        return [{"at": r["at"], **json.loads(r["value"])} for r in rows[::stride]]


class SampleWriter:
    """Persists samples on a dedicated thread so a stalled disk can never delay controller telemetry."""

    def __init__(self, store: Store, retention_days: int, limit: int = 64):
        self.store, self.retention_days = store, retention_days
        # Sized for minutes of 7 s samples; a longer stall keeps the newest history and drops the oldest.
        self.pending = deque(maxlen=limit)
        self.ready = threading.Condition()
        self.closed = False
        self.dropped = 0
        self.warned = None
        self.thread = threading.Thread(target=self.run, name="sample-writer", daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, sample: dict):
        # Called on the event loop: the condition is only ever held briefly, never across SQLite calls.
        with self.ready:
            if len(self.pending) == self.pending.maxlen:
                self.dropped += 1
            self.pending.append((time.time(), sample))
            self.ready.notify()
            dropped, now = self.dropped, time.monotonic()
            if not dropped or self.warned is not None and now - self.warned < 60:
                return
            self.dropped, self.warned = 0, now
        log.warning("Sample store is stalled; dropped %d oldest unsaved samples", dropped)

    def run(self):
        while True:
            with self.ready:
                while not self.pending and not self.closed:
                    self.ready.wait()
                if not self.pending:
                    return
                at, sample = self.pending.popleft()
            try:
                self.store.record(sample, self.retention_days, at)
            except Exception:
                log.exception("Could not persist monitoring sample")

    def close(self, timeout: float):
        with self.ready:
            self.closed = True
            self.ready.notify()
        if self.thread.is_alive():
            self.thread.join(timeout)
