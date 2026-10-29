import unittest

from scripts.workflow_lane_sources import SEEDS, matching_seeds, source_pack_metadata


class WorkflowLaneSourceTests(unittest.TestCase):
    def test_pack_has_four_bounded_lanes(self):
        metadata = source_pack_metadata()
        self.assertEqual(set(metadata["lanes"]), {seed.lane for seed in SEEDS})
        self.assertFalse(metadata["web_retrieval"])
        self.assertEqual(metadata["max_candidates_per_lane"], 1)

    def test_conceptual_terms_reach_non_operational_lanes(self):
        text = "strategy factory research papers; backtest dataset provenance; agent architecture contract"
        lanes = {seed.lane for seed in matching_seeds(text)}
        self.assertEqual(lanes, {"strategy_research", "backtest_data_reproducibility", "agent_workflow_architecture"})

    def test_old_generic_morning_notes_do_not_create_seed(self):
        self.assertEqual(matching_seeds("reviewed the morning checklist"), [])


if __name__ == "__main__":
    unittest.main()
