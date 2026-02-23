from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Protocol


@dataclass(frozen=True)
class EventCounters:
    ran: int = 0
    skipped: int = 0
    failed: int = 0
    deduped: int = 0


@dataclass(frozen=True)
class LastEventRecord:
    event_id: str
    ts_ms: int
    status: str
    reason: str
    run_reason: str
    output_text: str = ""
    error: str = ""
    dedupe_suppressed: bool = False


@dataclass(frozen=True)
class DedupeRecord:
    key: str
    last_seen_ms: int
    suppress_until_ms: int
    hits: int = 1


@dataclass(frozen=True)
class StoreSnapshot:
    enabled: bool
    last_event: LastEventRecord | None
    counters: EventCounters
    dedupe: dict[str, DedupeRecord] = field(default_factory=dict)


class HeartbeatStateStore(Protocol):
    def get_load_warning(self) -> str | None:
        ...

    def get_enabled(self) -> bool:
        ...

    def set_enabled(self, enabled: bool) -> bool:
        ...

    def get_last_event(self) -> LastEventRecord | None:
        ...

    def set_last_event(self, event: LastEventRecord) -> None:
        ...

    def get_counters(self) -> EventCounters:
        ...

    def set_counters(self, counters: EventCounters) -> None:
        ...

    def get_dedupe(self, key: str) -> DedupeRecord | None:
        ...

    def set_dedupe(self, rec: DedupeRecord) -> None:
        ...

    def prune_dedupe(self, now_ms: int) -> int:
        ...

    def snapshot(self) -> StoreSnapshot:
        ...


