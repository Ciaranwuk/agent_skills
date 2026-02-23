from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HeartbeatConfig:
    """Minimal Phase 0/1 heartbeat configuration."""

    enabled: bool = True
    heartbeat_file: str = "HEARTBEAT.md"
    ack_token: str = "HEARTBEAT_OK"
    ack_max_chars: int = 300
    include_reasoning: bool = False

    def __post_init__(self) -> None:
        heartbeat_file = (self.heartbeat_file or "").strip()
        ack_token = (self.ack_token or "").strip()
        if not heartbeat_file:
            raise ValueError("heartbeat_file must be a non-empty string")
        if not ack_token:
            raise ValueError("ack_token must be a non-empty string")
        if self.ack_max_chars < 0:
            raise ValueError("ack_max_chars must be >= 0")
