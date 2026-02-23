from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from heartbeat_system.store import (
    DedupeRecord,
    EventCounters,
    InMemoryHeartbeatStateStore,
    JsonFileHeartbeatStateStore,
    LastEventRecord,
)


class TestInMemoryHeartbeatStateStore(unittest.TestCase):
    def test_defaults(self) -> None:
        store = InMemoryHeartbeatStateStore()

        self.assertTrue(store.get_enabled())
        self.assertIsNone(store.get_last_event())
        self.assertEqual(store.get_counters(), EventCounters())
        self.assertIsNone(store.get_dedupe("missing"))
        self.assertEqual(store.snapshot().dedupe, {})

    def test_set_enabled_returns_previous_and_updates_value(self) -> None:
        store = InMemoryHeartbeatStateStore()

        prev_1 = store.set_enabled(False)
        prev_2 = store.set_enabled(True)

        self.assertTrue(prev_1)
        self.assertFalse(prev_2)
        self.assertTrue(store.get_enabled())

    def test_last_event_roundtrip(self) -> None:
        store = InMemoryHeartbeatStateStore()
        event = LastEventRecord(
            event_id="evt-1",
            ts_ms=123,
            status="ran",
            reason="delivered",
            run_reason="manual",
            output_text="hello",
            error="",
            dedupe_suppressed=False,
        )

        store.set_last_event(event)

        self.assertEqual(store.get_last_event(), event)

    def test_counters_roundtrip(self) -> None:
        store = InMemoryHeartbeatStateStore()
        counters = EventCounters(ran=2, skipped=3, failed=4, deduped=5)

        store.set_counters(counters)

        self.assertEqual(store.get_counters(), counters)

    def test_dedupe_set_get_and_replace(self) -> None:
        store = InMemoryHeartbeatStateStore()
        first = DedupeRecord(
            key="k1",
            last_seen_ms=10,
            suppress_until_ms=100,
            hits=1,
        )
        second = DedupeRecord(
            key="k1",
            last_seen_ms=20,
            suppress_until_ms=200,
            hits=2,
        )

        store.set_dedupe(first)
        store.set_dedupe(second)

        self.assertEqual(store.get_dedupe("k1"), second)
        self.assertIsNone(store.get_dedupe("k2"))

    def test_prune_dedupe_removes_expired_records(self) -> None:
        store = InMemoryHeartbeatStateStore()
        store.set_dedupe(
            DedupeRecord(key="old", last_seen_ms=1, suppress_until_ms=50, hits=1)
        )
        store.set_dedupe(
            DedupeRecord(key="edge", last_seen_ms=2, suppress_until_ms=100, hits=2)
        )
        store.set_dedupe(
            DedupeRecord(key="new", last_seen_ms=3, suppress_until_ms=101, hits=3)
        )

        removed = store.prune_dedupe(now_ms=100)

        self.assertEqual(removed, 2)
        self.assertIsNone(store.get_dedupe("old"))
        self.assertIsNone(store.get_dedupe("edge"))
        self.assertIsNotNone(store.get_dedupe("new"))

    def test_snapshot_returns_copy_of_dedupe_map(self) -> None:
        store = InMemoryHeartbeatStateStore()
        rec = DedupeRecord(key="k", last_seen_ms=1, suppress_until_ms=10)
        store.set_dedupe(rec)

        snap = store.snapshot()
        del snap.dedupe["k"]

        self.assertEqual(store.get_dedupe("k"), rec)

    def test_snapshot_contains_current_state(self) -> None:
        store = InMemoryHeartbeatStateStore()
        store.set_enabled(False)
        event = LastEventRecord(
            event_id="evt-2",
            ts_ms=456,
            status="failed",
            reason="adapter-exception",
            run_reason="interval",
            output_text="",
            error="RuntimeError: boom",
            dedupe_suppressed=False,
        )
        counters = EventCounters(ran=1, skipped=2, failed=3, deduped=4)
        dedupe = DedupeRecord(key="abc", last_seen_ms=10, suppress_until_ms=20, hits=5)

        store.set_last_event(event)
        store.set_counters(counters)
        store.set_dedupe(dedupe)
        snap = store.snapshot()

        self.assertFalse(snap.enabled)
        self.assertEqual(snap.last_event, event)
        self.assertEqual(snap.counters, counters)
        self.assertEqual(snap.dedupe, {"abc": dedupe})

    def test_concurrent_dedupe_writes_and_reads_are_safe(self) -> None:
        store = InMemoryHeartbeatStateStore()
        started = threading.Barrier(5)

        def writer(idx: int) -> None:
            started.wait()
            for j in range(50):
                key = f"{idx}-{j}"
                store.set_dedupe(
                    DedupeRecord(
                        key=key,
                        last_seen_ms=j,
                        suppress_until_ms=10_000 + j,
                        hits=j + 1,
                    )
                )
                self.assertIsNotNone(store.get_dedupe(key))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()

        started.wait()

        for thread in threads:
            thread.join()

        snap = store.snapshot()
        self.assertEqual(len(snap.dedupe), 200)


class TestJsonFileHeartbeatStateStore(unittest.TestCase):
    def test_missing_file_starts_with_defaults(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "state.json"
            store = JsonFileHeartbeatStateStore(state_file)

            self.assertTrue(store.get_enabled())
            self.assertEqual(store.get_counters(), EventCounters())
            self.assertIsNone(store.get_last_event())
            self.assertEqual(store.snapshot().dedupe, {})
            self.assertIsNone(store.load_error)

    def test_corrupt_file_falls_back_to_defaults_without_crashing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "state.json"
            state_file.write_text("{not-valid-json", encoding="utf-8")

            store = JsonFileHeartbeatStateStore(state_file)

            self.assertTrue(store.get_enabled())
            self.assertEqual(store.get_counters(), EventCounters())
            self.assertIsNone(store.get_last_event())
            self.assertEqual(store.snapshot().dedupe, {})
            self.assertIsNotNone(store.load_error)

    def test_roundtrip_persists_and_restores_state(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "state.json"
            store = JsonFileHeartbeatStateStore(state_file)
            event = LastEventRecord(
                event_id="evt-restore",
                ts_ms=101,
                status="ran",
                reason="delivered",
                run_reason="manual",
                output_text="payload",
                error="",
                dedupe_suppressed=False,
            )
            counters = EventCounters(ran=7, skipped=3, failed=1, deduped=2)
            dedupe = DedupeRecord(
                key="dedupe-key",
                last_seen_ms=55,
                suppress_until_ms=77,
                hits=4,
            )

            store.set_enabled(False)
            store.set_counters(counters)
            store.set_last_event(event)
            store.set_dedupe(dedupe)

            restored = JsonFileHeartbeatStateStore(state_file)

            self.assertFalse(restored.get_enabled())
            self.assertEqual(restored.get_counters(), counters)
            self.assertEqual(restored.get_last_event(), event)
            self.assertEqual(restored.get_dedupe("dedupe-key"), dedupe)
            self.assertIsNone(restored.load_error)


if __name__ == "__main__":
    unittest.main()
