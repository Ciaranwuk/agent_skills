from __future__ import annotations

import inspect
import json
import threading
import time
from dataclasses import dataclass
from collections import deque
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Deque, Mapping, Sequence, cast

from .adapters.null_responder import NullResponder
from .config import HeartbeatConfig
from .contracts import HeartbeatResponder
from .events import HeartbeatEventService
from .events import EventIngestResult
from .runner import ConfigLike
from .scheduler import SchedulerHandle, SchedulerStatus, start_scheduler
from .store import (
    EventCounters,
    HeartbeatStateStore,
    InMemoryHeartbeatStateStore,
    JsonFileHeartbeatStateStore,
    LastEventRecord,
)
from .system_events import SessionSystemEventBus, SystemEvent
from .wake import WakeReason

_RUNNER_MODULE = "heartbeat_system.runner"
_DEFAULT_DEDUPE_WINDOW_MS = 60_000
_SYSTEM_EVENT_DRAIN_LIMIT = 20
_INGEST_DECISION_HISTORY_LIMIT = 20
_OPERATOR_CONTRACT = "heartbeat.operator"
_OPERATOR_CONTRACT_VERSION = "1.0"


@dataclass
class _RuntimeContext:
    scheduler_handle: SchedulerHandle | None
    state_store: HeartbeatStateStore
    event_service: HeartbeatEventService
    system_event_bus: SessionSystemEventBus
    ingest_diagnostics: "_IngestDecisionDiagnostics"


class _IngestDecisionDiagnostics:
    def __init__(self, *, max_entries: int = _INGEST_DECISION_HISTORY_LIMIT) -> None:
        self._max_entries = max(1, int(max_entries))
        self._entries: Deque[dict[str, Any]] = deque(maxlen=self._max_entries)
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {
            "total": 0,
            "manual": 0,
            "scheduler": 0,
            "delivered": 0,
            "suppressed": 0,
        }

    def record(
        self,
        *,
        source: str,
        run_result: Mapping[str, Any],
        ingest_result: EventIngestResult,
    ) -> None:
        entry = {
            "source": source,
            "event_id": ingest_result.event.event_id,
            "ts_ms": ingest_result.event.ts_ms,
            "status": str(run_result.get("status", "")),
            "reason": str(run_result.get("reason", "")),
            "run_reason": str(run_result.get("run_reason", "")),
            "should_deliver": bool(ingest_result.should_deliver),
            "dedupe_suppressed": bool(ingest_result.dedupe_suppressed),
            "dedupe_key": ingest_result.dedupe_key,
        }
        with self._lock:
            self._entries.append(entry)
            self._counters["total"] += 1
            self._counters[source] = self._counters.get(source, 0) + 1
            if ingest_result.should_deliver:
                self._counters["delivered"] += 1
            if ingest_result.dedupe_suppressed:
                self._counters["suppressed"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "history_limit": self._max_entries,
                "recent": list(self._entries),
                "counters": dict(self._counters),
            }


@dataclass
class _RuntimeManager:
    context: _RuntimeContext | None = None

    def init(self) -> _RuntimeContext:
        if self.context is None:
            self.context = _build_runtime_context()
        return self.context

    def get(self) -> _RuntimeContext:
        return self.init()

    def reset(self) -> _RuntimeContext:
        self.context = _build_runtime_context()
        return self.context

    def start(self, scheduler_handle: SchedulerHandle) -> None:
        self.get().scheduler_handle = scheduler_handle

    def stop(self) -> SchedulerHandle | None:
        runtime = self.get()
        handle = runtime.scheduler_handle
        runtime.scheduler_handle = None
        return handle


class HeartbeatUnavailableError(RuntimeError):
    """Raised when a requested heartbeat runtime function is unavailable."""


_runtime_lock = threading.Lock()


def _build_runtime_context() -> _RuntimeContext:
    store = InMemoryHeartbeatStateStore()
    return _RuntimeContext(
        scheduler_handle=None,
        state_store=store,
        event_service=HeartbeatEventService(
            store=store,
            dedupe_window_ms=_DEFAULT_DEDUPE_WINDOW_MS,
        ),
        system_event_bus=SessionSystemEventBus(),
        ingest_diagnostics=_IngestDecisionDiagnostics(),
    )


_runtime_manager = _RuntimeManager()
_runtime_manager.init()


