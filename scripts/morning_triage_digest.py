#!/usr/bin/env python3
"""Deterministic morning-triage digest worker."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from . import surprise_me_workflow_improvement  # type: ignore
except ImportError:  # pragma: no cover - direct script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import surprise_me_workflow_improvement  # type: ignore

DEFAULT_DB_PATH = Path("/home/cwilson/projects/mission_control/data/mission_control.sqlite")
DEFAULT_STAGING_ROOT = Path("/home/cwilson/projects/_vault_staging")
RUNS_RELATIVE_DIR = Path("00_Inbox/morning_triage/runs")
DEEP_SLEEP_RUNS_RELATIVE_DIR = Path("00_Inbox/deep_sleep/runs")
SURPRISE_ME_RUNS_RELATIVE_DIR = Path("00_Inbox/surprise_me/runs")
DEFAULT_JOB_NAMES = (
    "vault-staging-sync-daily",
    "deep-sleep-link-synthesis-nightly",
    "surprise-me-workflow-improvement-nightly",
)
ATTENTION_RUN_STATES = {"failed", "timed_out"}
RETRYING_RUN_STATES = {"queued", "running"}
DEEP_SLEEP_JOB_NAME = "deep-sleep-link-synthesis-nightly"
SURPRISE_ME_JOB_NAME = "surprise-me-workflow-improvement-nightly"


class MorningTriageError(Exception):
    """Raised when the worker cannot complete safely."""


@dataclass(frozen=True)
class ParseIssue:
    path: str
    error: str


@dataclass(frozen=True)
class CronRunRecord:
    run_id: str
    state: str
    attempt: int
    scheduled_for: str
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    error_code: str | None
    error_message: str | None
    duration_ms: int | None

    def finished_or_scheduled_at(self) -> datetime:
        for value in (self.finished_at, self.started_at, self.scheduled_for):
            if value:
                return _parse_iso_datetime(value)
        raise MorningTriageError(f"run {self.run_id} is missing all timestamps")

    def started_or_scheduled_at(self) -> datetime:
        for value in (self.started_at, self.scheduled_for):
            if value:
                return _parse_iso_datetime(value)
        raise MorningTriageError(f"run {self.run_id} is missing start and schedule timestamps")

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CronJobRecord:
    job_id: str
    name: str
    owner: str
    state: str
    max_retries: int
    failure_streak: int
    last_run_at: str | None
    last_success_at: str | None
    runs: tuple[CronRunRecord, ...]


@dataclass(frozen=True)
class TicketRecord:
    ticket_key: str
    title: str
    lane: str
    project: str
    priority: str
    owner: str | None
    updated_at: str
    created_at: str


@dataclass(frozen=True)
class TicketContext:
    total_open: int
    lane_counts: dict[str, int]
    spotlight_tickets: tuple[TicketRecord, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "total_open": self.total_open,
            "lane_counts": dict(self.lane_counts),
            "spotlight_tickets": [
                {
                    "ticket_key": ticket.ticket_key,
                    "title": ticket.title,
                    "lane": ticket.lane,
                    "project": ticket.project,
                    "priority": ticket.priority,
                    "owner": ticket.owner,
                    "updated_at": ticket.updated_at,
                    "created_at": ticket.created_at,
                }
                for ticket in self.spotlight_tickets
            ],
        }


@dataclass(frozen=True)
class ArtifactSummary:
    artifact_type: str
    job_name: str
    path: str
    status: str
    details: str

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RankedItem:
    rank_group: int
    job_name: str
    title: str
    detail: str
    severity: str
    item_type: str
    state: str = ""
    run_id: str = ""
    artifact_path: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "job_name": self.job_name,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
            "type": self.item_type,
            "kind": self.item_type,
            "state": self.state,
            "run_id": self.run_id,
            "artifact_path": self.artifact_path,
        }


@dataclass
class RunArtifacts:
    run_id: str
    run_report: Path
    json_sidecar: Path


DEEP_SLEEP_CONNECTION_PREVIEW_LIMIT = 2
_DEEP_SLEEP_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_DEEP_SLEEP_RELATION_LABELS = {
    "decision_affects_log": "decision affects log",
    "session_continues_plan": "session continues plan",
    "repeated_open_question": "repeated open question",
    "duplicate_or_near_duplicate_idea": "duplicate idea",
    "shared_theme": "shared theme",
    "follow_up_gap": "follow-up gap",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="morning_triage_digest")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))
    parser.add_argument("--lookback-hours", type=int, default=12)
    parser.add_argument(
        "--job-name",
        action="append",
        default=[],
        help="Repeatable cron job name to consider as an expected overnight job.",
    )
    parser.add_argument(
        "--mode",
        choices=("deterministic",),
        default="deterministic",
        help="Only deterministic mode is implemented in V1.",
    )
    return parser


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MorningTriageError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_job_names(values: list[str]) -> list[str]:
    names = values or list(DEFAULT_JOB_NAMES)
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in names:
        normalized = raw.strip()
        if not normalized:
            raise MorningTriageError("--job-name values must be non-empty")
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _ensure_under_root(root: Path, path: Path, label: str) -> Path:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
        raise MorningTriageError(f"{label} escapes allowed root: {path}")
    return path_resolved


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_cron_jobs(db_path: Path, lookback_start: datetime) -> dict[str, CronJobRecord]:
    with _connect(db_path) as conn:
        job_rows = conn.execute(
            """
            SELECT job_id, name, owner, state, max_retries, failure_streak, last_run_at, last_success_at
            FROM cron_jobs
            """
        ).fetchall()
        run_rows = conn.execute(
            """
            SELECT
                r.run_id,
                r.job_id,
                r.state,
                r.attempt,
                r.scheduled_for,
                r.started_at,
                r.finished_at,
                r.exit_code,
                r.error_code,
                r.error_message,
                r.duration_ms
            FROM cron_job_runs AS r
            WHERE r.scheduled_for >= ?
            ORDER BY r.scheduled_for DESC, r.run_id DESC
            """,
            (lookback_start.isoformat(timespec="seconds"),),
        ).fetchall()

    runs_by_job: dict[str, list[CronRunRecord]] = {}
    for row in run_rows:
        runs_by_job.setdefault(row["job_id"], []).append(
            CronRunRecord(
                run_id=row["run_id"],
                state=row["state"],
                attempt=int(row["attempt"]),
                scheduled_for=row["scheduled_for"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                exit_code=row["exit_code"],
                error_code=row["error_code"],
                error_message=row["error_message"],
                duration_ms=row["duration_ms"],
            )
        )

    jobs: dict[str, CronJobRecord] = {}
    for row in job_rows:
        jobs[row["name"]] = CronJobRecord(
            job_id=row["job_id"],
            name=row["name"],
            owner=row["owner"],
            state=row["state"],
            max_retries=int(row["max_retries"]),
            failure_streak=int(row["failure_streak"]),
            last_run_at=row["last_run_at"],
            last_success_at=row["last_success_at"],
            runs=tuple(runs_by_job.get(row["job_id"], [])),
        )
    return jobs


def _load_open_tickets(db_path: Path) -> list[TicketRecord]:
    with _connect(db_path) as conn:
        ticket_rows = conn.execute(
            """
            SELECT ticket_key, title, lane, project, priority, owner, updated_at, created_at
            FROM tickets
            WHERE lane != 'Done'
            ORDER BY
                CASE lane
                    WHEN 'Blocked' THEN 0
                    WHEN 'In Progress' THEN 1
                    WHEN 'Ready' THEN 2
                    WHEN 'Backlog' THEN 3
                    ELSE 4
                END,
                updated_at DESC,
                ticket_key ASC
            """
        ).fetchall()
    return [
        TicketRecord(
            ticket_key=row["ticket_key"],
            title=row["title"],
            lane=row["lane"],
            project=row["project"],
            priority=row["priority"],
            owner=row["owner"],
            updated_at=row["updated_at"],
            created_at=row["created_at"],
        )
        for row in ticket_rows
    ]


def _job_has_retrying_run(job: CronJobRecord) -> bool:
    return any(run.state in RETRYING_RUN_STATES and run.attempt > 1 for run in job.runs)


def _collect_jobs(all_jobs: dict[str, CronJobRecord], expected_names: list[str]) -> tuple[list[str], list[RankedItem]]:
    considered_names = list(expected_names)
    urgent_items: list[RankedItem] = []

    for name in expected_names:
        job = all_jobs.get(name)
        if job is None:
            urgent_items.append(
                RankedItem(
                    rank_group=2,
                    job_name=name,
                    title=f"Expected job missing from Mission Control: {name}",
                    detail="No cron_jobs row exists for this expected overnight job.",
                    severity="high",
                    item_type="missing_job_definition",
                )
            )
            continue
        if not job.runs:
            urgent_items.append(
                RankedItem(
                    rank_group=2,
                    job_name=name,
                    title=f"No recent run found for {name}",
                    detail="Mission Control has no run for this expected job inside the lookback window.",
                    severity="high",
                    item_type="missing_recent_run",
                )
            )

    for name, job in sorted(all_jobs.items()):
        has_attention = any(run.state in ATTENTION_RUN_STATES for run in job.runs)
        if not has_attention and not _job_has_retrying_run(job):
            continue
        if name not in considered_names:
            considered_names.append(name)

    return considered_names, urgent_items


def _latest_run(job: CronJobRecord | None) -> CronRunRecord | None:
    if job is None or not job.runs:
        return None
    return job.runs[0]


def _later_success_exists(job: CronJobRecord, *, after_run: CronRunRecord) -> bool:
    after_time = after_run.finished_or_scheduled_at()
    for candidate in job.runs:
        if candidate.state != "succeeded":
            continue
        if candidate.finished_or_scheduled_at() > after_time:
            return True
    return False


def _parse_run_timestamp_from_name(path: Path, prefix: str) -> datetime | None:
    stem = path.stem
    if not stem.startswith(prefix):
        return None
    parts = stem.split("-")
    if len(parts) < 5:
        return None
    try:
        return datetime.strptime(f"{parts[-2]}-{parts[-1]}", "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _find_matching_sidecar(
    runs_dir: Path,
    prefix: str,
    *,
    not_before: datetime,
) -> Path | None:
    candidates: list[tuple[datetime, Path]] = []
    if not runs_dir.is_dir():
        return None
    for path in sorted(runs_dir.glob(f"{prefix}*.json")):
        resolved = path.resolve()
        if resolved != path and runs_dir.resolve() not in resolved.parents:
            raise MorningTriageError(f"unsafe sidecar path outside staging root: {path}")
        timestamp = _parse_run_timestamp_from_name(path, prefix)
        if timestamp is None:
            continue
        candidates.append((timestamp, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1].name))
    for timestamp, path in candidates:
        if timestamp > not_before:
            return path
    return candidates[-1][1]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MorningTriageError(f"unable to read sidecar: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON sidecar must contain an object")
    return payload


def _artifact_rel_path(staging_root: Path, path: Path) -> str:
    return path.relative_to(staging_root).as_posix()


def _load_worker_artifact(
    *,
    staging_root: Path,
    job_name: str,
    job: CronJobRecord | None,
    runs_relative_dir: Path,
    prefix: str,
    parse_issues: list[ParseIssue],
    urgent_items: list[RankedItem],
    follow_ups: list[RankedItem],
    artifacts_considered: list[ArtifactSummary],
) -> None:
    latest = _latest_run(job)
    if latest is None or latest.state != "succeeded":
        return

    runs_dir = _ensure_under_root(staging_root, staging_root / runs_relative_dir, "worker runs directory")
    latest_run_started_at = latest.started_or_scheduled_at()
    sidecar_path = _find_matching_sidecar(runs_dir, prefix, not_before=latest_run_started_at)
    if sidecar_path is None:
        detail = f"No staged {prefix.rstrip('-')} sidecar is newer than or equal to run `{latest.run_id}`."
        urgent_items.append(
            RankedItem(
                rank_group=3,
                job_name=job_name,
                title=f"Worker artifact missing for successful job {job_name}",
                detail=detail,
                severity="medium",
                item_type="missing_artifact",
                state=latest.state,
                run_id=latest.run_id,
            )
        )
        artifacts_considered.append(
            ArtifactSummary(
                artifact_type=prefix.rstrip("-").lower(),
                job_name=job_name,
                path="",
                status="missing",
                details=detail,
            )
        )
        return

    rel_path = _artifact_rel_path(staging_root, sidecar_path)
    artifact_timestamp = _parse_run_timestamp_from_name(sidecar_path, prefix)
    if artifact_timestamp is None or artifact_timestamp <= latest_run_started_at:
        detail = f"Latest staged sidecar `{rel_path}` is older than run `{latest.run_id}`."
        parse_issues.append(ParseIssue(path=rel_path, error="stale artifact older than summarized Mission Control run"))
        urgent_items.append(
            RankedItem(
                rank_group=3,
                job_name=job_name,
                title=f"Worker artifact stale for successful job {job_name}",
                detail=detail,
                severity="medium",
                item_type="stale_artifact",
                state=latest.state,
                run_id=latest.run_id,
                artifact_path=rel_path,
            )
        )
        artifacts_considered.append(
            ArtifactSummary(
                artifact_type=prefix.rstrip("-").lower(),
                job_name=job_name,
                path=rel_path,
                status="stale",
                details=detail,
            )
        )
        return

    try:
        payload = _load_json(sidecar_path)
    except ValueError as exc:
        parse_issues.append(ParseIssue(path=rel_path, error=f"failed to parse sidecar: {exc}"))
        urgent_items.append(
            RankedItem(
                rank_group=3,
                job_name=job_name,
                title=f"Worker artifact unreadable for successful job {job_name}",
                detail=f"Staged sidecar `{rel_path}` could not be parsed.",
                severity="medium",
                item_type="unreadable_artifact",
                state=latest.state,
                run_id=latest.run_id,
                artifact_path=rel_path,
            )
        )
        artifacts_considered.append(
            ArtifactSummary(
                artifact_type=prefix.rstrip("-").lower(),
                job_name=job_name,
                path=rel_path,
                status="unreadable",
                details=str(exc),
            )
        )
        return

    low_confidence = _is_explicitly_low_confidence(payload)
    if low_confidence:
        urgent_items.append(
            RankedItem(
                rank_group=3,
                job_name=job_name,
                title=f"Worker artifact flagged low-confidence for {job_name}",
                detail=f"Staged sidecar `{rel_path}` explicitly marks low-confidence output.",
                severity="medium",
                item_type="low_confidence_artifact",
                state=latest.state,
                run_id=latest.run_id,
                artifact_path=rel_path,
            )
        )

    artifacts_considered.append(
        ArtifactSummary(
            artifact_type=prefix.rstrip("-").lower(),
            job_name=job_name,
            path=rel_path,
            status="loaded",
            details=f"Loaded staged sidecar for run `{latest.run_id}`.",
        )
    )
    highlight = _build_worker_highlight(
        job_name=job_name,
        payload=payload,
        rel_path=rel_path,
        run_id=latest.run_id,
        staging_root=staging_root,
    )
    if highlight is not None:
        follow_ups.append(highlight)


def _is_explicitly_low_confidence(payload: dict[str, Any]) -> bool:
    root_confidence = str(payload.get("confidence", "")).strip().lower()
    if root_confidence in {"low", "low_confidence", "tentative"}:
        return True
    selected_idea = payload.get("selected_idea")
    if isinstance(selected_idea, dict):
        confidence = str(selected_idea.get("confidence", "")).strip().lower()
        if confidence in {"low", "low_confidence", "tentative"}:
            return True
    return False


def _load_surprise_me_registry(staging_root: Path) -> surprise_me_workflow_improvement.IdeaRegistry:
    vault_root = surprise_me_workflow_improvement.DEFAULT_VAULT_ROOT.resolve()
    if vault_root.is_dir():
        try:
            return surprise_me_workflow_improvement.load_idea_memory(vault_root, staging_root)
        except surprise_me_workflow_improvement.SurpriseMeError:
            pass
    return surprise_me_workflow_improvement.load_idea_registry(staging_root)


def _surprise_me_suggestion_suppression_reason(
    payload: dict[str, Any],
    registry: surprise_me_workflow_improvement.IdeaRegistry,
) -> str:
    selected_idea = payload.get("selected_idea")
    if not isinstance(selected_idea, dict):
        return ""
    review_status = str(selected_idea.get("review_status", "")).strip().lower()
    if review_status and review_status != "unreviewed":
        return review_status
    idea_key = str(selected_idea.get("idea_key", "")).strip()
    if not idea_key:
        return ""
    entry = registry.entries.get(idea_key)
    if entry is None:
        return ""
    if entry.review_status != "unreviewed":
        return entry.review_status
    if surprise_me_workflow_improvement._review_note_marks_existing_coverage(entry.review_note):
        return "covered"
    return ""


def _build_worker_highlight(
    *,
    job_name: str,
    payload: dict[str, Any],
    rel_path: str,
    run_id: str,
    staging_root: Path,
) -> RankedItem | None:
    if job_name == DEEP_SLEEP_JOB_NAME:
        connections = payload.get("connections")
        high_confidence_count = 0
        if isinstance(connections, list):
            high_confidence_count = sum(
                1
                for item in connections
                if isinstance(item, dict) and str(item.get("confidence", "")).strip().lower() == "high"
            )
        connection_summary = _summarize_deep_sleep_connections(payload)
        title = "Deep-sleep highlights available"
        detail = (
            f"Clusters `{payload.get('clusters_found', 0)}`, high-confidence connections `{high_confidence_count}`, "
            f"report `{rel_path.replace('.json', '.md')}`."
        )
        if connection_summary:
            detail += f" New connections: {connection_summary}."
        return RankedItem(
            rank_group=4,
            job_name=job_name,
            title=title,
            detail=detail,
            severity="info",
            item_type="deep_sleep_highlight",
            state=str(payload.get("status", "")),
            run_id=run_id,
            artifact_path=rel_path,
        )

    if job_name == SURPRISE_ME_JOB_NAME:
        diagnostics = payload.get("selection_diagnostics")
        if isinstance(diagnostics, dict) and diagnostics.get("run_health") == "healthy_but_empty":
            lane_health = diagnostics.get("lane_health", {})
            active_lanes = 0
            if isinstance(lane_health, dict):
                active_lanes = sum(
                    1 for item in lane_health.values()
                    if isinstance(item, dict) and int(item.get("generated", 0) or 0) > 0
                )
            return RankedItem(
                rank_group=5,
                job_name=job_name,
                title="Workflow suggestion scout healthy but empty",
                detail=(
                    f"No eligible proposal was selected across `{active_lanes}` active lane(s); "
                    f"review lane counts and exclusions in `{rel_path.replace('.json', '.md')}`."
                ),
                severity="info",
                item_type="workflow_suggestion_healthy_empty",
                state=str(payload.get("status", "")),
                run_id=run_id,
                artifact_path=rel_path,
            )
        suppression_reason = _surprise_me_suggestion_suppression_reason(
            payload,
            _load_surprise_me_registry(staging_root),
        )
        if suppression_reason:
            return None
        selected_idea = payload.get("selected_idea")
        if not isinstance(selected_idea, dict):
            title = "Workflow suggestion lane ran with no proposal"
            detail = f"Worker finished cleanly with no selected suggestion; report `{rel_path.replace('.json', '.md')}`."
        else:
            title = str(selected_idea.get("title", "Workflow suggestion")).strip() or "Workflow suggestion"
            expected_benefit = str(selected_idea.get("expected_benefit", "")).strip() or "not stated"
            next_action = str(selected_idea.get("next_action", "")).strip() or "review report"
            rationale = str(selected_idea.get("rationale", "")).strip() or str(payload.get("follow_up_recommendation", "")).strip()
            context_summary = _summarize_surprise_me_context(payload, selected_idea)
            expected_benefit = expected_benefit.rstrip(".")
            triage_action = _render_surprise_me_triage_action(next_action, payload=payload, selected_idea=selected_idea)
            detail_parts = [
                f"Benefit: {expected_benefit}.",
                f"Triage: {triage_action}.",
            ]
            if rationale:
                detail_parts.append(f"Reason: {rationale.rstrip('.')}.")
            detail_parts.append(f"Context: {context_summary}.")
            detail_parts.append(f"Report `{rel_path.replace('.json', '.md')}`.")
            detail = " ".join(detail_parts)
        return RankedItem(
            rank_group=5,
            job_name=job_name,
            title=f"Workflow suggestion: {title}",
            detail=detail,
            severity="info",
            item_type="workflow_suggestion",
            state=str(payload.get("status", "")),
            run_id=run_id,
            artifact_path=rel_path,
        )

    return None


def _summarize_deep_sleep_connections(payload: dict[str, Any]) -> str:
    raw_connections = payload.get("connections")
    if not isinstance(raw_connections, list):
        return ""

    normalized: list[tuple[tuple[object, ...], str]] = []
    for item in raw_connections:
        if not isinstance(item, dict):
            continue
        relation_type = str(item.get("relation_type", "")).strip()
        source_notes_raw = item.get("source_notes")
        if not isinstance(source_notes_raw, list):
            continue
        source_notes = [str(note).strip() for note in source_notes_raw if str(note).strip()]
        if len(source_notes) < 2:
            continue
        confidence = str(item.get("confidence", "")).strip().lower()
        relation_label = _DEEP_SLEEP_RELATION_LABELS.get(relation_type, relation_type.replace("_", " ").strip() or "linked notes")
        preview = (
            f"{relation_label} "
            f"(`{_compact_note_ref(source_notes[0])}`, `{_compact_note_ref(source_notes[1])}`)"
        )
        sort_key = (
            _DEEP_SLEEP_CONFIDENCE_ORDER.get(confidence, 9),
            relation_label,
            source_notes[0],
            source_notes[1],
        )
        normalized.append((sort_key, preview))

    if not normalized:
        return ""

    normalized.sort(key=lambda item: item[0])
    preview_items = [item[1] for item in normalized[:DEEP_SLEEP_CONNECTION_PREVIEW_LIMIT]]
    remaining = len(normalized) - len(preview_items)
    if remaining > 0:
        preview_items.append(f"+{remaining} more")
    return "; ".join(preview_items)


def _compact_note_ref(note_path: str) -> str:
    value = str(note_path).strip()
    if not value:
        return "unknown"
    name = Path(value).name
    if name.endswith(".md"):
        return name[:-3] or name
    return name


def _summarize_surprise_me_context(payload: dict[str, Any], selected_idea: dict[str, Any]) -> str:
    source_notes_raw = selected_idea.get("source_notes")
    source_notes = [str(item).strip() for item in source_notes_raw if str(item).strip()] if isinstance(source_notes_raw, list) else []
    context_files_raw = payload.get("inputs", {}).get("context_files") if isinstance(payload.get("inputs"), dict) else []
    context_files = [str(item).strip() for item in context_files_raw if str(item).strip()] if isinstance(context_files_raw, list) else []
    deep_sleep_inputs_raw = payload.get("inputs", {}).get("deep_sleep_inputs") if isinstance(payload.get("inputs"), dict) else []
    deep_sleep_inputs = [str(item).strip() for item in deep_sleep_inputs_raw if str(item).strip()] if isinstance(deep_sleep_inputs_raw, list) else []

    sessions = sum(1 for note in source_notes if note.startswith("00_Inbox/SESSION-"))
    work_logs = sum(1 for note in source_notes if note.startswith("20_Work_Logs/LOG-"))
    decisions = sum(1 for note in source_notes if note.startswith("10_Decisions/"))
    other_notes = len(source_notes) - sessions - work_logs - decisions
    parts = [_count_label(len(source_notes), "note")]
    if sessions:
        parts.append(_count_label(sessions, "session"))
    if work_logs:
        parts.append(_count_label(work_logs, "work log"))
    if decisions:
        parts.append(_count_label(decisions, "decision"))
    if other_notes:
        parts.append(_count_label(other_notes, "other note"))
    if context_files:
        parts.append(_count_label(len(context_files), "context file"))
    if deep_sleep_inputs:
        parts.append(_count_label(len(deep_sleep_inputs), "deep-sleep input"))
    preview = source_notes[:3]
    if context_files:
        preview.extend(item for item in context_files if item not in preview)
    preview = preview[:4]
    if preview:
        parts.append("sample " + ", ".join(f"`{item}`" for item in preview))
    return "; ".join(parts)


def _render_surprise_me_triage_action(
    action: str,
    *,
    payload: dict[str, Any],
    selected_idea: dict[str, Any],
) -> str:
    normalized = action.strip().lower()
    title = str(selected_idea.get("title", "")).strip()
    follow_up = str(payload.get("follow_up_recommendation", "")).strip()
    if normalized == "proposal_only":
        if follow_up:
            return follow_up.rstrip(".")
        if title:
            return f"review `{title}` in the digest and decide whether to ignore it, note it, or turn it into a checklist or ticket"
        return "review the suggestion in the digest and decide whether to ignore it, note it, or turn it into a checklist or ticket"
    if normalized == "ticket_recommendation":
        if title:
            return f"turn `{title}` into a Mission Control ticket if the suggestion still looks useful after review"
        return "turn the suggestion into a Mission Control ticket if it still looks useful after review"
    return normalized or "review the staged report"


def _count_label(value: int, singular: str) -> str:
    suffix = "" if value == 1 else "s"
    return f"{value} {singular}{suffix}"


def _job_status_payload(job_name: str, job: CronJobRecord | None) -> dict[str, object]:
    if job is None:
        return {
            "job_name": job_name,
            "present": False,
            "job_state": "missing",
            "latest_run": None,
            "recent_runs": [],
            "failure_streak": 0,
            "max_retries": 0,
        }

    latest = _latest_run(job)
    return {
        "job_name": job.name,
        "present": True,
        "job_state": job.state,
        "latest_run": latest.to_json() if latest is not None else None,
        "recent_runs": [run.to_json() for run in job.runs],
        "failure_streak": job.failure_streak,
        "max_retries": job.max_retries,
        "last_run_at": job.last_run_at,
        "last_success_at": job.last_success_at,
    }


def _collect_run_attention(job_name: str, job: CronJobRecord, urgent_items: list[RankedItem]) -> None:
    for run in job.runs:
        if _later_success_exists(job, after_run=run):
            continue
        if run.state in ATTENTION_RUN_STATES:
            detail = f"Run `{run.run_id}` {run.state}."
            if run.error_code:
                detail += f" error_code=`{run.error_code}`."
            if run.error_message:
                detail += f" {run.error_message}"
            urgent_items.append(
                RankedItem(
                    rank_group=1,
                    job_name=job_name,
                    title=f"{job_name} {run.state}",
                    detail=detail,
                    severity="high",
                    item_type="failed_run" if run.state == "failed" else "timed_out_run",
                    state=run.state,
                    run_id=run.run_id,
                )
            )
        elif run.state in RETRYING_RUN_STATES and run.attempt > 1:
            urgent_items.append(
                RankedItem(
                    rank_group=1,
                    job_name=job_name,
                    title=f"{job_name} retrying",
                    detail=f"Run `{run.run_id}` is `{run.state}` on attempt `{run.attempt}`.",
                    severity="high",
                    item_type="retrying_run",
                    state=run.state,
                    run_id=run.run_id,
                )
            )


def _build_ticket_context(tickets: list[TicketRecord]) -> TicketContext:
    lane_counts: dict[str, int] = {}
    for ticket in tickets:
        lane_counts[ticket.lane] = lane_counts.get(ticket.lane, 0) + 1
    spotlight = tuple(ticket for ticket in tickets if ticket.lane in ("Blocked", "In Progress", "Ready"))[:5]
    return TicketContext(total_open=len(tickets), lane_counts=lane_counts, spotlight_tickets=spotlight)


def _collect_ticket_attention(
    tickets: list[TicketRecord],
    *,
    lookback_start: datetime,
    urgent_items: list[RankedItem],
    follow_ups: list[RankedItem],
) -> TicketContext:
    context = _build_ticket_context(tickets)
    for ticket in tickets:
        if ticket.lane != "Blocked":
            continue
        updated_at = _parse_iso_datetime(ticket.updated_at)
        if updated_at < lookback_start:
            continue
        urgent_items.append(
            RankedItem(
                rank_group=3,
                job_name="mission-control-tickets",
                title=f"Blocked ticket updated overnight: {ticket.ticket_key} {ticket.title}",
                detail=(
                    f"Mission Control ticket `{ticket.ticket_key}` is in `Blocked` and was updated at "
                    f"`{ticket.updated_at}`. Project `{ticket.project}`, priority `{ticket.priority}`."
                ),
                severity="medium",
                item_type="recently_blocked_ticket",
                state=ticket.lane,
                run_id=ticket.ticket_key,
            )
        )

    summary_parts = [f"{context.total_open} open ticket(s)"]
    for lane in ("In Progress", "Blocked", "Ready", "Backlog", "Review"):
        count = context.lane_counts.get(lane, 0)
        if count:
            summary_parts.append(f"{lane} {count}")
    spotlight_text = ", ".join(f"`{ticket.ticket_key}` {ticket.title}" for ticket in context.spotlight_tickets[:3])
    detail = "; ".join(summary_parts)
    if spotlight_text:
        detail += f". Spotlight: {spotlight_text}."
    follow_ups.append(
        RankedItem(
            rank_group=4,
            job_name="mission-control-tickets",
            title="Mission Control ticket context",
            detail=detail,
            severity="info",
            item_type="ticket_lane_summary",
        )
    )
    return context


def _sort_ranked_items(items: list[RankedItem]) -> list[RankedItem]:
    severity_order = {"high": 0, "medium": 1, "info": 2}
    return sorted(
        items,
        key=lambda item: (
            item.rank_group,
            severity_order.get(item.severity, 9),
            item.job_name,
            item.title,
            item.run_id,
        ),
    )


def _next_run_id(staging_root: Path) -> str:
    del staging_root
    return datetime.now(timezone.utc).strftime("MORNING-TRIAGE-RUN-%Y%m%d-%H%M%S")


def _ensure_output_paths(staging_root: Path, run_id: str) -> RunArtifacts:
    runs_dir = _ensure_under_root(staging_root, staging_root / RUNS_RELATIVE_DIR, "morning triage output directory")
    runs_dir.mkdir(parents=True, exist_ok=True)
    report = runs_dir / f"{run_id}.md"
    sidecar = runs_dir / f"{run_id}.json"
    if report.exists() or sidecar.exists():
        raise MorningTriageError(f"collision: run artifacts already exist for {run_id}")
    return RunArtifacts(run_id=run_id, run_report=report, json_sidecar=sidecar)


def _write_new_file(path: Path, content: str) -> None:
    if path.exists():
        raise MorningTriageError(f"refusing to overwrite existing file: {path}")
    path.write_text(content, encoding="utf-8")


def _render_run_report(
    *,
    run_id: str,
    generated_at: str,
    lookback_hours: int,
    status: str,
    jobs_considered: list[str],
    urgent_items: list[RankedItem],
    follow_ups: list[RankedItem],
    ticket_context: TicketContext,
    parse_issues: list[ParseIssue],
    artifacts_considered: list[ArtifactSummary],
    artifacts: RunArtifacts,
    staging_root: Path,
) -> str:
    output_files = [
        _artifact_rel_path(staging_root, artifacts.run_report),
        _artifact_rel_path(staging_root, artifacts.json_sidecar),
    ]
    lines = [
        "---",
        "type: morning_triage_run",
        f"date: {generated_at[:10]}",
        "project: mission-control",
        f"run_id: {run_id}",
        f"lookback_hours: {lookback_hours}",
        "mode: deterministic",
        f"status: {status}",
        f"jobs_considered: {len(jobs_considered)}",
        f"urgent_items: {len(urgent_items)}",
        "---",
        "",
        f"# {run_id}",
        "",
        "## Summary",
        f"- Status: `{status}`",
        f"- Lookback hours: `{lookback_hours}`",
        f"- Jobs considered: `{len(jobs_considered)}`",
        f"- Urgent items: `{len(urgent_items)}`",
        f"- Follow-ups: `{len(follow_ups)}`",
    ]
    if parse_issues:
        lines.append(f"- Degraded parse handling: `{len(parse_issues)}` issue(s)")
    lines.extend(
        [
            "",
            "## Jobs Considered",
            *(f"- `{name}`" for name in jobs_considered),
            "",
            "## Mission Control Ticket Context",
            f"- Open tickets: `{ticket_context.total_open}`",
        ]
    )
    if ticket_context.lane_counts:
        for lane in ("In Progress", "Blocked", "Ready", "Backlog", "Review"):
            count = ticket_context.lane_counts.get(lane, 0)
            if count:
                lines.append(f"- {lane}: `{count}`")
    else:
        lines.append("- No open Mission Control tickets.")
    if ticket_context.spotlight_tickets:
        lines.append("- Spotlight:")
        for ticket in ticket_context.spotlight_tickets:
            lines.append(
                f"  - `{ticket.ticket_key}` `{ticket.lane}` `{ticket.priority}`: {ticket.title}"
            )
    lines.extend(
        [
            "",
            "## Urgent Items",
        ]
    )
    if urgent_items:
        for item in urgent_items:
            lines.append(f"- {item.title}")
            lines.append(f"  - Severity: `{item.severity}`")
            lines.append(f"  - Detail: {item.detail}")
            if item.run_id:
                lines.append(f"  - Run id: `{item.run_id}`")
            if item.artifact_path:
                lines.append(f"  - Artifact: `{item.artifact_path}`")
    else:
        lines.append("- Nothing urgent overnight.")
    lines.extend(["", "## Follow-Ups"])
    if follow_ups:
        for item in follow_ups:
            lines.append(f"- {item.title}")
            lines.append(f"  - Detail: {item.detail}")
            if item.artifact_path:
                lines.append(f"  - Artifact: `{item.artifact_path}`")
    else:
        lines.append("- No additional follow-up beyond routine morning review.")
    lines.extend(["", "## Artifacts Considered"])
    if artifacts_considered:
        for artifact in artifacts_considered:
            suffix = f" `{artifact.path}`" if artifact.path else ""
            lines.append(f"- `{artifact.job_name}` `{artifact.status}`{suffix}: {artifact.details}")
    else:
        lines.append("- No staged worker artifacts were considered.")
    if parse_issues:
        lines.extend(["", "## Parse Issues"])
        for issue in parse_issues:
            lines.append(f"- `{issue.path}`: {issue.error}")
    lines.extend(["", "## Output Files"])
    for item in output_files:
        lines.append(f"- `{item}`")
    return "\n".join(lines) + "\n"


def _sidecar_payload(
    *,
    run_id: str,
    generated_at: str,
    args: argparse.Namespace,
    jobs_considered: list[str],
    job_statuses: list[dict[str, object]],
    ticket_context: TicketContext,
    artifacts_considered: list[ArtifactSummary],
    urgent_items: list[RankedItem],
    follow_ups: list[RankedItem],
    parse_issues: list[ParseIssue],
    artifacts: RunArtifacts,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "status": "attention_required" if urgent_items else "nothing_urgent",
        "generated_at": generated_at,
        "lookback_hours": args.lookback_hours,
        "mode": args.mode,
        "jobs_considered": jobs_considered,
        "job_statuses": job_statuses,
        "ticket_context": ticket_context.to_json(),
        "artifacts_considered": [item.to_json() for item in artifacts_considered],
        "urgent_items": [item.to_json() for item in urgent_items],
        "follow_ups": [item.to_json() for item in follow_ups],
        "outputs": {
            "run_report": artifacts.run_report.as_posix(),
            "json_sidecar": artifacts.json_sidecar.as_posix(),
        },
        "parse_issues": [asdict(item) for item in parse_issues],
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    db_path = Path(args.db_path).resolve()
    staging_root = Path(args.staging_root).resolve()
    if args.lookback_hours < 1:
        raise MorningTriageError("--lookback-hours must be >= 1")
    if not db_path.is_file():
        raise MorningTriageError(f"Mission Control DB not found: {db_path}")
    if not staging_root.exists():
        raise MorningTriageError(f"staging root not found: {staging_root}")

    expected_names = _normalize_job_names(args.job_name)
    now = datetime.now(timezone.utc)
    generated_at = now.isoformat(timespec="seconds")
    lookback_start = now - timedelta(hours=args.lookback_hours)

    all_jobs = _load_cron_jobs(db_path, lookback_start)
    open_tickets = _load_open_tickets(db_path)
    jobs_considered, urgent_items = _collect_jobs(all_jobs, expected_names)
    follow_ups: list[RankedItem] = []
    parse_issues: list[ParseIssue] = []
    artifacts_considered: list[ArtifactSummary] = []

    for job_name in jobs_considered:
        job = all_jobs.get(job_name)
        if job is None:
            continue
        _collect_run_attention(job_name, job, urgent_items)

    _load_worker_artifact(
        staging_root=staging_root,
        job_name=DEEP_SLEEP_JOB_NAME,
        job=all_jobs.get(DEEP_SLEEP_JOB_NAME),
        runs_relative_dir=DEEP_SLEEP_RUNS_RELATIVE_DIR,
        prefix="DEEP-SLEEP-RUN-",
        parse_issues=parse_issues,
        urgent_items=urgent_items,
        follow_ups=follow_ups,
        artifacts_considered=artifacts_considered,
    )
    _load_worker_artifact(
        staging_root=staging_root,
        job_name=SURPRISE_ME_JOB_NAME,
        job=all_jobs.get(SURPRISE_ME_JOB_NAME),
        runs_relative_dir=SURPRISE_ME_RUNS_RELATIVE_DIR,
        prefix="SURPRISE-ME-RUN-",
        parse_issues=parse_issues,
        urgent_items=urgent_items,
        follow_ups=follow_ups,
        artifacts_considered=artifacts_considered,
    )
    ticket_context = _collect_ticket_attention(
        open_tickets,
        lookback_start=lookback_start,
        urgent_items=urgent_items,
        follow_ups=follow_ups,
    )

    sorted_urgent_items = _sort_ranked_items(urgent_items)
    sorted_follow_ups = _sort_ranked_items(follow_ups)
    status = "attention_required" if sorted_urgent_items else "nothing_urgent"
    run_id = _next_run_id(staging_root)
    artifacts = _ensure_output_paths(staging_root, run_id)

    report = _render_run_report(
        run_id=run_id,
        generated_at=generated_at,
        lookback_hours=args.lookback_hours,
        status=status,
        jobs_considered=jobs_considered,
        urgent_items=sorted_urgent_items,
        follow_ups=sorted_follow_ups,
        ticket_context=ticket_context,
        parse_issues=parse_issues,
        artifacts_considered=artifacts_considered,
        artifacts=artifacts,
        staging_root=staging_root,
    )
    _write_new_file(artifacts.run_report, report)

    job_statuses = [_job_status_payload(name, all_jobs.get(name)) for name in jobs_considered]
    sidecar = _sidecar_payload(
        run_id=run_id,
        generated_at=generated_at,
        args=args,
        jobs_considered=jobs_considered,
        job_statuses=job_statuses,
        ticket_context=ticket_context,
        artifacts_considered=artifacts_considered,
        urgent_items=sorted_urgent_items,
        follow_ups=sorted_follow_ups,
        parse_issues=parse_issues,
        artifacts=artifacts,
    )
    _write_new_file(artifacts.json_sidecar, json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    return {
        "run_id": run_id,
        "status": status,
        "jobs_considered": len(jobs_considered),
        "urgent_items": len(sorted_urgent_items),
        "output_file": artifacts.run_report.as_posix(),
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = run(args)
    except MorningTriageError as exc:
        print(
            json.dumps(
                {
                    "run_id": "",
                    "status": "failed",
                    "jobs_considered": 0,
                    "urgent_items": 0,
                    "output_file": "",
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
