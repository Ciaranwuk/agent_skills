from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory_system.api import memory_get, memory_rebuild, memory_search
from memory_system.backend import BuiltinSearchBackend, QmdSearchBackend, SearchBackend
from memory_system.cli import build_parser, main as cli_main
from memory_system.index import FileEntry, MemoryIndex
from memory_system import index as memory_index_mod
from memory_system.paths import build_qmd_collections
from memory_system.qmd_adapter import QmdCommandResult, QmdLifecycleAdapter, QmdRuntimeStatus, _build_qmd_env


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

    def test_search_builtin_backend_alias_preserves_contract(self) -> None:
        index = MemoryIndex(self.workspace, self.db_path)
        index.sync(force=True)

        payload = memory_search("token", workspace=self.workspace, db_path=self.db_path, backend="builtin")
        self.assertEqual(payload["provider"], "builtin")
        self.assertEqual(payload["citations"], "off")
        self.assertEqual(payload["mode"], "fts-only")
        self.assertGreaterEqual(len(payload["results"]), 1)

    def test_search_none_backend_matches_builtin_default(self) -> None:
        index = MemoryIndex(self.workspace, self.db_path)
        index.sync(force=True)

        default_payload = memory_search("token", workspace=self.workspace, db_path=self.db_path)
        builtin_payload = memory_search("token", workspace=self.workspace, db_path=self.db_path, backend="builtin")
        self.assertEqual(default_payload, builtin_payload)

    def test_search_allows_backend_injection(self) -> None:
        class StubBackend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int, float]] = []

            def search(self, query: str, *, max_results: int, min_score: float) -> dict[str, object]:
                self.calls.append((query, max_results, min_score))
                return {
                    "results": [],
                    "provider": "stub",
                    "model": "test-model",
                    "citations": "off",
                    "mode": "stubbed",
                }

        stub: SearchBackend = StubBackend()
        payload = memory_search(
            " token ",
            workspace=self.workspace,
            db_path=self.db_path,
            maxResults=7,
            minScore=0.25,
            backend=stub,
        )
        self.assertEqual(
            payload,
            {
                "results": [],
                "provider": "stub",
                "model": "test-model",
                "citations": "off",
                "mode": "stubbed",
            },
        )
        self.assertEqual(stub.calls, [("token", 7, 0.25)])

    def test_search_rejects_unknown_backend_name(self) -> None:
        with self.assertRaises(ValueError):
            memory_search("token", workspace=self.workspace, db_path=self.db_path, backend="invalid")

    def test_search_qmd_backend_falls_back_to_builtin_when_runtime_unavailable(self) -> None:
        index = MemoryIndex(self.workspace, self.db_path)
        index.sync(force=True)

        payload = memory_search("token", workspace=self.workspace, db_path=self.db_path, backend="qmd")
        self.assertEqual(payload["provider"], "builtin")
        self.assertEqual(payload["mode"], "fts-only")
        self.assertIn("fallback", payload)
        self.assertEqual(payload["fallback"]["requestedProvider"], "qmd")
        self.assertIn("qmd binary not found on PATH", payload["fallback"]["details"])
        self.assertGreaterEqual(len(payload["results"]), 1)

    def test_qmd_backend_without_fallback_returns_deterministic_unavailable_payload(self) -> None:
        backend = QmdSearchBackend(
            workspace=self.workspace,
            db_path=self.db_path,
            enable_fallback=False,
        )
        payload = backend.search("token", max_results=5, min_score=0.0)
        self.assertEqual(payload["provider"], "qmd")
        self.assertEqual(payload["mode"], "hybrid")
        self.assertTrue(payload["disabled"])
        self.assertTrue(payload["unavailable"])
        self.assertEqual(
            payload["action"],
            "Install QMD prerequisites or retry memory_search with backend='builtin'.",
        )
        self.assertIn("qmd binary not found on PATH", payload["details"])

    def test_search_rejects_invalid_backend_type(self) -> None:
        with self.assertRaises(TypeError):
            memory_search("token", workspace=self.workspace, db_path=self.db_path, backend=object())

    def test_qmd_backend_status_reports_runtime_diagnostics(self) -> None:
        backend = QmdSearchBackend(workspace=self.workspace, db_path=self.db_path)
        status = backend.status()
        self.assertEqual(status.provider, "qmd")
        self.assertFalse(status.available)
        self.assertIsNotNone(status.reason)
        self.assertTrue(any(detail.startswith("qmd binary") for detail in status.details))

    def test_build_qmd_env_uses_workspace_local_runtime_dirs(self) -> None:
        env = _build_qmd_env(self.workspace)
        runtime_root = self.workspace / ".qmd"
        self.assertEqual(env["HOME"], str(runtime_root / "home"))
        self.assertEqual(env["XDG_CONFIG_HOME"], str(runtime_root / "xdg-config"))
        self.assertEqual(env["XDG_CACHE_HOME"], str(runtime_root / "xdg-cache"))
        self.assertEqual(env["XDG_DATA_HOME"], str(runtime_root / "xdg-data"))
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime_root / "xdg-runtime"))
        self.assertTrue((runtime_root / "home").is_dir())
        self.assertTrue((runtime_root / "xdg-config").is_dir())
        self.assertTrue((runtime_root / "xdg-cache").is_dir())
        self.assertTrue((runtime_root / "xdg-data").is_dir())
        self.assertTrue((runtime_root / "xdg-runtime").is_dir())

    def test_build_qmd_env_overrides_user_profile_dirs(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "HOME": "/home/test-user",
                "XDG_CONFIG_HOME": "/home/test-user/.config",
                "XDG_CACHE_HOME": "/home/test-user/.cache",
                "XDG_DATA_HOME": "/home/test-user/.local/share",
                "XDG_RUNTIME_DIR": "/run/user/9999",
            },
            clear=False,
        ):
            env = _build_qmd_env(self.workspace)

        runtime_root = self.workspace / ".qmd"
        self.assertEqual(env["HOME"], str(runtime_root / "home"))
        self.assertEqual(env["XDG_CONFIG_HOME"], str(runtime_root / "xdg-config"))
        self.assertEqual(env["XDG_CACHE_HOME"], str(runtime_root / "xdg-cache"))
        self.assertEqual(env["XDG_DATA_HOME"], str(runtime_root / "xdg-data"))
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime_root / "xdg-runtime"))

    def test_search_rejects_blank_query(self) -> None:
        with self.assertRaises(ValueError):
            memory_search("   ", workspace=self.workspace, db_path=self.db_path)

    def test_builtin_backend_runtime_failure_returns_unavailable_payload(self) -> None:
        class RaisingIndex:
            @staticmethod
            def is_available() -> tuple[bool, str | None]:
                return True, None

            @staticmethod
            def search(query: str, max_results: int = 10, min_score: float = 0.0) -> list[dict[str, object]]:
                raise RuntimeError("boom")

        backend = BuiltinSearchBackend(workspace=self.workspace, db_path=self.db_path)
        backend._index = RaisingIndex()  # type: ignore[assignment]

        payload = backend.search("token", max_results=5, min_score=0.0)
        self.assertEqual(payload["results"], [])
        self.assertTrue(payload["disabled"])
        self.assertTrue(payload["unavailable"])
        self.assertEqual(payload["warning"], "Memory search is unavailable due to local index unavailability.")
        self.assertEqual(payload["action"], "Rebuild local memory index and retry memory_search.")
        self.assertIn("boom", payload["error"])

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

    def test_memory_get_allows_workspace_relative_markdown_with_colon(self) -> None:
        target = self.workspace / "memory" / "a:b.md"
        target.write_text("colon path token\n", encoding="utf-8")
        payload = memory_get("memory/a:b.md", workspace=self.workspace)
        self.assertEqual(payload["path"], "memory/a:b.md")
        self.assertIn("colon path token", payload["text"])

    def test_memory_get_denies_symlink_file(self) -> None:
        target = self.workspace / "memory" / "linked.md"
        target.symlink_to(self.workspace / "memory" / "notes.md")
        payload = memory_get("memory/linked.md", workspace=self.workspace)
        self.assertEqual(payload["error"], "path not allowed")

    def test_memory_rebuild_builtin_preserves_contract(self) -> None:
        payload = memory_rebuild(workspace=self.workspace, db_path=self.db_path, force=True)
        self.assertEqual(payload, {"ok": True, "stats": {"indexed": 3, "removed": 0, "unchanged": 0}})

    def test_memory_rebuild_qmd_unavailable_returns_actionable_payload(self) -> None:
        payload = memory_rebuild(workspace=self.workspace, backend="qmd")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["backend"], "qmd")
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["action"], "Install QMD prerequisites, then rerun rebuild --backend qmd, or retry with backend='builtin'.")
        self.assertEqual(payload["warning"], "QMD rebuild is unavailable due to runtime prerequisites or adapter failures.")
        self.assertEqual(payload["stats"]["collections_failed"], 0)
        self.assertIn("qmd binary not found on PATH", payload["details"])

    def test_qmd_lifecycle_adapter_rebuild_uses_status_add_update_and_embed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tmp") as extra_dir:
            extra_root = Path(extra_dir)
            collections = build_qmd_collections(self.workspace, [extra_root])
            workspace_collection = collections[0].name
            extra_collection = collections[1].name

            class RunnerStub:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, ...]] = []

                def run(self, args: list[str], *, cwd: Path, timeout_s: float) -> QmdCommandResult:
                    command = tuple(args)
                    self.calls.append(command)
                    if command[:3] == ("qmd", "collection", "add"):
                        if command[5] == extra_collection:
                            return QmdCommandResult(args=command, exit_code=0, stdout="added", stderr="")
                        return QmdCommandResult(args=command, exit_code=1, stdout="", stderr="collection already exists")
                    return QmdCommandResult(args=command, exit_code=0, stdout="ok", stderr="")

            runner = RunnerStub()
            adapter = QmdLifecycleAdapter(workspace=self.workspace, source_roots=[extra_root], runner=runner)

            with mock.patch.object(adapter, "status") as status_mock:
                status_mock.return_value = QmdRuntimeStatus(
                    available=True,
                    reason=None,
                    details=(),
                    qmd_binary="/usr/bin/qmd",
                    js_runtime="/usr/bin/node",
                )
                payload = adapter.rebuild(force=True)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["backend"], "qmd")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["stats"]["collections_total"], 2)
        self.assertEqual(payload["stats"]["collections_added"], 1)
        self.assertEqual(payload["stats"]["collections_existing"], 1)
        self.assertEqual(payload["stats"]["update_ran"], 1)
        self.assertEqual(payload["stats"]["embed_ran"], 1)

        calls = runner.calls
        self.assertEqual(calls[0][:6], ("qmd", "collection", "add", str(self.workspace.resolve()), "--name", workspace_collection))
        self.assertEqual(calls[1][:6], ("qmd", "collection", "add", str(extra_root.resolve()), "--name", extra_collection))
        self.assertEqual(calls[2], ("qmd", "update"))
        self.assertEqual(calls[3], ("qmd", "embed", "-f"))
        self.assertEqual(calls[4], ("qmd", "status"))

    def test_qmd_lifecycle_adapter_rebuild_surfaces_command_failure(self) -> None:
        class RunnerStub:
            def run(self, args: list[str], *, cwd: Path, timeout_s: float) -> QmdCommandResult:
                command = tuple(args)
                if command[:2] == ("qmd", "embed"):
                    return QmdCommandResult(args=command, exit_code=9, stdout="", stderr="embed exploded")
                return QmdCommandResult(args=command, exit_code=0, stdout="ok", stderr="")

        adapter = QmdLifecycleAdapter(workspace=self.workspace, runner=RunnerStub())
        with mock.patch.object(adapter, "status") as status_mock:
            status_mock.return_value = QmdRuntimeStatus(
                available=True,
                reason=None,
                details=(),
                qmd_binary="/usr/bin/qmd",
                js_runtime="/usr/bin/node",
            )
            payload = adapter.rebuild(force=False)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["stats"]["collections_failed"], 1)
        self.assertIn("qmd embed failed", payload["error"])

    def test_qmd_lifecycle_adapter_rebuild_surfaces_status_failure(self) -> None:
        class RunnerStub:
            def run(self, args: list[str], *, cwd: Path, timeout_s: float) -> QmdCommandResult:
                command = tuple(args)
                if command[:2] == ("qmd", "status"):
                    return QmdCommandResult(args=command, exit_code=7, stdout="", stderr="status exploded")
                return QmdCommandResult(args=command, exit_code=0, stdout="ok", stderr="")

        adapter = QmdLifecycleAdapter(workspace=self.workspace, runner=RunnerStub())
        with mock.patch.object(adapter, "status") as status_mock:
            status_mock.return_value = QmdRuntimeStatus(
                available=True,
                reason=None,
                details=(),
                qmd_binary="/usr/bin/qmd",
                js_runtime="/usr/bin/node",
            )
            payload = adapter.rebuild(force=False)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertIn("qmd status failed", payload["error"])

    def test_qmd_lifecycle_adapter_search_normalizes_rows(self) -> None:
        collection_name = build_qmd_collections(self.workspace)[0].name

        class RunnerStub:
            def run(self, args: list[str], *, cwd: Path, timeout_s: float) -> QmdCommandResult:
                command = tuple(args)
                if command[:2] == ("qmd", "query"):
                    payload = json.dumps(
                        [
                            {
                                "collection": collection_name,
                                "path": "memory/notes.md",
                                "score": 0.75,
                                "snippet": "@@ -2,1\nsecond line with token",
                            }
                        ]
                    )
                    return QmdCommandResult(args=command, exit_code=0, stdout=payload, stderr="")
                raise AssertionError(f"unexpected command: {command}")

        adapter = QmdLifecycleAdapter(workspace=self.workspace, runner=RunnerStub())
        with mock.patch.object(adapter, "status") as status_mock:
            status_mock.return_value = QmdRuntimeStatus(
                available=True,
                reason=None,
                details=(),
                qmd_binary="/usr/bin/qmd",
                js_runtime="/usr/bin/node",
            )
            payload = adapter.search("token", max_results=5, min_score=0.0)

        self.assertEqual(payload["provider"], "qmd")
        self.assertEqual(payload["mode"], "hybrid")
        self.assertEqual(payload["results"][0]["path"], "memory/notes.md")
        self.assertEqual(payload["results"][0]["startLine"], 2)
        self.assertEqual(payload["results"][0]["endLine"], 2)
        self.assertEqual(payload["results"][0]["snippet"], "second line with token")

    def test_qmd_lifecycle_adapter_search_supports_multiroot_paths_for_memory_get(self) -> None:
        with tempfile.TemporaryDirectory() as vault_dir:
            vault = Path(vault_dir)
            (vault / "decision.md").write_text("vault decision token\n", encoding="utf-8")
            collection_name = build_qmd_collections(self.workspace, [vault])[1].name

            class RunnerStub:
                def run(self, args: list[str], *, cwd: Path, timeout_s: float) -> QmdCommandResult:
                    payload = json.dumps(
                        [
                            {
                                "collection": collection_name,
                                "path": "decision.md",
                                "score": 0.9,
                                "snippet": "@@ -1,1\nvault decision token",
                            }
                        ]
                    )
                    return QmdCommandResult(args=tuple(args), exit_code=0, stdout=payload, stderr="")

            adapter = QmdLifecycleAdapter(workspace=self.workspace, source_roots=[vault], runner=RunnerStub())
            with mock.patch.object(adapter, "status") as status_mock:
                status_mock.return_value = QmdRuntimeStatus(
                    available=True,
                    reason=None,
                    details=(),
                    qmd_binary="/usr/bin/qmd",
                    js_runtime="/usr/bin/node",
                )
                payload = adapter.search("vault token", max_results=5, min_score=0.0)

            self.assertEqual(payload["results"][0]["path"], "root1:decision.md")
            get_payload = memory_get(payload["results"][0]["path"], workspace=self.workspace, source_roots=[vault])
            self.assertIn("vault decision token", get_payload["text"])

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

    def test_qmd_collection_mapping_is_deterministic_across_root_order(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            forward = build_qmd_collections(self.workspace, [first, second])
            reverse = build_qmd_collections(self.workspace, [second, first])

            forward_map = {item.context["source_root"]: item.name for item in forward}
            reverse_map = {item.context["source_root"]: item.name for item in reverse}
            self.assertEqual(forward_map, reverse_map)
            self.assertEqual(forward_map[str(self.workspace.resolve())], reverse[0].name)

    def test_qmd_collection_mapping_context_tracks_root_type(self) -> None:
        with tempfile.TemporaryDirectory() as vault_dir:
            vault = Path(vault_dir)
            collections = build_qmd_collections(self.workspace, [vault])
            by_alias = {item.root.alias: item for item in collections}
            self.assertEqual(by_alias["workspace"].context["root_type"], "workspace")
            self.assertEqual(by_alias["root1"].context["root_type"], "source-root")
            self.assertEqual(by_alias["root1"].context["source_root"], str(vault.resolve()))

    def test_memory_get_rejects_unknown_root_alias(self) -> None:
        payload = memory_get("root99:notes.md", workspace=self.workspace, source_roots=[self.workspace / "memory"])
        self.assertEqual(payload["error"], "path not allowed")

    def test_cli_parser_accepts_explicit_backend_selection(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--backend", "qmd", "search", "token"])
        self.assertEqual(args.backend, "qmd")
        self.assertEqual(args.command, "search")

    def test_cli_get_rejects_non_builtin_backend(self) -> None:
        with mock.patch.object(sys, "argv", ["memory_system", "--backend", "qmd", "get", "memory/notes.md"]):
            with self.assertRaises(SystemExit) as ctx, mock.patch("sys.stderr", new_callable=StringIO) as stderr:
                cli_main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("backend 'qmd' is not available for get", stderr.getvalue())

    def test_cli_rebuild_rejects_non_builtin_backend(self) -> None:
        with mock.patch.object(sys, "argv", ["memory_system", "--backend", "qmd", "rebuild"]):
            with mock.patch("sys.stdout", new_callable=StringIO) as stdout:
                code = cli_main()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["backend"], "qmd")
        self.assertEqual(payload["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
