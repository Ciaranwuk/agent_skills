from __future__ import annotations

from ..contracts import HeartbeatRequest, HeartbeatResponder, HeartbeatResponse


class NullResponder(HeartbeatResponder):
    """Deterministic no-op responder for tests and offline scaffolding."""

    def __init__(self, *, ack_token: str = "HEARTBEAT_OK", text: str | None = None) -> None:
        token = (ack_token or "").strip()
        if not token:
            raise ValueError("ack_token must be a non-empty string")
        self._ack_token = token
        self._text = text

    def respond(self, request: HeartbeatRequest) -> HeartbeatResponse:
        # Keep response deterministic and provider-agnostic.
        del request
        return HeartbeatResponse(
            text=self._text if self._text is not None else self._ack_token,
            raw={"provider": "null"},
        )
