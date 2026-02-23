from __future__ import annotations

import unittest

from heartbeat_system.normalize import normalize_heartbeat_text


class TestNormalizeHeartbeatText(unittest.TestCase):
    def test_ack_only_skips_delivery(self) -> None:
        result = normalize_heartbeat_text("HEARTBEAT_OK")

        self.assertFalse(result.should_deliver)
        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "ack-only")

    def test_ack_plus_short_remainder_skips_delivery(self) -> None:
        result = normalize_heartbeat_text(
            "HEARTBEAT_OK ok",
            ack_max_chars=5,
        )

        self.assertFalse(result.should_deliver)
        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "ack-short-remainder")

    def test_non_trivial_remainder_delivers_with_ack_stripped(self) -> None:
        remainder = "x" * 11
        result = normalize_heartbeat_text(
            f"HEARTBEAT_OK {remainder}",
            ack_max_chars=10,
        )

        self.assertTrue(result.should_deliver)
        self.assertEqual(result.text, remainder)
        self.assertEqual(result.reason, "delivered")

    def test_non_ack_text_delivers(self) -> None:
        result = normalize_heartbeat_text("  heartbeat alert  ")

        self.assertTrue(result.should_deliver)
        self.assertEqual(result.text, "heartbeat alert")
        self.assertEqual(result.reason, "delivered")


if __name__ == "__main__":
    unittest.main()
