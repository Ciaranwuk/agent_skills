from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import ChannelRuntimeError, OutboundMessage
from telegram_channel.adapter import TelegramChannelAdapter
from telegram_channel.api import TelegramApiError
from telegram_channel.cursor_state import CursorStateError, CursorStateSnapshot, DurableCursorStateStore


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


class _ApiSendFailureStub(_ApiStub):
    def send_message(self, *, chat_id, text, reply_to_message_id=None):
        raise TelegramApiError(
            operation="sendMessage",
            kind="http-error",
            transient=True,
            description="telegram unavailable",
            status_code=503,
            retry_class="transient",
        )


class _StateStoreFailureStub:
    def __init__(self, *, fail_load: bool = False, fail_save: bool = False):
        self.fail_load = fail_load
        self.fail_save = fail_save
        self.saved_floors: list[int] = []

    def load(self) -> CursorStateSnapshot:
        if self.fail_load:
            raise CursorStateError(kind="state-load-io", detail="load down")
        return CursorStateSnapshot(committed_floor=None)

    def save(self, *, committed_floor: int) -> None:
        if self.fail_save:
            raise CursorStateError(kind="state-save-io", detail="save down")
        self.saved_floors.append(int(committed_floor))


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

    def test_unacked_duplicate_update_id_is_retried_in_later_poll(self) -> None:
        batch1 = [
            {
                "update_id": 60,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "retry",
                },
            }
        ]
        batch2 = [
            {
                "update_id": 60,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "retry",
                },
            }
        ]
        batch3 = []

        api = _ApiStub([batch1, batch2, batch3])
        adapter = TelegramChannelAdapter(api)

        first = adapter.fetch_updates()
        self.assertEqual([item.update_id for item in first], ["60"])
        self.assertEqual(adapter._next_offset, 60)

        second = adapter.fetch_updates()
        self.assertEqual([item.update_id for item in second], ["60"])
        self.assertEqual(adapter._next_offset, 60)

        adapter.ack_update("60")
        self.assertEqual(adapter._next_offset, 61)

        third = adapter.fetch_updates()
        self.assertEqual(third, [])
        self.assertEqual(api.offset_calls, [None, 60, 61])

    def test_send_message_maps_outbound_payload(self) -> None:
        api = _ApiStub([[]])
        adapter = TelegramChannelAdapter(api)

        adapter.send_message(OutboundMessage(chat_id="55", text="hi", reply_to_message_id="3"))

        self.assertEqual(
            api.sent_payloads,
            [{"chat_id": "55", "text": "hi", "reply_to_message_id": "3"}],
        )

    def test_send_message_api_error_is_wrapped_deterministically(self) -> None:
        adapter = TelegramChannelAdapter(_ApiSendFailureStub([[]]))

        with self.assertRaises(ChannelRuntimeError) as ctx:
            adapter.send_message(OutboundMessage(chat_id="55", text="hello"))

        self.assertIn("send_message failed", str(ctx.exception))
        self.assertIn("http-error", str(ctx.exception))

    def test_fetch_api_error_is_wrapped_deterministically(self) -> None:
        adapter = TelegramChannelAdapter(_ApiFailureStub([]))

        with self.assertRaises(ChannelRuntimeError) as ctx:
            adapter.fetch_updates()

        self.assertIn("fetch_updates failed", str(ctx.exception))
        self.assertIn("network-error", str(ctx.exception))

    def test_cursor_state_persists_floor_and_resumes_after_restart(self) -> None:
        batch1 = [
            {
                "update_id": 30,
                "message": {
                    "message_id": 8,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "one",
                },
            },
            {
                "update_id": 31,
                "message": {
                    "message_id": 9,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "two",
                },
            },
        ]
        batch2 = []
        batch3 = [
            {
                "update_id": 29,
                "message": {
                    "message_id": 7,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "stale",
                },
            },
            {
                "update_id": 32,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 300},
                    "from": {"id": 400},
                    "text": "fresh",
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "cursor-state.json"
            store = DurableCursorStateStore(state_path)
            first_api = _ApiStub([batch1, batch2])
            first_adapter = TelegramChannelAdapter(first_api, cursor_state_store=store)

            first_fetch = first_adapter.fetch_updates()
            self.assertEqual([item.update_id for item in first_fetch], ["30", "31"])
            self.assertEqual(first_adapter._next_offset, 30)

            first_adapter.ack_update("30")
            self.assertEqual(first_adapter._next_offset, 31)

            first_adapter.ack_update("31")
            self.assertEqual(first_adapter._next_offset, 32)

            first_adapter.fetch_updates()
            self.assertEqual(first_api.offset_calls, [None, 32])

            second_api = _ApiStub([batch3])
            second_adapter = TelegramChannelAdapter(second_api, cursor_state_store=store)

            self.assertEqual(second_adapter._next_offset, 32)
            second_fetch = second_adapter.fetch_updates()
            self.assertEqual([item.update_id for item in second_fetch], ["32"])
            self.assertEqual(second_api.offset_calls, [32])

    def test_stale_updates_below_committed_floor_are_dropped_with_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableCursorStateStore(Path(tmp) / "cursor-state.json")
            store.save(committed_floor=50)
            batch = [
                {
                    "update_id": 49,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 100},
                        "from": {"id": 200},
                        "text": "stale",
                    },
                },
                {
                    "update_id": 50,
                    "message": {
                        "message_id": 2,
                        "chat": {"id": 100},
                        "from": {"id": 200},
                        "text": "current",
                    },
                },
            ]
            api = _ApiStub([batch])
            adapter = TelegramChannelAdapter(api, cursor_state_store=store)

            updates = adapter.fetch_updates()
            self.assertEqual([item.update_id for item in updates], ["50"])

            diagnostics = adapter.drain_diagnostics()
            stale_entries = [item for item in diagnostics if item.get("code") == "stale-drop"]
            self.assertEqual(len(stale_entries), 1)
            self.assertEqual(stale_entries[0].get("update_id"), "49")

    def test_monotonic_floor_never_regresses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableCursorStateStore(Path(tmp) / "cursor-state.json")
            store.save(committed_floor=100)
            batch = [
                {
                    "update_id": 95,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 100},
                        "from": {"id": 200},
                        "text": "stale",
                    },
                },
                {
                    "update_id": 101,
                    "message": {
                        "message_id": 2,
                        "chat": {"id": 100},
                        "from": {"id": 200},
                        "text": "fresh",
                    },
                },
            ]
            api = _ApiStub([batch])
            adapter = TelegramChannelAdapter(api, cursor_state_store=store)

            updates = adapter.fetch_updates()
            self.assertEqual([item.update_id for item in updates], ["101"])
            self.assertEqual(adapter._next_offset, 101)
            adapter.ack_update("101")
            self.assertEqual(adapter._next_offset, 102)
            self.assertEqual(store.load().committed_floor, 102)

    def test_state_load_failure_is_non_fatal_by_default(self) -> None:
        batch = []
        api = _ApiStub([batch])
        adapter = TelegramChannelAdapter(api, cursor_state_store=_StateStoreFailureStub(fail_load=True))

        updates = adapter.fetch_updates()
        self.assertEqual(updates, [])
        diagnostics = adapter.drain_diagnostics()
        self.assertTrue(any(item.get("code") == "cursor-state-load-error" for item in diagnostics))

    def test_state_save_failure_is_non_fatal_by_default(self) -> None:
        batch = [
            {
                "update_id": 41,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 100},
                    "from": {"id": 200},
                    "text": "one",
                },
            }
        ]
        api = _ApiStub([batch])
        adapter = TelegramChannelAdapter(api, cursor_state_store=_StateStoreFailureStub(fail_save=True))

        updates = adapter.fetch_updates()
        self.assertEqual([item.update_id for item in updates], ["41"])
        adapter.ack_update("41")
        diagnostics = adapter.drain_diagnostics()
        self.assertTrue(any(item.get("code") == "cursor-state-save-error" for item in diagnostics))

    def test_state_io_failure_raises_in_strict_mode(self) -> None:
        batch = []
        api = _ApiStub([batch])
        with self.assertRaises(ChannelRuntimeError) as ctx:
            TelegramChannelAdapter(
                api,
                cursor_state_store=_StateStoreFailureStub(fail_load=True),
                strict_state_io=True,
            )
        self.assertIn("cursor_state_load failed", str(ctx.exception))

    def test_ack_update_rejects_non_numeric_ids(self) -> None:
        adapter = TelegramChannelAdapter(_ApiStub([[]]))

        for bad_update_id in ("", "  ", "abc", "10.5"):
            with self.subTest(update_id=bad_update_id):
                with self.assertRaises(ChannelRuntimeError) as ctx:
                    adapter.ack_update(bad_update_id)
                self.assertIn("ack_update requires a numeric update_id", str(ctx.exception))

    def test_skipped_only_fetch_advances_offset(self) -> None:
        batch = [
            {"update_id": 70, "edited_message": {"text": "ignored"}},
            {
                "update_id": 71,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 100},
                    "from": {"id": 200},
                    "text": "   ",
                },
            },
        ]
        api = _ApiStub([batch, []])
        adapter = TelegramChannelAdapter(api)

        first = adapter.fetch_updates()
        self.assertEqual(first, [])
        self.assertEqual(adapter._next_offset, 72)

        second = adapter.fetch_updates()
        self.assertEqual(second, [])
        self.assertEqual(api.offset_calls, [None, 72])


if __name__ == "__main__":
    unittest.main()
