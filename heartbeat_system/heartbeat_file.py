"""Heartbeat prompt file helpers for deterministic preflight checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HeartbeatPromptLoad:
    """Represents loaded heartbeat prompt content and emptiness status."""

    path: str
    text: str
    is_empty: bool


def is_heartbeat_content_empty(text: str | None) -> bool:
    """True when content is missing, empty, or whitespace-only."""
    return text is None or text.strip() == ""


def load_heartbeat_prompt(path: str | Path) -> HeartbeatPromptLoad:
    """
    Load HEARTBEAT prompt content from disk.

    Missing files are treated as empty. Other IO errors are raised to caller.
    """
    heartbeat_path = Path(path)
    try:
        text = heartbeat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    return HeartbeatPromptLoad(
        path=str(heartbeat_path),
        text=text,
        is_empty=is_heartbeat_content_empty(text),
    )

