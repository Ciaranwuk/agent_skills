from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_channel.cursor_state import CursorStateError, DurableCursorStateStore


class TestDurableCursorStateStore(unittest.TestCase):
    def test_load_missing_file_returns_none_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableCursorStateStore(Path(tmp) / "cursor-state.json")
            snapshot = store.load()
            self.assertIsNone(snapshot.committed_floor)

    def test_save_then_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursor-state.json"
            store = DurableCursorStateStore(path)
            store.save(committed_floor=77)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["committed_floor"], 77)
            self.assertEqual(store.load().committed_floor, 77)

    def test_load_invalid_json_raises_deterministic_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursor-state.json"
            path.write_text("{", encoding="utf-8")
            store = DurableCursorStateStore(path)

            with self.assertRaises(CursorStateError) as ctx:
                store.load()
            self.assertEqual(ctx.exception.kind, "state-load-json")

    def test_save_rejects_negative_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableCursorStateStore(Path(tmp) / "cursor-state.json")
            with self.assertRaises(CursorStateError) as ctx:
                store.save(committed_floor=-1)
            self.assertEqual(ctx.exception.kind, "state-save-floor")


if __name__ == "__main__":
    unittest.main()
