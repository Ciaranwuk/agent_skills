from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import ChannelRuntimeError, OutboundMessage
from telegram_channel.adapter import TelegramChannelAdapter
from telegram_channel.api import TelegramApiError


class _ApiStub:
    def __init__(self, batches):
        self._batches = list(batches)
        self.offset_calls: list[int | None] = []
        self.sent_payloads: list[dict[str, object]] = []

    def get_updates(self, *, offset=None, timeout_s=0, limit=100, allowed_updates=None):
        self.offset_calls.append(offset)
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_message(self, *, chat_id, text, reply_to_message_id=None):
        self.sent_payloads.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return {"message_id": 999}


class _ApiFailureStub(_ApiStub):
    def get_updates(self, *, offset=None, timeout_s=0, limit=100, allowed_updates=None):
        raise TelegramApiError(
            operation="getUpdates",
            kind="network-error",
            transient=True,
            description="network down",
        )


class TestTelegramAdapter(unittest.TestCase):
    def test_fetch_ack_progression_with_skips_and_duplicates(self) -> None:
        batch1 = [
            {"update_id": 10, "edited_message": {"text": "ignored"}},
            {
                "update_id": 11,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 100},
                    "from": {"id": 200},
                    "text": "first",
                },
            },
            {
                "update_id": 11,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 100},
                    "from": {"id": 200},
                    "text": "duplicate",
                },
            },
            {
                "update_id": 12,
                "message": {
                    "message_id": 2,
                    "chat": {"id": 100},
                    "from": {"id": 200},
                    "text": "second",
                },
            },
        ]

        api = _ApiStub([batch1, []])
        adapter = TelegramChannelAdapter(api)

        first_fetch = adapter.fetch_updates()

        self.assertEqual(api.offset_calls, [None])
        self.assertEqual([item.update_id for item in first_fetch], ["11", "12"])
        self.assertEqual(adapter._next_offset, 11)

        adapter.ack_update("12")
        self.assertEqual(adapter._next_offset, 11)

        adapter.ack_update("11")
        self.assertEqual(adapter._next_offset, 13)

        second_fetch = adapter.fetch_updates()
        self.assertEqual(api.offset_calls, [None, 13])
        self.assertEqual(second_fetch, [])

    def test_duplicate_update_id_not_reprocessed_in_later_poll(self) -> None:
        batch1 = [
            {
                "update_id": 20,
                "message": {
                    "message_id": 5,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "one",
                },
            }
        ]
        batch2 = [
            {
                "update_id": 20,
                "message": {
                    "message_id": 5,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "dup",
                },
            },
            {
                "update_id": 21,
                "message": {
                    "message_id": 6,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "two",
                },
            },
        ]

        api = _ApiStub([batch1, batch2])
        adapter = TelegramChannelAdapter(api)

        first = adapter.fetch_updates()
        self.assertEqual([item.update_id for item in first], ["20"])

        adapter.ack_update("20")
        self.assertEqual(adapter._next_offset, 21)

        second = adapter.fetch_updates()
        self.assertEqual([item.update_id for item in second], ["21"])

    def test_send_message_maps_outbound_payload(self) -> None:
        api = _ApiStub([[]])
        adapter = TelegramChannelAdapter(api)

        adapter.send_message(OutboundMessage(chat_id="55", text="hi", reply_to_message_id="3"))

        self.assertEqual(
            api.sent_payloads,
            [{"chat_id": "55", "text": "hi", "reply_to_message_id": "3"}],
        )

    def test_fetch_api_error_is_wrapped_deterministically(self) -> None:
        adapter = TelegramChannelAdapter(_ApiFailureStub([]))

        with self.assertRaises(ChannelRuntimeError) as ctx:
            adapter.fetch_updates()

        self.assertIn("fetch_updates failed", str(ctx.exception))
        self.assertIn("network-error", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
