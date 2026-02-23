from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class HeartbeatRequest:
    prompt: str
    reason: str
    now_ms: int
    session_key: str
    system_events: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HeartbeatResponse:
    text: str
    media_urls: list[str] = field(default_factory=list)
    reasoning: str | None = None
    raw: dict[str, Any] | None = None


class HeartbeatResponder(Protocol):
    def respond(self, request: HeartbeatRequest) -> HeartbeatResponse:
        """Return a heartbeat response for a single request."""
