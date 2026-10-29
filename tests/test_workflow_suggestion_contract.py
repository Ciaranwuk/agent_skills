import unittest

from scripts.workflow_suggestion_contract import (
    CONTRACT_VERSION,
    LANES,
    V1_AUTHORITY,
    build_workflow_suggestion,
    classify_lane,
)


class WorkflowSuggestionContractTests(unittest.TestCase):
    def test_each_v1_lane_is_reachable_from_local_terms(self) -> None:
        examples = {
            "operational_workflow": "manual morning checklist handoff",
            "strategy_research": "research paper strategy hypothesis",
            "backtest_data_reproducibility": "backtest dataset provenance seed",
            "agent_workflow_architecture": "agent worker architecture contract",
        }
        self.assertEqual(tuple(examples), LANES)
        for expected, text in examples.items():
            with self.subTest(expected=expected):
                diagnostic = classify_lane(text)
                self.assertEqual(diagnostic.selected_lane, expected)
                self.assertEqual(diagnostic.code, "local_terms_matched")

    def test_known_category_wins_over_ambiguous_terms(self) -> None:
        diagnostic = classify_lane(
            "agent architecture research", category_hint="manual_loop_reduction"
        )
        self.assertEqual(diagnostic.selected_lane, "operational_workflow")
        self.assertEqual(diagnostic.code, "category_hint_matched")

    def test_envelope_is_versioned_sorted_and_side_effect_free(self) -> None:
        payload = build_workflow_suggestion(
            title="Capture backtest provenance",
            hypothesis="A dataset snapshot will reproduce the backtest.",
            metric="One reproducible result",
            next_action="Review a local-note checklist.",
            safety_boundary="proposal only",
            source_notes=["b.md", "a.md", "b.md"],
            evidence=[{"note_path": "a.md", "line": 2, "snippet": "dataset snapshot"}],
        )
        self.assertEqual(payload["contract_version"], CONTRACT_VERSION)
        self.assertEqual(payload["lane"], "backtest_data_reproducibility")
        self.assertEqual(payload["suggestion"]["source_notes"], ["a.md", "b.md"])
        self.assertEqual(payload["diagnostics"]["input_scope"], "local_notes_only")
        self.assertEqual(payload["diagnostics"]["side_effects"], "proposal_artifact_only")
        self.assertEqual(payload["diagnostics"]["authority"], V1_AUTHORITY)
        self.assertFalse(any(payload["diagnostics"]["authority"].values()))
        self.assertEqual(payload["diagnostics"]["source_note_count"], 2)
        self.assertEqual(payload["diagnostics"]["evidence_count"], 1)

    def test_no_term_match_has_deterministic_conservative_diagnostic(self) -> None:
        first = classify_lane("Improve the recurring thing")
        second = classify_lane("Improve the recurring thing")
        self.assertEqual(first, second)
        self.assertEqual(first.selected_lane, "operational_workflow")
        self.assertEqual(first.code, "defaulted_operational")
        self.assertEqual(first.matched_terms, ())
        self.assertEqual(first.lane_scores, {lane: 0 for lane in LANES})


if __name__ == "__main__":
    unittest.main()
