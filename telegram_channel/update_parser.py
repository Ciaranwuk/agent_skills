from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from channel_core.contracts import InboundMessage


@dataclass(frozen=True)
class ParsedTelegramUpdate:
    """Normalized parser output for one Telegram update."""

    update_id: str | None
    inbound: InboundMessage | None
    skip_reason: str | None = None


def parse_update(raw_update: Mapping[str, Any]) -> ParsedTelegramUpdate:
    """Parse text messages and normalize Telegram voice notes into inbound metadata."""
    update_id = _coerce_required_id(raw_update.get("update_id"))
    if update_id is None:
        return ParsedTelegramUpdate(update_id=None, inbound=None, skip_reason="invalid-update-id")

    callback = raw_update.get("callback_query")
    callback_sender: Mapping[str, Any] | None = None
    if isinstance(callback, Mapping):
        message = callback.get("message")
        callback_sender = callback.get("from") if isinstance(callback.get("from"), Mapping) else None
        callback_data = callback.get("data")
        if isinstance(callback_data, str) and callback_data.startswith("trade:"):
            raw_trade = callback_data.split(":", 1)[1].strip()
            if raw_trade.isdigit():
                message = dict(message) if isinstance(message, Mapping) else {}
                message["text"] = f"/trade {raw_trade}"
                raw_update = dict(raw_update)
                raw_update["message"] = message
                raw_update["_telegram_callback_query_id"] = _coerce_required_id(callback.get("id"))
            else:
                return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="invalid-callback-data")
        else:
            return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="unsupported-callback-data")
    else:
        message = raw_update.get("message")
    if not isinstance(message, Mapping):
        return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="unsupported-update-type")

    chat = message.get("chat")
    sender = callback_sender or message.get("from")
    chat_id = _coerce_required_id(chat.get("id") if isinstance(chat, Mapping) else None)
    user_id = _coerce_required_id(sender.get("id") if isinstance(sender, Mapping) else None)
    if chat_id is None or user_id is None:
        return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="missing-chat-or-user-id")

    text = message.get("text")
    metadata: dict[str, Any] = {"source": "telegram", "update_type": "message", "content_type": "text"}
    callback_query_id = raw_update.get("_telegram_callback_query_id")
    if callback_query_id:
        metadata["telegram_callback_query_id"] = callback_query_id
    inbound_text = text
    if not isinstance(text, str) or not text.strip():
        voice = message.get("voice")
        if not isinstance(voice, Mapping):
            return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="unsupported-message-text")

        file_id = _coerce_required_id(voice.get("file_id"))
        if file_id is None:
            return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="unsupported-message-voice")

        inbound_text = "[Voice note]"
        metadata["content_type"] = "voice"
        metadata["telegram_voice"] = {
            "file_id": file_id,
            "file_unique_id": _coerce_optional_id(voice.get("file_unique_id")),
            "duration_s": _coerce_optional_int(voice.get("duration")),
            "mime_type": _coerce_optional_id(voice.get("mime_type")),
            "file_size": _coerce_optional_int(voice.get("file_size")),
        }

    inbound = InboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        text=str(inbound_text),
        message_id=_coerce_optional_id(message.get("message_id")),
        timestamp_s=_coerce_optional_int(message.get("date")),
        metadata=metadata,
    )
    return ParsedTelegramUpdate(update_id=update_id, inbound=inbound, skip_reason=None)


def _coerce_required_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _coerce_optional_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
