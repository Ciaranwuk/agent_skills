from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .contracts import HeartbeatResponder
from .runner import ConfigLike, run_heartbeat_once
from .wake import WakeQueue, WakeReason


NowMs = Callable[[], int]
SleepMs = Callable[[int], None]
RunResultHook = Callable[[Any, str], None]
SystemEventProvider = Callable[[], Sequence[str]]


@dataclass(frozen=True)
class SchedulerStatus:
    enabled: bool
    running: bool
    in_flight: bool
    next_due_ms: int | None
    pending_wake_reason: WakeReason | None
    last_run_reason: str | None
    thread_alive: bool
    health: str
    health_reason: str | None
    run_attempts: int
    run_failures: int
    consecutive_failures: int
    callback_failures: int
    last_run_started_ms: int | None
    last_run_finished_ms: int | None
    last_run_status: str | None
    last_run_result_reason: str | None
    last_run_error: str | None


class SchedulerHandle(Protocol):
    def wake(self, reason: WakeReason = "manual") -> dict[str, Any]:
        ...

    def get_status(self) -> SchedulerStatus:
        ...

    def stop(self) -> None:
        ...

    def set_enabled(self, enabled: bool) -> bool:
        ...


def start_scheduler(
    *,
    config: ConfigLike,
    responder: HeartbeatResponder,
    now_ms: NowMs | None = None,
    sleep_ms: SleepMs | None = None,
    on_run_result: RunResultHook | None = None,
    system_event_provider: SystemEventProvider | None = None,
) -> SchedulerHandle:
    return _SchedulerHandle(
        config=config,
        responder=responder,
        now_ms=now_ms or _default_now_ms,
        sleep_ms=sleep_ms or _default_sleep_ms,
        on_run_result=on_run_result,
        system_event_provider=system_event_provider,
    )


