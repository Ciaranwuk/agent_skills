from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from urllib import error

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_channel.api import TelegramApiClient, TelegramApiError
from telegram_channel.update_parser import parse_update


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TestUpdateParser(unittest.TestCase):
    def test_parse_message_text_preserves_required_fields(self) -> None:
        update = {
            "update_id": 701,
            "message": {
                "message_id": 42,
                "date": "1700000001",
                "chat": {"id": -100123},
                "from": {"id": 991},
                "text": "hello",
            },
        }

        parsed = parse_update(update)

        self.assertIsNotNone(parsed.inbound)
        assert parsed.inbound is not None
        self.assertEqual(parsed.update_id, "701")
        self.assertEqual(parsed.inbound.update_id, "701")
        self.assertEqual(parsed.inbound.message_id, "42")
        self.assertEqual(parsed.inbound.chat_id, "-100123")
        self.assertEqual(parsed.inbound.user_id, "991")
        self.assertEqual(parsed.inbound.timestamp_s, 1700000001)
        self.assertEqual(parsed.inbound.text, "hello")

    def test_parse_unsupported_update_type_is_safe_skip(self) -> None:
        parsed = parse_update({"update_id": 9, "edited_message": {"text": "changed"}})
        self.assertIsNone(parsed.inbound)
        self.assertEqual(parsed.skip_reason, "unsupported-update-type")

    def test_parse_media_only_message_is_safe_skip(self) -> None:
        parsed = parse_update(
            {
                "update_id": 11,
                "message": {
                    "message_id": 77,
                    "chat": {"id": 10},
                    "from": {"id": 20},
                    "photo": [{"file_id": "abc"}],
                },
            }
        )
        self.assertIsNone(parsed.inbound)
        self.assertEqual(parsed.skip_reason, "unsupported-message-text")

    def test_parse_invalid_update_id_is_safe_skip(self) -> None:
        parsed = parse_update({"update_id": " ", "message": {"text": "x"}})
        self.assertIsNone(parsed.inbound)
        self.assertEqual(parsed.skip_reason, "invalid-update-id")

    def test_parse_missing_chat_or_user_is_safe_skip(self) -> None:
        no_chat = parse_update(
            {
                "update_id": 12,
                "message": {
                    "message_id": 77,
                    "from": {"id": 20},
                    "text": "hello",
                },
            }
        )
        self.assertIsNone(no_chat.inbound)
        self.assertEqual(no_chat.skip_reason, "missing-chat-or-user-id")

        no_user = parse_update(
            {
                "update_id": 13,
                "message": {
                    "message_id": 78,
                    "chat": {"id": 10},
                    "text": "hello",
                },
            }
        )
        self.assertIsNone(no_user.inbound)
        self.assertEqual(no_user.skip_reason, "missing-chat-or-user-id")

    def test_parse_coercion_edges_for_optional_fields(self) -> None:
        parsed = parse_update(
            {
                "update_id": " 91 ",
                "message": {
                    "message_id": "   ",
                    "date": "not-an-int",
                    "chat": {"id": "  -200  "},
                    "from": {"id": " 300 "},
                    "text": "hi",
                },
            }
        )

        self.assertIsNotNone(parsed.inbound)
        assert parsed.inbound is not None
        self.assertEqual(parsed.update_id, "91")
        self.assertEqual(parsed.inbound.chat_id, "-200")
        self.assertEqual(parsed.inbound.user_id, "300")
        self.assertIsNone(parsed.inbound.message_id)
        self.assertIsNone(parsed.inbound.timestamp_s)


