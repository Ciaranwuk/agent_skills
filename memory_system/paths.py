from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DENY_ERROR = "path not allowed"
DEFAULT_EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
}


@dataclass(frozen=True)
class PathCheck:
    allowed: bool
    normalized: str
    target: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class SourceRoot:
    alias: str
    path: Path


@dataclass(frozen=True)
class DiscoveredFile:
    key: str
    rel: str
    abs_path: Path
    root: SourceRoot


def normalize_source_roots(workspace: Path, source_roots: Iterable[str | Path] | None = None) -> list[SourceRoot]:
    roots: list[Path] = [workspace.resolve()]
    for raw in source_roots or []:
        candidate = Path(raw).expanduser().resolve()
        if candidate not in roots:
            roots.append(candidate)

    normalized: list[SourceRoot] = []
    for idx, root in enumerate(roots):
        alias = "workspace" if idx == 0 else f"root{idx}"
        normalized.append(SourceRoot(alias=alias, path=root))
    return normalized


def check_allowed_path(source_roots: list[SourceRoot], raw_path: str) -> PathCheck:
    text = (raw_path or "").strip().replace("\\", "/")
    if not text:
        return PathCheck(False, "", DENY_ERROR)

    if text.startswith("/"):
        return PathCheck(False, text, DENY_ERROR)

    multi_root = len(source_roots) > 1
    selected_root = source_roots[0]
    rel_input = text
    if ":" in text:
        candidate_alias, rel_candidate = text.split(":", 1)
        for root in source_roots:
            if root.alias == candidate_alias:
                selected_root = root
                rel_input = rel_candidate
                break

    norm = posixpath.normpath(rel_input)
    if norm in {"", ".", ".."} or norm.startswith("../"):
        return PathCheck(False, norm, DENY_ERROR)

    if not norm.endswith(".md"):
        return PathCheck(False, norm, DENY_ERROR)

    target = selected_root.path / Path(norm)
    if not _is_within_root(selected_root.path, target):
        return PathCheck(False, norm, DENY_ERROR)

    if _has_symlink_component(selected_root.path, Path(norm)):
        return PathCheck(False, norm, DENY_ERROR)

    if target.exists() and not target.is_file():
        return PathCheck(False, norm, DENY_ERROR)

    normalized = _canonical_key(selected_root, norm, multi_root)
    return PathCheck(True, normalized, target=target)


def discover_markdown_files(source_roots: list[SourceRoot], excluded_dirs: set[str] | None = None) -> list[DiscoveredFile]:
    excluded = excluded_dirs or DEFAULT_EXCLUDED_DIRS
    multi_root = len(source_roots) > 1
    discovered: list[DiscoveredFile] = []
    seen_abs_paths: set[str] = set()

    for root in source_roots:
        base = root.path
        if not base.exists() or not base.is_dir() or base.is_symlink():
            continue

        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            current = Path(dirpath)
            # prune excluded and symlink directories
            dirnames[:] = [d for d in dirnames if d not in excluded and not (current / d).is_symlink()]
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                p = current / name
                if not _allowed_regular_file(base, p):
                    continue
                abs_key = str(p.resolve(strict=False))
                if abs_key in seen_abs_paths:
                    continue
                seen_abs_paths.add(abs_key)
                rel = p.relative_to(base).as_posix()
                key = _canonical_key(root, rel, multi_root)
                discovered.append(DiscoveredFile(key=key, rel=rel, abs_path=p, root=root))

    discovered.sort(key=lambda item: item.key)
    return discovered


def _allowed_regular_file(root: Path, path: Path) -> bool:
    if not path.exists() or not path.is_file() or path.is_symlink():
        return False
    if not _is_within_root(root, path):
        return False
    rel = path.relative_to(root)
    return not _has_symlink_component(root, rel)


def _is_within_root(root: Path, target: Path) -> bool:
    ws = root.resolve()
    candidate = target.resolve(strict=False)
    try:
        candidate.relative_to(ws)
        return True
    except ValueError:
        return False


def _has_symlink_component(workspace: Path, rel: Path) -> bool:
    cur = workspace
    for part in rel.parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            return True
    return False


def _canonical_key(root: SourceRoot, rel: str, multi_root: bool) -> str:
    if multi_root:
        return f"{root.alias}:{rel}"
    return rel