def _resolve_runner_function(*names: str, strict: bool) -> Callable[..., Any] | None:
    try:
        module = import_module(_RUNNER_MODULE)
    except Exception as exc:
        if strict:
            joined = ", ".join(names)
            raise HeartbeatUnavailableError(
                f"heartbeat runner is not available yet; expected one of: {joined}"
            ) from exc
        return None

    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn

    if strict:
        joined = ", ".join(names)
        raise HeartbeatUnavailableError(
            f"heartbeat runner does not expose required function(s): {joined}"
        )
    return None


def _invoke_runner(fn: Callable[..., Any], **kwargs: Any) -> Any:
    signature = inspect.signature(fn)
    params = signature.parameters
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return fn(**kwargs)
    accepted = {key: value for key, value in kwargs.items() if key in params}
    return fn(**accepted)


def run_once(
    *,
    responder: HeartbeatResponder | None = None,
    config: HeartbeatConfig | None = None,
    reason: str = "manual",
) -> dict[str, Any]:
    cfg = config or HeartbeatConfig()
    effective_responder = responder or NullResponder(ack_token=cfg.ack_token)
    fn = _resolve_runner_function("run_heartbeat_once", "run_once", strict=True)
    runtime = _runtime_manager.get()
    session_key = _config_session_key(cfg)
    system_events = _drain_formatted_system_events(runtime, session_key=session_key)
    try:
        raw_result = _invoke_runner(
            fn,
            responder=effective_responder,
            config=cfg,
            reason=reason,
            system_events=system_events,
        )
    except ValueError as exc:
        raw_result = {
            "status": "failed",
            "reason": "invalid-config",
            "run_reason": reason,
            "output_text": "",
            "error": str(exc),
            "error_code": "invalid-config",
            "error_reason": "invalid-config",
        }

    result_payload = _normalize_run_result(raw_result, run_reason=reason)
    ingest_result = runtime.event_service.ingest_run_result(
        {
            "status": str(result_payload.get("status", "")),
            "reason": str(result_payload.get("reason", "")),
            "run_reason": str(result_payload.get("run_reason", reason)),
            "output_text": str(result_payload.get("output_text", "")),
            "error": str(result_payload.get("error", "")),
        }
    )
    runtime.ingest_diagnostics.record(
        source="manual",
        run_result=result_payload,
        ingest_result=ingest_result,
    )

    payload = dict(result_payload)
    payload["event"] = _event_record_as_dict(ingest_result.event)
    payload["counters"] = _counters_as_dict(ingest_result.counters)
    if ingest_result.dedupe_key is not None:
        payload["dedupe_suppressed"] = ingest_result.dedupe_suppressed
        payload["dedupe_key"] = ingest_result.dedupe_key
        payload["should_deliver"] = ingest_result.should_deliver
    return _with_operator_contract(payload)


