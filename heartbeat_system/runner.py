"""Core run-once heartbeat path (Phase 0/1 lane B)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .config import HeartbeatConfig
from .contracts import HeartbeatRequest, HeartbeatResponder, HeartbeatResponse
from .heartbeat_file import load_heartbeat_prompt
from .normalize import NormalizeResult, normalize_heartbeat_text


@dataclass(frozen=True)
class HeartbeatRunResult:
    """Structured machine-readable run outcome."""

    status: str
    reason: str
    output_text: str = ""
    error: str = ""
    run_reason: str = ""


ConfigLike = HeartbeatConfig | Mapping[str, object]


def run_heartbeat_once(
    config: ConfigLike,
    responder: HeartbeatResponder,
    *,
    reason: str = "manual",
    system_events: Sequence[str] | None = None,
) -> dict[str, str]:
    """
    Execute one heartbeat cycle through the injected responder.

    Status contract:
    - ran: normalized output should be delivered
    - skipped: deterministic preflight/normalization skip
    - failed: adapter error
    """
    if not _config_bool(config, "enabled", True):
        return asdict(HeartbeatRunResult(status="skipped", reason="disabled", run_reason=reason))

    heartbeat_file = _config_value(config, "heartbeat_file", "HEARTBEAT.md")
    prompt_load = load_heartbeat_prompt(str(heartbeat_file))
    if prompt_load.is_empty:
        return asdict(
            HeartbeatRunResult(
                status="skipped",
                reason="empty-heartbeat-file",
                run_reason=reason,
            )
        )

    request = _build_request(
        config=config,
        reason=reason,
        prompt=prompt_load.text,
        system_events=list(system_events or ()),
    )

    try:
        response = responder.respond(request)
    except Exception as exc:
        return asdict(
            HeartbeatRunResult(
                status="failed",
                reason="adapter-exception",
                error=_sanitize_exception(exc),
                run_reason=reason,
            )
        )

    normalized = _normalize_response(config=config, response=response)
    if normalized.should_deliver:
        return asdict(
            HeartbeatRunResult(
                status="ran",
                reason="delivered",
                output_text=normalized.text,
                run_reason=reason,
            )
        )
    return asdict(
        HeartbeatRunResult(
            status="skipped",
            reason=normalized.reason,
            run_reason=reason,
        )
    )


def _normalize_response(
    *,
    config: ConfigLike,
    response: HeartbeatResponse | Mapping[str, object],
) -> NormalizeResult:
    text = _extract_response_text(response)
    ack_token = str(_config_value(config, "ack_token", "HEARTBEAT_OK"))
    ack_max_chars = int(_config_value(config, "ack_max_chars", 300))
    return normalize_heartbeat_text(
        text,
        ack_token=ack_token,
        ack_max_chars=ack_max_chars,
    )


def _build_request(
    *,
    config: ConfigLike,
    reason: str,
    prompt: str,
    system_events: list[str],
) -> HeartbeatRequest:
    return HeartbeatRequest(
        prompt=prompt,
        reason=reason,
        now_ms=_now_ms(),
        session_key=str(_config_value(config, "session_key", "default")),
        system_events=system_events,
        metadata={},
    )


def _extract_response_text(response: HeartbeatResponse | Mapping[str, object]) -> str:
    if isinstance(response, Mapping):
        return str(response.get("text", ""))
    return str(response.text)


def _config_value(config: ConfigLike, key: str, default: object) -> object:
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _config_bool(config: ConfigLike, key: str, default: bool) -> bool:
    return bool(_config_value(config, key, default))


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _sanitize_exception(exc: Exception) -> str:
    raw = f"{type(exc).__name__}: {exc}".strip()
    compact = " ".join(raw.split())
    return compact[:500]
