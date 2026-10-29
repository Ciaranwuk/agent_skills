from __future__ import annotations

import argparse
import json
from pathlib import Path

from .api import memory_get, memory_rebuild, memory_search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory_system")
    parser.add_argument("--workspace", default=".", help="Primary workspace root")
    parser.add_argument("--db", default=None, help="SQLite DB path (defaults to <workspace>/.memory_index.sqlite)")
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        help="Additional markdown source root (repeatable). If omitted, only --workspace is scanned.",
    )
    parser.add_argument(
        "--backend",
        choices=("builtin", "qmd"),
        default="builtin",
        help="Memory backend to use. get remains builtin-only; rebuild/search support builtin and draft qmd flows.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("rebuild", help="Rebuild/sync the index")
    p_index.add_argument("--force", action="store_true", help="Force full rebuild")

    p_search = sub.add_parser("search", help="Run memory_search")
    p_search.add_argument("query")
    p_search.add_argument("--max-results", type=int, default=10)
    p_search.add_argument("--min-score", type=float, default=0.0)

    p_get = sub.add_parser("get", help="Run memory_get")
    p_get.add_argument("path")
    p_get.add_argument("--from", dest="from_", type=int, default=None)
    p_get.add_argument("--lines", type=int, default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()

    if args.command == "rebuild":
        payload = memory_rebuild(
            workspace=workspace,
            db_path=args.db,
            source_roots=args.source_root,
            backend=args.backend,
            force=args.force,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0 if bool(payload.get("ok")) else 1

    if args.command == "search":
        payload = memory_search(
            query=args.query,
            maxResults=args.max_results,
            minScore=args.min_score,
            workspace=workspace,
            db_path=args.db,
            source_roots=args.source_root,
            backend=args.backend,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "get":
        if args.backend != "builtin":
            parser.error(f"backend '{args.backend}' is not available for get. memory_get stays Python-owned.")
        payload = memory_get(
            path=args.path,
            from_=args.from_,
            lines=args.lines,
            workspace=workspace,
            source_roots=args.source_root,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
