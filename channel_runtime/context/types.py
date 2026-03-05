from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ContextSessionMetadata:
    """Lightweight per-session metadata persisted alongside transcript JSONL."""

    schema_version: int
    session_id: str
    transcript_path: str
    created_at: str
    updated_at: str
    turn_count: int
    last_entry_id: str = ""
    chat_id: str = ""


DiagnosticCode = Literal[
    "context-store-malformed-line",
    "context-store-invalid-record",
    "context-store-session-mismatch",
]


@dataclass(frozen=True)
class ContextStoreDiagnostic:
    """Deterministic diagnostic emitted when non-strict parsing skips a line."""

    code: DiagnosticCode
    session_id: str
    line_number: int
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "session_id": self.session_id,
            "line_number": self.line_number,
            "message": self.message,
        }