class _SchedulerHandle:
    def __init__(
        self,
        *,
        config: ConfigLike,
        responder: HeartbeatResponder,
        now_ms: NowMs,
        sleep_ms: SleepMs,
        on_run_result: RunResultHook | None,
        system_event_provider: SystemEventProvider | None,
    ) -> None:
        self._config = config
        self._responder = responder
        self._now_ms = now_ms
        self._sleep_ms = sleep_ms
        self._uses_default_sleep = sleep_ms is _default_sleep_ms
        self._on_run_result = on_run_result
        self._system_event_provider = system_event_provider
        self._wake_queue = WakeQueue()
        self._lock = threading.Lock()
        self._wake_signal = threading.Event()

        self._interval_ms = max(1, int(_config_value(config, "interval_ms", 60000)))
        self._enabled = bool(_config_value(config, "enabled", True))
        initial_now = self._now_ms()
        self._next_due_ms: int | None = (
            initial_now + self._interval_ms if self._enabled else None
        )
        self._running = True
        self._stopped = False
        self._in_flight = False
        self._last_run_reason: str | None = None
        self._run_attempts = 0
        self._run_failures = 0
        self._consecutive_failures = 0
        self._callback_failures = 0
        self._last_run_started_ms: int | None = None
        self._last_run_finished_ms: int | None = None
        self._last_run_status: str | None = None
        self._last_run_result_reason: str | None = None
        self._last_run_error: str | None = None
        self._thread = threading.Thread(target=self._loop, name="heartbeat-scheduler", daemon=True)
        self._thread.start()

    def wake(self, reason: WakeReason = "manual") -> dict[str, Any]:
        with self._lock:
            if self._stopped:
                return {
                    "status": "ignored",
                    "accepted": False,
                    "reason": reason,
                    "queue_size": 0,
                    "replaced_reason": None,
                }
            decision = self._wake_queue.request_wake(reason, now_ms=self._now_ms())
        self._wake_signal.set()
        return {
            "status": "accepted" if bool(getattr(decision, "accepted", False)) else "ignored",
            **asdict(decision),
        }

    def get_status(self) -> SchedulerStatus:
        with self._lock:
            thread_alive = self._thread.is_alive()
            if self._running and not self._stopped and not thread_alive:
                self._running = False
            pending = self._wake_queue.peek()
            pending_reason = None if pending is None else pending.reason
            health, health_reason = self._compute_health(thread_alive=thread_alive)
            return SchedulerStatus(
                enabled=self._enabled,
                running=self._running,
                in_flight=self._in_flight,
                next_due_ms=self._next_due_ms,
                pending_wake_reason=pending_reason,
                last_run_reason=self._last_run_reason,
                thread_alive=thread_alive,
                health=health,
                health_reason=health_reason,
                run_attempts=self._run_attempts,
                run_failures=self._run_failures,
                consecutive_failures=self._consecutive_failures,
                callback_failures=self._callback_failures,
                last_run_started_ms=self._last_run_started_ms,
                last_run_finished_ms=self._last_run_finished_ms,
                last_run_status=self._last_run_status,
                last_run_result_reason=self._last_run_result_reason,
                last_run_error=self._last_run_error,
            )

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._running = False
        self._wake_signal.set()
        self._thread.join()
        with self._lock:
            self._in_flight = False
            self._wake_queue.clear()

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            previous = self._enabled
            self._enabled = bool(enabled)
            now = self._now_ms()
            if self._enabled:
                self._next_due_ms = now + self._interval_ms
            else:
                self._next_due_ms = None
                self._wake_queue.clear()
        self._wake_signal.set()
        return previous

    def _loop(self) -> None:
        while True:
            if self._should_stop():
                return

            now = self._now_ms()
            should_run_reason: str | None = None
            sleep_for_ms = 25

            with self._lock:
                if self._stopped:
                    return
                self._enqueue_interval_wake(now)
                wake = self._wake_queue.pop_next()
                if wake is not None:
                    self._in_flight = True
                    should_run_reason = str(wake.reason)
                elif self._next_due_ms is not None:
                    sleep_for_ms = max(1, self._next_due_ms - now)

            if should_run_reason is not None:
                raw_result: Any | None = None
                run_started_ms = self._now_ms()
                try:
                    with self._lock:
                        self._run_attempts += 1
                        self._last_run_started_ms = run_started_ms
                        self._last_run_reason = should_run_reason
                    system_events: Sequence[str] = ()
                    if self._system_event_provider is not None:
                        try:
                            system_events = tuple(self._system_event_provider())
                        except Exception:
                            system_events = ()
                    raw_result = run_heartbeat_once(
                        config=self._config,
                        responder=self._responder,
                        reason=should_run_reason,
                        system_events=system_events,
                    )
                    status = _result_value(raw_result, "status", "ran")
                    result_reason = _result_value(raw_result, "reason", "delivered")
                    result_error = _result_value(raw_result, "error", "")
                    with self._lock:
                        self._consecutive_failures = 0
                        self._last_run_status = status
                        self._last_run_result_reason = result_reason
                        self._last_run_error = result_error or None
                    if self._on_run_result is not None:
                        try:
                            self._on_run_result(raw_result, should_run_reason)
                        except Exception:
                            with self._lock:
                                self._callback_failures += 1
                            # Keep scheduler behavior deterministic even if callback fails.
                            pass
                except Exception as exc:
                    # Keep loop alive even if an unexpected exception escapes run_once.
                    with self._lock:
                        self._run_failures += 1
                        self._consecutive_failures += 1
                        self._last_run_status = "failed"
                        self._last_run_result_reason = "scheduler-exception"
                        self._last_run_error = str(exc) or exc.__class__.__name__
                finally:
                    with self._lock:
                        self._in_flight = False
                        self._last_run_finished_ms = self._now_ms()
                continue

            self._wait_or_sleep(sleep_for_ms)

    def _enqueue_interval_wake(self, now_ms_value: int) -> None:
        if not self._enabled or self._next_due_ms is None:
            return
        if now_ms_value < self._next_due_ms:
            return
        self._wake_queue.request_wake("interval", now_ms=now_ms_value)
        while self._next_due_ms is not None and self._next_due_ms <= now_ms_value:
            self._next_due_ms += self._interval_ms

    def _wait_or_sleep(self, sleep_for_ms: int) -> None:
        timeout_ms = max(1, sleep_for_ms)
        if self._wake_signal.wait(timeout=timeout_ms / 1000.0):
            self._wake_signal.clear()
            return
        if not self._uses_default_sleep:
            self._sleep_ms(timeout_ms)

    def _should_stop(self) -> bool:
        with self._lock:
            return self._stopped

    def _compute_health(self, *, thread_alive: bool) -> tuple[str, str | None]:
        if self._stopped:
            return ("stopped", None)
        if not thread_alive:
            return ("degraded", "scheduler-thread-terminated")
        if self._in_flight:
            return ("busy", None)
        if self._consecutive_failures > 0:
            return ("degraded", "consecutive-run-failures")
        return ("ok", None)


def _config_value(config: ConfigLike, key: str, default: object) -> object:
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def _default_sleep_ms(duration_ms: int) -> None:
    time.sleep(max(0, duration_ms) / 1000.0)


def _result_value(raw_result: Any, key: str, default: str) -> str:
    if not isinstance(raw_result, Mapping):
        return default
    value = raw_result.get(key, default)
    return str(value)
