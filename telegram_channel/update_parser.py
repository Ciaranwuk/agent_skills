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
    """Parse only message.text updates; safely skip all unsupported payloads."""
    update_id = _coerce_required_id(raw_update.get("update_id"))
    if update_id is None:
        return ParsedTelegramUpdate(update_id=None, inbound=None, skip_reason="invalid-update-id")

    message = raw_update.get("message")
    if not isinstance(message, Mapping):
        return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="unsupported-update-type")

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="unsupported-message-text")

    chat = message.get("chat")
    sender = message.get("from")
    chat_id = _coerce_required_id(chat.get("id") if isinstance(chat, Mapping) else None)
    user_id = _coerce_required_id(sender.get("id") if isinstance(sender, Mapping) else None)
    if chat_id is None or user_id is None:
        return ParsedTelegramUpdate(update_id=update_id, inbound=None, skip_reason="missing-chat-or-user-id")

    inbound = InboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        message_id=_coerce_optional_id(message.get("message_id")),
        timestamp_s=_coerce_optional_int(message.get("date")),
        metadata={"source": "telegram", "update_type": "message"},
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
