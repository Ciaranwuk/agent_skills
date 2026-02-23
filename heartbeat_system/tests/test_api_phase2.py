from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from heartbeat_system import api
from heartbeat_system.adapters.null_responder import NullResponder


class _FakeClock:
    def __init__(self, start_ms: int = 1_000) -> None:
        self._now = start_ms
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return self._now

    def sleep_ms(self, duration_ms: int) -> None:
        del duration_ms
        time.sleep(0.001)


class _BlockingSleep:
    """Keeps scheduler loop parked briefly so wake queue assertions are deterministic."""

    def __init__(self, timeout_s: float = 0.05) -> None:
        self._timeout_s = timeout_s
        self._release = threading.Event()

    def sleep_ms(self, _duration_ms: int) -> None:
        self._release.wait(timeout=self._timeout_s)

    def release(self) -> None:
        self._release.set()


class TestApiPhase2(unittest.TestCase):
    def setUp(self) -> None:
        api.reset_runtime_for_tests()

    def tearDown(self) -> None:
        api.reset_runtime_for_tests()

    def _config(self, heartbeat_file: str, **overrides: object) -> dict[str, object]:
        cfg: dict[str, object] = {
            "enabled": True,
            "heartbeat_file": heartbeat_file,
            "ack_token": "HEARTBEAT_OK",
            "ack_max_chars": 300,
            "interval_ms": 1_000_000,
        }
        cfg.update(overrides)
        return cfg

    def test_start_runner_sets_live_handle_and_status_snapshot(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()

            started = api.start_heartbeat_runner(
                config=self._config(heartbeat_file),
                responder=NullResponder(),
                now_ms=clock.now_ms,
                sleep_ms=clock.sleep_ms,
            )
            status = api.get_status()

        self.assertEqual(started, {"status": "started"})
        self.assertEqual(status["status"], "ok")
        self.assertTrue(status["running"])
        self.assertTrue(status["enabled"])
        self.assertFalse(status["in_flight"])
        self.assertIsNone(status["pending_wake_reason"])
        self.assertIsNone(status["last_run_reason"])

    def test_request_heartbeat_now_delegates_live_handle_payloads(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()
            blocker = _BlockingSleep(timeout_s=0.05)

            api.start_heartbeat_runner(
                config=self._config(heartbeat_file),
                responder=NullResponder(),
                now_ms=clock.now_ms,
                sleep_ms=blocker.sleep_ms,
            )
            time.sleep(0.005)

            accepted = api.request_heartbeat_now("manual")
            ignored = api.request_heartbeat_now("retry")
            blocker.release()

        self.assertEqual(accepted["status"], "accepted")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["reason"], "manual")
        self.assertEqual(accepted["queue_size"], 1)

        self.assertEqual(ignored["status"], "ignored")
        self.assertFalse(ignored["accepted"])
        self.assertEqual(ignored["reason"], "retry")
        self.assertEqual(ignored["queue_size"], 1)

    def test_invalid_wake_reason_in_live_mode_returns_failed_payload(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()

            api.start_heartbeat_runner(
                config=self._config(heartbeat_file),
                responder=NullResponder(),
                now_ms=clock.now_ms,
                sleep_ms=clock.sleep_ms,
            )
            result = api.request_heartbeat_now("not-a-valid-reason")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "invalid-wake-reason")
        self.assertEqual(result["wake_reason"], "not-a-valid-reason")
        self.assertEqual(result["error_code"], "invalid-wake-reason")
        self.assertEqual(result["error_reason"], "invalid-wake-reason")
        self.assertFalse(result["ok"])
        self.assertIn("invalid wake reason", result["error"])

    def test_stop_runner_is_idempotent_and_restores_non_live_behavior(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            clock = _FakeClock()

            api.start_heartbeat_runner(
                config=self._config(heartbeat_file),
                responder=NullResponder(),
                now_ms=clock.now_ms,
                sleep_ms=clock.sleep_ms,
            )

            first_stop = api.stop_heartbeat_runner()
            second_stop = api.stop_heartbeat_runner()
            status_after_stop = api.get_status()
            wake_after_stop = api.request_heartbeat_now("manual")

        self.assertEqual(first_stop, {"status": "stopped"})
        self.assertEqual(second_stop, {"status": "not-running"})
        self.assertEqual(status_after_stop["status"], "idle")
        self.assertEqual(wake_after_stop["status"], "not-running")


if __name__ == "__main__":
    unittest.main()
