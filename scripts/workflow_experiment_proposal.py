#!/usr/bin/env python3
"""Deterministic read-only workflow experiment proposal worker."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

# Mission Control invokes this worker as a file (``python3 scripts/...py``),
# while the unit tests import it as ``scripts.workflow_experiment_proposal``.
# In file mode Python puts ``scripts/`` on ``sys.path`` but not the repository
# root, so the package-qualified import below would fail.  Add the repository
# root only for that entry-point mode and keep the normal package import path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.workflow_suggestion_contract import build_workflow_suggestion

DEFAULT_VAULT_ROOT = Path("/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record")
DEFAULT_STAGING_ROOT = Path("/home/cwilson/projects/_vault_staging")
RUNS_RELATIVE_DIR = Path("00_Inbox/workflow_experiments/runs")
DATE_IN_NAME_RE = re.compile(r"(\d{8})")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,}")
FRICTION_TERMS = (
    "again",
    "blocked",
    "check",
    "checked",
    "confirm",
    "confirmed",
    "failed",
    "friction",
    "manual",
    "manually",
    "missing",
    "re-open",
    "re-read",
    "repeat",
    "repeated",
    "review",
    "reviewed",
    "slow",
    "stale",
    "timeout",
    "verify",
    "verified",
)
STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "but",
    "codex",
    "for",
    "from",
    "have",
    "into",
    "july",
    "not",
    "notes",
    "project",
    "session",
    "that",
    "the",
    "this",
    "vault",
    "what",
    "with",
    "work",
}


class WorkflowExperimentError(Exception):
    """Raised when the workflow experiment worker cannot proceed safely."""


@dataclass(frozen=True)
class SourceSignal:
    note_path: str
    line: int
    snippet: str
    score: int


@dataclass(frozen=True)
class ExperimentProposal:
    title: str
    hypothesis: str
    metric: str
    next_action: str
    review_cadence: str
    safety_boundary: str
    source_notes: tuple[str, ...]
    signals: tuple[SourceSignal, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow_experiment_proposal")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--include-sessions", action="store_true")
    parser.add_argument("--include-work-logs", action="store_true")
    parser.add_argument(
        "--context-file",
        action="append",
        default=[],
        help="Vault-relative markdown file to include as read-only context.",
    )
    parser.add_argument(
        "--mode",
        choices=("deterministic",),
        default="deterministic",
        help="Only deterministic mode is implemented in V1.",
    )
    return parser


def _parse_date_token(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _file_date_from_name(path: Path) -> date | None:
    match = DATE_IN_NAME_RE.search(path.name)
    if not match:
        return None
    return _parse_date_token(match.group(1))


def _ensure_under_root(root: Path, path: Path, label: str) -> Path:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
        raise WorkflowExperimentError(f"{label} escapes allowed root: {path}")
    return path_resolved


def _relative_note_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise WorkflowExperimentError(f"note escapes vault root: {path}") from exc


def _iter_allowed_paths(vault_root: Path, args: argparse.Namespace) -> list[Path]:
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=max(args.lookback_days - 1, 0))
    selected: list[Path] = []

    if args.include_sessions:
        for path in sorted((vault_root / "00_Inbox").glob("SESSION-*.md")):
            note_date = _file_date_from_name(path)
            if note_date is not None and start_date <= note_date <= today:
                selected.append(path.resolve())

    if args.include_work_logs:
        for path in sorted((vault_root / "20_Work_Logs").glob("LOG-*.md")):
            note_date = _file_date_from_name(path)
            if note_date is not None and start_date <= note_date <= today:
                selected.append(path.resolve())

    for rel_path in sorted(dict.fromkeys(args.context_file)):
        if not str(rel_path).strip():
            raise WorkflowExperimentError("--context-file values must not be empty")
        context_path = _ensure_under_root(vault_root, vault_root / rel_path, "context file")
        if not context_path.is_file():
            raise WorkflowExperimentError(f"context file not found: {rel_path}")
        selected.append(context_path)

    return sorted(dict.fromkeys(selected), key=lambda item: item.as_posix())


def _tokenize(value: str) -> tuple[str, ...]:
    return tuple(
        token.lower()
        for token in TOKEN_RE.findall(value)
        if token.lower() not in STOPWORDS and not token.isdigit()
    )


def _iter_friction_signals(vault_root: Path, paths: Iterable[Path]) -> list[SourceSignal]:
    signals: list[SourceSignal] = []
    for path in paths:
        rel_path = _relative_note_path(path, vault_root)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise WorkflowExperimentError(f"failed to read {rel_path}: {exc}") from exc
        for idx, line in enumerate(lines, start=1):
            normalized = line.lower()
            score = sum(1 for term in FRICTION_TERMS if term in normalized)
            if score <= 0:
                continue
            snippet = re.sub(r"\s+", " ", line).strip()
            if not snippet:
                continue
            signals.append(SourceSignal(note_path=rel_path, line=idx, snippet=snippet[:240], score=score))
    signals.sort(key=lambda item: (-item.score, item.note_path, item.line))
    return signals[:8]


def _build_proposal(signals: list[SourceSignal]) -> ExperimentProposal | None:
    if not signals:
        return None
    source_notes = tuple(sorted(dict.fromkeys(signal.note_path for signal in signals)))
    token_counts = Counter(
        token
        for signal in signals
        for token in _tokenize(signal.snippet)
        if token not in {"again", "checked", "confirmed", "reviewed", "verified"}
    )
    focus_terms = [token for token, _count in token_counts.most_common(4)]
    focus = ", ".join(focus_terms) if focus_terms else "recurring workflow friction"
    title = f"Run one read-only workflow experiment for {focus}"
    return ExperimentProposal(
        title=title,
        hypothesis=(
            "A single approved experiment aimed at the most repeated recent workflow friction "
            "will reduce next-morning review effort without granting the automation write authority."
        ),
        metric="One fewer repeated manual check or clearer next action in the next morning triage window.",
        next_action="Review this proposal, choose one bounded experiment, and only then approve any code or config change.",
        review_cadence="nightly proposal, human-approved implementation, next-morning result review",
        safety_boundary="read-only proposal artifact; no vault, repo, ticket, or schedule mutation",
        source_notes=source_notes,
        signals=tuple(signals[:5]),
    )


def _next_run_id(staging_root: Path) -> str:
    base = datetime.now(timezone.utc).strftime("WORKFLOW-EXPERIMENT-RUN-%Y%m%d-%H%M%S")
    runs_dir = staging_root / RUNS_RELATIVE_DIR
    candidate = base
    counter = 1
    while (runs_dir / f"{candidate}.md").exists() or (runs_dir / f"{candidate}.json").exists():
        candidate = f"{base}-{counter:02d}"
        counter += 1
    return candidate


def _write_new_file(path: Path, content: str) -> None:
    if path.exists():
        raise WorkflowExperimentError(f"refusing to overwrite existing file: {path}")
    path.write_text(content, encoding="utf-8")


def _render_report(
    *,
    run_id: str,
    status: str,
    lookback_days: int,
    inputs: list[str],
    proposal: ExperimentProposal | None,
    report_path: Path,
    sidecar_path: Path,
) -> str:
    lines = [
        "---",
        "type: workflow_experiment_run",
        f"date: {datetime.now(timezone.utc).date().isoformat()}",
        "project: agent_skills",
        f"run_id: {run_id}",
        f"status: {status}",
        "---",
        "",
        f"# {run_id}",
        "",
        "## Summary",
        f"- Status: `{status}`",
        f"- Lookback days: `{lookback_days}`",
        f"- Inputs read: `{len(inputs)}`",
    ]
    if proposal is None:
        lines.extend(
            [
                "- Selected experiment: `none`",
                "",
                "## Proposal",
                "- No repeated workflow-friction signal met the V1 threshold.",
            ]
        )
    else:
        lines.extend(
            [
                f"- Selected experiment: `{proposal.title}`",
                "",
                "## Proposal",
                f"- Title: {proposal.title}",
                f"- Hypothesis: {proposal.hypothesis}",
                f"- Metric: {proposal.metric}",
                f"- Next action: {proposal.next_action}",
                f"- Review cadence: {proposal.review_cadence}",
                f"- Safety boundary: {proposal.safety_boundary}",
                f"- Source notes: {', '.join(f'`{item}`' for item in proposal.source_notes)}",
                "",
                "## Evidence",
            ]
        )
        for signal in proposal.signals:
            lines.append(f'- `{signal.note_path}:{signal.line}` "{signal.snippet}"')
    lines.extend(
        [
            "",
            "## Inputs",
            *(f"- `{item}`" for item in inputs),
            "",
            "## Output Files",
            f"- `{report_path.as_posix()}`",
            f"- `{sidecar_path.as_posix()}`",
        ]
    )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> dict[str, object]:
    vault_root = Path(args.vault_root).resolve()
    staging_root = Path(args.staging_root).resolve()
    if not vault_root.is_dir():
        raise WorkflowExperimentError(f"vault root not found: {vault_root}")
    if not staging_root.exists():
        raise WorkflowExperimentError(f"staging root not found: {staging_root}")
    if args.lookback_days < 1:
        raise WorkflowExperimentError("--lookback-days must be >= 1")
    if not any((args.include_sessions, args.include_work_logs)):
        raise WorkflowExperimentError("at least one include flag is required")

    paths = _iter_allowed_paths(vault_root, args)
    inputs = [_relative_note_path(path, vault_root) for path in paths]
    signals = _iter_friction_signals(vault_root, paths)
    proposal = _build_proposal(signals)
    status = "succeeded" if proposal is not None else "no_findings"

    runs_dir = _ensure_under_root(staging_root, staging_root / RUNS_RELATIVE_DIR, "workflow experiment output directory")
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = _next_run_id(staging_root)
    report_path = runs_dir / f"{run_id}.md"
    sidecar_path = runs_dir / f"{run_id}.json"
    report = _render_report(
        run_id=run_id,
        status=status,
        lookback_days=args.lookback_days,
        inputs=inputs,
        proposal=proposal,
        report_path=report_path,
        sidecar_path=sidecar_path,
    )
    sidecar = {
        "run_id": run_id,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": args.lookback_days,
        "inputs": inputs,
        "selected_experiment": asdict(proposal) if proposal is not None else None,
        "workflow_suggestion": (
            build_workflow_suggestion(
                title=proposal.title,
                hypothesis=proposal.hypothesis,
                metric=proposal.metric,
                next_action=proposal.next_action,
                safety_boundary=proposal.safety_boundary,
                source_notes=proposal.source_notes,
                evidence=(asdict(signal) for signal in proposal.signals),
            )
            if proposal is not None
            else None
        ),
        "outputs": {
            "run_report": report_path.as_posix(),
            "json_sidecar": sidecar_path.as_posix(),
        },
    }
    _write_new_file(report_path, report)
    _write_new_file(sidecar_path, json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    return {
        "run_id": run_id,
        "status": status,
        "inputs_read": len(inputs),
        "signals_found": len(signals),
        "selected_experiment": proposal.title if proposal else "",
        "output_file": report_path.as_posix(),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run(args)
    except WorkflowExperimentError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