class TestTelegramApiClient(unittest.TestCase):
    def test_get_updates_retries_transient_failures_then_succeeds(self) -> None:
        calls = {"count": 0}
        sleeps: list[float] = []

        def opener(req, timeout):
            calls["count"] += 1
            if calls["count"] < 3:
                raise error.URLError("network down")
            return _FakeResponse(b'{"ok":true,"result":[{"update_id":1}]}')

        client = TelegramApiClient(
            token="abc",
            max_retries=2,
            backoff_seconds=(0.0, 0.1),
            opener=opener,
            sleeper=sleeps.append,
        )

        updates = client.get_updates(offset=9)

        self.assertEqual(calls["count"], 3)
        self.assertEqual(sleeps, [0.0, 0.1])
        self.assertEqual(updates[0]["update_id"], 1)

    def test_get_updates_structured_error_after_retry_budget_exhausted(self) -> None:
        calls = {"count": 0}
        sleeps: list[float] = []

        def opener(req, timeout):
            calls["count"] += 1
            raise error.URLError("timeout")

        client = TelegramApiClient(
            token="abc",
            max_retries=1,
            backoff_seconds=(0.25,),
            opener=opener,
            sleeper=sleeps.append,
        )

        with self.assertRaises(TelegramApiError) as ctx:
            client.get_updates()

        exc = ctx.exception
        self.assertEqual(calls["count"], 2)
        self.assertEqual(exc.operation, "getUpdates")
        self.assertEqual(exc.kind, "network-error")
        self.assertTrue(exc.transient)
        self.assertEqual(exc.retry_class, "transient")
        self.assertEqual(sleeps, [0.25])

    def test_http_400_is_non_transient_and_not_retried(self) -> None:
        calls = {"count": 0}

        def opener(req, timeout):
            calls["count"] += 1
            payload = b'{"ok":false,"error_code":400,"description":"Bad Request"}'
            raise error.HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(payload))

        client = TelegramApiClient(token="abc", max_retries=3, opener=opener, backoff_seconds=(0.0, 0.0, 0.0))

        with self.assertRaises(TelegramApiError) as ctx:
            client.get_updates()

        self.assertEqual(calls["count"], 1)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(ctx.exception.transient)
        self.assertEqual(ctx.exception.retry_class, "non-retryable")

    def test_http_429_retry_after_hint_is_used_for_retry_delay(self) -> None:
        calls = {"count": 0}
        sleeps: list[float] = []

        def opener(req, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                payload = b'{"ok":false,"error_code":429,"description":"Too Many Requests","parameters":{"retry_after":2}}'
                raise error.HTTPError(req.full_url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(payload))
            return _FakeResponse(b'{"ok":true,"result":[{"update_id":5}]}')

        client = TelegramApiClient(
            token="abc",
            max_retries=1,
            backoff_seconds=(9.0,),
            opener=opener,
            sleeper=sleeps.append,
        )

        updates = client.get_updates()

        self.assertEqual(calls["count"], 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(updates[0]["update_id"], 5)

    def test_api_error_429_without_retry_after_falls_back_to_backoff(self) -> None:
        calls = {"count": 0}
        sleeps: list[float] = []

        def opener(req, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                return _FakeResponse(b'{"ok":false,"error_code":429,"description":"Too Many Requests"}')
            return _FakeResponse(b'{"ok":true,"result":[{"update_id":6}]}')

        client = TelegramApiClient(
            token="abc",
            max_retries=1,
            backoff_seconds=(0.4,),
            opener=opener,
            sleeper=sleeps.append,
        )

        updates = client.get_updates()

        self.assertEqual(calls["count"], 2)
        self.assertEqual(sleeps, [0.4])
        self.assertEqual(updates[0]["update_id"], 6)

    def test_http_429_retry_after_header_is_parsed(self) -> None:
        calls = {"count": 0}
        sleeps: list[float] = []

        def opener(req, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                payload = b'{"ok":false,"error_code":429,"description":"Too Many Requests"}'
                raise error.HTTPError(
                    req.full_url,
                    429,
                    "Too Many Requests",
                    hdrs={"Retry-After": "1.5"},
                    fp=io.BytesIO(payload),
                )
            return _FakeResponse(b'{"ok":true,"result":[{"update_id":7}]}')

        client = TelegramApiClient(
            token="abc",
            max_retries=1,
            backoff_seconds=(7.7,),
            opener=opener,
            sleeper=sleeps.append,
        )

        updates = client.get_updates()

        self.assertEqual(calls["count"], 2)
        self.assertEqual(sleeps, [1.5])
        self.assertEqual(updates[0]["update_id"], 7)

    def test_invalid_json_response_is_structured_and_not_retried(self) -> None:
        calls = {"count": 0}

        def opener(req, timeout):
            calls["count"] += 1
            return _FakeResponse(b"{invalid")

        client = TelegramApiClient(token="abc", max_retries=2, opener=opener, backoff_seconds=(0.0, 0.0))

        with self.assertRaises(TelegramApiError) as ctx:
            client.get_updates()

        self.assertEqual(calls["count"], 1)
        self.assertEqual(ctx.exception.kind, "invalid-json")
        self.assertFalse(ctx.exception.transient)

    def test_invalid_response_shape_is_structured_and_not_retried(self) -> None:
        calls = {"count": 0}

        def opener(req, timeout):
            calls["count"] += 1
            return _FakeResponse(b"[]")

        client = TelegramApiClient(token="abc", max_retries=3, opener=opener, backoff_seconds=(0.0, 0.0, 0.0))

        with self.assertRaises(TelegramApiError) as ctx:
            client.get_updates()

        self.assertEqual(calls["count"], 1)
        self.assertEqual(ctx.exception.kind, "invalid-response-shape")
        self.assertFalse(ctx.exception.transient)

    def test_ok_false_transient_error_retries_then_succeeds(self) -> None:
        calls = {"count": 0}
        sleeps: list[float] = []

        def opener(req, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                return _FakeResponse(b'{"ok":false,"error_code":500,"description":"Server exploded"}')
            return _FakeResponse(b'{"ok":true,"result":[{"update_id":22}]}')

        client = TelegramApiClient(
            token="abc",
            max_retries=1,
            opener=opener,
            sleeper=sleeps.append,
            backoff_seconds=(0.3,),
        )

        updates = client.get_updates()

        self.assertEqual(calls["count"], 2)
        self.assertEqual(sleeps, [0.3])
        self.assertEqual(updates[0]["update_id"], 22)

    def test_ok_false_non_retryable_error_fails_without_retry(self) -> None:
        calls = {"count": 0}

        def opener(req, timeout):
            calls["count"] += 1
            return _FakeResponse(b'{"ok":false,"error_code":400,"description":"Bad Request"}')

        client = TelegramApiClient(token="abc", max_retries=2, opener=opener, backoff_seconds=(0.0, 0.0))

        with self.assertRaises(TelegramApiError) as ctx:
            client.get_updates()

        self.assertEqual(calls["count"], 1)
        self.assertEqual(ctx.exception.kind, "api-error")
        self.assertEqual(ctx.exception.retry_class, "non-retryable")
        self.assertFalse(ctx.exception.transient)


if __name__ == "__main__":
    unittest.main()
