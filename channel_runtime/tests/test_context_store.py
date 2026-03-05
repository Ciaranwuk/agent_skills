from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_runtime.context.contracts import ContextTurn
from channel_runtime.context.errors import ContextStoreError
from channel_runtime.context.store import ContextStore


class TestContextStore(unittest.TestCase):
    def test_append_and_load_transcript_persists_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            now_value = datetime(2026, 3, 4, 12, 0, 0, tzinfo=timezone.utc)

            store_a = ContextStore(root_dir=root, strict_io=False, now_utc=lambda: now_value)
            store_a.append_turn(
                session_id="telegram:123",
                turn=ContextTurn(role="user", text="hello", turn_id="u-1", metadata={"chat_id": "123"}),
            )
            store_a.append_turn(
                session_id="telegram:123",
                turn=ContextTurn(role="assistant", text="hi there", turn_id="a-1"),
            )

            store_b = ContextStore(root_dir=root, strict_io=False, now_utc=lambda: now_value)
            turns = store_b.load_transcript(session_id="telegram:123")

            self.assertEqual(len(turns), 2)
            self.assertEqual(turns[0].role, "user")
            self.assertEqual(turns[0].text, "hello")
            self.assertEqual(turns[0].turn_id, "u-1")
            self.assertEqual(turns[1].role, "assistant")
            self.assertEqual(turns[1].text, "hi there")
            self.assertEqual(turns[1].turn_id, "a-1")

            metadata = store_b.load_session_metadata(session_id="telegram:123")
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(metadata.schema_version, 1)
            self.assertEqual(metadata.session_id, "telegram:123")
            self.assertEqual(metadata.turn_count, 2)
            self.assertEqual(metadata.last_entry_id, "a-1")
            self.assertEqual(metadata.chat_id, "123")
            self.assertEqual(metadata.transcript_path, "transcripts/telegram%3A123.jsonl")

    def test_strict_mode_raises_store_error_for_malformed_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            transcript_dir = root / "transcripts"
            transcript_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = transcript_dir / "telegram%3A123.jsonl"

            transcript_path.write_text(
                '{"schema_version":1,"entry_id":"1","session_id":"telegram:123","timestamp":"2026-03-04T12:00:00Z","type":"user","text":"hello"}\n'
                '{"schema_version":1,"entry_id":"2","session_id":"telegram:123"\n',
                encoding="utf-8",
            )

            store = ContextStore(root_dir=root, strict_io=True)
            with self.assertRaises(ContextStoreError) as ctx:
                store.load_transcript(session_id="telegram:123")

            self.assertEqual(ctx.exception.spec.code, "context-store-load-error")
            self.assertEqual(ctx.exception.spec.operation, "load_transcript")
            self.assertIn("line=2", str(ctx.exception))
            self.assertIn("malformed json", str(ctx.exception))

    def test_non_strict_mode_skips_malformed_lines_and_records_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            transcript_dir = root / "transcripts"
            transcript_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = transcript_dir / "telegram%3A456.jsonl"

            transcript_path.write_text(
                '{"schema_version":1,"entry_id":"1","session_id":"telegram:456","timestamp":"2026-03-04T12:00:00Z","type":"user","text":"first"}\n'
                'not-json\n'
                '{"schema_version":1,"entry_id":"3","session_id":"telegram:456","timestamp":"2026-03-04T12:00:02Z","type":"assistant","text":"third"}\n',
                encoding="utf-8",
            )

            store = ContextStore(root_dir=root, strict_io=False)
            turns = store.load_transcript(session_id="telegram:456")

            self.assertEqual(len(turns), 2)
            self.assertEqual(turns[0].text, "first")
            self.assertEqual(turns[1].text, "third")

            diagnostics = store.diagnostics()
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0].code, "context-store-malformed-line")
            self.assertEqual(diagnostics[0].session_id, "telegram:456")
            self.assertEqual(diagnostics[0].line_number, 2)
            self.assertIn("malformed json", diagnostics[0].message)


if __name__ == "__main__":
    unittest.main()
