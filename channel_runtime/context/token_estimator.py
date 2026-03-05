from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .contracts import ContextEstimatorPort, ContextTurn
from .errors import ContextEstimatorError

ConversationTurn = Mapping[str, str | None]


@dataclass(frozen=True)
class TokenEstimatorPolicy:
    """Deterministic policy inputs for heuristic token estimation."""

    chars_per_token: int = 4
    turn_overhead_tokens: int = 6
    window_overhead_tokens: int = 12
    safety_multiplier: float = 1.15

    def __post_init__(self) -> None:
        if int(self.chars_per_token) < 1:
            raise ValueError("chars_per_token must be >= 1")
        if int(self.turn_overhead_tokens) < 0:
            raise ValueError("turn_overhead_tokens must be >= 0")
        if int(self.window_overhead_tokens) < 0:
            raise ValueError("window_overhead_tokens must be >= 0")
        if not math.isfinite(float(self.safety_multiplier)) or float(self.safety_multiplier) <= 0:
            raise ValueError("safety_multiplier must be a finite number > 0")


@dataclass(frozen=True)
class ContextPressure:
    """Deterministic context pressure snapshot for a single estimate."""

    estimated_tokens: int
    available_tokens: int
    pressure_ratio: float
    overflow_tokens: int
    is_over_budget: bool


class TokenEstimator(ContextEstimatorPort):
    """Baseline deterministic token estimation and pressure math."""

    def __init__(self, *, policy: TokenEstimatorPolicy | None = None) -> None:
        self._policy = policy or TokenEstimatorPolicy()

    @property
    def policy(self) -> TokenEstimatorPolicy:
        return self._policy

    def estimate_turn(self, *, turn: ContextTurn) -> int:
        text_tokens = _estimate_text_tokens(text=turn.text, chars_per_token=self._policy.chars_per_token)
        raw_tokens = text_tokens + self._policy.turn_overhead_tokens
        return _apply_safety(raw_tokens=raw_tokens, safety_multiplier=self._policy.safety_multiplier)

    def estimate_window(self, *, turns: tuple[ContextTurn, ...]) -> int:
        if not isinstance(turns, tuple):
            raise ContextEstimatorError("turns must be a tuple")
        raw_tokens = self._policy.window_overhead_tokens
        for turn in turns:
            raw_tokens += self.estimate_turn_raw(turn=turn)
        return _apply_safety(raw_tokens=raw_tokens, safety_multiplier=self._policy.safety_multiplier)

    def estimate_assembled_window(self, *, conversation_history: tuple[ConversationTurn, ...]) -> int:
        if not isinstance(conversation_history, tuple):
            raise ContextEstimatorError("conversation_history must be a tuple")
        turns: list[ContextTurn] = []
        for pair in conversation_history:
            if not isinstance(pair, Mapping):
                raise ContextEstimatorError("each conversation history item must be a mapping")
            user_text = str(pair.get("user_text") or "")
            assistant_text = pair.get("assistant_text")
            if user_text:
                turns.append(ContextTurn(role="user", text=user_text))
            if assistant_text is not None and str(assistant_text):
                turns.append(ContextTurn(role="assistant", text=str(assistant_text)))
        return self.estimate_window(turns=tuple(turns))

    def estimate_window_pressure(
        self,
        *,
        turns: tuple[ContextTurn, ...],
        context_window_tokens: int,
        reserve_tokens: int,
    ) -> ContextPressure:
        estimated_tokens = self.estimate_window(turns=turns)
        return self.compute_pressure(
            estimated_tokens=estimated_tokens,
            context_window_tokens=context_window_tokens,
            reserve_tokens=reserve_tokens,
        )

    def compute_pressure(
        self,
        *,
        estimated_tokens: int,
        context_window_tokens: int,
        reserve_tokens: int,
    ) -> ContextPressure:
        estimated_value = _require_nonnegative_int(estimated_tokens, field_name="estimated_tokens")
        context_window_value = _require_positive_int(context_window_tokens, field_name="context_window_tokens")
        reserve_value = _require_nonnegative_int(reserve_tokens, field_name="reserve_tokens")
        if context_window_value <= reserve_value:
            raise ContextEstimatorError("context_window_tokens must be greater than reserve_tokens")

        available_tokens = context_window_value - reserve_value
        overflow_tokens = max(0, estimated_value - available_tokens)
        pressure_ratio = estimated_value / available_tokens
        return ContextPressure(
            estimated_tokens=estimated_value,
            available_tokens=available_tokens,
            pressure_ratio=pressure_ratio,
            overflow_tokens=overflow_tokens,
            is_over_budget=overflow_tokens > 0,
        )

    def estimate_turn_raw(self, *, turn: ContextTurn) -> int:
        text_tokens = _estimate_text_tokens(text=turn.text, chars_per_token=self._policy.chars_per_token)
        return text_tokens + self._policy.turn_overhead_tokens


def _estimate_text_tokens(*, text: str, chars_per_token: int) -> int:
    char_count = len(str(text or ""))
    if char_count == 0:
        return 0
    return math.ceil(char_count / chars_per_token)


def _apply_safety(*, raw_tokens: int, safety_multiplier: float) -> int:
    return int(math.ceil(int(raw_tokens) * float(safety_multiplier)))


def _require_positive_int(value: int, *, field_name: str) -> int:
    normalized = int(value)
    if normalized < 1:
        raise ContextEstimatorError(f"{field_name} must be >= 1")
    return normalized


def _require_nonnegative_int(value: int, *, field_name: str) -> int:
    normalized = int(value)
    if normalized < 0:
        raise ContextEstimatorError(f"{field_name} must be >= 0")
    return normalized


__all__ = [
    "ContextPressure",
    "TokenEstimator",
    "TokenEstimatorPolicy",
]
