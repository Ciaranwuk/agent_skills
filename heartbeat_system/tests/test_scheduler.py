from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from heartbeat_system.scheduler import start_scheduler


@dataclass
class _StubResponder:
    def respond(self, request):  # noqa: ANN001 - protocol stub
        del request
        return {"text": "HEARTBEAT_OK"}


class _FakeClock:
    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return self._now

    def sleep_ms(self, duration_ms: int) -> None:
        with self._lock:
            self._now += max(1, duration_ms)
        time.sleep(0.001)


def _wait_until(predicate, timeout: float = 1.0) -> bool:  # noqa: ANN001 - test helper
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class TestScheduler(unittest.TestCase):
    def _config(self, heartbeat_file: str, **overrides):
        cfg = {
            "enabled": True,
            "heartbeat_file": heartbeat_file,
            "ack_token": "HEARTBEAT_OK",
            "ack_max_chars": 20,
            "interval_ms": 50,
        }
        cfg.update(overrides)
        return cfg

    def test_start_stop_lifecycle(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()
            handle = start_scheduler(
                config=self._config(heartbeat_file, interval_ms=1000),
                responder=_StubResponder(),
                now_ms=clock.now_ms,
                sleep_ms=clock.sleep_ms,
            )
            status_before = handle.get_status()
            self.assertTrue(status_before.running)

            handle.stop()

            status_after = handle.get_status()
            self.assertFalse(status_after.running)
            self.assertFalse(status_after.in_flight)

    def test_interval_due_tick_triggers_run(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()
            calls: list[str] = []

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder
                del system_events
                calls.append(reason)
                return {"status": "ran", "reason": "delivered", "run_reason": reason}

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                handle = start_scheduler(
                    config=self._config(heartbeat_file, interval_ms=25),
                    responder=_StubResponder(),
                    now_ms=clock.now_ms,
                    sleep_ms=clock.sleep_ms,
                )
                self.assertTrue(_wait_until(lambda: len(calls) >= 1))
                handle.stop()

            self.assertEqual(calls[0], "interval")

    def test_wake_while_in_flight_coalesces_no_parallel_runs(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()
            started = threading.Event()
            release = threading.Event()
            call_reasons: list[str] = []
            running_calls = 0
            max_running_calls = 0
            lock = threading.Lock()

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder
                del system_events
                nonlocal running_calls, max_running_calls
                with lock:
                    running_calls += 1
                    max_running_calls = max(max_running_calls, running_calls)
                call_reasons.append(reason)
                started.set()
                release.wait(timeout=1.0)
                with lock:
                    running_calls -= 1
                return {"status": "ran", "reason": "delivered", "run_reason": reason}

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                handle = start_scheduler(
                    config=self._config(heartbeat_file, interval_ms=10_000),
                    responder=_StubResponder(),
                    now_ms=clock.now_ms,
                    sleep_ms=clock.sleep_ms,
                )
                handle.wake("manual")
                self.assertTrue(started.wait(timeout=1.0))

                retry_result = handle.wake("retry")
                manual_result = handle.wake("manual")
                self.assertIn(retry_result["status"], {"accepted", "ignored"})
                self.assertTrue(manual_result["accepted"])
                self.assertEqual(handle.get_status().pending_wake_reason, "manual")

                release.set()
                self.assertTrue(_wait_until(lambda: len(call_reasons) >= 2))
                handle.stop()

            self.assertEqual(max_running_calls, 1)
            self.assertEqual(call_reasons[:2], ["manual", "manual"])

    def test_repeated_stop_idempotent(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()
            handle = start_scheduler(
                config=self._config(heartbeat_file, interval_ms=1000),
                responder=_StubResponder(),
                now_ms=clock.now_ms,
                sleep_ms=clock.sleep_ms,
            )

            handle.stop()
            handle.stop()

            status = handle.get_status()
            self.assertFalse(status.running)

    def test_runner_exception_does_not_kill_loop(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()
            calls: list[str] = []

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder
                del system_events
                calls.append(reason)
                if len(calls) == 1:
                    raise RuntimeError("boom")
                return {"status": "ran", "reason": "delivered", "run_reason": reason}

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                handle = start_scheduler(
                    config=self._config(heartbeat_file, interval_ms=1_000_000_000),
                    responder=_StubResponder(),
                    now_ms=lambda: 0,
                    sleep_ms=lambda _duration_ms: time.sleep(0.001),
                )

                handle.wake("manual")
                self.assertTrue(_wait_until(lambda: len(calls) >= 1))
                handle.wake("manual")
                self.assertTrue(_wait_until(lambda: len(calls) >= 2))
                status = handle.get_status()
                handle.stop()

            self.assertEqual(calls[:2], ["manual", "manual"])
            self.assertEqual(status.run_attempts, 2)
            self.assertEqual(status.run_failures, 1)
            self.assertEqual(status.consecutive_failures, 0)
            self.assertEqual(status.last_run_status, "ran")
            self.assertEqual(status.last_run_result_reason, "delivered")
            self.assertIsNone(status.last_run_error)

    def test_wake_during_idle_wait_runs_promptly(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            run_event = threading.Event()
            call_reasons: list[str] = []

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder
                del system_events
                call_reasons.append(reason)
                run_event.set()
                return {"status": "ran", "reason": "delivered", "run_reason": reason}

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                handle = start_scheduler(
                    config=self._config(heartbeat_file, interval_ms=5_000),
                    responder=_StubResponder(),
                )
                wake_start = time.monotonic()
                handle.wake("manual")
                self.assertTrue(run_event.wait(timeout=1.5))
                wake_elapsed = time.monotonic() - wake_start
                handle.stop()

            self.assertLess(wake_elapsed, 1.0)
            self.assertEqual(call_reasons[:1], ["manual"])

    def test_stop_during_idle_wait_completes_promptly(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            handle = start_scheduler(
                config=self._config(heartbeat_file, interval_ms=5_000),
                responder=_StubResponder(),
            )

            stop_start = time.monotonic()
            handle.stop()
            stop_elapsed = time.monotonic() - stop_start

            self.assertLess(stop_elapsed, 1.0)
            self.assertFalse(handle.get_status().running)

    def test_on_run_result_callback_receives_raw_result(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            run_event = threading.Event()
            callback_payloads: list[tuple[object, str]] = []

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder
                del system_events
                run_event.set()
                return {"status": "ran", "reason": "delivered", "run_reason": reason}

            def _on_run_result(raw_result: object, run_reason: str) -> None:
                callback_payloads.append((raw_result, run_reason))

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                handle = start_scheduler(
                    config=self._config(heartbeat_file, interval_ms=5_000),
                    responder=_StubResponder(),
                    on_run_result=_on_run_result,
                )
                handle.wake("manual")
                self.assertTrue(run_event.wait(timeout=1.0))
                self.assertTrue(_wait_until(lambda: len(callback_payloads) == 1))
                handle.stop()

            payload, reason = callback_payloads[0]
            self.assertEqual(reason, "manual")
            self.assertEqual(payload["status"], "ran")  # type: ignore[index]

    def test_on_run_result_callback_exception_does_not_kill_loop(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            run_reasons: list[str] = []
            callback_calls: list[str] = []

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder
                del system_events
                run_reasons.append(reason)
                return {"status": "ran", "reason": "delivered", "run_reason": reason}

            def _on_run_result(raw_result: object, run_reason: str) -> None:
                del raw_result
                callback_calls.append(run_reason)
                if len(callback_calls) == 1:
                    raise RuntimeError("callback boom")

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                handle = start_scheduler(
                    config=self._config(heartbeat_file, interval_ms=1_000_000_000),
                    responder=_StubResponder(),
                    now_ms=lambda: 0,
                    sleep_ms=lambda _duration_ms: time.sleep(0.001),
                    on_run_result=_on_run_result,
                )

                handle.wake("manual")
                self.assertTrue(_wait_until(lambda: len(run_reasons) >= 1))
                self.assertTrue(handle.get_status().running)

                handle.wake("manual")
                self.assertTrue(_wait_until(lambda: len(run_reasons) >= 2))
                self.assertTrue(_wait_until(lambda: len(callback_calls) >= 2))
                status = handle.get_status()
                handle.stop()

            self.assertEqual(run_reasons[:2], ["manual", "manual"])
            self.assertEqual(callback_calls[:2], ["manual", "manual"])
            self.assertEqual(status.callback_failures, 1)
            self.assertEqual(status.run_attempts, 2)
            self.assertEqual(status.run_failures, 0)

    def test_system_exit_in_runner_marks_scheduler_degraded(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder, reason, system_events
                raise SystemExit("thread terminated")

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                handle = start_scheduler(
                    config=self._config(heartbeat_file, interval_ms=1_000_000_000),
                    responder=_StubResponder(),
                    now_ms=lambda: 0,
                    sleep_ms=lambda _duration_ms: time.sleep(0.001),
                )

                handle.wake("manual")
                self.assertTrue(_wait_until(lambda: not handle.get_status().thread_alive))
                status = handle.get_status()
                handle.stop()

        self.assertFalse(status.running)
        self.assertFalse(status.thread_alive)
        self.assertEqual(status.health, "degraded")
        self.assertEqual(status.health_reason, "scheduler-thread-terminated")

    def test_system_event_provider_payload_is_forwarded_to_runner(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            run_event = threading.Event()
            captured_system_events: list[list[str]] = []

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder, reason
                captured_system_events.append(list(system_events or ()))
                run_event.set()
                return {"status": "ran", "reason": "delivered", "run_reason": "manual"}

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                handle = start_scheduler(
                    config=self._config(heartbeat_file, interval_ms=5_000),
                    responder=_StubResponder(),
                    system_event_provider=lambda: ["source=runner;text=one;context={}"],
                )
                handle.wake("manual")
                self.assertTrue(run_event.wait(timeout=1.0))
                handle.stop()

        self.assertEqual(captured_system_events[:1], [["source=runner;text=one;context={}"]])


if __name__ == "__main__":
    unittest.main()
