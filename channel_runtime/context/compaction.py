from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

from .assembler import ContextAssembler, ConversationTurn
from .contracts import ContextStorePort, ContextTurn
from .errors import ContextCompactionError
from .token_estimator import ContextPressure, TokenEstimator


@dataclass(frozen=True)
class CompactionPolicy:
    """Deterministic policy inputs for threshold-triggered context compaction."""

    context_window_tokens: int
    reserve_tokens: int
    keep_recent_tokens: int
    min_compaction_gain_tokens: int
    cooldown_window_s: float

    def __post_init__(self) -> None:
        if int(self.context_window_tokens) < 1:
            raise ValueError("context_window_tokens must be >= 1")
        if int(self.reserve_tokens) < 0:
            raise ValueError("reserve_tokens must be >= 0")
        if int(self.keep_recent_tokens) < 1:
            raise ValueError("keep_recent_tokens must be >= 1")
        if int(self.min_compaction_gain_tokens) < 0:
            raise ValueError("min_compaction_gain_tokens must be >= 0")
        if float(self.cooldown_window_s) < 0:
            raise ValueError("cooldown_window_s must be >= 0")
        if int(self.context_window_tokens) <= int(self.reserve_tokens):
            raise ValueError("context_window_tokens must be greater than reserve_tokens")


CompactionStatus = Literal["compacted", "skipped", "failed"]


@dataclass(frozen=True)
class CompactionResult:
    """Deterministic outcome for one compaction evaluation pass."""

    status: CompactionStatus
    reason: str
    pressure_before: ContextPressure
    estimated_tokens_before: int
    estimated_tokens_after: int
    gained_tokens: int
    conversation_history: tuple[ConversationTurn, ...]
    transcript_turns: tuple[ContextTurn, ...]


