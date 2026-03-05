from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContextTurn
from .errors import ContextAssemblerError
from .token_estimator import ContextPressure, TokenEstimator

ConversationTurn = dict[str, str | None]
_COMPACTION_SUMMARY_MARKER = "[compaction-summary]"


@dataclass(frozen=True)
class AssembledConversationEstimate:
    """Assembled conversation history with additive token estimate metadata."""

    conversation_history: tuple[ConversationTurn, ...]
    estimated_tokens: int
    pressure: ContextPressure | None = None


class ContextAssembler:
    """Deterministically reconstruct request conversation history from transcript turns."""

    def __init__(
        self,
        *,
        default_max_turns: int | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        if default_max_turns is not None and int(default_max_turns) < 1:
            raise ValueError("default_max_turns must be >= 1 when provided")
        self._default_max_turns = int(default_max_turns) if default_max_turns is not None else None
        self._token_estimator = token_estimator or TokenEstimator()

    def assemble_conversation_history(
        self,
        *,
        session_id: str,
        turns: tuple[ContextTurn, ...],
        max_turns: int | None = None,
    ) -> tuple[ConversationTurn, ...]:
        normalized_session = str(session_id).strip()
        if not normalized_session:
            raise ContextAssemblerError("session_id must be a non-empty string")

        limit = self._default_max_turns if max_turns is None else int(max_turns)
        if limit is not None and limit < 1:
            raise ContextAssemblerError("max_turns must be >= 1 when provided")

        history = _pair_transcript_turns(turns)
        if limit is not None and len(history) > limit:
            history = history[-limit:]
        return tuple(history)

    def assemble_with_estimate(
        self,
        *,
        session_id: str,
        turns: tuple[ContextTurn, ...],
        max_turns: int | None = None,
        context_window_tokens: int | None = None,
        reserve_tokens: int = 0,
    ) -> AssembledConversationEstimate:
        history = self.assemble_conversation_history(
            session_id=session_id,
            turns=turns,
            max_turns=max_turns,
        )
        estimated_tokens = self._token_estimator.estimate_assembled_window(conversation_history=history)
        pressure = None
        if context_window_tokens is not None:
            pressure = self._token_estimator.compute_pressure(
                estimated_tokens=estimated_tokens,
                context_window_tokens=int(context_window_tokens),
                reserve_tokens=int(reserve_tokens),
            )
        return AssembledConversationEstimate(
            conversation_history=history,
            estimated_tokens=estimated_tokens,
            pressure=pressure,
        )


def _pair_transcript_turns(turns: tuple[ContextTurn, ...]) -> list[ConversationTurn]:
    paired: list[ConversationTurn] = []
    pending_user_text: str | None = None

    for turn in turns:
        role = str(turn.role or "").strip().lower()
        text = str(turn.text or "")
        source_type = str(turn.metadata.get("source_type", "")).strip().lower()
        if source_type == "compaction":
            if pending_user_text is not None:
                paired.append({"user_text": pending_user_text, "assistant_text": None})
                pending_user_text = None
            paired.append({"user_text": _COMPACTION_SUMMARY_MARKER, "assistant_text": text})
            continue
        if role == "user":
            if pending_user_text is not None:
                paired.append({"user_text": pending_user_text, "assistant_text": None})
            pending_user_text = text
            continue
        if role != "assistant":
            continue
        if pending_user_text is None:
            continue
        paired.append({"user_text": pending_user_text, "assistant_text": text})
        pending_user_text = None

    if pending_user_text is not None:
        paired.append({"user_text": pending_user_text, "assistant_text": None})
    return paired


__all__ = ["AssembledConversationEstimate", "ContextAssembler"]
