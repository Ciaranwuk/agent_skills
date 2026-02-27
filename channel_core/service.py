from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

from .contracts import (
    ChannelAdapterPort,
    ChannelRuntimeError,
    InboundMessage,
    OrchestratorPort,
    OutboundMessage,
)
from .session_map import session_id_for_inbound


@dataclass(frozen=True)
class ProcessOnceResult:
    """Machine-readable outcome of one service cycle."""

    status: str
    reason: str
    fetched_count: int = 0
    sent_count: int = 0
    acked_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


def process_once(
    adapter: ChannelAdapterPort,
    orchestrator: OrchestratorPort,
    *,
    session_resolver: Callable[[InboundMessage], str] = session_id_for_inbound,
) -> dict[str, object]:
    """
    Run one deterministic fetch/process/send/ack cycle.

    Safety behavior:
    - Adapter fetch exceptions are returned as failed outcomes.
    - Per-update failures are collected and do not crash the caller.
    - One inbound update can produce at most one outbound message.
    """
    try:
        updates = adapter.fetch_updates()
    except Exception as exc:
        message = _sanitize_exception(exc)
        return asdict(
            ProcessOnceResult(
                status="failed",
                reason="adapter-fetch-exception",
                error_count=1,
                errors=[message],
            )
        )

    if not updates:
        return asdict(ProcessOnceResult(status="ok", reason="no-updates"))

    sent_count = 0
    acked_count = 0
    errors: list[str] = []

    for inbound in updates:
        try:
            session_id = session_resolver(inbound)
            outbound = orchestrator.handle_message(inbound, session_id=session_id)
            if outbound is None:
                pass
            elif isinstance(outbound, OutboundMessage):
                adapter.send_message(outbound)
                sent_count += 1
            else:
                raise ChannelRuntimeError(
                    f"orchestrator returned unsupported output type: {type(outbound).__name__}"
                )
        except Exception as exc:
            errors.append(f"update {inbound.update_id}: {_sanitize_exception(exc)}")
        finally:
            try:
                adapter.ack_update(inbound.update_id)
                acked_count += 1
            except Exception as exc:
                errors.append(f"update {inbound.update_id}: ack failed: {_sanitize_exception(exc)}")

    reason = "processed" if not errors else "completed-with-errors"
    return asdict(
        ProcessOnceResult(
            status="ok",
            reason=reason,
            fetched_count=len(updates),
            sent_count=sent_count,
            acked_count=acked_count,
            error_count=len(errors),
            errors=errors,
        )
    )


def _sanitize_exception(exc: Exception) -> str:
    raw = f"{type(exc).__name__}: {exc}".strip()
    compact = " ".join(raw.split())
    return compact[:500]
