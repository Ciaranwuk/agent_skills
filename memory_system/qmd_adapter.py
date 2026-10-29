from __future__ import annotations

import json
import os
import posixpath
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .paths import QmdCollection, build_qmd_collections, normalize_source_roots

QMD_REBUILD_ACTION = "Install QMD prerequisites, then rerun rebuild --backend qmd, or retry with backend='builtin'."
QMD_REBUILD_WARNING = "QMD rebuild is unavailable due to runtime prerequisites or adapter failures."
QMD_SEARCH_ACTION = "Install QMD prerequisites or retry memory_search with backend='builtin'."
QMD_COLLECTION_MASK = "**/*.md"
QMD_DEFAULT_TIMEOUT_S = 120.0
QMD_UPDATE_TIMEOUT_S = 120.0
QMD_EMBED_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class QmdRuntimeStatus:
    available: bool
    reason: str | None
    details: tuple[str, ...]
    qmd_binary: str | None
    js_runtime: str | None


@dataclass(frozen=True)
class QmdCommandResult:
    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


class QmdCommandRunner(Protocol):
    def run(self, args: list[str], *, cwd: Path, timeout_s: float) -> QmdCommandResult:
        ...


class SubprocessQmdRunner:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def run(self, args: list[str], *, cwd: Path, timeout_s: float) -> QmdCommandResult:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                cwd=str(cwd),
                timeout=timeout_s,
                env=_build_qmd_env(self.workspace),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"qmd command timed out after {timeout_s:.1f}s: {' '.join(args[1:])}") from exc
        except OSError as exc:
            raise RuntimeError(f"qmd command failed to start: {exc}") from exc
        return QmdCommandResult(
            args=tuple(args),
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


def detect_qmd_runtime() -> QmdRuntimeStatus:
    qmd_binary = shutil.which("qmd")
    bun_binary = shutil.which("bun")
    node_binary = shutil.which("node")
    js_runtime = bun_binary or node_binary

    details: list[str] = []
    if qmd_binary:
        details.append(f"qmd binary detected at {qmd_binary}")
    else:
        details.append("qmd binary not found on PATH")

    if bun_binary:
        details.append(f"bun detected at {bun_binary}")
    elif node_binary:
        details.append(f"node detected at {node_binary}")
    else:
        details.append("no JS runtime detected on PATH (expected bun or node)")

    reasons: list[str] = []
    if not qmd_binary:
        reasons.append("qmd binary not found on PATH")
    if not js_runtime:
        reasons.append("no JS runtime detected on PATH")

    return QmdRuntimeStatus(
        available=not reasons,
        reason="; ".join(reasons) if reasons else None,
        details=tuple(details),
        qmd_binary=qmd_binary,
        js_runtime=js_runtime,
    )


class QmdLifecycleAdapter:
    def __init__(
        self,
        *,
        workspace: str | Path,
        source_roots: list[str | Path] | None = None,
        runner: QmdCommandRunner | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.source_roots = normalize_source_roots(self.workspace, source_roots)
        self.collections = build_qmd_collections(self.workspace, source_roots)
        self._runner = runner or SubprocessQmdRunner(self.workspace)

    def status(self) -> QmdRuntimeStatus:
        return detect_qmd_runtime()

    def rebuild(self, *, force: bool = False) -> dict[str, Any]:
        runtime = self.status()
        stats = {
            "collections_total": len(self.collections),
            "collections_added": 0,
            "collections_existing": 0,
            "collections_failed": 0,
            "update_ran": 0,
            "embed_ran": 0,
        }
        base_payload = {
            "backend": "qmd",
            "force": bool(force),
            "collections": [self._collection_descriptor(item) for item in self.collections],
            "stats": stats,
        }

        if not runtime.available:
            return {
                **base_payload,
                "ok": False,
                "status": "unavailable",
                "error": runtime.reason or "qmd backend unavailable",
                "warning": QMD_REBUILD_WARNING,
                "action": QMD_REBUILD_ACTION,
                "details": list(runtime.details),
            }

        try:
            for collection in self.collections:
                created = self._ensure_collection(collection)
                if created:
                    stats["collections_added"] += 1
                else:
                    stats["collections_existing"] += 1
            self._run_checked(["qmd", "update"], timeout_s=QMD_UPDATE_TIMEOUT_S)
            stats["update_ran"] = 1
            embed_args = ["qmd", "embed", "-f"] if force else ["qmd", "embed"]
            self._run_checked(embed_args, timeout_s=QMD_EMBED_TIMEOUT_S)
            stats["embed_ran"] = 1
            status_result = self._run_checked(["qmd", "status"], timeout_s=QMD_DEFAULT_TIMEOUT_S)
        except RuntimeError as exc:
            stats["collections_failed"] = 1
            return {
                **base_payload,
                "ok": False,
                "status": "failed",
                "error": str(exc),
                "warning": QMD_REBUILD_WARNING,
                "action": QMD_REBUILD_ACTION,
            }
        return {
            **base_payload,
            "ok": True,
            "status": "ok",
            "qmd_status": {
                "stdout": status_result.stdout.strip(),
                "stderr": status_result.stderr.strip(),
            },
        }

    def search(self, query: str, *, max_results: int, min_score: float) -> dict[str, Any]:
        runtime = self.status()
        if not runtime.available:
            return self._build_unavailable_search_payload(
                runtime.reason or "qmd backend unavailable",
                details=runtime.details,
            )
        try:
            result = self._runner.run(
                [
                    "qmd",
                    "query",
                    query,
                    "--json",
                    "-n",
                    str(int(max_results)),
                    "--min-score",
                    str(float(min_score)),
                ],
                cwd=self.workspace,
                timeout_s=QMD_DEFAULT_TIMEOUT_S,
            )
        except RuntimeError as exc:
            return self._build_unavailable_search_payload(str(exc))

        if result.exit_code != 0:
            return self._build_unavailable_search_payload(self._format_command_error("query", result))

        try:
            rows = self._parse_query_rows(result.stdout, result.stderr)
            results = [self._normalize_search_row(row) for row in rows]
        except RuntimeError as exc:
            return self._build_unavailable_search_payload(str(exc))

        return {
            "results": results,
            "provider": "qmd",
            "model": "qmd",
            "citations": "off",
            "mode": "hybrid",
        }

    def _ensure_collection(self, collection: QmdCollection) -> bool:
        result = self._runner.run(
            [
                "qmd",
                "collection",
                "add",
                str(collection.root.path),
                "--name",
                collection.name,
                "--mask",
                QMD_COLLECTION_MASK,
            ],
            cwd=self.workspace,
            timeout_s=QMD_UPDATE_TIMEOUT_S,
        )
        if result.exit_code == 0:
            return True
        text = " ".join(part.strip().lower() for part in (result.stdout, result.stderr) if part.strip())
        if "already exists" in text or "exists already" in text:
            return False
        raise RuntimeError(self._format_command_error(f"collection add for {collection.name}", result))

    def _run_checked(self, args: list[str], *, timeout_s: float) -> QmdCommandResult:
        result = self._runner.run(args, cwd=self.workspace, timeout_s=timeout_s)
        if result.exit_code != 0:
            raise RuntimeError(self._format_command_error(" ".join(args[1:]), result))
        return result

    def _build_unavailable_search_payload(self, reason: str, *, details: tuple[str, ...] = ()) -> dict[str, Any]:
        payload = {
            "results": [],
            "provider": "qmd",
            "model": "",
            "citations": "off",
            "mode": "hybrid",
            "disabled": True,
            "unavailable": True,
            "error": f"memory index unavailable: {reason}",
            "warning": "Memory search is unavailable due to local index unavailability.",
            "action": QMD_SEARCH_ACTION,
        }
        if details:
            payload["details"] = list(details)
        return payload

    def _parse_query_rows(self, stdout: str, stderr: str) -> list[dict[str, Any]]:
        trimmed_stdout = (stdout or "").strip()
        trimmed_stderr = (stderr or "").strip()
        if _is_qmd_no_results_output(trimmed_stdout) or (not trimmed_stdout and _is_qmd_no_results_output(trimmed_stderr)):
            return []
        payload = _parse_json_array(trimmed_stdout)
        if payload is None:
            noisy = _extract_first_json_array(trimmed_stdout)
            payload = _parse_json_array(noisy) if noisy else None
        if payload is None:
            raise RuntimeError("qmd query returned invalid JSON")
        return payload

    def _normalize_search_row(self, row: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_row_path(row)
        if path is None:
            raise RuntimeError("qmd result could not be mapped to an allowed markdown path")
        score = row.get("score", 0.0)
        if not isinstance(score, (int, float)):
            raise RuntimeError("qmd result score must be numeric")
        start_line, end_line, snippet = self._extract_line_span_and_snippet(row)
        return {
            "path": path,
            "startLine": start_line,
            "endLine": end_line,
            "score": float(score),
            "snippet": snippet,
            "source": "memory",
        }

    def _resolve_row_path(self, row: dict[str, Any]) -> str | None:
        for key in ("file", "path", "docid"):
            value = row.get(key)
            if isinstance(value, str):
                normalized = self._normalize_result_path(value, collection_name=row.get("collection"))
                if normalized is not None:
                    return normalized
        return None

    def _normalize_result_path(self, raw: str, *, collection_name: Any) -> str | None:
        value = raw.strip()
        if not value:
            return None
        if Path(value).is_absolute():
            return self._normalize_absolute_path(Path(value))
        if ":" in value:
            prefix, _, suffix = value.partition(":")
            for collection in self.collections:
                if collection.name == prefix:
                    return self._normalize_absolute_path(collection.root.path / Path(suffix))
            if any(root.alias == prefix for root in self.source_roots):
                return self._normalize_relative_path(prefix, suffix)
        if isinstance(collection_name, str):
            for collection in self.collections:
                if collection.name == collection_name:
                    return self._normalize_absolute_path(collection.root.path / Path(value))
        for root in self.source_roots:
            normalized = self._normalize_absolute_path(root.path / Path(value))
            if normalized is not None:
                return normalized
        return None

    def _normalize_absolute_path(self, path: Path) -> str | None:
        candidate = path.expanduser().resolve(strict=False)
        for root in self.source_roots:
            try:
                relative = candidate.relative_to(root.path)
            except ValueError:
                continue
            relative_text = relative.as_posix()
            if not relative_text.endswith(".md"):
                return None
            if self._has_symlink_component(root.path, relative):
                return None
            if len(self.source_roots) > 1:
                return f"{root.alias}:{relative_text}"
            return relative_text
        return None

    def _normalize_relative_path(self, alias: str, rel: str) -> str | None:
        normalized = posixpath.normpath(rel)
        if normalized in {"", ".", ".."} or normalized.startswith("../") or not normalized.endswith(".md"):
            return None
        if len(self.source_roots) > 1:
            return f"{alias}:{normalized}"
        return normalized

    def _extract_line_span_and_snippet(self, row: dict[str, Any]) -> tuple[int, int, str]:
        start_value = row.get("startLine", row.get("start_line", 1))
        end_value = row.get("endLine", row.get("end_line", start_value))
        if not isinstance(start_value, int) or not isinstance(end_value, int):
            raise RuntimeError("qmd result line span must be integer-valued")
        snippet = row.get("snippet")
        if not isinstance(snippet, str):
            snippet = row.get("body") if isinstance(row.get("body"), str) else ""
        if snippet.startswith("@@"):
            header, _, remainder = snippet.partition("\n")
            start_value, end_value = _parse_snippet_header(header, default_start=start_value, default_end=end_value)
            snippet = remainder
        return max(1, start_value), max(start_value, end_value), " ".join(snippet.split())[:240]

    @staticmethod
    def _has_symlink_component(root: Path, relative: Path) -> bool:
        current = root
        for part in relative.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                return True
        return False

    @staticmethod
    def _collection_descriptor(collection: QmdCollection) -> dict[str, Any]:
        return {
            "name": collection.name,
            "root_alias": collection.root.alias,
            "root_type": collection.context["root_type"],
            "source_root": collection.context["source_root"],
            "mask": QMD_COLLECTION_MASK,
        }

    @staticmethod
    def _format_command_error(operation: str, result: QmdCommandResult) -> str:
        message = (result.stderr or result.stdout or f"exit code {result.exit_code}").strip()
        return f"qmd {operation} failed: {message}"


def _build_qmd_env(workspace: Path) -> dict[str, str]:
    env = dict(os.environ)
    runtime_root = workspace / ".qmd"
    home_dir = runtime_root / "home"
    config_dir = runtime_root / "xdg-config"
    cache_dir = runtime_root / "xdg-cache"
    data_dir = runtime_root / "xdg-data"
    runtime_dir = runtime_root / "xdg-runtime"

    for path in (home_dir, config_dir, cache_dir, data_dir, runtime_dir):
        path.mkdir(parents=True, exist_ok=True)

    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_dir)
    env["XDG_CACHE_HOME"] = str(cache_dir)
    env["XDG_DATA_HOME"] = str(data_dir)
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env.setdefault("NO_COLOR", "1")
    return env


def _parse_json_array(raw: str | None) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    rows: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise RuntimeError("qmd query returned a non-object result row")
        rows.append(entry)
    return rows


def _extract_first_json_array(raw: str) -> str | None:
    start = raw.find("[")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return None


def _is_qmd_no_results_output(raw: str) -> bool:
    lines = [" ".join(line.strip().lower().split()) for line in raw.splitlines() if line.strip()]
    return any(
        line in {"no results found", "no results found."}
        or line.endswith("no results found")
        or line.endswith("no results found.")
        for line in lines
    )


def _parse_snippet_header(header: str, *, default_start: int, default_end: int) -> tuple[int, int]:
    text = header.replace("@@", "").strip()
    if not text.startswith("-") or "," not in text:
        return default_start, default_end
    start_text, _, count_text = text[1:].partition(",")
    if not start_text.isdigit() or not count_text.isdigit():
        return default_start, default_end
    start_value = int(start_text)
    line_count = max(1, int(count_text))
    return start_value, start_value + line_count - 1
