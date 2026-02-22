from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory_system.api import memory_get, memory_search
from memory_system.index import FileEntry, MemoryIndex
from memory_system import index as memory_index_mod


class MemorySystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "memory").mkdir(parents=True, exist_ok=True)
        (self.workspace / "MEMORY.md").write_text("# Root Memory\nalpha beta\n", encoding="utf-8")
        (self.workspace / "memory" / "notes.md").write_text(
            "first line\nsecond line with token\nthird line\n", encoding="utf-8"
        )
        (self.workspace / "notes-outside-memory.md").write_text("outside memory folder token\n", encoding="utf-8")
        self.db_path = self.workspace / ".memory_index.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_search_unavailable_without_index(self) -> None:
        payload = memory_search("alpha", workspace=self.workspace, db_path=self.db_path)
        self.assertEqual(payload["results"], [])
        self.assertTrue(payload["disabled"])
        self.assertTrue(payload["unavailable"])
        self.assertIn("memory index unavailable", payload["error"])

    def test_search_success_contract(self) -> None:
        index = MemoryIndex(self.workspace, self.db_path)
        index.sync(force=True)

        payload = memory_search("token", workspace=self.workspace, db_path=self.db_path)
        self.assertEqual(payload["provider"], "builtin")
        self.assertEqual(payload["citations"], "off")
        self.assertEqual(payload["mode"], "fts-only")
        self.assertGreaterEqual(len(payload["results"]), 1)
        first = payload["results"][0]
        self.assertTrue(first["path"].endswith(".md"))
        self.assertEqual(first["source"], "memory")
        self.assertGreaterEqual(first["score"], 0)

    def test_memory_get_full_and_slice(self) -> None:
        full = memory_get("memory/notes.md", workspace=self.workspace)
        self.assertIn("second line", full["text"])

        sliced = memory_get("memory/notes.md", from_=2, lines=1, workspace=self.workspace)
        self.assertEqual(sliced["text"], "second line with token\n")

        sliced_from_alias = memory_get("memory/notes.md", workspace=self.workspace, **{"from": 2, "lines": 1})
        self.assertEqual(sliced_from_alias["text"], "second line with token\n")

    def test_memory_get_missing_allowed_path(self) -> None:
        payload = memory_get("missing.md", workspace=self.workspace)
        self.assertEqual(payload, {"path": "missing.md", "text": ""})

    def test_memory_get_denies_forbidden_paths(self) -> None:
        traversal = memory_get("../etc/passwd", workspace=self.workspace)
        self.assertEqual(traversal["error"], "path not allowed")

        non_md = memory_get("any/test.txt", workspace=self.workspace)
        self.assertEqual(non_md["error"], "path not allowed")

        sessions = memory_get("sessions/log.jsonl", workspace=self.workspace)
        self.assertEqual(sessions["error"], "path not allowed")

    def test_memory_get_denies_symlink_file(self) -> None:
        target = self.workspace / "memory" / "linked.md"
        target.symlink_to(self.workspace / "memory" / "notes.md")
        payload = memory_get("memory/linked.md", workspace=self.workspace)
        self.assertEqual(payload["error"], "path not allowed")

    def test_incremental_and_stale_cleanup(self) -> None:
        index = MemoryIndex(self.workspace, self.db_path)
        first = index.sync(force=True)
        self.assertEqual(first["indexed"], 3)

        second = index.sync(force=False)
        self.assertEqual(second["indexed"], 0)
        self.assertEqual(second["unchanged"], 3)

        (self.workspace / "memory" / "notes.md").unlink()
        third = index.sync(force=False)
        self.assertEqual(third["removed"], 1)

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM files WHERE path='memory/notes.md'").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_sync_handles_file_deleted_during_reindex(self) -> None:
        class VanishingIndex(MemoryIndex):
            def _build_manifest(self):  # type: ignore[override]
                missing = self.workspace / "memory" / "gone.md"
                return {
                    "memory/gone.md": FileEntry(
                        path="memory/gone.md",
                        abs_path=missing,
                        mtime=0,
                        size=0,
                        file_hash="0" * 64,
                    )
                }

        index = VanishingIndex(self.workspace, self.db_path)
        stats = index.sync(force=True)
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(stats["removed"], 0)

    def test_missing_fts_table_triggers_rebuild(self) -> None:
        index = MemoryIndex(self.workspace, self.db_path)
        index.sync(force=True)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP TABLE chunks_fts")
            conn.commit()
        finally:
            conn.close()

        stats = index.sync(force=False)
        self.assertEqual(stats["indexed"], 3)

    def test_sync_single_flight_returns_in_progress(self) -> None:
        index = MemoryIndex(self.workspace, self.db_path)
        lock = memory_index_mod._get_sync_lock(index.db_path)
        lock.acquire()
        try:
            stats = index.sync(force=False)
        finally:
            lock.release()
        self.assertEqual(stats.get("in_progress"), 1)

    def test_search_and_get_input_bounds(self) -> None:
        index = MemoryIndex(self.workspace, self.db_path)
        index.sync(force=True)

        with self.assertRaises(ValueError):
            memory_search("token", workspace=self.workspace, db_path=self.db_path, maxResults=0)

        with self.assertRaises(ValueError):
            memory_search("token", workspace=self.workspace, db_path=self.db_path, maxResults=51)

        with self.assertRaises(ValueError):
            memory_search("token", workspace=self.workspace, db_path=self.db_path, minScore=-0.1)

        with self.assertRaises(ValueError):
            memory_search("token", workspace=self.workspace, db_path=self.db_path, minScore=1.1)

        with self.assertRaises(ValueError):
            memory_get("memory/notes.md", workspace=self.workspace, from_=0, lines=1)

        with self.assertRaises(ValueError):
            memory_get("memory/notes.md", workspace=self.workspace, from_=1, lines=0)

        with self.assertRaises(ValueError):
            memory_get("memory/notes.md", workspace=self.workspace, from_=1, lines=2001)

    def test_multiroot_index_and_get(self) -> None:
        with tempfile.TemporaryDirectory() as vault_dir:
            vault = Path(vault_dir)
            (vault / "decision.md").write_text("vault token document\n", encoding="utf-8")

            index = MemoryIndex(self.workspace, self.db_path, source_roots=[vault])
            stats = index.sync(force=True)
            self.assertEqual(stats["indexed"], 4)

            payload = memory_search("vault token", workspace=self.workspace, db_path=self.db_path, source_roots=[vault])
            self.assertGreaterEqual(len(payload["results"]), 1)
            first = payload["results"][0]
            self.assertTrue(first["path"].startswith("root1:"))
            self.assertTrue(first["path"].endswith("decision.md"))

            get_payload = memory_get(first["path"], workspace=self.workspace, source_roots=[vault])
            self.assertIn("vault token document", get_payload["text"])


if __name__ == "__main__":
    unittest.main()
