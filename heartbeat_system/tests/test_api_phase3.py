from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from heartbeat_system import api
from heartbeat_system.adapters.null_responder import NullResponder
from heartbeat_system.store import (
    DedupeRecord,
    EventCounters,
    JsonFileHeartbeatStateStore,
    LastEventRecord,
)


class _FakeHandle:
    def __init__(self) -> None:
        self.enabled_values: list[bool] = []

    def get_status(self):  # noqa: ANN001 - scheduler protocol stub
        class _Status:
            enabled = True
            running = True
            in_flight = False
            next_due_ms = 123
            pending_wake_reason = None
            last_run_reason = None

        return _Status()

    def set_enabled(self, enabled: bool) -> bool:
        self.enabled_values.append(enabled)
        return not enabled

    def wake(self, reason: str = "manual") -> dict[str, object]:
        return {
            "status": "accepted",
            "accepted": True,
            "reason": reason,
            "queue_size": 1,
            "replaced_reason": None,
        }

    def stop(self) -> None:
        return None


class TestApiPhase3(unittest.TestCase):
    def setUp(self) -> None:
        api.reset_runtime_for_tests()

    def tearDown(self) -> None:
        api.reset_runtime_for_tests()

    def test_get_status_idle_is_deterministic_without_scheduler(self) -> None:
        status = api.get_status()

        self.assertEqual(
            status,
            {
                "contract": "heartbeat.operator",
                "contract_version": "1.0",
                "contract_metadata": {"name": "heartbeat.operator", "version": "1.0"},
                "error_code": None,
                "error_reason": None,
                "ok": True,
                "status": "idle",
                "enabled": True,
                "running": False,
                "in_flight": False,
                "next_due_ms": None,
                "pending_wake_reason": None,
                "last_run_reason": None,
                "scheduler_diagnostics": None,
                "counters": {"ran": 0, "skipped": 0, "failed": 0, "deduped": 0},
                "last_event_present": False,
                "ingest_diagnostics": {
                    "history_limit": 20,
                    "recent": [],
                    "counters": {
                        "total": 0,
                        "manual": 0,
                        "scheduler": 0,
                        "delivered": 0,
                        "suppressed": 0,
                    },
                },
                "store_load_warning": None,
            },
        )

    def test_get_last_event_empty_before_runs(self) -> None:
        payload = api.get_last_event()

        self.assertEqual(
            payload,
            {
                "contract": "heartbeat.operator",
                "contract_version": "1.0",
                "contract_metadata": {"name": "heartbeat.operator", "version": "1.0"},
                "error_code": None,
                "error_reason": None,
                "ok": True,
                "status": "empty",
                "event": None,
            },
        )

    def test_run_once_ingests_and_returns_dedupe_metadata_on_duplicate(self) -> None:
        with patch(
            "heartbeat_system.runner.run_heartbeat_once",
            return_value={
                "status": "ran",
                "reason": "delivered",
                "output_text": "same output",
                "run_reason": "manual",
            },
        ):
            first = api.run_once(reason="manual")
            second = api.run_once(reason="manual")

        self.assertEqual(first["status"], "ran")
        self.assertEqual(first["contract"], "heartbeat.operator")
        self.assertEqual(first["contract_version"], "1.0")
        self.assertEqual(first["contract_metadata"], {"name": "heartbeat.operator", "version": "1.0"})
        self.assertIsNone(first["error_code"])
        self.assertIsNone(first["error_reason"])
        self.assertTrue(first["ok"])
        self.assertIn("event", first)
        self.assertIn("counters", first)
        self.assertFalse(first["dedupe_suppressed"])
        self.assertTrue(first["should_deliver"])
        self.assertIn("dedupe_key", first)
        self.assertEqual(
            sorted(first.keys()),
            sorted(
                [
                    "contract",
                    "contract_version",
                    "contract_metadata",
                    "error_code",
                    "error_reason",
                    "ok",
                    "status",
                    "reason",
                    "output_text",
                    "run_reason",
                    "error",
                    "event",
                    "counters",
                    "dedupe_suppressed",
                    "dedupe_key",
                    "should_deliver",
                ]
            ),
        )

        self.assertEqual(second["status"], "ran")
        self.assertEqual(second["contract"], "heartbeat.operator")
        self.assertEqual(second["contract_version"], "1.0")
        self.assertEqual(second["contract_metadata"], {"name": "heartbeat.operator", "version": "1.0"})
        self.assertIsNone(second["error_code"])
        self.assertIsNone(second["error_reason"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["dedupe_suppressed"])
        self.assertFalse(second["should_deliver"])
        self.assertEqual(second["counters"]["ran"], 2)
        self.assertEqual(second["counters"]["deduped"], 1)
        self.assertEqual(
            sorted(second.keys()),
            sorted(
                [
                    "contract",
                    "contract_version",
                    "contract_metadata",
                    "error_code",
                    "error_reason",
                    "ok",
                    "status",
                    "reason",
                    "output_text",
                    "run_reason",
                    "error",
                    "event",
                    "counters",
                    "dedupe_suppressed",
                    "dedupe_key",
                    "should_deliver",
                ]
            ),
        )

        status = api.get_status()
        self.assertEqual(status["ingest_diagnostics"]["counters"]["total"], 2)
        self.assertEqual(status["ingest_diagnostics"]["counters"]["manual"], 2)
        self.assertEqual(status["ingest_diagnostics"]["counters"]["delivered"], 1)
        self.assertEqual(status["ingest_diagnostics"]["counters"]["suppressed"], 1)
        self.assertEqual(len(status["ingest_diagnostics"]["recent"]), 2)
        self.assertEqual(status["ingest_diagnostics"]["recent"][0]["source"], "manual")
        self.assertEqual(status["ingest_diagnostics"]["recent"][1]["source"], "manual")

        last_event = api.get_last_event()
        self.assertEqual(last_event["status"], "ok")
        self.assertEqual(last_event["contract"], "heartbeat.operator")
        self.assertEqual(last_event["contract_version"], "1.0")
        self.assertTrue(last_event["event"]["dedupe_suppressed"])

    def test_run_once_includes_and_consumes_session_system_events(self) -> None:
        write_result = api.publish_system_event(
            session_key="default",
            text="queue drained",
            source="scheduler",
            context={"z": 2, "a": 1},
        )
        captured_system_events: list[list[str]] = []

        def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
            del config, responder, reason
            captured_system_events.append(list(system_events or ()))
            return {
                "status": "ran",
                "reason": "delivered",
                "output_text": "ok",
                "run_reason": "manual",
            }

        with patch("heartbeat_system.runner.run_heartbeat_once", _fake_run_once):
            first = api.run_once(reason="manual")
            second = api.run_once(reason="manual")

        self.assertEqual(write_result["status"], "accepted")
        self.assertEqual(first["status"], "ran")
        self.assertEqual(second["status"], "ran")
        self.assertEqual(
            captured_system_events[:1],
            [['source=scheduler;text=queue drained;context={"a":1,"z":2}']],
        )
        self.assertEqual(captured_system_events[1:], [[]])

    def test_run_once_system_events_apply_fifo_overflow_marker(self) -> None:
        limit = api._SYSTEM_EVENT_DRAIN_LIMIT
        for index in range(limit + 2):
            api.publish_system_event(
                session_key="default",
                text=f"event-{index}",
                source="worker",
            )

        captured_system_events: list[list[str]] = []

        def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
            del config, responder, reason
            captured_system_events.append(list(system_events or ()))
            return {
                "status": "ran",
                "reason": "delivered",
                "output_text": "ok",
                "run_reason": "manual",
            }

        with patch("heartbeat_system.runner.run_heartbeat_once", _fake_run_once):
            api.run_once(reason="manual")
            api.run_once(reason="manual")

        first = captured_system_events[0]
        self.assertEqual(len(first), limit)
        self.assertEqual(first[0], "source=worker;text=event-0;context={}")
        self.assertEqual(first[limit - 2], f"source=worker;text=event-{limit - 2};context={{}}")
        self.assertEqual(first[-1], "source=system;text=[overflow dropped=3];context={}")
        self.assertEqual(captured_system_events[1], [])

    def test_enable_disable_updates_store_when_scheduler_absent(self) -> None:
        disabled = api.disable_heartbeat()
        status_after_disable = api.get_status()
        enabled = api.enable_heartbeat()
        status_after_enable = api.get_status()

        self.assertEqual(
            disabled,
            {
                "contract": "heartbeat.operator",
                "contract_version": "1.0",
                "contract_metadata": {"name": "heartbeat.operator", "version": "1.0"},
                "error_code": None,
                "error_reason": None,
                "ok": True,
                "status": "ok",
                "enabled": False,
                "previous_enabled": True,
                "applied_to_scheduler": False,
            },
        )
        self.assertFalse(status_after_disable["enabled"])

        self.assertEqual(
            enabled,
            {
                "contract": "heartbeat.operator",
                "contract_version": "1.0",
                "contract_metadata": {"name": "heartbeat.operator", "version": "1.0"},
                "error_code": None,
                "error_reason": None,
                "ok": True,
                "status": "ok",
                "enabled": True,
                "previous_enabled": False,
                "applied_to_scheduler": False,
            },
        )
        self.assertTrue(status_after_enable["enabled"])

    def test_enable_disable_applies_to_live_scheduler_when_supported(self) -> None:
        fake = _FakeHandle()
        with patch("heartbeat_system.api.start_scheduler", return_value=fake):
            started = api.start_heartbeat_runner(config={"enabled": True}, responder=NullResponder())

        self.assertEqual(started, {"status": "started"})
        api.disable_heartbeat()
        api.enable_heartbeat()
        api.stop_heartbeat_runner()

        self.assertEqual(fake.enabled_values, [False, True])

    def test_wake_without_scheduler_returns_not_running_contract(self) -> None:
        payload = api.wake(reason="manual")

        self.assertEqual(
            payload,
            {
                "contract": "heartbeat.operator",
                "contract_version": "1.0",
                "contract_metadata": {"name": "heartbeat.operator", "version": "1.0"},
                "status": "not-running",
                "accepted": False,
                "reason": "manual",
                "wake_reason": "manual",
                "queue_size": 0,
                "replaced_reason": None,
                "error": "",
                "error_code": "runtime-not-started",
                "error_reason": "runtime-not-started",
                "ok": False,
            },
        )

    def test_scheduler_wake_run_updates_counters_and_last_event(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            config = {
                "enabled": True,
                "heartbeat_file": heartbeat_file,
                "ack_token": "HEARTBEAT_OK",
                "ack_max_chars": 300,
                "interval_ms": 1_000_000,
            }

            with patch(
                "heartbeat_system.scheduler.run_heartbeat_once",
                return_value={
                    "status": "ran",
                    "reason": "delivered",
                    "output_text": "scheduled output",
                    "run_reason": "manual",
                },
            ):
                started = api.start_heartbeat_runner(config=config, responder=NullResponder())
                wake_payload = api.request_heartbeat_now("manual")
                self.assertTrue(_wait_until(lambda: api.get_status()["counters"]["ran"] == 1))
                status = api.get_status()
                last_event = api.get_last_event()
                api.stop_heartbeat_runner()

        self.assertEqual(started, {"status": "started"})
        self.assertEqual(wake_payload["status"], "accepted")
        self.assertEqual(status["counters"]["ran"], 1)
        self.assertTrue(status["last_event_present"])
        self.assertIn("scheduler_diagnostics", status)
        self.assertTrue(status["scheduler_diagnostics"]["thread_alive"])
        self.assertEqual(status["scheduler_diagnostics"]["health"], "ok")
        self.assertEqual(status["scheduler_diagnostics"]["run_attempts"], 1)
        self.assertEqual(status["scheduler_diagnostics"]["run_failures"], 0)
        self.assertEqual(status["ingest_diagnostics"]["counters"]["scheduler"], 1)
        self.assertEqual(last_event["status"], "ok")
        self.assertEqual(last_event["event"]["status"], "ran")
        self.assertEqual(last_event["event"]["run_reason"], "manual")

    def test_scheduler_run_includes_drained_session_system_events(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            config = {
                "enabled": True,
                "heartbeat_file": heartbeat_file,
                "ack_token": "HEARTBEAT_OK",
                "ack_max_chars": 300,
                "interval_ms": 1_000_000,
                "session_key": "session-a",
            }
            api.publish_system_event(
                session_key="session-a",
                text="wake fired",
                source="watcher",
                context={"id": 7},
            )
            captured_system_events: list[list[str]] = []

            def _fake_run_once(*, config, responder, reason, system_events=None):  # noqa: ANN001 - patch stub
                del config, responder, reason
                captured_system_events.append(list(system_events or ()))
                return {
                    "status": "ran",
                    "reason": "delivered",
                    "output_text": "scheduled output",
                    "run_reason": "manual",
                }

            with patch("heartbeat_system.scheduler.run_heartbeat_once", _fake_run_once):
                started = api.start_heartbeat_runner(config=config, responder=NullResponder())
                wake_payload = api.request_heartbeat_now("manual")
                self.assertTrue(_wait_until(lambda: len(captured_system_events) >= 1))
                api.stop_heartbeat_runner()

        self.assertEqual(started, {"status": "started"})
        self.assertEqual(wake_payload["status"], "accepted")
        self.assertEqual(
            captured_system_events[0],
            ['source=watcher;text=wake fired;context={"id":7}'],
        )

    def test_start_runner_uses_json_backend_and_restores_counters(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            state_file = Path(tmp_dir) / "state.json"
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            seed_store = JsonFileHeartbeatStateStore(state_file)
            seed_store.set_enabled(True)
            seed_store.set_counters(EventCounters(ran=4, skipped=3, failed=2, deduped=1))
            seed_store.set_last_event(
                LastEventRecord(
                    event_id="evt-seed",
                    ts_ms=42,
                    status="ran",
                    reason="delivered",
                    run_reason="manual",
                    output_text="seed",
                    error="",
                    dedupe_suppressed=False,
                )
            )
            seed_store.set_dedupe(
                DedupeRecord(
                    key="seed-key",
                    last_seen_ms=42,
                    suppress_until_ms=55,
                    hits=2,
                )
            )
            config = {
                "enabled": True,
                "heartbeat_file": heartbeat_file,
                "ack_token": "HEARTBEAT_OK",
                "ack_max_chars": 300,
                "interval_ms": 1_000_000,
                "state_path": str(state_file),
            }

            started = api.start_heartbeat_runner(config=config, responder=NullResponder())
            status = api.get_status()
            api.stop_heartbeat_runner()

        self.assertEqual(started, {"status": "started"})
        self.assertEqual(
            status["counters"],
            {"ran": 4, "skipped": 3, "failed": 2, "deduped": 1},
        )
        self.assertTrue(status["last_event_present"])

    def test_start_runner_with_corrupt_json_state_file_falls_back_to_defaults(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            state_file = Path(tmp_dir) / "state.json"
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            state_file.write_text("{bad-json", encoding="utf-8")
            config = {
                "enabled": True,
                "heartbeat_file": heartbeat_file,
                "ack_token": "HEARTBEAT_OK",
                "ack_max_chars": 300,
                "interval_ms": 1_000_000,
                "state_path": str(state_file),
            }

            started = api.start_heartbeat_runner(config=config, responder=NullResponder())
            status = api.get_status()
            api.stop_heartbeat_runner()

        self.assertEqual(started, {"status": "started"})
        self.assertEqual(
            status["counters"],
            {"ran": 0, "skipped": 0, "failed": 0, "deduped": 0},
        )
        self.assertFalse(status["last_event_present"])
        self.assertIn("store_load_warning", status)
        self.assertIn("persistent-state-load-warning", status["store_load_warning"])

    def test_start_runner_prunes_expired_dedupe_records_using_now_ms(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            state_file = Path(tmp_dir) / "state.json"
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")

            seed_store = JsonFileHeartbeatStateStore(state_file)
            seed_store.set_dedupe(
                DedupeRecord(key="expired", last_seen_ms=1, suppress_until_ms=99, hits=1)
            )
            seed_store.set_dedupe(
                DedupeRecord(key="edge", last_seen_ms=2, suppress_until_ms=100, hits=2)
            )
            seed_store.set_dedupe(
                DedupeRecord(key="live", last_seen_ms=3, suppress_until_ms=101, hits=3)
            )

            config = {
                "enabled": True,
                "heartbeat_file": heartbeat_file,
                "ack_token": "HEARTBEAT_OK",
                "ack_max_chars": 300,
                "interval_ms": 1_000_000,
                "state_path": str(state_file),
            }

            started = api.start_heartbeat_runner(
                config=config,
                responder=NullResponder(),
                now_ms=lambda: 100,
            )
            api.stop_heartbeat_runner()

            restored = JsonFileHeartbeatStateStore(state_file)

        self.assertEqual(started, {"status": "started"})
        self.assertEqual(list(restored.snapshot().dedupe.keys()), ["live"])

    def test_state_path_persists_across_runtime_rebuild(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            heartbeat_file = str(Path(tmp_dir) / "HEARTBEAT.md")
            state_file = Path(tmp_dir) / "state.json"
            Path(heartbeat_file).write_text("prompt", encoding="utf-8")
            config = {
                "enabled": True,
                "heartbeat_file": heartbeat_file,
                "ack_token": "HEARTBEAT_OK",
                "ack_max_chars": 300,
                "interval_ms": 1_000_000,
                "state_path": str(state_file),
            }

            with patch(
                "heartbeat_system.runner.run_heartbeat_once",
                return_value={
                    "status": "ran",
                    "reason": "delivered",
                    "output_text": "persist me",
                    "run_reason": "manual",
                },
            ):
                first_start = api.start_heartbeat_runner(
                    config=config,
                    responder=NullResponder(),
                )
                api.run_once(reason="manual")
                first_stop = api.stop_heartbeat_runner()

            api.reset_runtime_for_tests()

            second_start = api.start_heartbeat_runner(config=config, responder=NullResponder())
            status = api.get_status()
            last_event = api.get_last_event()
            second_stop = api.stop_heartbeat_runner()

        self.assertEqual(first_start, {"status": "started"})
        self.assertEqual(first_stop, {"status": "stopped"})
        self.assertEqual(second_start, {"status": "started"})
        self.assertEqual(second_stop, {"status": "stopped"})
        self.assertEqual(
            status["counters"],
            {"ran": 1, "skipped": 0, "failed": 0, "deduped": 0},
        )
        self.assertTrue(status["last_event_present"])
        self.assertEqual(last_event["status"], "ok")
        self.assertEqual(last_event["event"]["output_text"], "persist me")


def _wait_until(predicate, timeout: float = 1.0) -> bool:  # noqa: ANN001 - test helper
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


if __name__ == "__main__":
    unittest.main()
