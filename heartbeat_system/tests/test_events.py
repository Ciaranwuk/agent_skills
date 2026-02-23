from __future__ import annotations

import hashlib
import threading
import unittest

from heartbeat_system.events import (
    DedupeRecord,
    EventCounters,
    HeartbeatEventService,
    LastEventRecord,
)


class _FakeStateStore:
    def __init__(self) -> None:
        self._last_event: LastEventRecord | None = None
        self._counters = EventCounters()
        self._dedupe: dict[str, DedupeRecord] = {}

    def get_last_event(self) -> LastEventRecord | None:
        return self._last_event

    def set_last_event(self, event: LastEventRecord) -> None:
        self._last_event = event

    def get_counters(self) -> EventCounters:
        return self._counters

    def set_counters(self, counters: EventCounters) -> None:
        self._counters = counters

    def get_dedupe(self, key: str) -> DedupeRecord | None:
        return self._dedupe.get(key)

    def set_dedupe(self, record: DedupeRecord) -> None:
        self._dedupe[record.key] = record


class _Clock:
    def __init__(self, values: list[int]) -> None:
        self._values = list(values)

    def now_ms(self) -> int:
        if not self._values:
            raise AssertionError("clock exhausted")
        return self._values.pop(0)


class TestHeartbeatEventService(unittest.TestCase):
    def test_first_ran_with_output_delivers_and_updates_last_event(self) -> None:
        store = _FakeStateStore()
        clock = _Clock([1_000])
        service = HeartbeatEventService(
            store=store,
            dedupe_window_ms=5_000,
            now_ms=clock.now_ms,
        )

        result = service.ingest_run_result(
            {
                "status": "ran",
                "reason": "delivered",
                "run_reason": "manual",
                "output_text": "hello world",
            }
        )

        expected_key = hashlib.sha256(b"hello world").hexdigest()
        self.assertTrue(result.should_deliver)
        self.assertFalse(result.dedupe_suppressed)
        self.assertEqual(result.dedupe_key, expected_key)
        self.assertEqual(result.counters, EventCounters(ran=1, skipped=0, failed=0, deduped=0))
        self.assertEqual(result.event.ts_ms, 1_000)
        self.assertEqual(result.event.status, "ran")
        self.assertFalse(result.event.dedupe_suppressed)
        self.assertEqual(service.get_last_event(), result.event)
        self.assertEqual(service.get_counters(), EventCounters(ran=1, skipped=0, failed=0, deduped=0))

    def test_duplicate_ran_output_suppressed_inside_dedupe_window(self) -> None:
        store = _FakeStateStore()
        clock = _Clock([1_000, 1_200])
        service = HeartbeatEventService(
            store=store,
            dedupe_window_ms=500,
            now_ms=clock.now_ms,
        )

        first = service.ingest_run_result(
            {
                "status": "ran",
                "reason": "delivered",
                "run_reason": "interval",
                "output_text": "duplicate me",
            }
        )
        second = service.ingest_run_result(
            {
                "status": "ran",
                "reason": "delivered",
                "run_reason": "interval",
                "output_text": "duplicate me",
            }
        )

        self.assertTrue(first.should_deliver)
        self.assertFalse(first.dedupe_suppressed)

        self.assertFalse(second.should_deliver)
        self.assertTrue(second.dedupe_suppressed)
        self.assertEqual(second.event.status, "ran")
        self.assertEqual(second.event.ts_ms, 1_200)
        self.assertTrue(second.event.dedupe_suppressed)
        self.assertEqual(second.counters, EventCounters(ran=2, skipped=0, failed=0, deduped=1))
        self.assertEqual(service.get_last_event(), second.event)
        self.assertEqual(service.get_counters(), EventCounters(ran=2, skipped=0, failed=0, deduped=1))

    def test_skipped_and_failed_increment_counters_without_delivery(self) -> None:
        store = _FakeStateStore()
        clock = _Clock([2_000, 3_000])
        service = HeartbeatEventService(
            store=store,
            dedupe_window_ms=1_000,
            now_ms=clock.now_ms,
        )

        skipped = service.ingest_run_result(
            {
                "status": "skipped",
                "reason": "ack-only",
                "run_reason": "manual",
            }
        )
        failed = service.ingest_run_result(
            {
                "status": "failed",
                "reason": "adapter-exception",
                "run_reason": "manual",
                "error": "RuntimeError: boom",
            }
        )

        self.assertFalse(skipped.should_deliver)
        self.assertFalse(skipped.dedupe_suppressed)
        self.assertIsNone(skipped.dedupe_key)
        self.assertEqual(skipped.counters, EventCounters(ran=0, skipped=1, failed=0, deduped=0))

        self.assertFalse(failed.should_deliver)
        self.assertFalse(failed.dedupe_suppressed)
        self.assertIsNone(failed.dedupe_key)
        self.assertEqual(failed.counters, EventCounters(ran=0, skipped=1, failed=1, deduped=0))
        self.assertEqual(service.get_last_event(), failed.event)

    def test_concurrent_ingest_preserves_all_counter_increments(self) -> None:
        store = _FakeStateStore()
        now_lock = threading.Lock()
        next_ms = 10_000

        def now_ms() -> int:
            nonlocal next_ms
            with now_lock:
                current = next_ms
                next_ms += 1
                return current

        service = HeartbeatEventService(
            store=store,
            dedupe_window_ms=5_000,
            now_ms=now_ms,
        )

        worker_count = 16
        ingests_per_worker = 40
        start_barrier = threading.Barrier(worker_count)
        results: list = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def worker() -> None:
            try:
                start_barrier.wait(timeout=5)
                for _ in range(ingests_per_worker):
                    result = service.ingest_run_result(
                        {
                            "status": "skipped",
                            "reason": "ack-only",
                            "run_reason": "interval",
                        }
                    )
                    with result_lock:
                        results.append(result)
            except BaseException as exc:  # pragma: no cover - only for thread error capture
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads), "threads did not finish")
        self.assertEqual(errors, [])

        expected = worker_count * ingests_per_worker
        self.assertEqual(len(results), expected)
        self.assertEqual(
            service.get_counters(),
            EventCounters(ran=0, skipped=expected, failed=0, deduped=0),
        )
        self.assertTrue(all(not result.should_deliver for result in results))
        self.assertTrue(all(not result.dedupe_suppressed for result in results))
        self.assertTrue(all(result.dedupe_key is None for result in results))


if __name__ == "__main__":
    unittest.main()
