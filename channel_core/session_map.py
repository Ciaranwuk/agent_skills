from __future__ import annotations

from .contracts import ContractValidationError, InboundMessage


def telegram_session_id(chat_id: str | int) -> str:
    """Default stable session mapping for Telegram-style chat IDs."""
    value = str(chat_id).strip()
    if not value:
        raise ContractValidationError("chat_id must be a non-empty string")
    return f"telegram:{value}"


def session_id_for_inbound(inbound: InboundMessage) -> str:
    """Resolve the default session identifier for one inbound message."""
    return telegram_session_id(inbound.chat_id)
