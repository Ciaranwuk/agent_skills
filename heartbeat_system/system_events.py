from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from time import time
from typing import Any


@dataclass(frozen=True)
class SystemEvent:
    event_id: str
    ts_ms: int
    text: str
    source: str = "system"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueueWriteResult:
    accepted: bool
    queue_size: int
    dropped: int = 0
    deduped: bool = False


class SystemEventQueue:
    def __init__(self, *, max_items: int = 100, dedupe_consecutive: bool = True) -> None:
        if max_items <= 0:
            raise ValueError("max_items must be >= 1")
        self._max_items = max_items
        self._dedupe_consecutive = dedupe_consecutive
        self._items: list[SystemEvent] = []
        self._next_id = 1
        self._lock = RLock()

    def publish(
        self,
        text: str,
        *,
        source: str = "system",
        context: dict[str, Any] | None = None,
    ) -> QueueWriteResult:
        event_text = str(text)
        event_context = dict(context or {})

        with self._lock:
            if self._dedupe_consecutive and self._items:
                prev = self._items[-1]
                if (
                    prev.text == event_text
                    and prev.source == source
                    and prev.context == event_context
                ):
                    return QueueWriteResult(
                        accepted=False,
                        queue_size=len(self._items),
                        dropped=0,
                        deduped=True,
                    )

            event = SystemEvent(
                event_id=f"se-{self._next_id}",
                ts_ms=int(time() * 1000),
                text=event_text,
                source=source,
                context=event_context,
            )
            self._next_id += 1
            self._items.append(event)

            dropped = 0
            while len(self._items) > self._max_items:
                self._items.pop(0)
                dropped += 1

            return QueueWriteResult(
                accepted=True,
                queue_size=len(self._items),
                dropped=dropped,
                deduped=False,
            )

    def drain(self, *, limit: int | None = None) -> list[SystemEvent]:
        with self._lock:
            if limit is None:
                drained = list(self._items)
                self._items.clear()
                return drained

            if limit <= 0:
                return []

            count = min(limit, len(self._items))
            drained = self._items[:count]
            del self._items[:count]
            return drained

    def peek(self, *, limit: int = 10) -> list[SystemEvent]:
        with self._lock:
            if limit <= 0:
                return []
            return list(self._items[:limit])

    def size(self) -> int:
        with self._lock:
            return len(self._items)


class SessionSystemEventBus:
    def __init__(self, *, max_items: int = 100, dedupe_consecutive: bool = True) -> None:
        self._max_items = max_items
        self._dedupe_consecutive = dedupe_consecutive
        self._queues: dict[str, SystemEventQueue] = {}
        self._lock = RLock()

    def get_queue(self, session_key: str) -> SystemEventQueue:
        with self._lock:
            queue = self._queues.get(session_key)
            if queue is None:
                queue = SystemEventQueue(
                    max_items=self._max_items,
                    dedupe_consecutive=self._dedupe_consecutive,
                )
                self._queues[session_key] = queue
            return queue

    def publish(
        self,
        session_key: str,
        text: str,
        *,
        source: str = "system",
        context: dict[str, Any] | None = None,
    ) -> QueueWriteResult:
        queue = self.get_queue(session_key)
        return queue.publish(text, source=source, context=context)

    def drain(self, session_key: str, *, limit: int | None = None) -> list[SystemEvent]:
        queue = self.get_queue(session_key)
        return queue.drain(limit=limit)
