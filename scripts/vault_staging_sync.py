#!/usr/bin/env python3
"""Promote staged vault files into the canonical vault deterministically."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_STAGING_ROOT = Path("/home/cwilson/projects/_vault_staging")
DEFAULT_CANONICAL_ROOT = Path("/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record")
WRITE_RETRY_ATTEMPTS = 3
WRITE_RETRY_DELAY_SECONDS = 1.0
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
SURPRISE_ME_IDEA_REGISTRY_RELATIVE_PATH = "00_Inbox/surprise_me/idea_registry.json"
SURPRISE_ME_IDEA_REGISTRY_OVERLAY_RELATIVE_PATH = "00_Inbox/surprise_me/idea_registry.overlay.json"
SURPRISE_ME_IDEA_REGISTRY_RELATIVE_PATHS = {
    SURPRISE_ME_IDEA_REGISTRY_RELATIVE_PATH,
    SURPRISE_ME_IDEA_REGISTRY_OVERLAY_RELATIVE_PATH,
}


class VaultStagingSyncError(Exception):
    """Raised when deterministic sync cannot proceed safely."""


@dataclass
class SyncSummary:
    copied: list[str]
    merged: list[str]
    skipped: list[str]
    pruned: list[str]
    conflicts: list[str]
    staging_index_reset: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "copied": self.copied,
            "merged": self.merged,
            "skipped": self.skipped,
            "pruned": self.pruned,
            "conflicts": self.conflicts,
            "staging_index_reset": self.staging_index_reset,
        }


def _marker_for(rel_path: str, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"<!-- vault-staging-sync:{rel_path}:{digest} -->"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: PermissionError | None = None
    for attempt in range(WRITE_RETRY_ATTEMPTS):
        try:
            path.write_text(content, encoding="utf-8")
            return
        except PermissionError as exc:
            last_error = exc
            if attempt == WRITE_RETRY_ATTEMPTS - 1:
                break
            time.sleep(WRITE_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: PermissionError | None = None
    for attempt in range(WRITE_RETRY_ATTEMPTS):
        try:
            shutil.copy2(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt == WRITE_RETRY_ATTEMPTS - 1:
                break
            time.sleep(WRITE_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error


def _list_staged_files(staging_root: Path) -> list[Path]:
    if not staging_root.is_dir():
        raise VaultStagingSyncError(f"staging root does not exist: {staging_root}")
    return sorted(path for path in staging_root.rglob("*") if path.is_file())


def _is_mergeable_note(rel_path: str) -> bool:
    parts = rel_path.split("/")
    if len(parts) != 2:
        return False
    folder, name = parts
    return (
        folder == "00_Inbox"
        and name.startswith("SESSION-")
        and name.endswith(".md")
    ) or (
        folder == "20_Work_Logs"
        and name.startswith("LOG-")
        and name.endswith(".md")
    )


def _is_mergeable_structured_markdown_note(rel_path: str) -> bool:
    parts = rel_path.split("/")
    if len(parts) != 2:
        return False
    folder, name = parts
    return folder in {"10_Decisions", "60_How_To"} and name.endswith(".md")


def _is_staging_owned_artifact(rel_path: str) -> bool:
    parts = rel_path.split("/")
    return (
        len(parts) >= 4
        and parts[0] == "00_Inbox"
        and parts[2] == "runs"
        and parts[-1].endswith((".md", ".json"))
    )


def _is_surprise_me_idea_registry(rel_path: str) -> bool:
    return rel_path in SURPRISE_ME_IDEA_REGISTRY_RELATIVE_PATHS


def _merge_note(target_path: Path, rel_path: str, staged_content: str) -> bool:
    marker = _marker_for(rel_path, staged_content)
    target_content = _read_text(target_path)
    if marker in target_content or staged_content == target_content:
        return False
    merged = f"{target_content.rstrip()}\n\n{marker}\n{staged_content.rstrip()}\n"
    _write_text(target_path, merged)
    return True


_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+.+$")


def _split_frontmatter(content: str) -> tuple[list[str], list[str]]:
    lines = content.splitlines()
    if len(lines) >= 3 and lines[0].strip() == "---":
        try:
            closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        except StopIteration:
            return [], lines
        return lines[: closing + 1], lines[closing + 1 :]
    return [], lines


def _frontmatter_key(line: str) -> str | None:
    if ":" not in line:
        return None
    key = line.split(":", 1)[0].strip()
    return key or None


def _merge_frontmatter(canonical_lines: list[str], staged_lines: list[str]) -> list[str]:
    if not canonical_lines:
        return staged_lines
    if not staged_lines:
        return canonical_lines

    canonical_body = canonical_lines[1:-1]
    staged_body = staged_lines[1:-1]
    canonical_keys = [_frontmatter_key(line) for line in canonical_body]
    staged_keys = [_frontmatter_key(line) for line in staged_body]
    if any(key is None for key in canonical_keys + staged_keys):
        raise VaultStagingSyncError("frontmatter contains non key-value lines")
    if len(set(canonical_keys)) != len(canonical_keys) or len(set(staged_keys)) != len(staged_keys):
        raise VaultStagingSyncError("frontmatter contains duplicate keys")

    staged_map = dict(zip(staged_keys, staged_body))
    merged_body: list[str] = []
    seen: set[str] = set()
    for key, line in zip(canonical_keys, canonical_body):
        if key in staged_map:
            merged_body.append(staged_map[key])
            seen.add(key)
        else:
            merged_body.append(line)
            seen.add(key)
    for key, line in zip(staged_keys, staged_body):
        if key in seen:
            continue
        merged_body.append(line)
    return ["---", *merged_body, "---"]


def _split_markdown_sections(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    if not lines:
        return [], []
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in lines:
        if _HEADING_PATTERN.match(line):
            if current_heading is None:
                if preamble:
                    preamble = preamble
            else:
                sections.append((current_heading, current_lines))
            current_heading = line
            current_lines = [line]
            continue
        if current_heading is None:
            preamble.append(line)
        else:
            current_lines.append(line)

    if current_heading is not None:
        sections.append((current_heading, current_lines))

    return preamble, sections


def _ensure_unique_headings(sections: list[tuple[str, list[str]]]) -> None:
    headings = [heading for heading, _ in sections]
    if len(headings) != len(set(headings)):
        raise VaultStagingSyncError("markdown note contains duplicate headings")


def _insert_missing_staged_sections(
    canonical_order: list[str],
    staged_order: list[str],
) -> list[str]:
    final_order = list(canonical_order)
    for index, heading in enumerate(staged_order):
        if heading in final_order:
            continue
        previous = next((candidate for candidate in reversed(staged_order[:index]) if candidate in final_order), None)
        if previous is not None:
            insert_at = final_order.index(previous) + 1
            final_order.insert(insert_at, heading)
            continue
        following = next((candidate for candidate in staged_order[index + 1 :] if candidate in final_order), None)
        if following is not None:
            insert_at = final_order.index(following)
            final_order.insert(insert_at, heading)
            continue
        final_order.append(heading)
    return final_order


def _merge_markdown_body(canonical_lines: list[str], staged_lines: list[str]) -> list[str]:
    canonical_preamble, canonical_sections = _split_markdown_sections(canonical_lines)
    staged_preamble, staged_sections = _split_markdown_sections(staged_lines)
    _ensure_unique_headings(canonical_sections)
    _ensure_unique_headings(staged_sections)

    canonical_map = {heading: chunk for heading, chunk in canonical_sections}
    staged_map = {heading: chunk for heading, chunk in staged_sections}
    final_order = _insert_missing_staged_sections(list(canonical_map), list(staged_map))
    merged_lines = staged_preamble if staged_preamble else canonical_preamble

    for heading in final_order:
        chunk = staged_map.get(heading) or canonical_map.get(heading)
        if chunk is None:
            continue
        if merged_lines:
            merged_lines.append("")
        merged_lines.extend(chunk)
    return merged_lines


def _merge_structured_markdown_note(target_path: Path, staged_path: Path) -> bool:
    canonical_content = _read_text(target_path)
    staged_content = _read_text(staged_path)
    canonical_frontmatter, canonical_body = _split_frontmatter(canonical_content)
    staged_frontmatter, staged_body = _split_frontmatter(staged_content)
    merged_frontmatter = _merge_frontmatter(canonical_frontmatter, staged_frontmatter)
    merged_body = _merge_markdown_body(canonical_body, staged_body)

    merged_parts: list[str] = []
    if merged_frontmatter:
        merged_parts.extend(merged_frontmatter)
    if merged_body:
        if merged_parts:
            merged_parts.append("")
        merged_parts.extend(merged_body)
    merged_text = "\n".join(merged_parts).rstrip() + "\n"
    if merged_text == canonical_content:
        return False
    _write_text(target_path, merged_text)
    return True


def _merge_index(target_path: Path, staged_path: Path) -> bool:
    target_content = _read_text(target_path)
    staged_lines = [line for line in _read_text(staged_path).splitlines() if line.startswith("| ")]
    new_lines = [line for line in staged_lines if line not in target_content]
    if not new_lines:
        return False
    merged = f"{target_content.rstrip()}\n\n<!-- Promoted from _vault_staging -->\n" + "\n".join(new_lines) + "\n"
    _write_text(target_path, merged)
    return True


def _load_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(_read_text(path))
    if not isinstance(payload, dict):
        raise VaultStagingSyncError(f"expected JSON object in {path}")
    return payload


def _parse_iso(raw: object) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _choose_earliest(*candidates: object) -> str:
    parsed = [(dt, str(raw).strip()) for raw in candidates if (dt := _parse_iso(raw)) is not None]
    if not parsed:
        for raw in candidates:
            value = str(raw or "").strip()
            if value:
                return value
        return ""
    return min(parsed, key=lambda item: item[0])[1]


def _choose_latest(*candidates: object) -> str:
    parsed = [(dt, str(raw).strip()) for raw in candidates if (dt := _parse_iso(raw)) is not None]
    if not parsed:
        for raw in reversed(candidates):
            value = str(raw or "").strip()
            if value:
                return value
        return ""
    return max(parsed, key=lambda item: item[0])[1]


def _merge_unique_list(*values: object) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return sorted(merged)


def _review_tuple(entry: dict[str, object]) -> tuple[int, str, str]:
    status = str(entry.get("review_status") or "unreviewed").strip().lower()
    reviewed_at = str(entry.get("reviewed_at") or "").strip()
    if status == "approved":
        return (2, reviewed_at, status)
    if status == "discarded":
        return (1, reviewed_at, status)
    return (0, reviewed_at, status)


def _merge_registry_entry(canonical: dict[str, object], staged: dict[str, object]) -> dict[str, object]:
    canonical_review = _review_tuple(canonical)
    staged_review = _review_tuple(staged)
    review_source = canonical
    if staged_review > canonical_review:
        review_source = staged
    latest_source = canonical
    if _choose_latest(canonical.get("last_seen_at"), staged.get("last_seen_at")) == str(staged.get("last_seen_at") or "").strip():
        latest_source = staged
    return {
        "category": str((latest_source.get("category") or canonical.get("category") or staged.get("category") or "")).strip(),
        "first_seen_at": _choose_earliest(canonical.get("first_seen_at"), staged.get("first_seen_at")),
        "first_seen_run_id": str(
            canonical.get("first_seen_run_id")
            if _choose_earliest(canonical.get("first_seen_at"), staged.get("first_seen_at")) == str(canonical.get("first_seen_at") or "").strip()
            else staged.get("first_seen_run_id")
            or canonical.get("first_seen_run_id")
            or ""
        ).strip(),
        "idea_key": str(canonical.get("idea_key") or staged.get("idea_key") or "").strip(),
        "last_seen_at": _choose_latest(canonical.get("last_seen_at"), staged.get("last_seen_at")),
        "last_seen_run_id": str(
            staged.get("last_seen_run_id")
            if _choose_latest(canonical.get("last_seen_at"), staged.get("last_seen_at")) == str(staged.get("last_seen_at") or "").strip()
            else canonical.get("last_seen_run_id")
            or staged.get("last_seen_run_id")
            or ""
        ).strip(),
        "latest_expected_benefit": str(
            latest_source.get("latest_expected_benefit")
            or canonical.get("latest_expected_benefit")
            or staged.get("latest_expected_benefit")
            or ""
        ).strip(),
        "latest_idea_id": str(latest_source.get("latest_idea_id") or canonical.get("latest_idea_id") or staged.get("latest_idea_id") or "").strip(),
        "latest_next_action": str(
            latest_source.get("latest_next_action")
            or canonical.get("latest_next_action")
            or staged.get("latest_next_action")
            or ""
        ).strip(),
        "latest_rationale": str(
            latest_source.get("latest_rationale")
            or canonical.get("latest_rationale")
            or staged.get("latest_rationale")
            or ""
        ).strip(),
        "review_note": str(review_source.get("review_note") or "").strip(),
        "review_status": str(review_source.get("review_status") or "unreviewed").strip().lower() or "unreviewed",
        "reviewed_at": str(review_source.get("reviewed_at") or "").strip(),
        "seen_idea_ids": _merge_unique_list(canonical.get("seen_idea_ids"), staged.get("seen_idea_ids")),
        "source_notes": _merge_unique_list(canonical.get("source_notes"), staged.get("source_notes")),
        "times_seen": max(int(canonical.get("times_seen") or 0), int(staged.get("times_seen") or 0)),
        "times_selected": max(int(canonical.get("times_selected") or 0), int(staged.get("times_selected") or 0)),
        "title": str(latest_source.get("title") or canonical.get("title") or staged.get("title") or "").strip(),
    }


def _merge_surprise_me_idea_registry(target_path: Path, staged_path: Path) -> bool:
    canonical_payload = _load_json_object(target_path)
    staged_payload = _load_json_object(staged_path)
    canonical_ideas = canonical_payload.get("ideas", {})
    staged_ideas = staged_payload.get("ideas", {})
    if not isinstance(canonical_ideas, dict) or not isinstance(staged_ideas, dict):
        raise VaultStagingSyncError("idea registry ideas must contain a JSON object")
    merged_ideas: dict[str, object] = {}
    for idea_key in sorted(set(canonical_ideas) | set(staged_ideas)):
        canonical_entry = canonical_ideas.get(idea_key)
        staged_entry = staged_ideas.get(idea_key)
        if not isinstance(canonical_entry, dict):
            canonical_entry = {}
        if not isinstance(staged_entry, dict):
            staged_entry = {}
        if not canonical_entry:
            merged_ideas[str(idea_key)] = staged_entry
            continue
        if not staged_entry:
            merged_ideas[str(idea_key)] = canonical_entry
            continue
        merged_ideas[str(idea_key)] = _merge_registry_entry(canonical_entry, staged_entry)
    counts = {"total": len(merged_ideas), "unreviewed": 0, "approved": 0, "discarded": 0}
    for entry in merged_ideas.values():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("review_status") or "unreviewed").strip().lower()
        if status not in {"approved", "discarded"}:
            status = "unreviewed"
        counts[status] += 1
    merged_payload = {
        "version": max(int(canonical_payload.get("version") or 1), int(staged_payload.get("version") or 1)),
        "updated_at": _choose_latest(canonical_payload.get("updated_at"), staged_payload.get("updated_at")),
        "counts": counts,
        "ideas": merged_ideas,
    }
    merged_text = json.dumps(merged_payload, indent=2, sort_keys=True) + "\n"
    if merged_text == _read_text(target_path):
        return False
    _write_text(target_path, merged_text)
    return True


def _promote_files(staging_root: Path, canonical_root: Path) -> SyncSummary:
    copied: list[str] = []
    merged: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []

    for staged_path in _list_staged_files(staging_root):
        rel_path = staged_path.relative_to(staging_root).as_posix()
        target_path = canonical_root / rel_path

        if rel_path == "INDEX.md":
            if not target_path.exists():
                _copy_file(staged_path, target_path)
                copied.append(rel_path)
            elif _merge_index(target_path, staged_path):
                merged.append(rel_path)
            else:
                skipped.append(rel_path)
            continue

        if _is_staging_owned_artifact(rel_path):
            skipped.append(rel_path)
            continue

        if not target_path.exists():
            _copy_file(staged_path, target_path)
            copied.append(rel_path)
            continue

        staged_content = _read_text(staged_path)
        target_content = _read_text(target_path)
        if staged_content == target_content:
            skipped.append(rel_path)
            continue

        if _is_mergeable_note(rel_path):
            if _merge_note(target_path, rel_path, staged_content):
                merged.append(rel_path)
            else:
                skipped.append(rel_path)
            continue

        if _is_mergeable_structured_markdown_note(rel_path):
            if _merge_structured_markdown_note(target_path, staged_path):
                merged.append(rel_path)
            else:
                skipped.append(rel_path)
            continue

        if _is_surprise_me_idea_registry(rel_path):
            if _merge_surprise_me_idea_registry(target_path, staged_path):
                merged.append(rel_path)
            else:
                skipped.append(rel_path)
            continue

        conflicts.append(rel_path)

    return SyncSummary(
        copied=copied,
        merged=merged,
        skipped=skipped,
        pruned=[],
        conflicts=conflicts,
        staging_index_reset=False,
    )


def _prune_staging_files(staging_root: Path) -> list[str]:
    pruned: list[str] = []
    for path in _list_staged_files(staging_root):
        rel_path = path.relative_to(staging_root).as_posix()
        if rel_path == "INDEX.md" or _is_staging_owned_artifact(rel_path):
            continue
        path.unlink()
        pruned.append(rel_path)
    return pruned


def _reset_staging_index(staging_root: Path) -> bool:
    index_path = staging_root / "INDEX.md"
    current = _read_text(index_path) if index_path.exists() else ""
    if current == EMPTY_STAGING_INDEX:
        return False
    _write_text(index_path, EMPTY_STAGING_INDEX)
    return True


def sync_vault_staging(
    *,
    staging_root: Path = DEFAULT_STAGING_ROOT,
    canonical_root: Path = DEFAULT_CANONICAL_ROOT,
    prune_staging: bool = True,
    reset_staging_index: bool = True,
    fail_on_conflict: bool = True,
) -> SyncSummary:
    if not canonical_root.is_dir():
        raise VaultStagingSyncError(f"canonical root does not exist: {canonical_root}")

    summary = _promote_files(staging_root, canonical_root)
    if summary.conflicts and fail_on_conflict:
        raise VaultStagingSyncError(
            "conflicting staged files require manual review: "
            + ", ".join(summary.conflicts)
        )

    if prune_staging:
        summary.pruned = _prune_staging_files(staging_root)

    if reset_staging_index:
        summary.staging_index_reset = _reset_staging_index(staging_root)

    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault-staging-sync")
    parser.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING_ROOT)
    parser.add_argument("--canonical-root", type=Path, default=DEFAULT_CANONICAL_ROOT)
    parser.add_argument(
        "--keep-staging-files",
        action="store_true",
        help="Do not delete promoted staging files after sync",
    )
    parser.add_argument(
        "--skip-index-reset",
        action="store_true",
        help="Do not rewrite _vault_staging/INDEX.md to the empty-state template",
    )
    parser.add_argument(
        "--allow-conflicts",
        action="store_true",
        help="Do not fail when non-mergeable path collisions are present",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        summary = sync_vault_staging(
            staging_root=args.staging_root,
            canonical_root=args.canonical_root,
            prune_staging=not args.keep_staging_files,
            reset_staging_index=not args.skip_index_reset,
            fail_on_conflict=not args.allow_conflicts,
        )
    except VaultStagingSyncError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2, sort_keys=True))
        return 1

    print(
        json.dumps(
            {"status": "ok", **summary.to_dict()},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
