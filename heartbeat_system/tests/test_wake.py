from __future__ import annotations

import unittest
from typing import cast

from heartbeat_system.wake import WakeQueue, WakeReason


class TestWakeQueue(unittest.TestCase):
    _REASONS: tuple[WakeReason, ...] = (
        "manual",
        "exec-event",
        "hook",
        "other",
        "interval",
        "retry",
    )
    _PRIORITY: dict[WakeReason, int] = {
        "manual": 3,
        "exec-event": 3,
        "hook": 3,
        "other": 2,
        "interval": 1,
        "retry": 0,
    }

    def test_empty_queue_accepts_all_reasons(self) -> None:
        for reason in self._REASONS:
            with self.subTest(reason=reason):
                queue = WakeQueue()

                decision = queue.request_wake(reason, now_ms=100)
                pending = queue.peek()

                self.assertTrue(decision.accepted)
                self.assertEqual(decision.reason, reason)
                self.assertEqual(decision.queue_size, 1)
                self.assertIsNone(decision.replaced_reason)
                self.assertIsNotNone(pending)
                self.assertEqual(pending.reason, reason)
                self.assertEqual(pending.requested_at_ms, 100)

    def test_priority_coalescing_matrix(self) -> None:
        for pending_reason in self._REASONS:
            for incoming_reason in self._REASONS:
                with self.subTest(pending=pending_reason, incoming=incoming_reason):
                    queue = WakeQueue()
                    queue.request_wake(pending_reason, now_ms=10)

                    decision = queue.request_wake(incoming_reason, now_ms=20)
                    pending = queue.peek()

                    incoming_priority = self._PRIORITY[incoming_reason]
                    pending_priority = self._PRIORITY[pending_reason]

                    self.assertIsNotNone(pending)
                    self.assertEqual(decision.reason, incoming_reason)
                    self.assertEqual(decision.queue_size, 1)

                    if incoming_priority >= pending_priority:
                        self.assertTrue(decision.accepted)
                        self.assertEqual(decision.replaced_reason, pending_reason)
                        self.assertEqual(pending.reason, incoming_reason)
                        self.assertEqual(pending.requested_at_ms, 20)
                    else:
                        self.assertFalse(decision.accepted)
                        self.assertIsNone(decision.replaced_reason)
                        self.assertEqual(pending.reason, pending_reason)
                        self.assertEqual(pending.requested_at_ms, 10)

    def test_pop_next_returns_pending_and_clears_slot(self) -> None:
        queue = WakeQueue()
        queue.request_wake("other", now_ms=50)

        first = queue.pop_next()
        second = queue.pop_next()

        self.assertIsNotNone(first)
        self.assertEqual(first.reason, "other")
        self.assertEqual(first.requested_at_ms, 50)
        self.assertIsNone(second)
        self.assertIsNone(queue.peek())

    def test_peek_does_not_remove_pending(self) -> None:
        queue = WakeQueue()
        queue.request_wake("interval", now_ms=75)

        first = queue.peek()
        second = queue.peek()

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.reason, "interval")
        self.assertEqual(second.reason, "interval")
        self.assertEqual(first.requested_at_ms, 75)
        self.assertEqual(second.requested_at_ms, 75)

    def test_clear_empties_pending_slot(self) -> None:
        queue = WakeQueue()
        queue.request_wake("manual", now_ms=90)

        queue.clear()

        self.assertIsNone(queue.peek())
        self.assertIsNone(queue.pop_next())

    def test_invalid_reason_raises_value_error_without_mutation(self) -> None:
        queue = WakeQueue()
        queue.request_wake("hook", now_ms=40)

        with self.assertRaisesRegex(ValueError, "invalid wake reason"):
            queue.request_wake(cast(WakeReason, "not-real"), now_ms=41)

        pending = queue.peek()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.reason, "hook")
        self.assertEqual(pending.requested_at_ms, 40)


if __name__ == "__main__":
    unittest.main()
