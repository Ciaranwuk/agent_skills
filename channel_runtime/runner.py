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
from .context.compaction import CompactionPolicy, CompactionService
from .context.store import ContextStore


PublishSystemEventFn = Callable[..., dict[str, Any]]
MemoryLookupFn = Callable[[str], str | None]
CodexInvokeFn = Callable[[CodexInvocationRequest], str | None]
DiagnosticEntry = tuple[str, dict[str, Any]]
TelemetryDigest = dict[str, Any]

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

    def drain_context_telemetry(self) -> dict[str, Any]:
        delegate_drain = getattr(self._delegate, "drain_context_telemetry", None)
        if not callable(delegate_drain):
            return {}
        drained = delegate_drain()
        if not isinstance(drained, Mapping):
            return {}
        return dict(drained)


class DurableContextCanaryOrchestrator:
    """Routes messages between durable and baseline delegates by chat ID canary allowlist."""

    def __init__(
        self,
        *,
        durable_delegate: OrchestratorPort,
        baseline_delegate: OrchestratorPort,
        canary_chat_ids: tuple[str, ...],
        configured_mode: str,
    ) -> None:
        self._durable_delegate = durable_delegate
        self._baseline_delegate = baseline_delegate
        self._canary_chat_ids = {
            normalized
            for normalized in (_normalize_chat_id_value(value) for value in canary_chat_ids)
            if normalized is not None
        }
        self._configured_mode = str(configured_mode).strip().lower()

    def handle_message(self, inbound: InboundMessage, *, session_id: str) -> OutboundMessage | None:
        delegate = self._baseline_delegate
        chat_id = _normalize_chat_id_value(inbound.chat_id)
        if chat_id in self._canary_chat_ids:
            delegate = self._durable_delegate
        return delegate.handle_message(inbound, session_id=session_id)

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        for delegate in (self._durable_delegate, self._baseline_delegate):
            delegate_drain = getattr(delegate, "drain_diagnostics", None)
            if callable(delegate_drain):
                drained = delegate_drain()
                if isinstance(drained, list):
                    diagnostics.extend(item for item in drained if isinstance(item, Mapping))
        return diagnostics

    def drain_context_telemetry(self) -> dict[str, Any]:
        durable = _drain_delegate_context_telemetry(self._durable_delegate)
        baseline = _drain_delegate_context_telemetry(self._baseline_delegate)
        return _merge_context_telemetry(
            durable,
            baseline,
            default_mode=self._configured_mode,
        )


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
        context_telemetry: Mapping[str, Any],
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
                context_telemetry=context_telemetry,
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
        context_telemetry = _drain_context_telemetry(gated_orchestrator, context_mode=config.context_mode)
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
            context_telemetry=context_telemetry,
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
        failed_result["runtime_digest"] = _build_runtime_digest(
            context_mode=config.context_mode,
            context_telemetry=context_telemetry,
        )
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
            context_mode=config.context_mode,
            context_telemetry=context_telemetry,
        )
        return failed_result

    diagnostics: list[DiagnosticEntry] = []
    diagnostics.extend(_drain_diagnostics("orchestrator", gated_orchestrator))
    diagnostics.extend(_drain_diagnostics("adapter", resolved_adapter))
    context_telemetry = _drain_context_telemetry(gated_orchestrator, context_mode=config.context_mode)

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
            context_telemetry=context_telemetry,
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
            context_telemetry=context_telemetry,
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
    result["runtime_digest"] = _build_runtime_digest(
        context_mode=config.context_mode,
        context_telemetry=context_telemetry,
    )
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
        context_mode=config.context_mode,
        context_telemetry=context_telemetry,
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
    context_telemetry: Mapping[str, Any],
) -> TelemetryDigest:
    counters = context_telemetry.get("counters")
    gauges = context_telemetry.get("gauges")
    if not isinstance(counters, Mapping):
        counters = {}
    if not isinstance(gauges, Mapping):
        gauges = {}
    return {
        "fetch_total": int(fetch_total),
        "send_total": int(send_total),
        "drop_total": int(drop_total),
        "cycle_total_ms": int(cycle_total_ms),
        "context_mode": str(context_telemetry.get("mode", "")).strip().lower(),
        "context_compaction_attempted_total": int(counters.get("compaction_attempted_total", 0)),
        "context_compaction_succeeded_total": int(counters.get("compaction_succeeded_total", 0)),
        "context_compaction_failed_total": int(counters.get("compaction_failed_total", 0)),
        "context_compaction_fallback_used_total": int(counters.get("compaction_fallback_used_total", 0)),
        "context_compaction_reason_threshold_total": int(
            _coerce_mapping_value(counters.get("compaction_reasons"), "threshold_total")
        ),
        "context_compaction_reason_overflow_total": int(
            _coerce_mapping_value(counters.get("compaction_reasons"), "overflow_total")
        ),
        "context_compaction_reason_manual_total": int(
            _coerce_mapping_value(counters.get("compaction_reasons"), "manual_total")
        ),
        "context_tokens_estimated_total": int(counters.get("tokens_estimated_total", 0)),
        "context_tokens_build_failures_total": int(counters.get("build_failures_total", 0)),
        "context_current_tokens_estimate": _coerce_optional_int(gauges.get("current_tokens_estimate")),
        "context_summary_tokens_estimate": _coerce_optional_int(gauges.get("summary_tokens_estimate")),
        "context_recent_tokens_estimate": _coerce_optional_int(gauges.get("recent_tokens_estimate")),
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
    context_mode: str,
    context_telemetry: Mapping[str, Any],
) -> dict[str, Any]:
    counters = context_telemetry.get("counters")
    gauges = context_telemetry.get("gauges")
    if not isinstance(counters, Mapping):
        counters = {}
    if not isinstance(gauges, Mapping):
        gauges = {}
    reason_counters = counters.get("compaction_reasons")
    if not isinstance(reason_counters, Mapping):
        reason_counters = {}
    return {
        "contract": _TELEMETRY_CONTRACT,
        "version": _TELEMETRY_VERSION,
        "context": {
            "mode": str(context_mode).strip().lower(),
            "compaction": {
                "attempted_total": int(counters.get("compaction_attempted_total", 0)),
                "succeeded_total": int(counters.get("compaction_succeeded_total", 0)),
                "failed_total": int(counters.get("compaction_failed_total", 0)),
                "fallback_used_total": int(counters.get("compaction_fallback_used_total", 0)),
                "reasons": {
                    "threshold_total": int(reason_counters.get("threshold_total", 0)),
                    "overflow_total": int(reason_counters.get("overflow_total", 0)),
                    "manual_total": int(reason_counters.get("manual_total", 0)),
                },
            },
            "tokens": {
                "estimated_total": int(counters.get("tokens_estimated_total", 0)),
                "build_failures_total": int(counters.get("build_failures_total", 0)),
                "current_estimate": _coerce_optional_int(gauges.get("current_tokens_estimate")),
                "summary_estimate": _coerce_optional_int(gauges.get("summary_tokens_estimate")),
                "recent_estimate": _coerce_optional_int(gauges.get("recent_tokens_estimate")),
            },
        },
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


def _build_runtime_digest(
    *,
    context_mode: str,
    context_telemetry: Mapping[str, Any],
) -> dict[str, Any]:
    counters = context_telemetry.get("counters")
    gauges = context_telemetry.get("gauges")
    if not isinstance(counters, Mapping):
        counters = {}
    if not isinstance(gauges, Mapping):
        gauges = {}
    return {
        "context_mode": str(context_mode).strip().lower(),
        "context_compaction": {
            "attempted_total": int(counters.get("compaction_attempted_total", 0)),
            "succeeded_total": int(counters.get("compaction_succeeded_total", 0)),
            "failed_total": int(counters.get("compaction_failed_total", 0)),
            "fallback_used_total": int(counters.get("compaction_fallback_used_total", 0)),
        },
        "context_tokens": {
            "estimated_total": int(counters.get("tokens_estimated_total", 0)),
            "build_failures_total": int(counters.get("build_failures_total", 0)),
            "current_estimate": _coerce_optional_int(gauges.get("current_tokens_estimate")),
            "summary_estimate": _coerce_optional_int(gauges.get("summary_tokens_estimate")),
            "recent_estimate": _coerce_optional_int(gauges.get("recent_tokens_estimate")),
        },
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
        if config.context_mode == "durable" and config.context_canary_chat_ids:
            return DurableContextCanaryOrchestrator(
                durable_delegate=_build_codex_orchestrator(
                    config=config,
                    codex_invoke=codex_invoke,
                    context_mode="durable",
                ),
                baseline_delegate=_build_codex_orchestrator(
                    config=config,
                    codex_invoke=codex_invoke,
                    context_mode="legacy",
                ),
                canary_chat_ids=config.context_canary_chat_ids,
                configured_mode=config.context_mode,
            )
        return _build_codex_orchestrator(
            config=config,
            codex_invoke=codex_invoke,
            context_mode=config.context_mode,
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


def _build_codex_orchestrator(
    *,
    config: RuntimeConfig,
    codex_invoke: CodexInvokeFn | None,
    context_mode: str,
) -> CodexOrchestrator:
    session_policy = CodexSessionPolicy(
        max_sessions=config.codex_session_max,
        idle_ttl_s=config.codex_session_idle_ttl_s,
    )
    resolved_mode = str(context_mode).strip().lower()
    context_store = None
    compaction_service = None
    compaction_policy = None
    if resolved_mode == "durable":
        context_store = ContextStore(strict_io=config.context_strict_io)
        compaction_service = CompactionService(store=context_store)
        compaction_policy = CompactionPolicy(
            context_window_tokens=config.context_window_tokens,
            reserve_tokens=config.context_reserve_tokens,
            keep_recent_tokens=config.context_keep_recent_tokens,
            min_compaction_gain_tokens=0,
            cooldown_window_s=0.0,
        )
    return CodexOrchestrator(
        timeout_s=config.codex_timeout_s,
        notify_on_error=config.notify_on_orchestrator_error,
        invoke_fn=codex_invoke,
        session_manager=CodexSessionManager(policy=session_policy),
        context_mode=resolved_mode,
        context_store=context_store,
        compaction_service=compaction_service,
        compaction_policy=compaction_policy,
        enable_context_operator_controls=config.context_manual_compact,
    )


def _drain_delegate_context_telemetry(target: Any) -> dict[str, Any]:
    drain_fn = getattr(target, "drain_context_telemetry", None)
    if not callable(drain_fn):
        return {}
    drained = drain_fn()
    if not isinstance(drained, Mapping):
        return {}
    return dict(drained)


def _merge_context_telemetry(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    default_mode: str,
) -> dict[str, Any]:
    primary_counters = primary.get("counters")
    secondary_counters = secondary.get("counters")
    primary_gauges = primary.get("gauges")
    secondary_gauges = secondary.get("gauges")
    if not isinstance(primary_counters, Mapping):
        primary_counters = {}
    if not isinstance(secondary_counters, Mapping):
        secondary_counters = {}
    if not isinstance(primary_gauges, Mapping):
        primary_gauges = {}
    if not isinstance(secondary_gauges, Mapping):
        secondary_gauges = {}
    primary_reasons = primary_counters.get("compaction_reasons")
    secondary_reasons = secondary_counters.get("compaction_reasons")
    if not isinstance(primary_reasons, Mapping):
        primary_reasons = {}
    if not isinstance(secondary_reasons, Mapping):
        secondary_reasons = {}
    return {
        "mode": str(default_mode).strip().lower(),
        "counters": {
            "tokens_estimated_total": int(primary_counters.get("tokens_estimated_total", 0))
            + int(secondary_counters.get("tokens_estimated_total", 0)),
            "compaction_attempted_total": int(primary_counters.get("compaction_attempted_total", 0))
            + int(secondary_counters.get("compaction_attempted_total", 0)),
            "compaction_succeeded_total": int(primary_counters.get("compaction_succeeded_total", 0))
            + int(secondary_counters.get("compaction_succeeded_total", 0)),
            "compaction_failed_total": int(primary_counters.get("compaction_failed_total", 0))
            + int(secondary_counters.get("compaction_failed_total", 0)),
            "compaction_fallback_used_total": int(primary_counters.get("compaction_fallback_used_total", 0))
            + int(secondary_counters.get("compaction_fallback_used_total", 0)),
            "compaction_reasons": {
                "threshold_total": int(primary_reasons.get("threshold_total", 0))
                + int(secondary_reasons.get("threshold_total", 0)),
                "overflow_total": int(primary_reasons.get("overflow_total", 0))
                + int(secondary_reasons.get("overflow_total", 0)),
                "manual_total": int(primary_reasons.get("manual_total", 0))
                + int(secondary_reasons.get("manual_total", 0)),
            },
            "build_failures_total": int(primary_counters.get("build_failures_total", 0))
            + int(secondary_counters.get("build_failures_total", 0)),
        },
        "gauges": {
            "current_tokens_estimate": _first_non_none(
                primary_gauges.get("current_tokens_estimate"),
                secondary_gauges.get("current_tokens_estimate"),
            ),
            "summary_tokens_estimate": _first_non_none(
                primary_gauges.get("summary_tokens_estimate"),
                secondary_gauges.get("summary_tokens_estimate"),
            ),
            "recent_tokens_estimate": _first_non_none(
                primary_gauges.get("recent_tokens_estimate"),
                secondary_gauges.get("recent_tokens_estimate"),
            ),
        },
    }


def _first_non_none(first: object, second: object) -> object:
    if first is not None:
        return first
    return second


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
        is_context_code = _is_context_diagnostic_code(code)
        layer = str(item.get("layer", "")).strip()
        if not layer:
            layer = "context" if is_context_code else "orchestrator"
        operation = str(item.get("operation", "")).strip()
        if not operation:
            operation = _infer_context_diagnostic_operation(code) if is_context_code else "handle_message"
        return _build_error_detail(
            code=code or "orchestrator-error",
            message=message,
            retryable=bool(item.get("retryable", False)),
            source="orchestrator.diagnostics",
            category="error",
            layer=layer,
            operation=operation,
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


def _drain_context_telemetry(target: Any, *, context_mode: str) -> dict[str, Any]:
    drain_fn = getattr(target, "drain_context_telemetry", None)
    if not callable(drain_fn):
        return _default_context_telemetry(context_mode=context_mode)
    drained = drain_fn()
    if not isinstance(drained, Mapping):
        return _default_context_telemetry(context_mode=context_mode)

    merged = _default_context_telemetry(context_mode=context_mode)
    merged.update(dict(drained))
    return merged


def _default_context_telemetry(*, context_mode: str) -> dict[str, Any]:
    return {
        "mode": str(context_mode).strip().lower(),
        "counters": {
            "tokens_estimated_total": 0,
            "compaction_attempted_total": 0,
            "compaction_succeeded_total": 0,
            "compaction_failed_total": 0,
            "compaction_fallback_used_total": 0,
            "compaction_reasons": {
                "threshold_total": 0,
                "overflow_total": 0,
                "manual_total": 0,
            },
            "build_failures_total": 0,
        },
        "gauges": {
            "current_tokens_estimate": None,
            "summary_tokens_estimate": None,
            "recent_tokens_estimate": None,
        },
    }


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_mapping_value(value: Any, key: str) -> int:
    if not isinstance(value, Mapping):
        return 0
    try:
        return int(value.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _is_context_diagnostic_code(code: str) -> bool:
    normalized = str(code or "").strip().lower()
    return normalized.startswith("context-") or normalized == "context-build-failed"


def _infer_context_diagnostic_operation(code: str) -> str:
    normalized = str(code or "").strip().lower()
    if normalized in {"context-store-load-error", "context-store-malformed-line", "context-store-invalid-record"}:
        return "store_load"
    if normalized in {"context-store-save-error"}:
        return "store_save"
    if normalized in {"context-assembler-error", "context-build-failed"}:
        return "assemble"
    if normalized in {"context-estimator-error"}:
        return "estimate"
    return "compact"
