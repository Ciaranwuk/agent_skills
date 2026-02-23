from __future__ import annotations

import unittest
from unittest.mock import patch

from heartbeat_system.system_events import SessionSystemEventBus, SystemEventQueue


class SystemEventQueueTests(unittest.TestCase):
    def test_capacity_drops_oldest_and_surfaces_dropped_count(self) -> None:
        queue = SystemEventQueue(max_items=3, dedupe_consecutive=False)

        queue.publish("e1")
        queue.publish("e2")
        queue.publish("e3")
        write = queue.publish("e4")

        self.assertTrue(write.accepted)
        self.assertEqual(write.dropped, 1)
        self.assertEqual(write.queue_size, 3)

        drained = queue.drain()
        self.assertEqual([event.text for event in drained], ["e2", "e3", "e4"])

    def test_consecutive_dedupe_can_suppress_duplicate_publish(self) -> None:
        queue = SystemEventQueue(max_items=5, dedupe_consecutive=True)

        first = queue.publish("same", source="runner", context={"a": 1})
        second = queue.publish("same", source="runner", context={"a": 1})
        third = queue.publish("same", source="runner", context={"a": 2})

        self.assertTrue(first.accepted)
        self.assertFalse(first.deduped)

        self.assertFalse(second.accepted)
        self.assertTrue(second.deduped)
        self.assertEqual(second.queue_size, 1)

        self.assertTrue(third.accepted)
        self.assertFalse(third.deduped)
        self.assertEqual(queue.size(), 2)

    def test_drain_and_peek_respect_fifo_and_limit(self) -> None:
        queue = SystemEventQueue(max_items=10, dedupe_consecutive=False)
        queue.publish("a")
        queue.publish("b")
        queue.publish("c")

        peeked = queue.peek(limit=2)
        self.assertEqual([event.text for event in peeked], ["a", "b"])
        self.assertEqual(queue.size(), 3)

        drained = queue.drain(limit=2)
        self.assertEqual([event.text for event in drained], ["a", "b"])
        self.assertEqual(queue.size(), 1)

        remainder = queue.drain()
        self.assertEqual([event.text for event in remainder], ["c"])
        self.assertEqual(queue.size(), 0)

    def test_invalid_max_items_raises(self) -> None:
        with self.assertRaises(ValueError):
            SystemEventQueue(max_items=0)

    def test_event_ids_are_monotonic_and_timestamps_are_int(self) -> None:
        queue = SystemEventQueue(max_items=10)
        with patch("heartbeat_system.system_events.time", side_effect=[1000.001, 1000.002]):
            queue.publish("a")
            queue.publish("b")

        events = queue.drain()
        self.assertEqual([event.event_id for event in events], ["se-1", "se-2"])
        self.assertEqual([event.ts_ms for event in events], [1000001, 1000002])


class SessionSystemEventBusTests(unittest.TestCase):
    def test_session_partitioning_keeps_queues_isolated(self) -> None:
        bus = SessionSystemEventBus(max_items=10, dedupe_consecutive=False)

        bus.publish("s1", "one")
        bus.publish("s2", "two")
        bus.publish("s1", "three")

        s1 = bus.drain("s1")
        s2 = bus.drain("s2")

        self.assertEqual([event.text for event in s1], ["one", "three"])
        self.assertEqual([event.text for event in s2], ["two"])

    def test_publish_and_drain_helpers_forward_limits(self) -> None:
        bus = SessionSystemEventBus(max_items=10, dedupe_consecutive=False)
        bus.publish("s1", "a")
        bus.publish("s1", "b")
        bus.publish("s1", "c")

        drained = bus.drain("s1", limit=2)
        self.assertEqual([event.text for event in drained], ["a", "b"])
        self.assertEqual([event.text for event in bus.drain("s1")], ["c"])


if __name__ == "__main__":
    unittest.main()
