#!/usr/bin/env python3
"""Manage persisted surprise-me idea review statuses."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from . import surprise_me_workflow_improvement  # type: ignore
except ImportError:  # pragma: no cover - direct script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import surprise_me_workflow_improvement  # type: ignore


class SurpriseMeIdeaRegistryError(RuntimeError):
    """Raised when surprise-me idea registry operations cannot proceed."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="surprise_me_idea_registry")
    parser.add_argument("--vault-root", default=str(surprise_me_workflow_improvement.DEFAULT_VAULT_ROOT))
    parser.add_argument("--staging-root", default=str(surprise_me_workflow_improvement.DEFAULT_STAGING_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--status", choices=surprise_me_workflow_improvement.REVIEW_STATUSES, default="")

    set_status_parser = subparsers.add_parser("set-status")
    set_status_parser.add_argument("--idea-key", default="")
    set_status_parser.add_argument("--idea-id", default="")
    set_status_parser.add_argument("--status", choices=surprise_me_workflow_improvement.REVIEW_STATUSES, required=True)
    set_status_parser.add_argument("--note", default="")

    return parser


def _load_registry(
    vault_root: Path,
    staging_root: Path,
) -> surprise_me_workflow_improvement.IdeaRegistry:
    if not vault_root.exists():
        raise SurpriseMeIdeaRegistryError(f"vault root not found: {vault_root}")
    if not staging_root.exists():
        raise SurpriseMeIdeaRegistryError(f"staging root not found: {staging_root}")
    registry = surprise_me_workflow_improvement.load_idea_memory(vault_root, staging_root)
    return surprise_me_workflow_improvement.IdeaRegistry(
        path=staging_root / surprise_me_workflow_improvement.IDEA_REGISTRY_RELATIVE_PATH,
        entries=registry.entries,
    )


def _resolve_entry(
    registry: surprise_me_workflow_improvement.IdeaRegistry,
    *,
    idea_key: str,
    idea_id: str,
) -> surprise_me_workflow_improvement.RegistryEntry:
    key = idea_key.strip()
    if key:
        entry = registry.entries.get(key)
        if entry is None:
            raise SurpriseMeIdeaRegistryError(f"idea key not found: {key}")
        return entry

    idea_id = idea_id.strip()
    if not idea_id:
        raise SurpriseMeIdeaRegistryError("--idea-key or --idea-id is required")
    matches = [entry for entry in registry.entries.values() if entry.latest_idea_id == idea_id or idea_id in entry.seen_idea_ids]
    if not matches:
        raise SurpriseMeIdeaRegistryError(f"idea id not found: {idea_id}")
    if len(matches) > 1:
        raise SurpriseMeIdeaRegistryError(f"idea id is ambiguous; use --idea-key instead: {idea_id}")
    return matches[0]


def _list_payload(
    registry: surprise_me_workflow_improvement.IdeaRegistry,
    *,
    status: str,
) -> dict[str, object]:
    items = []
    for entry in sorted(registry.entries.values(), key=lambda item: (item.review_status, item.last_seen_at, item.idea_key), reverse=True):
        if status and entry.review_status != status:
            continue
        items.append(entry.to_json())
    return {
        "status": "ok",
        "registry_path": registry.path.as_posix(),
        "counts": registry.counts(),
        "ideas": items,
    }


def _set_status_payload(
    registry: surprise_me_workflow_improvement.IdeaRegistry,
    *,
    idea_key: str,
    idea_id: str,
    status: str,
    note: str,
) -> dict[str, object]:
    entry = _resolve_entry(registry, idea_key=idea_key, idea_id=idea_id)
    updated_entry = surprise_me_workflow_improvement.RegistryEntry(
        idea_key=entry.idea_key,
        category=entry.category,
        title=entry.title,
        review_status=status,
        first_seen_run_id=entry.first_seen_run_id,
        last_seen_run_id=entry.last_seen_run_id,
        first_seen_at=entry.first_seen_at,
        last_seen_at=entry.last_seen_at,
        times_seen=entry.times_seen,
        times_selected=entry.times_selected,
        latest_idea_id=entry.latest_idea_id,
        seen_idea_ids=entry.seen_idea_ids,
        source_notes=entry.source_notes,
        latest_expected_benefit=entry.latest_expected_benefit,
        latest_next_action=entry.latest_next_action,
        latest_rationale=entry.latest_rationale,
        review_note=note.strip(),
        reviewed_at=surprise_me_workflow_improvement.datetime.now(surprise_me_workflow_improvement.timezone.utc).isoformat(),
    )
    updated_entries = dict(registry.entries)
    updated_entries[updated_entry.idea_key] = updated_entry
    updated_registry = surprise_me_workflow_improvement.IdeaRegistry(path=registry.path, entries=updated_entries)
    surprise_me_workflow_improvement.write_idea_registry(updated_registry)
    return {
        "status": "updated",
        "registry_path": updated_registry.path.as_posix(),
        "idea": updated_entry.to_json(),
        "counts": updated_registry.counts(),
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    vault_root = Path(args.vault_root).resolve()
    staging_root = Path(args.staging_root).resolve()
    registry = _load_registry(vault_root, staging_root)
    if args.command == "list":
        return _list_payload(registry, status=str(args.status).strip())
    if args.command == "set-status":
        return _set_status_payload(
            registry,
            idea_key=str(args.idea_key),
            idea_id=str(args.idea_id),
            status=str(args.status),
            note=str(args.note),
        )
    raise SurpriseMeIdeaRegistryError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run(args)
    except SurpriseMeIdeaRegistryError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    except surprise_me_workflow_improvement.SurpriseMeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