def start_heartbeat_runner(
    *,
    config: ConfigLike,
    responder: HeartbeatResponder,
    now_ms: Callable[[], int] | None = None,
    sleep_ms: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    with _runtime_lock:
        runtime = _runtime_manager.get()
        existing = runtime.scheduler_handle
        if existing is not None and existing.get_status().running:
            return {"status": "already-running"}

        if existing is not None:
            stale = _runtime_manager.stop()
            if stale is not None:
                stale.stop()

        runtime.state_store = _select_store_backend(
            config=config,
            current_store=runtime.state_store,
        )
        current_now_ms = (
            int(now_ms())
            if now_ms is not None
            else int(time.time() * 1000)
        )
        runtime.state_store.prune_dedupe(current_now_ms)
        runtime.event_service = HeartbeatEventService(
            store=runtime.state_store,
            dedupe_window_ms=_DEFAULT_DEDUPE_WINDOW_MS,
        )
        enabled = _config_enabled(config)
        runtime.state_store.set_enabled(enabled)
        session_key = _config_session_key(config)

        def _ingest_scheduler_run(raw_result: Any, run_reason: str) -> None:
            result_payload = _normalize_run_result(raw_result, run_reason=run_reason)
            ingest_result = runtime.event_service.ingest_run_result(
                {
                    "status": str(result_payload.get("status", "")),
                    "reason": str(result_payload.get("reason", "")),
                    "run_reason": str(result_payload.get("run_reason", run_reason)),
                    "output_text": str(result_payload.get("output_text", "")),
                    "error": str(result_payload.get("error", "")),
                }
            )
            runtime.ingest_diagnostics.record(
                source="scheduler",
                run_result=result_payload,
                ingest_result=ingest_result,
            )

        def _scheduler_system_event_provider() -> Sequence[str]:
            return _drain_formatted_system_events(runtime, session_key=session_key)

        scheduler_handle = start_scheduler(
            config=config,
            responder=responder,
            now_ms=now_ms,
            sleep_ms=sleep_ms,
            on_run_result=_ingest_scheduler_run,
            system_event_provider=_scheduler_system_event_provider,
        )
        _runtime_manager.start(scheduler_handle)
        return {"status": "started"}


def stop_heartbeat_runner() -> dict[str, Any]:
    with _runtime_lock:
        handle = _runtime_manager.stop()
        if handle is None:
            return {"status": "not-running"}

        handle.stop()
        return {"status": "stopped"}


def request_heartbeat_now(reason: WakeReason = "manual") -> dict[str, Any]:
    return wake(reason=reason)


def get_status() -> dict[str, Any]:
    runtime = _runtime_manager.get()
    store_snapshot = runtime.state_store.snapshot()
    store_load_warning = _store_load_warning(runtime.state_store)
    with _runtime_lock:
        handle = runtime.scheduler_handle

    if handle is None:
        payload = {
            "status": "idle",
            "enabled": store_snapshot.enabled,
            "running": False,
            "in_flight": False,
            "next_due_ms": None,
            "pending_wake_reason": None,
            "last_run_reason": None,
            "scheduler_diagnostics": None,
            "counters": _counters_as_dict(store_snapshot.counters),
            "last_event_present": store_snapshot.last_event is not None,
            "ingest_diagnostics": runtime.ingest_diagnostics.snapshot(),
            "store_load_warning": None,
        }
        if store_load_warning is not None:
            payload["store_load_warning"] = store_load_warning
        return _with_operator_contract(payload)

    scheduler_status = handle.get_status()
    base = _scheduler_status_as_dict(scheduler_status)
    base["enabled"] = store_snapshot.enabled
    base["counters"] = _counters_as_dict(store_snapshot.counters)
    base["last_event_present"] = store_snapshot.last_event is not None
    base["status"] = "ok" if scheduler_status.running else "degraded"
    base["ingest_diagnostics"] = runtime.ingest_diagnostics.snapshot()
    base["store_load_warning"] = store_load_warning
    return _with_operator_contract(base)


def get_last_event() -> dict[str, Any]:
    event = _runtime_manager.get().event_service.get_last_event()
    if event is None:
        return _with_operator_contract({"status": "empty", "event": None})
    return _with_operator_contract({"status": "ok", "event": _event_record_as_dict(event)})


def wake(reason: str = "manual") -> dict[str, Any]:
    runtime = _runtime_manager.get()
    with _runtime_lock:
        handle = runtime.scheduler_handle
    if handle is not None and handle.get_status().running:
        try:
            return _normalize_wake_payload(handle.wake(reason=cast(WakeReason, reason)), wake_reason=reason)
        except ValueError as exc:
            return _with_operator_contract(
                {
                "status": "failed",
                "reason": "invalid-wake-reason",
                "wake_reason": reason,
                "error": str(exc),
                "accepted": False,
                "queue_size": 0,
                "replaced_reason": None,
                "error_code": "invalid-wake-reason",
                "error_reason": "invalid-wake-reason",
                }
            )

    return _with_operator_contract(
        {
            "status": "not-running",
            "accepted": False,
            "reason": reason,
            "wake_reason": reason,
            "queue_size": 0,
            "replaced_reason": None,
            "error": "",
            "error_code": "runtime-not-started",
            "error_reason": "runtime-not-started",
        }
    )


def enable_heartbeat() -> dict[str, Any]:
    return _set_heartbeat_enabled(True)


def disable_heartbeat() -> dict[str, Any]:
    return _set_heartbeat_enabled(False)


def publish_system_event(
    *,
    session_key: str,
    text: str,
    source: str = "system",
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = _runtime_manager.get()
    write_result = runtime.system_event_bus.publish(
        session_key,
        text,
        source=source,
        context=dict(context or {}),
    )
    return {
        "status": "accepted" if write_result.accepted else "ignored",
        "accepted": write_result.accepted,
        "session_key": session_key,
        "queue_size": write_result.queue_size,
        "dropped": write_result.dropped,
        "deduped": write_result.deduped,
    }


def _set_heartbeat_enabled(enabled: bool) -> dict[str, Any]:
    runtime = _runtime_manager.get()
    previous_enabled = runtime.state_store.set_enabled(enabled)

    applied_to_scheduler = False
    with _runtime_lock:
        handle = runtime.scheduler_handle
    if handle is not None:
        set_enabled_fn = getattr(handle, "set_enabled", None)
        if callable(set_enabled_fn):
            set_enabled_fn(enabled)
            applied_to_scheduler = True

    return _with_operator_contract(
        {
        "status": "ok",
        "enabled": enabled,
        "previous_enabled": previous_enabled,
        "applied_to_scheduler": applied_to_scheduler,
        }
    )


def reset_runtime_for_tests() -> None:
    stop_heartbeat_runner()
    with _runtime_lock:
        _runtime_manager.reset()


def _normalize_run_result(raw_result: Any, *, run_reason: str) -> dict[str, Any]:
    if isinstance(raw_result, dict):
        payload = dict(raw_result)
    else:
        payload = {
            "status": "ran",
            "reason": "delivered",
            "run_reason": run_reason,
            "output_text": str(raw_result),
            "error": "",
        }

    payload.setdefault("status", "ran")
    payload.setdefault("reason", "delivered")
    payload.setdefault("output_text", "")
    payload.setdefault("error", "")
    payload.setdefault("run_reason", run_reason)
    payload.setdefault("error_code", None)
    payload.setdefault("error_reason", None)
    return payload


def _with_operator_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    wrapped = dict(payload)
    wrapped["contract"] = _OPERATOR_CONTRACT
    wrapped["contract_version"] = _OPERATOR_CONTRACT_VERSION
    wrapped["contract_metadata"] = {
        "name": _OPERATOR_CONTRACT,
        "version": _OPERATOR_CONTRACT_VERSION,
    }
    wrapped.setdefault("error_code", None)
    wrapped.setdefault("error_reason", None)
    wrapped.setdefault("ok", _infer_ok(wrapped))
    return wrapped


def _infer_ok(payload: Mapping[str, Any]) -> bool:
    error_code = payload.get("error_code")
    if error_code not in (None, ""):
        return False
    status = str(payload.get("status", "")).strip().lower()
    if status in {"failed", "error", "not-running", "degraded"}:
        return False
    return True


def _normalize_wake_payload(payload: Mapping[str, Any], *, wake_reason: str) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("status", "accepted")
    normalized.setdefault("accepted", True)
    normalized.setdefault("reason", wake_reason)
    normalized.setdefault("wake_reason", wake_reason)
    normalized.setdefault("queue_size", 0)
    normalized.setdefault("replaced_reason", None)
    normalized.setdefault("error", "")
    normalized.setdefault("error_code", None)
    normalized.setdefault("error_reason", None)
    return _with_operator_contract(normalized)


def _config_enabled(config: ConfigLike) -> bool:
    if isinstance(config, Mapping):
        return bool(config.get("enabled", True))
    return bool(getattr(config, "enabled", True))


def _config_value(config: ConfigLike, key: str, default: object) -> object:
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _config_session_key(config: ConfigLike) -> str:
    return str(_config_value(config, "session_key", "default"))


def _drain_formatted_system_events(
    runtime: _RuntimeContext,
    *,
    session_key: str,
) -> list[str]:
    drained = runtime.system_event_bus.drain(session_key)
    if len(drained) <= _SYSTEM_EVENT_DRAIN_LIMIT:
        selected_events = drained
        overflow_count = 0
    else:
        selected_count = max(0, _SYSTEM_EVENT_DRAIN_LIMIT - 1)
        selected_events = drained[:selected_count]
        overflow_count = len(drained) - selected_count
    formatted = [_format_system_event(event) for event in selected_events]
    if overflow_count > 0:
        formatted.append(_format_system_event_overflow(overflow_count))
    return formatted


def _format_system_event(event: SystemEvent) -> str:
    source = str(event.source)
    text = str(event.text)
    context = event.context
    context_payload = context if isinstance(context, Mapping) else {"value": context}
    context_json = json.dumps(context_payload, separators=(",", ":"), sort_keys=True)
    return f"source={source};text={text};context={context_json}"


def _format_system_event_overflow(dropped_count: int) -> str:
    return f"source=system;text=[overflow dropped={dropped_count}];context={{}}"


def _select_store_backend(
    *,
    config: ConfigLike,
    current_store: HeartbeatStateStore,
) -> HeartbeatStateStore:
    state_path_override = _config_state_path(config)
    if state_path_override is not None:
        state_path = Path(state_path_override)
        if (
            isinstance(current_store, JsonFileHeartbeatStateStore)
            and current_store.state_file == state_path
        ):
            return current_store
        return JsonFileHeartbeatStateStore(state_path)

    backend_raw = str(_config_value(config, "state_backend", "memory")).strip().lower()
    if backend_raw in ("", "memory", "in-memory", "in_memory"):
        if isinstance(current_store, InMemoryHeartbeatStateStore):
            return current_store
        return InMemoryHeartbeatStateStore()

    if backend_raw in ("json", "json-file", "json_file", "file"):
        configured_path = str(
            _config_value(config, "state_file", ".heartbeat/heartbeat_state.json")
        ).strip()
        state_path = Path(configured_path or ".heartbeat/heartbeat_state.json")
        if (
            isinstance(current_store, JsonFileHeartbeatStateStore)
            and current_store.state_file == state_path
        ):
            return current_store
        return JsonFileHeartbeatStateStore(state_path)

    if isinstance(current_store, InMemoryHeartbeatStateStore):
        return current_store
    return InMemoryHeartbeatStateStore()


def _config_state_path(config: ConfigLike) -> str | None:
    if not isinstance(config, Mapping):
        return None
    raw = config.get("state_path")
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    return value


def _store_load_warning(store: HeartbeatStateStore) -> str | None:
    return store.get_load_warning()


def _scheduler_status_as_dict(snapshot: SchedulerStatus) -> dict[str, Any]:
    thread_alive = bool(getattr(snapshot, "thread_alive", False))
    health = str(getattr(snapshot, "health", "ok"))
    health_reason = getattr(snapshot, "health_reason", None)
    run_attempts = int(getattr(snapshot, "run_attempts", 0))
    run_failures = int(getattr(snapshot, "run_failures", 0))
    consecutive_failures = int(getattr(snapshot, "consecutive_failures", 0))
    callback_failures = int(getattr(snapshot, "callback_failures", 0))
    last_run_started_ms = getattr(snapshot, "last_run_started_ms", None)
    last_run_finished_ms = getattr(snapshot, "last_run_finished_ms", None)
    last_run_status = getattr(snapshot, "last_run_status", None)
    last_run_result_reason = getattr(snapshot, "last_run_result_reason", None)
    last_run_error = getattr(snapshot, "last_run_error", None)
    return {
        "enabled": snapshot.enabled,
        "running": snapshot.running,
        "in_flight": snapshot.in_flight,
        "next_due_ms": snapshot.next_due_ms,
        "pending_wake_reason": snapshot.pending_wake_reason,
        "last_run_reason": snapshot.last_run_reason,
        "scheduler_diagnostics": {
            "thread_alive": thread_alive,
            "health": health,
            "health_reason": health_reason,
            "run_attempts": run_attempts,
            "run_failures": run_failures,
            "consecutive_failures": consecutive_failures,
            "callback_failures": callback_failures,
            "last_run_started_ms": last_run_started_ms,
            "last_run_finished_ms": last_run_finished_ms,
            "last_run_reason": snapshot.last_run_reason,
            "last_run_status": last_run_status,
            "last_run_result_reason": last_run_result_reason,
            "last_run_error": last_run_error,
        },
    }


def _event_record_as_dict(event: LastEventRecord) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "ts_ms": event.ts_ms,
        "status": event.status,
        "reason": event.reason,
        "run_reason": event.run_reason,
        "output_text": event.output_text,
        "error": event.error,
        "dedupe_suppressed": event.dedupe_suppressed,
    }


def _counters_as_dict(counters: EventCounters) -> dict[str, int]:
    return {
        "ran": counters.ran,
        "skipped": counters.skipped,
        "failed": counters.failed,
        "deduped": counters.deduped,
    }
