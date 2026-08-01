from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import InboundMessage
from channel_runtime.dinner_selector import (
    DinnerSelectorError,
    build_dinner_command_response,
    load_meal_options,
    select_dinners,
)


def _inbound(update_id: str, *, text: str = "/dinners", chat_id: str = "100") -> InboundMessage:
    return InboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        user_id="u-1",
        text=text,
        message_id=f"m-{update_id}",
    )


class TestDinnerSelector(unittest.TestCase):
    def test_select_dinners_without_repeats_when_pool_is_large_enough(self) -> None:
        selection = select_dinners(meals=("A", "B", "C", "D", "E"), count=4)

        self.assertEqual(len(selection.meals), 4)
        self.assertEqual(len(set(selection.meals)), 4)
        self.assertFalse(selection.expanded_repeats)

    def test_select_dinners_allows_one_duplicate_when_request_is_pool_plus_one(self) -> None:
        selection = select_dinners(meals=("A", "B", "C"), count=4)

        self.assertEqual(len(selection.meals), 4)
        counts = {meal: selection.meals.count(meal) for meal in set(selection.meals)}
        self.assertEqual(sum(1 for value in counts.values() if value == 2), 1)
        self.assertEqual(max(counts.values()), 2)
        self.assertFalse(selection.expanded_repeats)

    def test_select_dinners_marks_expanded_repeats_when_request_exceeds_pool_plus_one(self) -> None:
        selection = select_dinners(meals=("A", "B"), count=5)

        self.assertEqual(len(selection.meals), 5)
        self.assertTrue(selection.expanded_repeats)

    def test_load_meal_options_rejects_missing_file(self) -> None:
        with self.assertRaisesRegex(DinnerSelectorError, "not configured yet"):
            load_meal_options("/tmp/does-not-exist-meals.json")

    def test_build_dinner_command_response_uses_default_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "meal_options.json"
            path.write_text('{"meals":["Chili","Curry","Pasta","Pie","Stew","Soup"]}', encoding="utf-8")

            outbound = build_dinner_command_response(
                inbound=_inbound("1", text="/dinners"),
                session_id="telegram:meal-plan",
                meal_options_path=path,
                orchestrator_mode="codex",
            )

        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertIn("Dinner plan (5 dinners):", outbound.text)
        self.assertEqual(outbound.metadata["orchestrator_mode"], "codex")

    def test_build_dinner_command_response_rejects_invalid_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "meal_options.json"
            path.write_text('{"meals":["Chili","Curry"]}', encoding="utf-8")

            outbound = build_dinner_command_response(
                inbound=_inbound("1", text="/dinners many"),
                session_id="telegram:meal-plan",
                meal_options_path=path,
                orchestrator_mode="default",
            )

        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertEqual(outbound.text, "Usage: /dinners or /dinners <count>")
