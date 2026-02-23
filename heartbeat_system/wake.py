from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

WakeReason = Literal["manual", "exec-event", "hook", "other", "interval", "retry"]

_VALID_WAKE_REASONS: tuple[WakeReason, ...] = (
    "manual",
    "exec-event",
    "hook",
    "other",
    "interval",
    "retry",
)

_PRIORITY: dict[WakeReason, int] = {
    "manual": 3,
    "exec-event": 3,
    "hook": 3,
    "other": 2,
    "interval": 1,
    "retry": 0,
}


@dataclass(frozen=True)
class WakeRequest:
    reason: WakeReason
    requested_at_ms: int


@dataclass(frozen=True)
class WakeDecision:
    accepted: bool
    reason: WakeReason
    queue_size: int
    replaced_reason: WakeReason | None = None


class WakeQueue:
    """Single-slot wake queue with deterministic coalescing behavior."""

    def __init__(self) -> None:
        self._pending: WakeRequest | None = None

    def request_wake(self, reason: WakeReason, *, now_ms: int) -> WakeDecision:
        normalized_reason = self._validate_reason(reason)
        incoming = WakeRequest(reason=normalized_reason, requested_at_ms=now_ms)
        pending = self._pending

        if pending is None:
            self._pending = incoming
            return WakeDecision(accepted=True, reason=normalized_reason, queue_size=1)

        pending_priority = _PRIORITY[pending.reason]
        incoming_priority = _PRIORITY[incoming.reason]

        if incoming_priority > pending_priority or incoming_priority == pending_priority:
            self._pending = incoming
            return WakeDecision(
                accepted=True,
                reason=normalized_reason,
                queue_size=1,
                replaced_reason=pending.reason,
            )

        return WakeDecision(accepted=False, reason=normalized_reason, queue_size=1)

    def pop_next(self) -> WakeRequest | None:
        pending = self._pending
        self._pending = None
        return pending

    def peek(self) -> WakeRequest | None:
        return self._pending

    def clear(self) -> None:
        self._pending = None

    @staticmethod
    def _validate_reason(reason: WakeReason) -> WakeReason:
        if reason in _VALID_WAKE_REASONS:
            return cast(WakeReason, reason)
        raise ValueError(
            f"invalid wake reason: {reason!r}; expected one of {', '.join(_VALID_WAKE_REASONS)}"
        )


__all__ = ["WakeDecision", "WakeQueue", "WakeReason", "WakeRequest"]