class InMemoryHeartbeatStateStore(HeartbeatStateStore):
    """RLock-protected process-local store for heartbeat runtime state."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._enabled = True
        self._last_event: LastEventRecord | None = None
        self._counters = EventCounters()
        self._dedupe: dict[str, DedupeRecord] = {}

    def get_load_warning(self) -> str | None:
        return None

    def get_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            previous = self._enabled
            self._enabled = enabled
            return previous

    def get_last_event(self) -> LastEventRecord | None:
        with self._lock:
            return self._last_event

    def set_last_event(self, event: LastEventRecord) -> None:
        with self._lock:
            self._last_event = event

    def get_counters(self) -> EventCounters:
        with self._lock:
            return self._counters

    def set_counters(self, counters: EventCounters) -> None:
        with self._lock:
            self._counters = counters

    def get_dedupe(self, key: str) -> DedupeRecord | None:
        with self._lock:
            return self._dedupe.get(key)

    def set_dedupe(self, rec: DedupeRecord) -> None:
        with self._lock:
            self._dedupe[rec.key] = rec

    def prune_dedupe(self, now_ms: int) -> int:
        with self._lock:
            expired = [
                key
                for key, rec in self._dedupe.items()
                if rec.suppress_until_ms <= now_ms
            ]
            for key in expired:
                del self._dedupe[key]
            return len(expired)

    def snapshot(self) -> StoreSnapshot:
        with self._lock:
            return StoreSnapshot(
                enabled=self._enabled,
                last_event=self._last_event,
                counters=self._counters,
                dedupe=dict(self._dedupe),
            )


class JsonFileHeartbeatStateStore(HeartbeatStateStore):
    """JSON-file-backed state store with safe fallback on missing/corrupt files."""

    def __init__(self, state_file: str | Path) -> None:
        self._lock = RLock()
        self._state_file = Path(state_file)
        self._load_error: str | None = None
        self._enabled = True
        self._last_event: LastEventRecord | None = None
        self._counters = EventCounters()
        self._dedupe: dict[str, DedupeRecord] = {}
        self._load_from_disk()

    @property
    def state_file(self) -> Path:
        return self._state_file

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def get_load_warning(self) -> str | None:
        if self._load_error is None:
            return None
        return f"persistent-state-load-warning: {self._load_error}"

    def get_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            previous = self._enabled
            self._enabled = bool(enabled)
            self._persist_locked()
            return previous

    def get_last_event(self) -> LastEventRecord | None:
        with self._lock:
            return self._last_event

    def set_last_event(self, event: LastEventRecord) -> None:
        with self._lock:
            self._last_event = event
            self._persist_locked()

    def get_counters(self) -> EventCounters:
        with self._lock:
            return self._counters

    def set_counters(self, counters: EventCounters) -> None:
        with self._lock:
            self._counters = counters
            self._persist_locked()

    def get_dedupe(self, key: str) -> DedupeRecord | None:
        with self._lock:
            return self._dedupe.get(key)

    def set_dedupe(self, rec: DedupeRecord) -> None:
        with self._lock:
            self._dedupe[rec.key] = rec
            self._persist_locked()

    def prune_dedupe(self, now_ms: int) -> int:
        with self._lock:
            expired = [
                key
                for key, rec in self._dedupe.items()
                if rec.suppress_until_ms <= now_ms
            ]
            for key in expired:
                del self._dedupe[key]
            if expired:
                self._persist_locked()
            return len(expired)

    def snapshot(self) -> StoreSnapshot:
        with self._lock:
            return StoreSnapshot(
                enabled=self._enabled,
                last_event=self._last_event,
                counters=self._counters,
                dedupe=dict(self._dedupe),
            )

    def _load_from_disk(self) -> None:
        self._load_error = None
        try:
            text = self._state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self._load_error = str(exc)
            return

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            self._load_error = str(exc)
            return

        if not isinstance(payload, dict):
            self._load_error = "invalid JSON payload type; expected object"
            return

        with self._lock:
            self._enabled = _coerce_bool(payload.get("enabled"), default=True)
            self._counters = _coerce_counters(payload.get("counters"))
            self._last_event = _coerce_last_event(payload.get("last_event"))
            self._dedupe = _coerce_dedupe_map(payload.get("dedupe"))

    def _persist_locked(self) -> None:
        payload = _snapshot_to_payload(
            StoreSnapshot(
                enabled=self._enabled,
                last_event=self._last_event,
                counters=self._counters,
                dedupe=self._dedupe,
            )
        )
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self._state_file.with_name(f"{self._state_file.name}.tmp")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        tmp_file.write_text(serialized, encoding="utf-8")
        tmp_file.replace(self._state_file)


def _snapshot_to_payload(snapshot: StoreSnapshot) -> dict[str, object]:
    counters = {
        "ran": int(snapshot.counters.ran),
        "skipped": int(snapshot.counters.skipped),
        "failed": int(snapshot.counters.failed),
        "deduped": int(snapshot.counters.deduped),
    }
    last_event = None
    if snapshot.last_event is not None:
        event = snapshot.last_event
        last_event = {
            "event_id": event.event_id,
            "ts_ms": int(event.ts_ms),
            "status": event.status,
            "reason": event.reason,
            "run_reason": event.run_reason,
            "output_text": event.output_text,
            "error": event.error,
            "dedupe_suppressed": bool(event.dedupe_suppressed),
        }
    dedupe = {
        key: {
            "key": rec.key,
            "last_seen_ms": int(rec.last_seen_ms),
            "suppress_until_ms": int(rec.suppress_until_ms),
            "hits": int(rec.hits),
        }
        for key, rec in snapshot.dedupe.items()
    }
    return {
        "enabled": bool(snapshot.enabled),
        "counters": counters,
        "last_event": last_event,
        "dedupe": dedupe,
    }


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _coerce_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _coerce_str(value: object, *, default: str = "") -> str:
    if isinstance(value, str):
        return value
    return default


def _coerce_counters(value: object) -> EventCounters:
    if not isinstance(value, dict):
        return EventCounters()
    return EventCounters(
        ran=_coerce_int(value.get("ran"), default=0),
        skipped=_coerce_int(value.get("skipped"), default=0),
        failed=_coerce_int(value.get("failed"), default=0),
        deduped=_coerce_int(value.get("deduped"), default=0),
    )


def _coerce_last_event(value: object) -> LastEventRecord | None:
    if not isinstance(value, dict):
        return None
    event_id = _coerce_str(value.get("event_id"))
    status = _coerce_str(value.get("status"))
    reason = _coerce_str(value.get("reason"))
    run_reason = _coerce_str(value.get("run_reason"))
    if not event_id or not status:
        return None
    return LastEventRecord(
        event_id=event_id,
        ts_ms=_coerce_int(value.get("ts_ms"), default=0),
        status=status,
        reason=reason,
        run_reason=run_reason,
        output_text=_coerce_str(value.get("output_text")),
        error=_coerce_str(value.get("error")),
        dedupe_suppressed=_coerce_bool(value.get("dedupe_suppressed"), default=False),
    )


def _coerce_dedupe_map(value: object) -> dict[str, DedupeRecord]:
    if not isinstance(value, dict):
        return {}

    dedupe: dict[str, DedupeRecord] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        if not isinstance(raw, dict):
            continue
        record_key = _coerce_str(raw.get("key"), default=key)
        if not record_key:
            continue
        dedupe[record_key] = DedupeRecord(
            key=record_key,
            last_seen_ms=_coerce_int(raw.get("last_seen_ms"), default=0),
            suppress_until_ms=_coerce_int(raw.get("suppress_until_ms"), default=0),
            hits=_coerce_int(raw.get("hits"), default=1),
        )
    return dedupe
