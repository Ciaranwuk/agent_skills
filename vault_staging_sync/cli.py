from __future__ import annotations

import argparse
import json
from pathlib import Path

from .sync import DEFAULT_CANONICAL_ROOT, DEFAULT_STAGING_ROOT, sync_staging_to_vault


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault_staging_sync")
    parser.add_argument(
        "--staging-root",
        default=str(DEFAULT_STAGING_ROOT),
        help="Workspace staging root containing vault-relative files.",
    )
    parser.add_argument(
        "--canonical-root",
        default=str(DEFAULT_CANONICAL_ROOT),
        help="Canonical vault root that receives promoted files.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete successfully promoted staged files after sync, but keep INDEX.md.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute actions without writing, deleting, or rewriting files.",
    )
    parser.add_argument(
        "--fail-on-collision",
        action="store_true",
        help="Exit non-zero when any non-mergeable staged file collides with canonical content.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    payload = sync_staging_to_vault(
        staging_root=Path(args.staging_root),
        canonical_root=Path(args.canonical_root),
        prune=args.prune,
        dry_run=args.dry_run,
    )
    print(json.dumps(payload, sort_keys=True))
    if args.fail_on_collision and payload["collisions"]:
        return 3
    return 0 if payload["ok"] else 1
