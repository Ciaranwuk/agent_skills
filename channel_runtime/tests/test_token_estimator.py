from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_runtime.context.contracts import ContextTurn
from channel_runtime.context.errors import ContextEstimatorError
from channel_runtime.context.token_estimator import TokenEstimator, TokenEstimatorPolicy


class TestTokenEstimator(unittest.TestCase):
    def test_estimate_turn_uses_chars_over_4_plus_overhead_and_safety(self) -> None:
        estimator = TokenEstimator()
        estimate = estimator.estimate_turn(turn=ContextTurn(role="user", text="abcd1234"))
        self.assertEqual(estimate, 10)

    def test_estimate_window_is_deterministic_with_default_constants(self) -> None:
        estimator = TokenEstimator()
        turns = (
            ContextTurn(role="user", text="abcd1234"),
            ContextTurn(role="assistant", text="xyz"),
        )
        self.assertEqual(estimator.estimate_window(turns=turns), 32)

    def test_estimate_assembled_window_counts_user_and_assistant_text(self) -> None:
        estimator = TokenEstimator()
        assembled = (
            {"user_text": "hello", "assistant_text": "response-ok"},
            {"user_text": "follow", "assistant_text": None},
        )
        self.assertEqual(estimator.estimate_assembled_window(conversation_history=assembled), 43)

    def test_compute_pressure_reports_under_budget(self) -> None:
        estimator = TokenEstimator()
        pressure = estimator.compute_pressure(
            estimated_tokens=32,
            context_window_tokens=100,
            reserve_tokens=20,
        )
        self.assertEqual(pressure.available_tokens, 80)
        self.assertEqual(pressure.overflow_tokens, 0)
        self.assertFalse(pressure.is_over_budget)
        self.assertEqual(pressure.pressure_ratio, 0.4)

    def test_compute_pressure_reports_over_budget(self) -> None:
        estimator = TokenEstimator()
        pressure = estimator.compute_pressure(
            estimated_tokens=90,
            context_window_tokens=100,
            reserve_tokens=20,
        )
        self.assertEqual(pressure.available_tokens, 80)
        self.assertEqual(pressure.overflow_tokens, 10)
        self.assertTrue(pressure.is_over_budget)
        self.assertEqual(pressure.pressure_ratio, 1.125)

    def test_custom_policy_allows_multiplier_override(self) -> None:
        estimator = TokenEstimator(
            policy=TokenEstimatorPolicy(
                chars_per_token=4,
                turn_overhead_tokens=6,
                window_overhead_tokens=12,
                safety_multiplier=1.0,
            )
        )
        turns = (
            ContextTurn(role="user", text="abcd1234"),
            ContextTurn(role="assistant", text="xyz"),
        )
        self.assertEqual(estimator.estimate_window(turns=turns), 27)

    def test_invalid_pressure_inputs_raise_context_estimator_error(self) -> None:
        estimator = TokenEstimator()
        with self.assertRaisesRegex(ContextEstimatorError, "context_window_tokens must be greater than reserve_tokens"):
            estimator.compute_pressure(estimated_tokens=10, context_window_tokens=100, reserve_tokens=100)

        with self.assertRaisesRegex(ContextEstimatorError, "estimated_tokens must be >= 0"):
            estimator.compute_pressure(estimated_tokens=-1, context_window_tokens=100, reserve_tokens=0)

    def test_invalid_policy_inputs_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "chars_per_token must be >= 1"):
            TokenEstimatorPolicy(chars_per_token=0)
        with self.assertRaisesRegex(ValueError, "safety_multiplier must be a finite number > 0"):
            TokenEstimatorPolicy(safety_multiplier=0.0)


if __name__ == "__main__":
    unittest.main()
