"""Bounded, instance-owned persistence for serializable run snapshots."""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .state import RunSnapshot


class RunStore(Protocol):
    def create(self, snapshot: RunSnapshot) -> bool: ...

    def load(self, run_id: str) -> RunSnapshot | None: ...

    def compare_and_swap(self, run_id: str, expected_version: int, snapshot: RunSnapshot) -> bool: ...


@dataclass(slots=True)
class _StoredRun:
    snapshot_json: str
    size: int
    expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(snapshot: RunSnapshot) -> str:
    return json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class InMemoryRunStore:
    """Thread-safe CAS with fixed run, byte, and age ceilings.

    Entries expire from insertion time; reads and saves do not extend their TTL.
    The oldest entry is evicted when a new run needs capacity.
    """

    def __init__(
        self, *, max_runs: int = 128, max_bytes: int = 16_777_216,
        ttl_seconds: float = 3_600.0,
        clock: Callable[[], datetime] = _utc_now,
        on_remove: Callable[[str], None] | None = None,
    ) -> None:
        if type(max_runs) is not int or max_runs < 1:
            raise ValueError("max_runs must be positive")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
        self.max_runs = max_runs
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._remove_listeners = [on_remove] if on_remove is not None else []
        self.cleanup_failures = 0
        self._entries: OrderedDict[str, _StoredRun] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._expired_ids: OrderedDict[str, None] = OrderedDict()
        self._last_now: datetime | None = None

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("store clock must be timezone-aware")
        normalized = now.astimezone(timezone.utc)
        if self._last_now is None or normalized > self._last_now:
            self._last_now = normalized
        return self._last_now

    def _remove(self, run_id: str, *, expired: bool = False) -> None:
        entry = self._entries.pop(run_id, None)
        if entry is None:
            return
        self._bytes -= entry.size
        if expired:
            self._expired_ids[run_id] = None
            while len(self._expired_ids) > self.max_runs:
                self._expired_ids.popitem(last=False)
        for listener in self._remove_listeners:
            try:
                listener(run_id)
            except Exception:
                self.cleanup_failures += 1

    def add_remove_listener(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            self._remove_listeners.append(listener)

    def _expire(self, now: datetime) -> None:
        for run_id, entry in tuple(self._entries.items()):
            if now >= entry.expires_at:
                self._remove(run_id, expired=True)

    def create(self, snapshot: RunSnapshot) -> bool:
        if snapshot.status != "created" or snapshot.optimistic_version != 1:
            raise ValueError("store create requires a new run snapshot")
        encoded = _encode(snapshot)
        size = len(encoded.encode("utf-8"))
        if size > self.max_bytes:
            raise ValueError("run snapshot exceeds store byte ceiling")
        with self._lock:
            now = self._now()
            self._expire(now)
            if snapshot.run_id in self._entries:
                return False
            self._expired_ids.pop(snapshot.run_id, None)
            while self._entries and (len(self._entries) >= self.max_runs or self._bytes + size > self.max_bytes):
                self._remove(next(iter(self._entries)))
            self._entries[snapshot.run_id] = _StoredRun(encoded, size, now + timedelta(seconds=self.ttl_seconds))
            self._bytes += size
            return True

    def load(self, run_id: str) -> RunSnapshot | None:
        with self._lock:
            self._expire(self._now())
            entry = self._entries.get(run_id)
            return RunSnapshot.from_dict(json.loads(entry.snapshot_json)) if entry is not None else None

    def compare_and_swap(self, run_id: str, expected_version: int, snapshot: RunSnapshot) -> bool:
        if snapshot.run_id != run_id or snapshot.optimistic_version != expected_version + 1:
            return False
        encoded = _encode(snapshot)
        size = len(encoded.encode("utf-8"))
        with self._lock:
            self._expire(self._now())
            entry = self._entries.get(run_id)
            if entry is None or size > self.max_bytes:
                return False
            current = RunSnapshot.from_dict(json.loads(entry.snapshot_json))
            if current.optimistic_version != expected_version:
                return False
            if self._bytes - entry.size + size > self.max_bytes:
                return False
            self._bytes += size - entry.size
            entry.snapshot_json = encoded
            entry.size = size
            return True

    def missing_reason(self, run_id: str) -> str:
        with self._lock:
            self._expire(self._now())
            return "run_expired" if run_id in self._expired_ids else "run_not_found"

    @property
    def run_count(self) -> int:
        with self._lock:
            self._expire(self._now())
            return len(self._entries)

    @property
    def stored_bytes(self) -> int:
        with self._lock:
            self._expire(self._now())
            return self._bytes
