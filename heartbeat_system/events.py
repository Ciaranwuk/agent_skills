"""Phase 3 event ingestion and dedupe behavior."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Mapping

from .store import (
    DedupeRecord,
    EventCounters,
    HeartbeatStateStore,
    LastEventRecord,
)


@dataclass(frozen=True)
class EventIngestResult:
    """Ingest decision and updated state snapshot."""

    event: LastEventRecord
    counters: EventCounters
    should_deliver: bool
    dedupe_suppressed: bool
    dedupe_key: str | None


class HeartbeatEventService:
    def __init__(
        self,
        *,
        store: HeartbeatStateStore,
        dedupe_window_ms: int,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._dedupe_window_ms = max(int(dedupe_window_ms), 0)
        self._now_ms = now_ms or _now_ms
        # Guards ingest read-modify-write flow so counters/dedupe/last_event stay coherent.
        self._ingest_lock = threading.Lock()

    def ingest_run_result(self, run_result: Mapping[str, str]) -> EventIngestResult:
        with self._ingest_lock:
            now = int(self._now_ms())

            status = str(run_result.get("status", "")).strip()
            reason = str(run_result.get("reason", ""))
            run_reason = str(run_result.get("run_reason", ""))
            output_text = str(run_result.get("output_text", ""))
            error = str(run_result.get("error", ""))

            counters = self._store.get_counters()
            if status == "ran":
                counters = replace(counters, ran=counters.ran + 1)
            elif status == "skipped":
                counters = replace(counters, skipped=counters.skipped + 1)
            elif status == "failed":
                counters = replace(counters, failed=counters.failed + 1)
            else:
                raise ValueError(f"unsupported run_result status: {status!r}")

            should_deliver = False
            dedupe_suppressed = False
            dedupe_key: str | None = None

            if status == "ran" and output_text:
                dedupe_key = _stable_dedupe_key(output_text)
                existing = self._store.get_dedupe(dedupe_key)
                if (
                    existing is not None
                    and self._dedupe_window_ms > 0
                    and now < existing.suppress_until_ms
                ):
                    dedupe_suppressed = True
                    counters = replace(counters, deduped=counters.deduped + 1)
                    hits = existing.hits + 1
                else:
                    should_deliver = True
                    hits = (existing.hits + 1) if existing is not None else 1

                self._store.set_dedupe(
                    DedupeRecord(
                        key=dedupe_key,
                        last_seen_ms=now,
                        suppress_until_ms=now + self._dedupe_window_ms,
                        hits=hits,
                    )
                )

            event = LastEventRecord(
                event_id=_event_id(
                    ts_ms=now,
                    status=status,
                    reason=reason,
                    run_reason=run_reason,
                    output_text=output_text,
                    error=error,
                    dedupe_suppressed=dedupe_suppressed,
                ),
                ts_ms=now,
                status=status,
                reason=reason,
                run_reason=run_reason,
                output_text=output_text,
                error=error,
                dedupe_suppressed=dedupe_suppressed,
            )

            self._store.set_counters(counters)
            self._store.set_last_event(event)

            return EventIngestResult(
                event=event,
                counters=counters,
                should_deliver=should_deliver,
                dedupe_suppressed=dedupe_suppressed,
                dedupe_key=dedupe_key,
            )

    def get_last_event(self) -> LastEventRecord | None:
        return self._store.get_last_event()

    def get_counters(self) -> EventCounters:
        return self._store.get_counters()


def _stable_dedupe_key(output_text: str) -> str:
    return hashlib.sha256(output_text.encode("utf-8")).hexdigest()


def _event_id(
    *,
    ts_ms: int,
    status: str,
    reason: str,
    run_reason: str,
    output_text: str,
    error: str,
    dedupe_suppressed: bool,
) -> str:
    material = "\x1f".join(
        [
            str(ts_ms),
            status,
            reason,
            run_reason,
            output_text,
            error,
            "1" if dedupe_suppressed else "0",
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"evt_{digest}"


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)