class CompactionService:
    """Applies threshold compaction with cooldown and min-gain guards."""

    def __init__(
        self,
        *,
        store: ContextStorePort,
        assembler: ContextAssembler | None = None,
        estimator: TokenEstimator | None = None,
        now_s: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._estimator = estimator or TokenEstimator()
        self._assembler = assembler or ContextAssembler(token_estimator=self._estimator)
        self._now_s = now_s or (lambda: datetime.now(timezone.utc).timestamp())

    def evaluate_and_compact(
        self,
        *,
        session_id: str,
        policy: CompactionPolicy,
    ) -> CompactionResult:
        turns: tuple[ContextTurn, ...] = ()
        pressure: ContextPressure | None = None
        before_tokens = 0
        try:
            turns = self._store.load_transcript(session_id=session_id)
            assembled = self._assembler.assemble_with_estimate(
                session_id=session_id,
                turns=turns,
                context_window_tokens=policy.context_window_tokens,
                reserve_tokens=policy.reserve_tokens,
            )
            pressure = assembled.pressure
            if pressure is None:
                raise ContextCompactionError("failed to compute pre-compaction pressure", retryable=False)

            before_tokens = int(pressure.estimated_tokens)
            if not pressure.is_over_budget:
                return self._result(
                    status="skipped",
                    reason="below-threshold",
                    pressure_before=pressure,
                    estimated_tokens_before=before_tokens,
                    estimated_tokens_after=before_tokens,
                    transcript_turns=turns,
                    session_id=session_id,
                )

            cooldown_remaining = self._cooldown_remaining_s(turns=turns, cooldown_window_s=policy.cooldown_window_s)
            if cooldown_remaining > 0:
                return self._result(
                    status="skipped",
                    reason="cooldown-active",
                    pressure_before=pressure,
                    estimated_tokens_before=before_tokens,
                    estimated_tokens_after=before_tokens,
                    transcript_turns=turns,
                    session_id=session_id,
                )

            recent_turns = _select_recent_raw_turns(
                turns=turns,
                keep_recent_tokens=policy.keep_recent_tokens,
                estimator=self._estimator,
            )
            dropped_turns = _derive_dropped_turns(turns=turns, kept_recent_turns=recent_turns)
            summary_text = _build_compaction_summary_text(dropped_turns=dropped_turns)
            compacted_turn = ContextTurn(
                role="compaction",
                text=summary_text,
                metadata={
                    "compaction_reason": "threshold",
                    "tokens_before": str(before_tokens),
                    "dropped_turns": str(len(dropped_turns)),
                    "kept_recent_turns": str(len(recent_turns)),
                },
            )
            compacted_turns = (compacted_turn, *recent_turns)
            estimated_after = self._estimator.estimate_window(turns=compacted_turns)
            gained_tokens = max(0, before_tokens - estimated_after)
            if gained_tokens < int(policy.min_compaction_gain_tokens):
                return self._result(
                    status="skipped",
                    reason="min-gain",
                    pressure_before=pressure,
                    estimated_tokens_before=before_tokens,
                    estimated_tokens_after=before_tokens,
                    transcript_turns=turns,
                    session_id=session_id,
                )

            compacted_turn = ContextTurn(
                role="compaction",
                text=summary_text,
                metadata={
                    "compaction_reason": "threshold",
                    "tokens_before": str(before_tokens),
                    "tokens_after": str(estimated_after),
                    "tokens_gained": str(gained_tokens),
                    "dropped_turns": str(len(dropped_turns)),
                    "kept_recent_turns": str(len(recent_turns)),
                },
            )
            compacted_turns = (compacted_turn, *recent_turns)
            self._store.replace_transcript(session_id=session_id, turns=compacted_turns)
            persisted_turns = self._store.load_transcript(session_id=session_id)
            return self._result(
                status="compacted",
                reason="threshold",
                pressure_before=pressure,
                estimated_tokens_before=before_tokens,
                estimated_tokens_after=estimated_after,
                transcript_turns=persisted_turns,
                session_id=session_id,
            )
        except Exception:
            return self._failed_result(
                session_id=session_id,
                policy=policy,
                turns=turns,
                pressure_before=pressure,
                estimated_tokens_before=before_tokens,
            )

    def _failed_result(
        self,
        *,
        session_id: str,
        policy: CompactionPolicy,
        turns: tuple[ContextTurn, ...],
        pressure_before: ContextPressure | None,
        estimated_tokens_before: int,
    ) -> CompactionResult:
        fallback_turns = _deterministic_fallback_turns(
            turns=turns,
            keep_recent_tokens=policy.keep_recent_tokens,
            estimator=self._estimator,
        )
        fallback_estimated_tokens = int(estimated_tokens_before)
        try:
            fallback_estimated_tokens = self._estimator.estimate_window(turns=fallback_turns)
        except Exception:
            fallback_estimated_tokens = int(estimated_tokens_before)

        pressure = pressure_before or ContextPressure(
            estimated_tokens=max(0, int(estimated_tokens_before)),
            context_window_tokens=int(policy.context_window_tokens),
            reserve_tokens=int(policy.reserve_tokens),
            available_tokens=max(0, int(policy.context_window_tokens) - int(policy.reserve_tokens)),
            is_over_budget=max(0, int(estimated_tokens_before))
            > max(0, int(policy.context_window_tokens) - int(policy.reserve_tokens)),
            overflow_tokens=max(
                0,
                max(0, int(estimated_tokens_before))
                - max(0, int(policy.context_window_tokens) - int(policy.reserve_tokens)),
            ),
        )
        return self._result(
            status="failed",
            reason="compaction-error",
            pressure_before=pressure,
            estimated_tokens_before=max(0, int(estimated_tokens_before)),
            estimated_tokens_after=max(0, int(fallback_estimated_tokens)),
            transcript_turns=fallback_turns,
            session_id=session_id,
        )

    def _result(
        self,
        *,
        status: CompactionStatus,
        reason: str,
        pressure_before: ContextPressure,
        estimated_tokens_before: int,
        estimated_tokens_after: int,
        transcript_turns: tuple[ContextTurn, ...],
        session_id: str,
    ) -> CompactionResult:
        history = self._assembler.assemble_conversation_history(session_id=session_id, turns=transcript_turns)
        return CompactionResult(
            status=status,
            reason=reason,
            pressure_before=pressure_before,
            estimated_tokens_before=int(estimated_tokens_before),
            estimated_tokens_after=int(estimated_tokens_after),
            gained_tokens=max(0, int(estimated_tokens_before) - int(estimated_tokens_after)),
            conversation_history=history,
            transcript_turns=transcript_turns,
        )

    def _cooldown_remaining_s(self, *, turns: tuple[ContextTurn, ...], cooldown_window_s: float) -> float:
        cooldown_window = float(cooldown_window_s)
        if cooldown_window <= 0:
            return 0.0
        now_value = float(self._now_s())
        latest_compaction_at_s: float | None = None
        for turn in turns:
            source_type = str(turn.metadata.get("source_type", "")).strip().lower()
            if source_type != "compaction":
                continue
            if turn.created_at_s is None:
                continue
            if latest_compaction_at_s is None or float(turn.created_at_s) > latest_compaction_at_s:
                latest_compaction_at_s = float(turn.created_at_s)
        if latest_compaction_at_s is None:
            return 0.0
        return max(0.0, cooldown_window - max(0.0, now_value - latest_compaction_at_s))


def _select_recent_raw_turns(
    *,
    turns: tuple[ContextTurn, ...],
    keep_recent_tokens: int,
    estimator: TokenEstimator,
) -> tuple[ContextTurn, ...]:
    keep_budget = int(keep_recent_tokens)
    selected_reversed: list[ContextTurn] = []
    consumed_tokens = 0
    for turn in reversed(turns):
        if turn.role not in {"user", "assistant"}:
            continue
        turn_tokens = estimator.estimate_turn(turn=turn)
        projected = consumed_tokens + turn_tokens
        if selected_reversed and projected > keep_budget:
            break
        selected_reversed.append(turn)
        consumed_tokens = projected
    return tuple(reversed(selected_reversed))


def _derive_dropped_turns(
    *,
    turns: tuple[ContextTurn, ...],
    kept_recent_turns: tuple[ContextTurn, ...],
) -> tuple[ContextTurn, ...]:
    kept_ids = {id(turn) for turn in kept_recent_turns}
    dropped: list[ContextTurn] = []
    for turn in turns:
        if id(turn) in kept_ids:
            continue
        dropped.append(turn)
    return tuple(dropped)


def _build_compaction_summary_text(*, dropped_turns: tuple[ContextTurn, ...]) -> str:
    lines: list[str] = ["Compaction Summary", f"Dropped turns: {len(dropped_turns)}"]
    if not dropped_turns:
        lines.append("- No dropped turns.")
        return "\n".join(lines)

    user_dropped = sum(1 for turn in dropped_turns if _summary_role(turn) == "user")
    assistant_dropped = sum(1 for turn in dropped_turns if _summary_role(turn) == "assistant")
    compaction_dropped = sum(1 for turn in dropped_turns if _summary_role(turn) == "compaction")
    lines.append(f"- User turns dropped: {user_dropped}")
    lines.append(f"- Assistant turns dropped: {assistant_dropped}")
    if compaction_dropped:
        lines.append(f"- Prior compaction entries dropped: {compaction_dropped}")

    latest_user = _latest_text_for_role(dropped_turns=dropped_turns, role="user")
    latest_assistant = _latest_text_for_role(dropped_turns=dropped_turns, role="assistant")
    if latest_user:
        lines.append(f"- Latest dropped user: {latest_user}")
    if latest_assistant:
        lines.append(f"- Latest dropped assistant: {latest_assistant}")
    return "\n".join(lines)


def _summary_role(turn: ContextTurn) -> str:
    if str(turn.metadata.get("source_type", "")).strip().lower() == "compaction":
        return "compaction"
    role = str(turn.role or "").strip().lower()
    if role in {"user", "assistant", "system"}:
        return role
    return "unknown"


def _latest_text_for_role(*, dropped_turns: tuple[ContextTurn, ...], role: str) -> str:
    target_role = str(role).strip().lower()
    for turn in reversed(dropped_turns):
        if _summary_role(turn) != target_role:
            continue
        text = " ".join(str(turn.text or "").split())
        if not text:
            return ""
        if len(text) <= 80:
            return text
        return f"{text[:77]}..."
    return ""


def _deterministic_fallback_turns(
    *,
    turns: tuple[ContextTurn, ...],
    keep_recent_tokens: int,
    estimator: TokenEstimator,
) -> tuple[ContextTurn, ...]:
    try:
        selected = _select_recent_raw_turns(
            turns=turns,
            keep_recent_tokens=keep_recent_tokens,
            estimator=estimator,
        )
    except Exception:
        selected = ()
    if selected:
        return selected
    raw_turns = tuple(turn for turn in turns if str(turn.role).strip().lower() in {"user", "assistant"})
    if not raw_turns:
        return ()
    return raw_turns[-2:]


__all__ = [
    "CompactionPolicy",
    "CompactionResult",
    "CompactionService",
]
