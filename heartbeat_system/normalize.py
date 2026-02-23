"""Normalization contract for HEARTBEAT_OK acknowledgment behavior."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizeResult:
    """Normalized classification for heartbeat responder output."""

    should_deliver: bool
    text: str
    reason: str


def normalize_heartbeat_text(
    text: str | None,
    *,
    ack_token: str = "HEARTBEAT_OK",
    ack_max_chars: int = 300,
) -> NormalizeResult:
    """
    Normalize heartbeat output into deliver-vs-skip outcome.

    Contract:
    - ack-only => skip (`ack-only`)
    - ack + remainder with length <= ack_max_chars => skip (`ack-short-remainder`)
    - non-trivial remainder => deliver with ack token stripped
    """
    normalized = (text or "").strip()
    if normalized == "":
        return NormalizeResult(
            should_deliver=False,
            text="",
            reason="empty-response",
        )

    if ack_token and ack_token in normalized:
        remainder = normalized.replace(ack_token, "").strip()
        if remainder == "":
            return NormalizeResult(
                should_deliver=False,
                text="",
                reason="ack-only",
            )
        if len(remainder) <= ack_max_chars:
            return NormalizeResult(
                should_deliver=False,
                text="",
                reason="ack-short-remainder",
            )
        return NormalizeResult(
            should_deliver=True,
            text=remainder,
            reason="delivered",
        )

    return NormalizeResult(
        should_deliver=True,
        text=normalized,
        reason="delivered",
    )

