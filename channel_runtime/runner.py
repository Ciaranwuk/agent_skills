from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from channel_core.contracts import InboundMessage, OrchestratorPort, OutboundMessage
from channel_core.service import process_once
from telegram_channel.adapter import TelegramChannelAdapter
from telegram_channel.api import TelegramApiClient
from telegram_channel.cursor_state import DurableCursorStateStore

from .codex_orchestrator import CodexInvocationRequest, CodexOrchestrator, CodexSessionManager, CodexSessionPolicy
from .config import RuntimeConfig


PublishSystemEventFn = Callable[..., dict[str, Any]]
MemoryLookupFn = Callable[[str], str | None]
CodexInvokeFn = Callable[[CodexInvocationRequest], str | None]
DiagnosticEntry = tuple[str, dict[str, Any]]
TelemetryDigest = dict[str, int]

_TELEMETRY_CONTRACT = "tg-live.runtime.telemetry"
_TELEMETRY_VERSION = "2.0"
_TELEMETRY_PLACEHOLDERS = {
    "retry_total": "pending-provider-attempt-instrumentation",
    "queue_depth": "pending-runtime-queue-introspection",
    "worker_restart_total": "pending-supervisor-integration",
}


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
    codex_invoke: CodexInvokeFn | None = None,
    failure_session_key: str = "telegram:runtime",
) -> dict[str, Any]:
    """Run one TG-P2 service cycle with default runtime wiring."""
    cycle_started_at = time.perf_counter()
    resolved_adapter = adapter or TelegramChannelAdapter(
        api_client or TelegramApiClient(config.token),
        cursor_state_store=_resolve_cursor_state_store(config.cursor_state_path),
        strict_state_io=config.strict_cursor_state_io,
    )
    resolved_orchestrator = orchestrator or _resolve_default_orchestrator(
        config=config,
        enable_memory_hook=enable_memory_hook,
        memory_lookup=memory_lookup,
        codex_invoke=codex_invoke,
    )
    emitter = heartbeat_emitter or HeartbeatEventEmitter()
    heartbeat_emit_attempts = 0
    heartbeat_emit_failures = 0
    gated_orchestrator = AllowlistGateOrchestrator(
        resolved_orchestrator,
        allowed_chat_ids=config.allowed_chat_ids,
    )

    def _emit_failure(
        *,
        text: str,
        base_context: Mapping[str, Any],
        fetch_total: int,
        send_total: int,
        drop_total: int,
    ) -> bool:
        nonlocal heartbeat_emit_attempts
        nonlocal heartbeat_emit_failures

        if emitter.enabled:
            heartbeat_emit_attempts += 1

        emit_state = _derive_heartbeat_emit_state(
            enabled=emitter.enabled,
            emit_attempted=heartbeat_emit_attempts,
            emit_failures=heartbeat_emit_failures,
        )
        context = _build_failure_event_context(
            base_context=base_context,
            heartbeat_emit_state=emit_state,
            telemetry_digest=_build_telemetry_digest(
                fetch_total=fetch_total,
                send_total=send_total,
                drop_total=drop_total,
                cycle_total_ms=_elapsed_ms(cycle_started_at),
            ),
        )
        emitted = _emit_best_effort_failure(
            emitter,
            session_key=failure_session_key,
            text=text,
            context=context,
        )
        if emitter.enabled and not emitted:
            heartbeat_emit_failures += 1
        return emitted

    try:
        result = dict(process_once(resolved_adapter, gated_orchestrator, ack_policy=config.ack_policy))
    except Exception as exc:
        message = _sanitize_exception(exc)
        _emit_failure(
            text=f"channel-runtime process_once exception: {message}",
            base_context={
                "code": "runtime-process-once-exception",
                "status": "failed",
                "reason": "runtime-process-once-exception",
                "error_count": 1,
            },
            fetch_total=0,
            send_total=0,
            drop_total=0,
        )
        detail = _build_error_detail(
            code="runtime-process-once-exception",
            message=message,
            retryable=True,
            source="runtime-wrapper",
            category="error",
            layer="runtime-wrapper",
            operation="run_cycle",
        )
        failed_result = {
            "status": "failed",
            "reason": "runtime-process-once-exception",
            "fetched_count": 0,
            "sent_count": 0,
            "acked_count": 0,
            "ack_skipped_count": 0,
            "error_count": 1,
            "errors": [message],
            "error_details": [detail],
            "heartbeat_emit_failures": heartbeat_emit_failures,
            "dropped_count": 0,
            "dropped_updates": [],
        }
        failed_result["telemetry"] = _build_runtime_telemetry(
            fetch_total=0,
            send_total=0,
            drop_total=0,
            heartbeat_emit_failures=heartbeat_emit_failures,
            cycle_total_ms=_elapsed_ms(cycle_started_at),
            heartbeat_emit_state=_derive_heartbeat_emit_state(
                enabled=emitter.enabled,
                emit_attempted=heartbeat_emit_attempts,
                emit_failures=heartbeat_emit_failures,
            ),
        )
        return failed_result

    diagnostics: list[DiagnosticEntry] = []
    diagnostics.extend(_drain_diagnostics("orchestrator", gated_orchestrator))
    diagnostics.extend(_drain_diagnostics("adapter", resolved_adapter))

    dropped_updates: list[dict[str, str]] = []
    diagnostic_errors: list[str] = []
    service_error_details = _map_process_once_errors(result)
    diagnostic_error_details: list[dict[str, Any]] = []

    if result.get("status") != "ok" or int(result.get("error_count", 0)) > 0:
        _emit_failure(
            text=f"channel-runtime cycle failure: {result.get('reason', 'unknown')}",
            base_context={
                "code": "service-cycle-error",
                "status": result.get("status"),
                "reason": result.get("reason"),
                "error_count": int(result.get("error_count", 0)),
            },
            fetch_total=int(result.get("fetched_count", 0)),
            send_total=int(result.get("sent_count", 0)),
            drop_total=0,
        )

    for source, item in diagnostics:
        code = str(item.get("code", "")).strip()
        mapped_detail = _map_runtime_diagnostic(source=source, item=item)
        if mapped_detail is not None:
            diagnostic_error_details.append(mapped_detail)
        if code in {"allowlist-drop", "stale-drop"}:
            dropped_updates.append(
                {
                    "update_id": str(item.get("update_id", "")),
                    "chat_id": str(item.get("chat_id", "")),
                    "reason": str(item.get("message", f"{code or 'diagnostic'} drop")),
                }
            )
            continue
        diagnostic_errors.append(str(item.get("message", "unknown")))
        _emit_failure(
            text=f"{source} failure: {item.get('message', 'unknown')}",
            base_context=dict(item),
            fetch_total=int(result.get("fetched_count", 0)),
            send_total=int(result.get("sent_count", 0)),
            drop_total=len(dropped_updates),
        )

    if diagnostic_errors:
        prior_errors = [str(err) for err in result.get("errors", [])]
        result["errors"] = prior_errors + diagnostic_errors
        result["error_count"] = int(result.get("error_count", 0)) + len(diagnostic_errors)
        if result.get("status") == "ok":
            result["reason"] = "completed-with-errors"

    result["dropped_count"] = len(dropped_updates)
    result["dropped_updates"] = dropped_updates
    result["heartbeat_emit_failures"] = heartbeat_emit_failures
    result["error_details"] = _dedupe_error_details(service_error_details + diagnostic_error_details)
    result["telemetry"] = _build_runtime_telemetry(
        fetch_total=int(result.get("fetched_count", 0)),
        send_total=int(result.get("sent_count", 0)),
        drop_total=int(result.get("dropped_count", 0)),
        heartbeat_emit_failures=heartbeat_emit_failures,
        cycle_total_ms=_elapsed_ms(cycle_started_at),
        heartbeat_emit_state=_derive_heartbeat_emit_state(
            enabled=emitter.enabled,
            emit_attempted=heartbeat_emit_attempts,
            emit_failures=heartbeat_emit_failures,
        ),
    )
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
        "ack_skipped_count": 0,
        "error_count": 0,
        "errors": [],
    }

    while True:
        cycles += 1
        try:
            last_result = dict(run_cycle_fn(config=config))
        except Exception as exc:
            message = _sanitize_exception(exc)
            last_result = {
                "status": "failed",
                "reason": "runtime-loop-cycle-exception",
                "fetched_count": 0,
                "sent_count": 0,
                "acked_count": 0,
                "ack_skipped_count": 0,
                "error_count": 1,
                "errors": [message],
                "error_details": [
                    _build_error_detail(
                        code="runtime-loop-cycle-exception",
                        message=message,
                        retryable=True,
                        source="runtime-wrapper",
                        category="error",
                        layer="runtime-wrapper",
                        operation="run_loop",
                    )
                ],
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


def _elapsed_ms(started_at: float) -> int:
    elapsed = (time.perf_counter() - started_at) * 1000.0
    return max(0, int(elapsed))


def _derive_heartbeat_emit_state(*, enabled: bool, emit_attempted: int, emit_failures: int) -> str:
    if not enabled or emit_attempted <= 0:
        return "disabled"
    if emit_failures > 0:
        return "emit-failed"
    return "emitted"


def _build_telemetry_digest(
    *,
    fetch_total: int,
    send_total: int,
    drop_total: int,
    cycle_total_ms: int,
) -> TelemetryDigest:
    return {
        "fetch_total": int(fetch_total),
        "send_total": int(send_total),
        "drop_total": int(drop_total),
        "cycle_total_ms": int(cycle_total_ms),
    }


def _build_failure_event_context(
    *,
    base_context: Mapping[str, Any],
    heartbeat_emit_state: str,
    telemetry_digest: TelemetryDigest,
) -> dict[str, Any]:
    context = dict(base_context)
    context["heartbeat"] = {"emit_state": heartbeat_emit_state}
    context["telemetry_digest"] = dict(telemetry_digest)
    return context


def _build_runtime_telemetry(
    *,
    fetch_total: int,
    send_total: int,
    drop_total: int,
    heartbeat_emit_failures: int,
    cycle_total_ms: int,
    heartbeat_emit_state: str,
) -> dict[str, Any]:
    return {
        "contract": _TELEMETRY_CONTRACT,
        "version": _TELEMETRY_VERSION,
        "counters": {
            "fetch_total": int(fetch_total),
            "send_total": int(send_total),
            "retry_total": None,
            "drop_total": int(drop_total),
            "queue_depth": None,
            "worker_restart_total": None,
            "heartbeat_emit_failures": int(heartbeat_emit_failures),
        },
        "timers_ms": {
            "cycle_total": int(cycle_total_ms),
            "fetch": None,
            "send": None,
        },
        "heartbeat": {"emit_state": heartbeat_emit_state},
        "placeholders": dict(_TELEMETRY_PLACEHOLDERS),
    }


def _resolve_memory_hook_flag(explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    raw = os.environ.get("CHANNEL_ENABLE_MEMORY_HOOK", "false")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_default_orchestrator(
    *,
    config: RuntimeConfig,
    enable_memory_hook: bool | None,
    memory_lookup: MemoryLookupFn | None,
    codex_invoke: CodexInvokeFn | None,
) -> OrchestratorPort:
    if config.orchestrator_mode == "codex":
        session_policy = CodexSessionPolicy(
            max_sessions=config.codex_session_max,
            idle_ttl_s=config.codex_session_idle_ttl_s,
        )
        return CodexOrchestrator(
            timeout_s=config.codex_timeout_s,
            notify_on_error=config.notify_on_orchestrator_error,
            invoke_fn=codex_invoke,
            session_manager=CodexSessionManager(policy=session_policy),
        )

    return DefaultOrchestrator(
        enable_memory_hook=_resolve_memory_hook_flag(enable_memory_hook),
        memory_lookup=memory_lookup,
    )


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


def _resolve_cursor_state_store(path: str) -> DurableCursorStateStore | None:
    normalized = str(path).strip()
    if not normalized:
        return None
    return DurableCursorStateStore(Path(normalized))


def _drain_diagnostics(source: str, target: Any) -> list[DiagnosticEntry]:
    drain_fn = getattr(target, "drain_diagnostics", None)
    if not callable(drain_fn):
        return []

    drained = drain_fn()
    if not isinstance(drained, list):
        return []

    diagnostics: list[DiagnosticEntry] = []
    for item in drained:
        if isinstance(item, Mapping):
            diagnostics.append((source, dict(item)))
    return diagnostics


def _normalize_chat_id_value(value: object) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(int(text))
    except ValueError:
        return text


def _map_process_once_errors(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    errors = [str(value) for value in result.get("errors", [])]
    if not errors:
        return []

    reason = str(result.get("reason", "")).strip()
    details: list[dict[str, Any]] = []
    if reason == "adapter-fetch-exception":
        for message in errors:
            details.append(
                _build_error_detail(
                    code="adapter-fetch-exception",
                    message=message,
                    retryable=True,
                    source="process_once",
                    category="error",
                    layer="service",
                    operation="fetch_updates",
                )
            )
        return details

    for message in errors:
        details.append(_map_process_once_error_message(message))
    return details


def _map_process_once_error_message(message: str) -> dict[str, Any]:
    ack_match = re.match(r"^update\s+([^:]+):\s+ack failed:\s+(.+)$", message)
    if ack_match is not None:
        update_id = str(ack_match.group(1)).strip()
        return _build_error_detail(
            code="ack-update-failed",
            message=message,
            retryable=True,
            source="process_once",
            category="error",
            layer="service",
            operation="ack_update",
            update_id=update_id,
        )

    update_match = re.match(r"^update\s+([^:]+):\s+(.+)$", message)
    if update_match is not None:
        update_id = str(update_match.group(1)).strip()
        detail_message = str(update_match.group(2)).strip()
        operation = _infer_service_operation(detail_message)
        retryable = _infer_retryable_service_error(detail_message, operation=operation)
        return _build_error_detail(
            code="update-processing-exception",
            message=message,
            retryable=retryable,
            source="process_once",
            category="error",
            layer="service",
            operation=operation,
            update_id=update_id,
        )

    return _build_error_detail(
        code="service-cycle-error",
        message=message,
        retryable=False,
        source="process_once",
        category="error",
        layer="service",
        operation="process_once",
    )


def _infer_service_operation(message: str) -> str:
    normalized = message.lower()
    if "ack failed" in normalized or "ack_update" in normalized:
        return "ack_update"
    if "send_message" in normalized or "send failed" in normalized:
        return "send_message"
    return "handle_message"


def _infer_retryable_service_error(message: str, *, operation: str) -> bool:
    if operation in {"send_message", "ack_update"}:
        return True
    normalized = message.lower()
    retryable_tokens = (
        "timeout",
        "temporar",
        "connection",
        "network",
        "unavailable",
        "too many requests",
        "rate limit",
    )
    return any(token in normalized for token in retryable_tokens)


def _map_runtime_diagnostic(*, source: str, item: Mapping[str, Any]) -> dict[str, Any] | None:
    code = str(item.get("code", "")).strip()
    message = str(item.get("message", "unknown"))
    update_id = str(item.get("update_id", ""))
    chat_id = str(item.get("chat_id", ""))
    session_id = str(item.get("session_id", ""))

    if source == "orchestrator":
        if code == "allowlist-drop":
            return _build_error_detail(
                code="allowlist-drop",
                message=message,
                retryable=False,
                source="orchestrator.diagnostics",
                category="drop",
                layer="gate",
                operation="allowlist_check",
                update_id=update_id,
                chat_id=chat_id,
            )
        return _build_error_detail(
            code=code or "orchestrator-error",
            message=message,
            retryable=bool(item.get("retryable", False)),
            source="orchestrator.diagnostics",
            category="error",
            layer="orchestrator",
            operation="handle_message",
            update_id=update_id,
            chat_id=chat_id,
            session_id=session_id,
        )

    if source == "adapter":
        if code == "stale-drop":
            return _build_error_detail(
                code="stale-drop",
                message=message,
                retryable=False,
                source="adapter.diagnostics",
                category="drop",
                layer="adapter",
                operation="stale_filter",
                update_id=update_id,
                chat_id=chat_id,
            )
        if code == "cursor-state-load-error":
            operation = "cursor_state_load"
            retryable = True
        elif code == "cursor-state-save-error":
            operation = "cursor_state_save"
            retryable = True
        else:
            operation = "fetch_updates"
            retryable = bool(item.get("retryable", False))
        return _build_error_detail(
            code=code or "adapter-diagnostic-error",
            message=message,
            retryable=retryable,
            source="adapter.diagnostics",
            category="error",
            layer="adapter",
            operation=operation,
            update_id=update_id,
            chat_id=chat_id,
            session_id=session_id,
        )

    return None


def _build_error_detail(
    *,
    code: str,
    message: str,
    retryable: bool,
    source: str,
    category: str,
    layer: str,
    operation: str,
    update_id: str = "",
    chat_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    detail = {
        "code": str(code or "").strip(),
        "message": str(message or "").strip(),
        "retryable": bool(retryable),
        "context": {
            "update_id": str(update_id or "").strip(),
            "chat_id": str(chat_id or "").strip(),
            "session_id": str(session_id or "").strip(),
            "layer": str(layer or "").strip(),
            "operation": str(operation or "").strip(),
        },
        "source": str(source or "").strip(),
        "category": str(category or "").strip(),
    }
    fingerprint = _detail_fingerprint(detail)
    detail["diagnostic_id"] = hashlib.sha256(
        "|".join(fingerprint).encode("utf-8")
    ).hexdigest()[:16]
    return detail


def _detail_fingerprint(detail: Mapping[str, Any]) -> tuple[str, ...]:
    context = detail.get("context")
    if not isinstance(context, Mapping):
        context = {}
    return (
        str(detail.get("code", "")),
        str(detail.get("message", "")),
        str(context.get("update_id", "")),
        str(context.get("chat_id", "")),
        str(context.get("session_id", "")),
        str(context.get("layer", "")),
        str(context.get("operation", "")),
        str(detail.get("category", "")),
    )


def _dedupe_error_details(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for detail in details:
        fingerprint = _detail_fingerprint(detail)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(detail)
    return unique
