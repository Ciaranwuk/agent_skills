from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ContractValidationError(ValueError):
    """Raised when a channel contract object has invalid required fields."""


class ConfigValidationError(ValueError):
    """Raised when runtime configuration cannot be validated."""


class ChannelRuntimeError(RuntimeError):
    """Raised for deterministic service/runtime failures."""


@dataclass(frozen=True)
class InboundMessage:
    """Provider-agnostic inbound message contract."""

    update_id: str
    chat_id: str
    user_id: str
    text: str
    message_id: str | None = None
    timestamp_s: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.update_id).strip():
            raise ContractValidationError("update_id must be a non-empty string")
        if not str(self.chat_id).strip():
            raise ContractValidationError("chat_id must be a non-empty string")
        if not str(self.user_id).strip():
            raise ContractValidationError("user_id must be a non-empty string")
        if not str(self.text).strip():
            raise ContractValidationError("text must be a non-empty string")


@dataclass(frozen=True)
class OutboundMessage:
    """Provider-agnostic outbound message contract."""

    chat_id: str
    text: str
    reply_to_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.chat_id).strip():
            raise ContractValidationError("chat_id must be a non-empty string")
        if not str(self.text).strip():
            raise ContractValidationError("text must be a non-empty string")


class OrchestratorPort(Protocol):
    """Port for business/orchestration logic."""

    def handle_message(self, inbound: InboundMessage, *, session_id: str) -> OutboundMessage | None:
        """Return zero or one outbound message for one inbound."""


class ChannelAdapterPort(Protocol):
    """Transport adapter port that core service uses."""

    def fetch_updates(self) -> list[InboundMessage]:
        """Return the next batch of normalized inbound messages."""

    def send_message(self, outbound: OutboundMessage) -> None:
        """Deliver one outbound message."""

    def ack_update(self, update_id: str) -> None:
        """Acknowledge update processing completion."""
