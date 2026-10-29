from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STAGING_ROOT = Path("/home/cwilson/projects/_vault_staging")
DEFAULT_CANONICAL_ROOT = Path("/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record")
EMPTY_STAGING_INDEX = """# Work Session Index

Purpose: Fast routing to relevant prior sessions and decisions.

How to use:
1. Identify current task scope first (project, subsystem, bug/feature).
2. Filter by project/tags and recency.
3. Open only the most relevant linked notes.

## Columns
- Date: `YYYY-MM-DD`
- Project: short project id/name
- Session Summary: one-line outcome
- Work Log: path to `20_Work_Logs/LOG-YYYYMMDD.md`
- Decisions: path(s) in `10_Decisions/` or `none`
- Code Links: repo path(s) changed
- Tags: comma-separated keywords

## Sessions
| Date | Project | Session Summary | Work Log | Decisions | Code Links | Tags |
| --- | --- | --- | --- | --- | --- | --- |
_No staged session files are currently present. This staging area is empty aside from `INDEX.md`._
"""

_SESSION_RE = re.compile(r"^SESSION-\d{8}\.md$")
_WORK_LOG_RE = re.compile(r"^LOG-\d{8}\.md$")
_INDEX_EMPTY_LINE = "_No staged session files are currently present. This staging area is empty aside from `INDEX.md`._"


@dataclass(frozen=True)
class Action:
    relative_path: str
    action: str
    detail: str | None = None


def sync_staging_to_vault(
    *,
    staging_root: Path,
    canonical_root: Path,
    prune: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    staging_root = staging_root.resolve()
    canonical_root = canonical_root.resolve()

    if not staging_root.exists():
        return _payload(False, [], [], prune, dry_run, errors=["staging root does not exist"])
    if not staging_root.is_dir():
        return _payload(False, [], [], prune, dry_run, errors=["staging root is not a directory"])
    if not canonical_root.exists():
        return _payload(False, [], [], prune, dry_run, errors=["canonical root does not exist"])
    if not canonical_root.is_dir():
        return _payload(False, [], [], prune, dry_run, errors=["canonical root is not a directory"])

    actions: list[Action] = []
    collisions: list[str] = []
    pruned: list[str] = []
    staged_files = [path for path in sorted(staging_root.rglob("*")) if path.is_file()]

    for staged_path in staged_files:
        relative_path = staged_path.relative_to(staging_root)
        if _is_staging_owned_artifact(relative_path):
            actions.append(Action(str(relative_path), "retained", "artifact remains staging-owned"))
            continue
        if _is_index_path(relative_path):
            action = _sync_index(staged_path, canonical_root / relative_path, relative_path, dry_run)
        elif _is_mergeable_note(relative_path):
            action = _sync_mergeable_markdown(
                staged_path,
                canonical_root / relative_path,
                relative_path,
                dry_run,
            )
        else:
            action = _sync_generic_file(staged_path, canonical_root / relative_path, relative_path, dry_run)

        actions.append(action)
        if action.action == "collision":
            collisions.append(action.relative_path)
            continue
        if prune and not dry_run and not _is_index_path(relative_path):
            staged_path.unlink(missing_ok=True)
            pruned.append(action.relative_path)

    if prune and not dry_run and _staging_is_clear(staging_root):
        (staging_root / "INDEX.md").write_text(EMPTY_STAGING_INDEX, encoding="utf-8")

    return _payload(True, actions, collisions, prune, dry_run, pruned=pruned)


def _payload(
    ok: bool,
    actions: list[Action],
    collisions: list[str],
    prune: bool,
    dry_run: bool,
    *,
    errors: list[str] | None = None,
    pruned: list[str] | None = None,
) -> dict[str, object]:
    return {
        "ok": ok,
        "dry_run": dry_run,
        "prune": prune,
        "errors": list(errors or []),
        "collisions": collisions,
        "pruned": sorted(pruned or []),
        "actions": [
            {
                "path": action.relative_path,
                "action": action.action,
                "detail": action.detail,
            }
            for action in actions
        ],
    }


def _sync_generic_file(staged_path: Path, canonical_path: Path, relative_path: Path, dry_run: bool) -> Action:
    staged_bytes = staged_path.read_bytes()
    if not canonical_path.exists():
        if not dry_run:
            canonical_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_path, canonical_path)
        return Action(str(relative_path), "copied")

    canonical_bytes = canonical_path.read_bytes()
    if canonical_bytes == staged_bytes:
        return Action(str(relative_path), "unchanged")

    return Action(str(relative_path), "collision", "canonical file differs; left staged copy in place")


def _sync_mergeable_markdown(
    staged_path: Path,
    canonical_path: Path,
    relative_path: Path,
    dry_run: bool,
) -> Action:
    staged_text = staged_path.read_text(encoding="utf-8")
    if not canonical_path.exists():
        if not dry_run:
            canonical_path.parent.mkdir(parents=True, exist_ok=True)
            canonical_path.write_text(staged_text, encoding="utf-8")
        return Action(str(relative_path), "copied")

    canonical_text = canonical_path.read_text(encoding="utf-8")
    if canonical_text == staged_text or _contains_merge_block(canonical_text, staged_text, relative_path):
        return Action(str(relative_path), "unchanged")

    merged = _append_merge_block(canonical_text, staged_text, relative_path)
    if not dry_run:
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_text(merged, encoding="utf-8")
    return Action(str(relative_path), "merged", "appended staged markdown as an idempotent merge block")


