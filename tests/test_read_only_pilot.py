import json
import tempfile
import unittest
from pathlib import Path
import importlib.util

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("read_only_pilot", ROOT / "skills/work-operating-system/scripts/run_read_only_pilot.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(module)

class ReadOnlyPilotTests(unittest.TestCase):
    def test_missing_contract_is_prioritized(self):
        proposal, observer = module.build_reports({"work_item_id": "WI-1", "required_evidence": []}, [])
        self.assertEqual(proposal["recommended_next_action"], "complete_missing_contract_fields")
        self.assertEqual(observer["next_state"], "human_approval_required")

    def test_cli_outputs_are_create_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); work = root / "work.json"; out = root / "out"
            work.write_text(json.dumps({"work_item_id": "WI-2", "required_evidence": []}), encoding="utf-8")
            out.mkdir(); (out / "scout_proposal.json").write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                with (out / "scout_proposal.json").open("x", encoding="utf-8"):
                    pass

if __name__ == "__main__":
    unittest.main()
