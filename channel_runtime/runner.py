from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from channel_core.contracts import InboundMessage, OrchestratorPort, OutboundMessage
from channel_core.service import process_once
from telegram_channel.adapter import TelegramChannelAdapter
from telegram_channel.api import TelegramApiClient

from .config import RuntimeConfig


PublishSystemEventFn = Callable[..., dict[str, Any]]
MemoryLookupFn = Callable[[str], str | None]


@dataclass(frozen=True)
class HeartbeatEventEmitter:
    """Best-effort runtime failure emitter using heartbeat system events."""

    publish_event: PublishSystemEventFn | None = None
    enabled: bool = True
    source: str = "channel-runtime"

    def emit_failure(
        self,
        *,
        session_key: str,
        text: str,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        if not self.enabled:
            return False

        publisher = self.publish_event or _default_publish_system_event
        try:
            publisher(
                session_key=session_key,
                text=text,
                source=self.source,
                context=dict(context or {}),
            )
            return True
        except Exception:
            return False


class DefaultOrchestrator:
    """Minimal TG-P2 orchestrator: echo text with optional memory note."""

    def __init__(
        self,
        *,
        enable_memory_hook: bool = False,
        memory_lookup: MemoryLookupFn | None = None,
    ) -> None:
        self._enable_memory_hook = bool(enable_memory_hook)
        self._memory_lookup = memory_lookup
        self._diagnostics: list[dict[str, Any]] = []

    def handle_message(self, inbound: InboundMessage, *, session_id: str) -> OutboundMessage | None:
        try:
            text = f"echo: {inbound.text}"
            if self._enable_memory_hook:
                lookup = self._memory_lookup or _default_memory_lookup
                memory_text = lookup(inbound.text)
                if memory_text:
                    text = f"{text}\n\nmemory: {memory_text}"

            return OutboundMessage(
                chat_id=inbound.chat_id,
                text=text,
                reply_to_message_id=inbound.message_id,
                metadata={"session_id": session_id},
            )
        except Exception as exc:
            self._diagnostics.append(
                {
                    "code": "orchestrator-error",
                    "update_id": inbound.update_id,
                    "message": _sanitize_exception(exc),
                }
            )
            return None

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        diagnostics = list(self._diagnostics)
        self._diagnostics.clear()
        return diagnostics


class AllowlistGateOrchestrator:
    """Wrapper that drops disallowed chat IDs before delegate orchestration."""

    def __init__(self, delegate: OrchestratorPort, *, allowed_chat_ids: tuple[str, ...]) -> None:
        self._delegate = delegate
        self._allowed_chat_ids = {
            normalized
            for normalized in (_normalize_chat_id_value(value) for value in allowed_chat_ids)
            if normalized is not None
        }
        self._diagnostics: list[dict[str, Any]] = []

    def handle_message(self, inbound: InboundMessage, *, session_id: str) -> OutboundMessage | None:
        chat_id = _normalize_chat_id_value(inbound.chat_id)
        if self._allowed_chat_ids and chat_id not in self._allowed_chat_ids:
            self._diagnostics.append(
                {
                    "code": "allowlist-drop",
                    "update_id": inbound.update_id,
                    "chat_id": str(inbound.chat_id),
                    "message": f"dropped update {inbound.update_id}: chat_id not allowlisted ({inbound.chat_id})",
                }
            )
            return None
        return self._delegate.handle_message(inbound, session_id=session_id)

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        diagnostics = list(self._diagnostics)
        self._diagnostics.clear()

        delegate_drain = getattr(self._delegate, "drain_diagnostics", None)
        if callable(delegate_drain):
            diagnostics.extend(list(delegate_drain()))
        return diagnostics


def run_cycle(
    *,
    config: RuntimeConfig,
    adapter: TelegramChannelAdapter | None = None,
    orchestrator: OrchestratorPort | None = None,
    api_client: TelegramApiClient | None = None,
    heartbeat_emitter: HeartbeatEventEmitter | None = None,
    enable_memory_hook: bool | None = None,
    memory_lookup: MemoryLookupFn | None = None,
    failure_session_key: str = "telegram:runtime",
) -> dict[str, Any]:
    """Run one TG-P2 service cycle with default runtime wiring."""
    resolved_adapter = adapter or TelegramChannelAdapter(api_client or TelegramApiClient(config.token))
    resolved_orchestrator = orchestrator or DefaultOrchestrator(
        enable_memory_hook=_resolve_memory_hook_flag(enable_memory_hook),
        memory_lookup=memory_lookup,
    )
    emitter = heartbeat_emitter or HeartbeatEventEmitter()
    gated_orchestrator = AllowlistGateOrchestrator(
        resolved_orchestrator,
        allowed_chat_ids=config.allowed_chat_ids,
    )

    try:
        result = dict(process_once(resolved_adapter, gated_orchestrator))
    except Exception as exc:
        message = _sanitize_exception(exc)
        emitted = _emit_best_effort_failure(
            emitter,
            session_key=failure_session_key,
            text=f"channel-runtime process_once exception: {message}",
            context={"code": "runtime-process-once-exception"},
        )
        return {
            "status": "failed",
            "reason": "runtime-process-once-exception",
            "fetched_count": 0,
            "sent_count": 0,
            "acked_count": 0,
            "error_count": 1,
            "errors": [message],
            "heartbeat_emit_failures": 0 if emitted else 1,
        }

    diagnostics: list[dict[str, Any]] = []
    drain_fn = getattr(gated_orchestrator, "drain_diagnostics", None)
    if callable(drain_fn):
        diagnostics = list(drain_fn())

    heartbeat_emit_failures = 0
    dropped_updates: list[dict[str, str]] = []

    if result.get("status") != "ok" or int(result.get("error_count", 0)) > 0:
        emitted = _emit_best_effort_failure(
            emitter,
            session_key=failure_session_key,
            text=f"channel-runtime cycle failure: {result.get('reason', 'unknown')}",
            context={
                "code": "service-cycle-error",
                "status": result.get("status"),
                "reason": result.get("reason"),
                "error_count": int(result.get("error_count", 0)),
            },
        )
        if not emitted:
            heartbeat_emit_failures += 1

    for item in diagnostics:
        if str(item.get("code", "")).strip() == "allowlist-drop":
            dropped_updates.append(
                {
                    "update_id": str(item.get("update_id", "")),
                    "chat_id": str(item.get("chat_id", "")),
                    "reason": str(item.get("message", "allowlist drop")),
                }
            )
            continue
        emitted = _emit_best_effort_failure(
            emitter,
            session_key=failure_session_key,
            text=f"orchestrator failure: {item.get('message', 'unknown')}",
            context=dict(item),
        )
        if not emitted:
            heartbeat_emit_failures += 1

    if diagnostics:
        errors = [
            str(entry.get("message", "unknown"))
            for entry in diagnostics
            if str(entry.get("code", "")).strip() != "allowlist-drop"
        ]
        prior_errors = [str(err) for err in result.get("errors", [])]
        result["errors"] = prior_errors + errors
        result["error_count"] = int(result.get("error_count", 0)) + len(errors)
        if errors and result.get("status") == "ok":
            result["reason"] = "completed-with-errors"

    result["dropped_count"] = len(dropped_updates)
    result["dropped_updates"] = dropped_updates
    result["heartbeat_emit_failures"] = heartbeat_emit_failures
    return result


def run_loop(
    *,
    config: RuntimeConfig,
    run_cycle_fn: Callable[..., dict[str, Any]] = run_cycle,
    sleep_fn: Callable[[float], None] = time.sleep,
    on_cycle: Callable[[dict[str, Any]], None] | None = None,
    max_cycles: int | None = None,
) -> dict[str, Any]:
    """Run either one cycle (--once) or a continuous polling loop."""
    cycles = 0
    last_result: dict[str, Any] = {
        "status": "ok",
        "reason": "not-started",
        "fetched_count": 0,
        "sent_count": 0,
        "acked_count": 0,
        "error_count": 0,
        "errors": [],
    }

    while True:
        cycles += 1
        try:
            last_result = dict(run_cycle_fn(config=config))
        except Exception as exc:
            last_result = {
                "status": "failed",
                "reason": "runtime-loop-cycle-exception",
                "fetched_count": 0,
                "sent_count": 0,
                "acked_count": 0,
                "error_count": 1,
                "errors": [_sanitize_exception(exc)],
                "heartbeat_emit_failures": 0,
                "dropped_count": 0,
                "dropped_updates": [],
            }

        if on_cycle is not None:
            on_cycle(last_result)

        if config.once:
            return last_result

        if max_cycles is not None and cycles >= max_cycles:
            return last_result

        sleep_fn(config.poll_interval_s)


def _emit_best_effort_failure(
    emitter: HeartbeatEventEmitter,
    *,
    session_key: str,
    text: str,
    context: Mapping[str, Any] | None,
) -> bool:
    return emitter.emit_failure(session_key=session_key, text=text, context=context)


def _resolve_memory_hook_flag(explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    raw = os.environ.get("CHANNEL_ENABLE_MEMORY_HOOK", "false")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _default_memory_lookup(query: str) -> str | None:
    from memory_system.api import memory_search

    response = memory_search(
        query=query,
        maxResults=1,
        minScore=0.0,
        workspace=Path.cwd(),
    )
    rows = response.get("results")
    if not isinstance(rows, list) or not rows:
        return None
    first = rows[0]
    if not isinstance(first, Mapping):
        return None
    snippet = first.get("snippet")
    if snippet is None:
        return None
    text = str(snippet).strip()
    return text or None


def _default_publish_system_event(*, session_key: str, text: str, source: str, context: Mapping[str, Any]) -> dict[str, Any]:
    from heartbeat_system.api import publish_system_event

    return publish_system_event(session_key=session_key, text=text, source=source, context=context)


def _sanitize_exception(exc: Exception) -> str:
    raw = f"{type(exc).__name__}: {exc}".strip()
    compact = " ".join(raw.split())
    return compact[:500]


def _normalize_chat_id_value(value: object) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(int(text))
    except ValueError:
        return text