def _sync_index(staged_path: Path, canonical_path: Path, relative_path: Path, dry_run: bool) -> Action:
    staged_text = staged_path.read_text(encoding="utf-8")
    canonical_text = canonical_path.read_text(encoding="utf-8") if canonical_path.exists() else ""
    merged = _merge_index_text(canonical_text, staged_text)

    if canonical_text == merged and canonical_path.exists():
        return Action(str(relative_path), "unchanged")

    if not dry_run:
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_text(merged, encoding="utf-8")
    return Action(str(relative_path), "merged", "merged index rows additively without overwriting canonical rows")


def _append_merge_block(canonical_text: str, staged_text: str, relative_path: Path) -> str:
    cleaned_canonical = canonical_text.rstrip()
    block = _build_merge_block(staged_text, relative_path)
    if not cleaned_canonical:
        return f"{block}\n"
    return f"{cleaned_canonical}\n\n{block}\n"


def _contains_merge_block(canonical_text: str, staged_text: str, relative_path: Path) -> bool:
    block = _build_merge_block(staged_text, relative_path)
    normalized_staged = staged_text.strip()
    normalized_canonical = canonical_text.strip()
    return block in canonical_text or (normalized_staged != "" and normalized_staged in normalized_canonical)


def _build_merge_block(staged_text: str, relative_path: Path) -> str:
    digest = hashlib.sha256(staged_text.encode("utf-8")).hexdigest()
    body = staged_text.rstrip("\n")
    marker = f'<!-- vault-staging-sync merge path="{relative_path.as_posix()}" sha256="{digest}" -->'
    end_marker = "<!-- /vault-staging-sync merge -->"
    return f"{marker}\n{body}\n{end_marker}"


def _merge_index_text(canonical_text: str, staged_text: str) -> str:
    canonical = _parse_index(canonical_text)
    staged = _parse_index(staged_text)

    prefix = canonical["prefix"] or staged["prefix"] or _parse_index(EMPTY_STAGING_INDEX)["prefix"]
    header = canonical["header"] or staged["header"] or "| Date | Project | Session Summary | Work Log | Decisions | Code Links | Tags |"
    separator = canonical["separator"] or staged["separator"] or "| --- | --- | --- | --- | --- | --- | --- |"
    suffix = canonical["suffix"] or staged["suffix"]

    merged_rows: list[str] = []
    seen: set[str] = set()
    for row in canonical["rows"] + staged["rows"]:
        key = row.strip()
        if key in seen:
            continue
        seen.add(key)
        merged_rows.append(row)

    lines = [*prefix, header, separator, *merged_rows]
    if not merged_rows:
        lines.append(_INDEX_EMPTY_LINE)
    lines.extend(suffix)
    return "\n".join(lines).rstrip() + "\n"


def _parse_index(text: str) -> dict[str, list[str] | str]:
    if not text.strip():
        return {"prefix": [], "header": "", "separator": "", "rows": [], "suffix": []}

    lines = text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.startswith("| Date |")), None)
    if header_idx is None:
        return {"prefix": lines, "header": "", "separator": "", "rows": [], "suffix": []}

    separator_idx = header_idx + 1 if header_idx + 1 < len(lines) else header_idx
    rows: list[str] = []
    suffix: list[str] = []
    idx = separator_idx + 1
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("|") and line != _INDEX_EMPTY_LINE:
            rows.append(line)
            idx += 1
            continue
        break

    for suffix_line in lines[idx:]:
        if suffix_line == _INDEX_EMPTY_LINE:
            continue
        suffix.append(suffix_line)

    return {
        "prefix": lines[:header_idx],
        "header": lines[header_idx],
        "separator": lines[separator_idx] if separator_idx < len(lines) else "",
        "rows": rows,
        "suffix": suffix,
    }


def _staging_is_clear(staging_root: Path) -> bool:
    return all(
        _is_index_path(path.relative_to(staging_root)) or _is_staging_owned_artifact(path.relative_to(staging_root))
        for path in staging_root.rglob("*")
        if path.is_file()
    )


def _is_index_path(relative_path: Path) -> bool:
    return relative_path.as_posix() == "INDEX.md"


def _is_mergeable_note(relative_path: Path) -> bool:
    if relative_path.suffix.lower() != ".md":
        return False
    if _is_index_path(relative_path):
        return False
    name = relative_path.name
    if _SESSION_RE.match(name):
        return True
    return relative_path.parent.as_posix() == "20_Work_Logs" and _WORK_LOG_RE.match(name) is not None


def _is_staging_owned_artifact(relative_path: Path) -> bool:
    normalized = relative_path.as_posix()
    parts = normalized.split("/")
    return (
        len(parts) >= 4
        and parts[0] == "00_Inbox"
        and parts[2] == "runs"
        and parts[-1].endswith((".md", ".json"))
    )
