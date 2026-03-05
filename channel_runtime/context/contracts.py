from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol


Role = str
SessionId = str
ChatId = str


@dataclass(frozen=True)
class ContextTurn:
    """A single transcript turn used by context assembly and compaction."""

    role: Role
    text: str
    turn_id: str = ""
    created_at_s: float | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextAssemblyRequest:
    """Inputs required to assemble model context for a single inbound message."""

    session_id: SessionId
    chat_id: ChatId
    user_text: str
    max_input_tokens: int | None = None
    max_turns: int | None = None


@dataclass(frozen=True)
class ContextWindow:
    """Assembled context window and deterministic accounting fields."""

    session_id: SessionId
    turns: tuple[ContextTurn, ...]
    estimated_tokens: int
    truncated: bool = False


@dataclass(frozen=True)
class CompactionPlan:
    """Result of compaction planning prior to persistence and prompt assembly."""

    strategy: str
    target_tokens: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    dropped_turns: int = 0
    summary_text: str | None = None


@dataclass(frozen=True)
class CompactionResult:
    """Post-compaction output: revised window + applied plan."""

    window: ContextWindow
    plan: CompactionPlan


class ContextStorePort(Protocol):
    """Durable transcript persistence boundary."""

    def load_transcript(self, *, session_id: SessionId) -> tuple[ContextTurn, ...]:
        ...

    def append_turn(self, *, session_id: SessionId, turn: ContextTurn) -> None:
        ...

    def replace_transcript(self, *, session_id: SessionId, turns: tuple[ContextTurn, ...]) -> None:
        ...


class ContextAssemblerPort(Protocol):
    """Builds the prompt-ready context window from transcript state."""

    def assemble(self, *, request: ContextAssemblyRequest, turns: tuple[ContextTurn, ...]) -> ContextWindow:
        ...


class ContextEstimatorPort(Protocol):
    """Estimates token usage for transcript/context windows."""

    def estimate_turn(self, *, turn: ContextTurn) -> int:
        ...

    def estimate_window(self, *, turns: tuple[ContextTurn, ...]) -> int:
        ...


class ContextCompactionPort(Protocol):
    """Compacts oversized context windows to remain within budget."""

    def compact(self, *, window: ContextWindow, target_tokens: int) -> CompactionResult:
        ...
