#!/usr/bin/env python3
"""Deterministic surprise-me workflow improvement worker."""

from __future__ import annotations

import argparse
from collections import Counter
from html import unescape
import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from memory_system.api import memory_get, memory_search
from memory_system.index import MemoryIndex
from scripts.workflow_suggestion_contract import build_workflow_suggestion
from scripts.workflow_lane_sources import matching_seeds, source_pack_metadata

DEFAULT_VAULT_ROOT = Path("/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record")
DEFAULT_STAGING_ROOT = Path("/home/cwilson/projects/_vault_staging")
DEFAULT_MEMORY_WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_ROADMAP_RELATIVE_PATH = Path("Technology_Roadmap.md")
DEFAULT_CURRENT_WORKFLOWS_RELATIVE_PATH = Path("60_How_To/HOWTO-current-workflows.md")
RUNS_RELATIVE_DIR = Path("00_Inbox/surprise_me/runs")
IDEA_REGISTRY_RELATIVE_PATH = Path("00_Inbox/surprise_me/idea_registry.json")
IDEA_REGISTRY_OVERLAY_RELATIVE_PATH = Path("00_Inbox/surprise_me/idea_registry.overlay.json")
DEEP_SLEEP_RUNS_RELATIVE_DIR = Path("00_Inbox/deep_sleep/runs")
WORKFLOW_EXPERIMENT_RUNS_RELATIVE_DIR = Path("00_Inbox/workflow_experiments/runs")
DATE_IN_NAME_RE = re.compile(r"(\d{8})")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")
ANCHOR_RE = re.compile(
    r"(?:\bMC-\d{4}\b|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:md|py|sh|json|sqlite))"
)
TOKEN_RE = re.compile(r"[A-Za-z0-9]{4,}")
LOW_SIGNAL_ANCHOR_SUFFIXES = (".md", ".json", ".sqlite")
STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "been",
    "before",
    "from",
    "have",
    "into",
    "just",
    "more",
    "note",
    "notes",
    "that",
    "them",
    "then",
    "they",
    "this",
    "through",
    "very",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
}
ACTION_TERMS = (
    "again",
    "blocked",
    "check",
    "checked",
    "confirm",
    "confirmed",
    "continue",
    "continued",
    "debug",
    "follow-up",
    "follow up",
    "friction",
    "investigate",
    "manual",
    "manually",
    "need to",
    "next action",
    "re-opened",
    "re-read",
    "recheck",
    "reconcile",
    "repeat",
    "repeated",
    "review",
    "reviewed",
    "slow",
    "verify",
    "verified",
)
MANUAL_TERMS = (
    "check",
    "checked",
    "confirm",
    "confirmed",
    "manual",
    "manually",
    "re-opened",
    "re-read",
    "reconcile",
    "review",
    "reviewed",
    "verify",
    "verified",
)
FOLLOW_UP_TERMS = (
    "blocked",
    "continue",
    "continued",
    "debug",
    "follow-up",
    "follow up",
    "friction",
    "investigate",
    "need to",
    "next action",
    "repeat",
    "repeated",
    "slow",
)
RETRIEVAL_STOPWORDS = STOPWORDS | {
    "agent",
    "agents",
    "assistant",
    "ciaran",
    "codex",
    "date",
    "dates",
    "digest",
    "inbox",
    "index",
    "logged",
    "logging",
    "mission",
    "morning",
    "project",
    "projects",
    "record",
    "records",
    "session",
    "sessions",
    "staged",
    "staging",
    "surprise",
    "today",
    "vault",
    "workflow",
    "workflows",
}
RETRIEVAL_EXCLUDED_PREFIXES = (
    "00_Inbox/deep_sleep/runs/",
    "00_Inbox/morning_triage/runs/",
    "00_Inbox/surprise_me/runs/",
    "99_Assets/Templates/",
)
RETRIEVAL_EXCLUDED_PATHS = {"INDEX.md"}
REDUNDANCY_REVIEW_MARKERS = (
    "already",
    "covered",
    "covers",
    "duplicate",
    "duplicates",
    "existing",
    "owned",
    "owns",
    "redundant",
)
MANUAL_LOOP_COVERAGE_TOKENS = frozenset(
    {
        "checks",
        "follow",
        "helper",
        "manual",
        "morning",
        "recurring",
        "review",
    }
)
CHECKLIST_COVERAGE_TOKENS = frozenset(
    {
        "checklist",
        "deep",
        "digest",
        "follow",
        "manual",
        "morning",
        "recurring",
        "review",
        "sleep",
        "triage",
        "workflow",
    }
)


class SurpriseMeError(Exception):
    """Raised when deterministic worker execution cannot proceed safely."""


REVIEW_STATUSES = ("unreviewed", "approved", "discarded")


@dataclass(frozen=True)
class Evidence:
    note_path: str
    line: int
    snippet: str

    def to_markdown(self) -> str:
        escaped = self.snippet.replace("\n", " ").strip()
        return f'- `{self.note_path}:{self.line}` "{escaped}"'


@dataclass(frozen=True)
class ParsedNote:
    path: Path
    rel_path: str
    note_type: str
    note_date: date | None
    title: str
    body: str
    lines: tuple[str, ...]
    tokens: frozenset[str]
    anchors: tuple[str, ...]
    open_questions: tuple[str, ...]
    unchecked_tasks: tuple[str, ...]
    frontmatter_ok: bool


@dataclass(frozen=True)
class CurrentWorkflowRule:
    heading: str
    owner: str
    applies_to_categories: tuple[str, ...]
    covers: tuple[str, ...]
    examples: tuple[str, ...]
    notes: tuple[str, ...]
    tokens: frozenset[str]


@dataclass(frozen=True)
class EvidenceSignal:
    kind: str
    note_path: str
    note_type: str
    line: int
    snippet: str
    anchors: tuple[str, ...]
    tokens: frozenset[str]

    def to_evidence(self) -> Evidence:
        return Evidence(note_path=self.note_path, line=self.line, snippet=self.snippet)


@dataclass(frozen=True)
class DeepSleepFinding:
    run_id: str
    relation_type: str
    confidence: str
    suggested_action: str
    evidence: tuple[Evidence, ...]
    source_notes: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowExperimentFinding:
    run_id: str
    title: str
    hypothesis: str
    metric: str
    next_action: str
    report_path: str
    evidence: tuple[Evidence, ...]
    source_notes: tuple[str, ...]


@dataclass(frozen=True)
class CandidateIdea:
    idea_id: str
    idea_key: str
    title: str
    category: str
    score: int
    rationale: str
    expected_benefit: str
    risk_level: str
    next_action: str
    evidence: tuple[Evidence, ...]
    source_notes: tuple[str, ...]
    review_status: str = "unreviewed"
    times_seen: int = 0
    times_selected: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "idea_id": self.idea_id,
            "idea_key": self.idea_key,
            "title": self.title,
            "category": self.category,
            "score": self.score,
            "rationale": self.rationale,
            "expected_benefit": self.expected_benefit,
            "risk_level": self.risk_level,
            "next_action": self.next_action,
            "evidence": [asdict(item) for item in self.evidence],
            "source_notes": list(self.source_notes),
            "review_status": self.review_status,
            "times_seen": self.times_seen,
            "times_selected": self.times_selected,
        }


@dataclass(frozen=True)
class ParseIssue:
    path: str
    error: str


@dataclass(frozen=True)
class MemoryRetrievalHit:
    path: str
    score: float
    start_line: int
    end_line: int
    snippet: str
    excerpt: str

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "score": self.score,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "snippet": self.snippet,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class MemoryRetrievalResult:
    enabled: bool
    status: str
    query: str
    terms: tuple[str, ...]
    backend: str
    workspace: str
    db_path: str
    matched_paths: tuple[str, ...]
    added_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]
    results: tuple[MemoryRetrievalHit, ...] = ()
    warning: str = ""
    action: str = ""
    error: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "status": self.status,
            "query": self.query,
            "terms": list(self.terms),
            "backend": self.backend,
            "workspace": self.workspace,
            "db_path": self.db_path,
            "matched_paths": list(self.matched_paths),
            "added_paths": list(self.added_paths),
            "skipped_paths": list(self.skipped_paths),
            "results": [item.to_json() for item in self.results],
            "warning": self.warning,
            "action": self.action,
            "error": self.error,
        }


@dataclass(frozen=True)
class RoadmapTopic:
    slug: str
    title: str
    linked_note: str
    goal: str
    how_items: tuple[str, ...]
    source_paths: tuple[str, ...]
    query_terms: tuple[str, ...]


@dataclass(frozen=True)
class RoadmapFallbackResearch:
    enabled: bool
    status: str
    roadmap_path: str
    topic_slug: str
    topic_title: str
    linked_note: str
    goal: str
    query: str
    terms: tuple[str, ...]
    source_paths: tuple[str, ...]
    added_paths: tuple[str, ...]
    selection_seed: str
    results: tuple[MemoryRetrievalHit, ...] = ()
    web_research: "WebResearchResult | None" = None
    warning: str = ""
    action: str = ""
    error: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "status": self.status,
            "roadmap_path": self.roadmap_path,
            "topic_slug": self.topic_slug,
            "topic_title": self.topic_title,
            "linked_note": self.linked_note,
            "goal": self.goal,
            "query": self.query,
            "terms": list(self.terms),
            "source_paths": list(self.source_paths),
            "added_paths": list(self.added_paths),
            "selection_seed": self.selection_seed,
            "results": [item.to_json() for item in self.results],
            "web_research": self.web_research.to_json() if self.web_research is not None else None,
            "warning": self.warning,
            "action": self.action,
            "error": self.error,
        }


@dataclass(frozen=True)
class WebResearchHit:
    rank: int
    title: str
    url: str
    snippet: str
    excerpt: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class WebResearchResult:
    enabled: bool
    status: str
    provider: str
    query: str
    search_url: str
    max_results: int
    fetched_urls: tuple[str, ...]
    results: tuple[WebResearchHit, ...] = ()
    warning: str = ""
    action: str = ""
    error: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "status": self.status,
            "provider": self.provider,
            "query": self.query,
            "search_url": self.search_url,
            "max_results": self.max_results,
            "fetched_urls": list(self.fetched_urls),
            "results": [item.to_json() for item in self.results],
            "warning": self.warning,
            "action": self.action,
            "error": self.error,
        }


DEFAULT_WORKFLOW_WEB_FALLBACK_TOPIC_SLUG = "workflow-web-idea-fallback"


def _default_workflow_web_fallback_topic() -> RoadmapTopic:
    return RoadmapTopic(
        slug=DEFAULT_WORKFLOW_WEB_FALLBACK_TOPIC_SLUG,
        title="Workflow Improvement Ideas",
        linked_note="",
        goal="Find one practical external workflow-improvement idea when local notes do not produce a proposal.",
        how_items=(
            "Search for current workflow automation, assistant operations, and productivity system ideas.",
        ),
        source_paths=(),
        query_terms=(
            "workflow",
            "automation",
            "assistant",
            "operations",
            "productivity",
            "ideas",
        ),
    )


@dataclass
class RunArtifacts:
    run_id: str
    run_report: Path
    json_sidecar: Path


@dataclass(frozen=True)
class RegistryEntry:
    idea_key: str
    category: str
    title: str
    review_status: str
    first_seen_run_id: str
    last_seen_run_id: str
    first_seen_at: str
    last_seen_at: str
    times_seen: int
    times_selected: int
    latest_idea_id: str
    seen_idea_ids: tuple[str, ...]
    source_notes: tuple[str, ...]
    latest_expected_benefit: str
    latest_next_action: str
    latest_rationale: str
    review_note: str = ""
    reviewed_at: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "idea_key": self.idea_key,
            "category": self.category,
            "title": self.title,
            "review_status": self.review_status,
            "first_seen_run_id": self.first_seen_run_id,
            "last_seen_run_id": self.last_seen_run_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "times_seen": self.times_seen,
            "times_selected": self.times_selected,
            "latest_idea_id": self.latest_idea_id,
            "seen_idea_ids": list(self.seen_idea_ids),
            "source_notes": list(self.source_notes),
            "latest_expected_benefit": self.latest_expected_benefit,
            "latest_next_action": self.latest_next_action,
            "latest_rationale": self.latest_rationale,
            "review_note": self.review_note,
            "reviewed_at": self.reviewed_at,
        }


@dataclass(frozen=True)
class IdeaRegistry:
    path: Path
    entries: dict[str, RegistryEntry]

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in REVIEW_STATUSES}
        for entry in self.entries.values():
            counts[entry.review_status] = counts.get(entry.review_status, 0) + 1
        counts["total"] = len(self.entries)
        return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="surprise_me_workflow_improvement")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--include-sessions", action="store_true")
    parser.add_argument("--include-work-logs", action="store_true")
    parser.add_argument("--include-decisions", action="store_true")
    parser.add_argument(
        "--context-file",
        action="append",
        default=[],
        help="Vault-relative markdown file to include as read-only context.",
    )
    parser.add_argument(
        "--include-deep-sleep-runs",
        action="store_true",
        help="Read recent deep-sleep JSON sidecars from staging as optional context.",
    )
    parser.add_argument(
        "--deep-sleep-limit",
        type=int,
        default=3,
        help="Maximum number of recent deep-sleep run sidecars to read.",
    )
    parser.add_argument(
        "--include-workflow-experiment-runs",
        action="store_true",
        help="Read recent workflow-experiment JSON sidecars from staging as optional proposal context.",
    )
    parser.add_argument(
        "--workflow-experiment-limit",
        type=int,
        default=1,
        help="Maximum number of recent workflow-experiment run sidecars to read.",
    )
    parser.add_argument(
        "--enable-memory-retrieval",
        dest="memory_retrieval_enabled",
        action="store_true",
        help="Explicitly enable memory_system-backed retrieval of older canonical-vault notes.",
    )
    parser.add_argument(
        "--disable-memory-retrieval",
        dest="memory_retrieval_enabled",
        action="store_false",
        help="Disable memory_system-backed retrieval of older canonical-vault notes.",
    )
    parser.set_defaults(memory_retrieval_enabled=None)
    parser.add_argument(
        "--memory-workspace",
        default="",
        help="Workspace used for memory_system index lookup; defaults to the project root for the canonical vault.",
    )
    parser.add_argument(
        "--memory-db-path",
        default="",
        help="Optional path to the memory_system SQLite index.",
    )
    parser.add_argument(
        "--memory-max-results",
        type=int,
        default=8,
        help="Maximum memory-system search rows to inspect before note dedupe/filtering.",
    )
    parser.add_argument(
        "--memory-note-limit",
        type=int,
        default=4,
        help="Maximum number of retrieved vault notes to add as extra context.",
    )
    parser.add_argument(
        "--memory-min-score",
        type=float,
        default=0.0,
        help="Minimum memory-system score for retrieved context candidates.",
    )
    parser.add_argument(
        "--memory-backend",
        choices=("builtin", "qmd"),
        default="builtin",
        help="memory_system backend used for local retrieval.",
    )
    parser.add_argument(
        "--memory-context-lines",
        type=int,
        default=8,
        help="Maximum number of lines to fetch per retrieved memory hit.",
    )
    parser.add_argument(
        "--roadmap-file",
        default=str(DEFAULT_ROADMAP_RELATIVE_PATH),
        help="Vault-relative roadmap note used for fallback topic research when normal candidate selection finds nothing new.",
    )
    parser.add_argument(
        "--enable-web-research",
        dest="web_research_enabled",
        action="store_true",
        help="Allow the roadmap fallback lane to augment local research with bounded web search.",
    )
    parser.add_argument(
        "--disable-web-research",
        dest="web_research_enabled",
        action="store_false",
        help="Disable bounded web search for the roadmap fallback lane.",
    )
    # Preserve the existing explicit web fallback for compatibility. The new
    # lane source pack itself remains local-note-only; disabling this legacy
    # fallback is available via --disable-web-research.
    parser.set_defaults(web_research_enabled=True)
    parser.add_argument(
        "--web-search-provider",
        choices=("duckduckgo",),
        default="duckduckgo",
        help="Bounded web-search provider used by the roadmap fallback lane.",
    )
    parser.add_argument(
        "--web-max-results",
        type=int,
        default=3,
        help="Maximum web-search hits to keep in roadmap fallback diagnostics.",
    )
    parser.add_argument(
        "--web-fetch-limit",
        type=int,
        default=2,
        help="Maximum number of top web-search hits to fetch for excerpts.",
    )
    parser.add_argument(
        "--web-timeout-seconds",
        type=float,
        default=8.0,
        help="Per-request timeout for roadmap fallback web research.",
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
        raise SurpriseMeError(f"{label} escapes allowed root: {path}")
    return path_resolved


def _relative_note_path(path: Path, *roots: Path) -> str:
    resolved = path.resolve()
    for root in roots:
        root_resolved = root.resolve()
        try:
            return resolved.relative_to(root_resolved).as_posix()
        except ValueError:
            continue
    raise SurpriseMeError(f"note escapes allowed roots: {path}")


def _default_context_files(vault_root: Path, staging_root: Path) -> list[str]:
    rel_path = DEFAULT_CURRENT_WORKFLOWS_RELATIVE_PATH.as_posix()
    if (vault_root / rel_path).is_file() or (staging_root / rel_path).is_file():
        return [rel_path]
    return []


def _effective_context_files(vault_root: Path, staging_root: Path, args: argparse.Namespace) -> list[str]:
    merged = list(args.context_file) + _default_context_files(vault_root, staging_root)
    return sorted(dict.fromkeys(item for item in merged if str(item).strip()))


def _resolve_context_file(vault_root: Path, staging_root: Path, rel_path: str) -> Path:
    staging_path = _ensure_under_root(staging_root, staging_root / rel_path, "staged context file")
    if staging_path.is_file():
        return staging_path
    canonical_path = _ensure_under_root(vault_root, vault_root / rel_path, "context file")
    if canonical_path.is_file():
        return canonical_path
    raise SurpriseMeError(f"context file not found in vault or staging overlay: {rel_path}")


def _iter_allowed_paths(vault_root: Path, staging_root: Path, args: argparse.Namespace) -> list[Path]:
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=max(args.lookback_days - 1, 0))
    selected: list[Path] = []

    if args.include_sessions:
        inbox = vault_root / "00_Inbox"
        if inbox.is_dir():
            for path in sorted(inbox.glob("SESSION-*.md")):
                note_date = _file_date_from_name(path)
                if note_date is not None and start_date <= note_date <= today:
                    selected.append(path.resolve())

    if args.include_work_logs:
        logs = vault_root / "20_Work_Logs"
        if logs.is_dir():
            for path in sorted(logs.glob("LOG-*.md")):
                note_date = _file_date_from_name(path)
                if note_date is not None and start_date <= note_date <= today:
                    selected.append(path.resolve())

    if args.include_decisions:
        decisions = vault_root / "10_Decisions"
        if decisions.is_dir():
            for path in sorted(decisions.glob("DEC-*.md")):
                note_date = _file_date_from_name(path)
                if note_date is not None and start_date <= note_date <= today:
                    selected.append(path.resolve())

    for rel_path in _effective_context_files(vault_root, staging_root, args):
        context_path = _resolve_context_file(vault_root, staging_root, rel_path)
        selected.append(context_path)

    deduped: dict[str, Path] = {}
    for path in selected:
        deduped[path.as_posix()] = path
    return sorted(deduped.values(), key=lambda item: item.as_posix())


def _extract_frontmatter(lines: list[str]) -> tuple[dict[str, str], list[str], bool]:
    if not lines or lines[0].strip() != "---":
        return {}, lines, True
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            body_lines = lines[idx + 1 :]
            metadata: dict[str, str] = {}
            ok = True
            for raw in lines[1:idx]:
                stripped = raw.strip()
                if not stripped:
                    continue
                if raw.startswith((" ", "\t", "- ")):
                    continue
                if ":" not in raw:
                    ok = False
                    continue
                key, value = raw.split(":", 1)
                metadata[key.strip()] = value.strip()
            return metadata, body_lines, ok
    return {}, lines, False


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _tokenize(value: str) -> frozenset[str]:
    return frozenset(
        token.lower()
        for token in TOKEN_RE.findall(value)
        if token.lower() not in STOPWORDS and not token.isdigit()
    )


def _extract_title(lines: Iterable[str], fallback: str) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _extract_section_items(lines: list[str], heading: str) -> tuple[str, ...]:
    heading_lower = heading.lower()
    collecting = False
    results: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("## "):
            collecting = stripped[3:].strip().lower() == heading_lower
            continue
        if collecting and stripped.startswith("## "):
            break
        if collecting and stripped.startswith(("- ", "* ", "1. ", "2. ", "3. ", "4. ", "5. ")):
            item = re.sub(r"^[-*]\s+|^\d+\.\s+", "", stripped).strip()
            if item:
                results.append(item)
    return tuple(results)


def _extract_unchecked_tasks(lines: list[str]) -> tuple[str, ...]:
    items: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("- [ ] "):
            items.append(stripped[6:].strip())
    return tuple(items)


def _split_structured_field(value: str) -> tuple[str, ...]:
    parts = re.split(r",|;", value)
    cleaned = [part.strip().strip("`") for part in parts if part.strip()]
    return tuple(cleaned)


def _extract_anchors(text: str) -> tuple[str, ...]:
    anchors = [match.group(0).strip("`") for match in ANCHOR_RE.finditer(text)]
    for match in WIKILINK_RE.finditer(text):
        candidate = match.group(1).strip()
        if candidate.startswith("10_Decisions/") or candidate.startswith("20_Work_Logs/"):
            anchors.append(candidate)
    for match in MARKDOWN_LINK_RE.finditer(text):
        anchors.append(match.group(1).strip())
    return tuple(sorted(dict.fromkeys(item for item in anchors if item)))


def _infer_note_type(name: str) -> str:
    if name.startswith("SESSION-"):
        return "session"
    if name.startswith("LOG-"):
        return "log"
    if name.startswith("DEC-"):
        return "decision"
    return "context"


def _parse_note(vault_root: Path, path: Path, *, staging_root: Path | None = None) -> ParsedNote:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SurpriseMeError(f"failed to read note {path}: {exc}") from exc
    raw_lines = text.splitlines()
    metadata, body_lines, frontmatter_ok = _extract_frontmatter(raw_lines)
    roots = [vault_root]
    if staging_root is not None:
        roots.append(staging_root)
    rel_path = _relative_note_path(path, *roots)
    note_type = metadata.get("type") or _infer_note_type(path.name)
    note_date = (
        _parse_date_token(metadata["date"].replace("-", ""))
        if "date" in metadata
        else _file_date_from_name(path)
    )
    body = "\n".join(body_lines).strip()
    title = _extract_title(body_lines, path.stem)
    tokens = _tokenize(body)
    return ParsedNote(
        path=path,
        rel_path=rel_path,
        note_type=note_type,
        note_date=note_date,
        title=title,
        body=body,
        lines=tuple(raw_lines),
        tokens=tokens,
        anchors=_extract_anchors(text),
        open_questions=tuple(_normalize_text(item) for item in _extract_section_items(body_lines, "Open Questions")),
        unchecked_tasks=tuple(_normalize_text(item) for item in _extract_unchecked_tasks(body_lines)),
        frontmatter_ok=frontmatter_ok,
    )


def _parse_current_workflow_rules(note: ParsedNote) -> tuple[CurrentWorkflowRule, ...]:
    if note.rel_path != DEFAULT_CURRENT_WORKFLOWS_RELATIVE_PATH.as_posix():
        return ()

    rules: list[CurrentWorkflowRule] = []
    current_heading = ""
    fields: dict[str, list[str]] = {}

    def flush() -> None:
        nonlocal current_heading, fields
        if not current_heading:
            fields = {}
            return
        owner = " ".join(fields.get("owner", ()))
        categories = tuple(
            item.lower().replace(" ", "_")
            for raw in fields.get("applies to categories", ())
            for item in _split_structured_field(raw)
        )
        covers = tuple(item for raw in fields.get("covers", ()) for item in _split_structured_field(raw))
        examples = tuple(
            item
            for raw in fields.get("already covered examples", ())
            for item in _split_structured_field(raw)
        )
        notes = tuple(
            item
            for key, values in fields.items()
            if key not in {"owner", "applies to categories", "covers", "already covered examples"}
            for item in values
        )
        token_text = " ".join((current_heading, owner, *categories, *covers, *examples, *notes))
        rules.append(
            CurrentWorkflowRule(
                heading=current_heading,
                owner=owner,
                applies_to_categories=categories,
                covers=covers,
                examples=examples,
                notes=notes,
                tokens=_tokenize(token_text),
            )
        )
        current_heading = ""
        fields = {}

    for raw in note.body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("### "):
            flush()
            current_heading = stripped[4:].strip()
            continue
        if not current_heading or not stripped.startswith("- "):
            continue
        item = stripped[2:].strip()
        if ":" in item:
            key, value = item.split(":", 1)
            fields.setdefault(key.strip().lower(), []).append(value.strip())
        else:
            fields.setdefault("notes", []).append(item)
    flush()
    return tuple(rules)


def _load_notes(
    vault_root: Path,
    selected_paths: list[Path],
    *,
    staging_root: Path | None = None,
) -> tuple[list[ParsedNote], list[ParseIssue]]:
    notes: list[ParsedNote] = []
    parse_issues: list[ParseIssue] = []
    for path in selected_paths:
        roots = [vault_root]
        if staging_root is not None:
            roots.append(staging_root)
        rel_path = _relative_note_path(path, *roots)
        try:
            note = _parse_note(vault_root, path, staging_root=staging_root)
        except SurpriseMeError as exc:
            parse_issues.append(ParseIssue(path=rel_path, error=str(exc)))
            continue
        if not note.frontmatter_ok:
            parse_issues.append(ParseIssue(path=rel_path, error="frontmatter parse degraded; body parsed without full metadata"))
        notes.append(note)
    notes.sort(key=lambda item: item.rel_path)
    return notes, parse_issues


def _extract_line_anchors(line: str) -> tuple[str, ...]:
    return _extract_anchors(line)


def _signal_kind(line: str) -> str | None:
    lowered = line.lower()
    if any(term in lowered for term in FOLLOW_UP_TERMS):
        return "follow_up"
    if any(term in lowered for term in MANUAL_TERMS):
        return "manual"
    if stripped := line.strip():
        if stripped.startswith("- [ ] "):
            return "task"
    return None


def _extract_signals(notes: list[ParsedNote]) -> list[EvidenceSignal]:
    signals: list[EvidenceSignal] = []
    for note in notes:
        for line_number, line in enumerate(note.lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "---")):
                continue
            kind = _signal_kind(stripped)
            if kind is None and any(term in stripped.lower() for term in ACTION_TERMS):
                kind = "manual"
            if kind is None:
                continue
            tokens = _tokenize(stripped)
            if len(tokens) < 3 and kind not in {"task"}:
                continue
            signals.append(
                EvidenceSignal(
                    kind=kind,
                    note_path=note.rel_path,
                    note_type=note.note_type,
                    line=line_number,
                    snippet=stripped,
                    anchors=_extract_line_anchors(stripped),
                    tokens=tokens,
                )
            )

    for note in notes:
        for question in note.open_questions:
            if len(question) < 18:
                continue
            signals.append(
                EvidenceSignal(
                    kind="question",
                    note_path=note.rel_path,
                    note_type=note.note_type,
                    line=1,
                    snippet=question,
                    anchors=(),
                    tokens=_tokenize(question),
                )
            )
        for task in note.unchecked_tasks:
            if len(task) < 18:
                continue
            signals.append(
                EvidenceSignal(
                    kind="task",
                    note_path=note.rel_path,
                    note_type=note.note_type,
                    line=1,
                    snippet=task,
                    anchors=(),
                    tokens=_tokenize(task),
                )
            )
    return signals


def _score_retrieval_terms(notes: list[ParsedNote], signals: list[EvidenceSignal]) -> list[str]:
    counter: Counter[str] = Counter()

    def add_tokens(tokens: Iterable[str], weight: int) -> None:
        for token in tokens:
            normalized = token.strip().lower()
            if len(normalized) < 4:
                continue
            if normalized.isdigit() or normalized in RETRIEVAL_STOPWORDS:
                continue
            counter[normalized] += weight

    for note in notes:
        add_tokens(note.tokens, 1 if note.note_type != "context" else 2)
        add_tokens(_tokenize(note.title), 2)
        for anchor in note.anchors:
            add_tokens(_tokenize(anchor.replace("/", " ").replace(".", " ")), 2)

    for signal in signals:
        weight = 3 if signal.kind in {"follow_up", "question", "task"} else 2
        add_tokens(signal.tokens, weight)

    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _count in ranked[:6]]


def _build_memory_query(notes: list[ParsedNote], signals: list[EvidenceSignal]) -> tuple[str, tuple[str, ...]]:
    ranked_terms = _score_retrieval_terms(notes, signals)
    if not ranked_terms:
        return "", ()
    query_terms: list[str] = []
    for term in ("review", "follow", "context", *ranked_terms):
        if term not in query_terms:
            query_terms.append(term)
    query = " OR ".join(query_terms)
    return query, tuple(query_terms)


def _memory_retrieval_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "memory_retrieval_enabled", None) is None:
        return True
    return bool(args.memory_retrieval_enabled)


def _web_research_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "web_research_enabled", True))


def _resolve_memory_workspace(vault_root: Path, args: argparse.Namespace) -> Path:
    raw_value = str(args.memory_workspace or "").strip()
    if raw_value:
        return Path(raw_value).expanduser().resolve()
    if vault_root == DEFAULT_VAULT_ROOT.resolve():
        return DEFAULT_MEMORY_WORKSPACE.resolve()
    return vault_root.parent.resolve()


def _resolve_memory_db_path(args: argparse.Namespace) -> Path | None:
    raw_value = str(args.memory_db_path or "").strip()
    if not raw_value:
        return None
    return Path(raw_value).expanduser().resolve()


def _normalize_memory_result_path(raw_path: object, *, workspace: Path, vault_root: Path) -> str | None:
    text = str(raw_path or "").strip().replace("\\", "/")
    if not text:
        return None
    if text.startswith("workspace:"):
        rel_path = text.split(":", 1)[1]
        candidate = (workspace / rel_path).resolve()
        try:
            return candidate.relative_to(vault_root).as_posix()
        except ValueError:
            return None
    if re.fullmatch(r"root\d+:.+", text):
        alias, rel_path = text.split(":", 1)
        if alias != "root1":
            return None
        return rel_path
    if ":" in text:
        return None
    return text


def _allow_memory_result_path(rel_path: str) -> bool:
    if rel_path in RETRIEVAL_EXCLUDED_PATHS:
        return False
    return not any(rel_path.startswith(prefix) for prefix in RETRIEVAL_EXCLUDED_PREFIXES)


def _load_memory_context_notes(
    vault_root: Path,
    selected_paths: list[Path],
    notes: list[ParsedNote],
    signals: list[EvidenceSignal],
    args: argparse.Namespace,
) -> tuple[list[ParsedNote], list[ParseIssue], MemoryRetrievalResult]:
    workspace = _resolve_memory_workspace(vault_root, args)
    db_path = _resolve_memory_db_path(args) or (workspace / ".memory_index.sqlite")
    retrieval_enabled = _memory_retrieval_enabled(args)
    default_result = MemoryRetrievalResult(
        enabled=retrieval_enabled,
        status="disabled" if not retrieval_enabled else "no_query",
        query="",
        terms=(),
        backend=str(args.memory_backend),
        workspace=workspace.as_posix(),
        db_path=db_path.as_posix(),
        matched_paths=(),
        added_paths=(),
        skipped_paths=(),
    )
    if not retrieval_enabled or args.memory_note_limit <= 0:
        return [], [], default_result

    query, terms = _build_memory_query(notes, signals)
    if not query:
        return [], [], default_result

    index = MemoryIndex(workspace=workspace, db_path=db_path, source_roots=[vault_root])
    available, reason = index.is_available()
    if not available or index._requires_rebuild():
        warning = "memory index unavailable" if not reason else f"memory index unavailable: {reason}"
        if available:
            warning = "memory index unavailable: rebuild required for current workspace/source roots"
        return [], [], replace(
            default_result,
            status="unavailable",
            query=query,
            terms=terms,
            warning=warning,
            action="Rebuild local memory index for the canonical vault, then rerun surprise_me.",
            error=warning,
        )

    try:
        payload = memory_search(
            query,
            maxResults=args.memory_max_results,
            minScore=args.memory_min_score,
            workspace=workspace,
            db_path=db_path,
            source_roots=[vault_root],
            backend=args.memory_backend,
        )
    except Exception as exc:
        return [], [], replace(
            default_result,
            status="unavailable",
            query=query,
            terms=terms,
            warning=f"memory search failed: {exc}",
            error=str(exc),
            action="Check the configured memory_system workspace/index and rerun surprise_me.",
        )
    if payload.get("unavailable") or payload.get("disabled"):
        warning = str(payload.get("warning") or "memory retrieval unavailable")
        error = str(payload.get("error") or warning)
        return [], [], replace(
            default_result,
            status="unavailable",
            query=query,
            terms=terms,
            backend=str(payload.get("provider") or args.memory_backend),
            warning=warning,
            action=str(payload.get("action") or ""),
            error=error,
        )

    rows = payload.get("results", [])
    if not isinstance(rows, list):
        rows = []

    existing_paths = {note.rel_path for note in notes}
    matched_paths: list[str] = []
    added_paths: list[str] = []
    skipped_paths: list[str] = []
    retrieved_paths: list[Path] = []
    retrieval_hits: list[MemoryRetrievalHit] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_path = str(row.get("path") or "").strip()
        rel_path = _normalize_memory_result_path(raw_path, workspace=workspace, vault_root=vault_root)
        if rel_path is None:
            continue
        matched_paths.append(rel_path)
        if not _allow_memory_result_path(rel_path):
            skipped_paths.append(rel_path)
            continue
        if rel_path in existing_paths or rel_path in added_paths:
            skipped_paths.append(rel_path)
            continue
        try:
            path = _ensure_under_root(vault_root, vault_root / rel_path, "retrieved context file")
        except SurpriseMeError:
            skipped_paths.append(rel_path)
            continue
        if not path.is_file():
            skipped_paths.append(rel_path)
            continue
        start_line = max(int(row.get("startLine") or 1), 1)
        end_line = max(int(row.get("endLine") or start_line), start_line)
        excerpt_payload = memory_get(
            raw_path,
            from_=start_line,
            lines=args.memory_context_lines,
            workspace=workspace,
            source_roots=[vault_root],
        )
        retrieval_hits.append(
            MemoryRetrievalHit(
                path=rel_path,
                score=float(row.get("score") or 0.0),
                start_line=start_line,
                end_line=end_line,
                snippet=str(row.get("snippet") or "").strip(),
                excerpt=str(excerpt_payload.get("text") or "").strip(),
            )
        )
        retrieved_paths.append(path)
        added_paths.append(rel_path)
        if len(retrieved_paths) >= args.memory_note_limit:
            break

    retrieved_notes, parse_issues = _load_notes(vault_root, retrieved_paths) if retrieved_paths else ([], [])
    status = "used" if retrieved_notes else ("no_results" if matched_paths else "no_results")
    return retrieved_notes, parse_issues, MemoryRetrievalResult(
        enabled=True,
        status=status,
        query=query,
        terms=terms,
        backend=str(payload.get("provider") or args.memory_backend),
        workspace=workspace.as_posix(),
        db_path=db_path.as_posix(),
        matched_paths=tuple(matched_paths),
        added_paths=tuple(added_paths),
        skipped_paths=tuple(skipped_paths),
        results=tuple(retrieval_hits),
        warning=str(payload.get("warning") or ""),
        action=str(payload.get("action") or ""),
        error=str(payload.get("error") or ""),
    )


def _roadmap_link_to_rel_path(link_target: str) -> str:
    target = link_target.strip()
    if not target:
        return ""
    normalized = target.replace("\\", "/").strip("/")
    if normalized.endswith(".md"):
        return normalized
    return f"{normalized}.md"


def _score_topic_terms(text: str) -> tuple[str, ...]:
    ranked = sorted(_tokenize(text))
    return tuple(item for item in ranked if len(item) >= 4)[:8]


def _load_roadmap_topics(vault_root: Path, roadmap_rel_path: str) -> tuple[list[RoadmapTopic], list[ParseIssue], Path | None]:
    if not roadmap_rel_path.strip():
        return [], [], None
    roadmap_path = _ensure_under_root(vault_root, vault_root / roadmap_rel_path, "roadmap file")
    if not roadmap_path.is_file():
        return [], [ParseIssue(path=roadmap_rel_path, error="roadmap file not found")], None

    try:
        text = roadmap_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [ParseIssue(path=roadmap_rel_path, error=f"failed to read roadmap file: {exc}")], roadmap_path

    lines = text.splitlines()
    topics: list[RoadmapTopic] = []
    current_title = ""
    linked_note = ""
    goal = ""
    how_items: list[str] = []

    def flush() -> None:
        nonlocal current_title, linked_note, goal, how_items
        if not current_title.strip():
            return
        query_parts = [current_title, linked_note.replace(".md", ""), goal, *how_items]
        rel_paths = [roadmap_rel_path]
        if linked_note:
            rel_paths.append(linked_note)
        topics.append(
            RoadmapTopic(
                slug=_idea_key("roadmap-topic", current_title),
                title=current_title.strip(),
                linked_note=linked_note,
                goal=goal.strip(),
                how_items=tuple(item.strip() for item in how_items if item.strip()),
                source_paths=tuple(dict.fromkeys(rel_paths)),
                query_terms=_score_topic_terms(" ".join(query_parts)),
            )
        )
        current_title = ""
        linked_note = ""
        goal = ""
        how_items = []

    collecting_how = False
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("### "):
            flush()
            current_title = stripped[4:].strip()
            collecting_how = False
            continue
        if not current_title:
            continue
        if stripped.startswith("[[") and stripped.endswith("]]") and not linked_note:
            linked_note = _roadmap_link_to_rel_path(stripped[2:-2].split("|", 1)[0].split("#", 1)[0])
            continue
        if stripped.lower() == "how:":
            collecting_how = True
            continue
        if stripped.startswith("### "):
            collecting_how = False
        if collecting_how and stripped.startswith("- "):
            how_items.append(stripped[2:].strip())
            continue
        if not goal and stripped and not stripped.startswith("## "):
            goal = stripped
    flush()
    return topics, [], roadmap_path


def _select_roadmap_topic(topics: list[RoadmapTopic], selection_seed: str) -> RoadmapTopic | None:
    if not topics:
        return None
    offset = sum(ord(ch) for ch in selection_seed) % len(topics)
    return topics[offset]


def _build_topic_query(topic: RoadmapTopic) -> tuple[str, tuple[str, ...]]:
    if not topic.query_terms:
        return "", ()
    terms = tuple(dict.fromkeys(topic.query_terms[:6]))
    return " OR ".join(terms), terms


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _strip_html_tags(value: str) -> str:
    without_scripts = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", value)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    return _collapse_whitespace(unescape(without_tags))


def _normalize_search_result_url(url: str) -> str:
    normalized = unescape(url.strip())
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    parsed = urllib_parse.urlparse(normalized)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = urllib_parse.parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return urllib_parse.unquote(target)
    return normalized


def _fetch_text_url(url: str, *, timeout_seconds: float) -> str:
    request = urllib_request.Request(
        url,
        headers={
            "User-Agent": "surprise-me/1.0 (+https://openai.com)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _search_duckduckgo_html(query: str, *, max_results: int, timeout_seconds: float) -> tuple[str, list[WebResearchHit]]:
    search_url = "https://duckduckgo.com/html/?" + urllib_parse.urlencode({"q": query})
    html_text = _fetch_text_url(search_url, timeout_seconds=timeout_seconds)
    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>(?P<rest>.*?)(?=<a[^>]+class="[^"]*result__a[^"]*"|$)',
        re.IGNORECASE | re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'<(?:a|div)[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</(?:a|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    results: list[WebResearchHit] = []
    for index, match in enumerate(pattern.finditer(html_text), start=1):
        title = _strip_html_tags(match.group("title"))
        if not title:
            continue
        snippet_match = snippet_pattern.search(match.group("rest"))
        snippet = _strip_html_tags(snippet_match.group("snippet")) if snippet_match else ""
        results.append(
            WebResearchHit(
                rank=index,
                title=title,
                url=_normalize_search_result_url(match.group("url")),
                snippet=snippet,
            )
        )
        if len(results) >= max_results:
            break
    return search_url, results


def _extract_web_excerpt(html_text: str, *, char_limit: int = 320) -> str:
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text)
    title = _strip_html_tags(title_match.group(1)) if title_match else ""
    text = _strip_html_tags(html_text)
    if title and text.lower().startswith(title.lower()):
        text = text[len(title) :].strip(" -:|")
    if not text:
        return title[:char_limit]
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]
    excerpt = " ".join(sentences[:2]) if sentences else text
    if len(excerpt) > char_limit:
        excerpt = excerpt[: char_limit - 1].rstrip() + "…"
    return excerpt


def _load_web_research(topic: RoadmapTopic, args: argparse.Namespace) -> WebResearchResult:
    query, _terms = _build_topic_query(topic)
    provider = str(args.web_search_provider)
    enabled = _web_research_enabled(args)
    default = WebResearchResult(
        enabled=enabled,
        status="disabled" if not enabled else "no_query",
        provider=provider,
        query=query,
        search_url="",
        max_results=max(1, int(args.web_max_results)),
        fetched_urls=(),
    )
    if not enabled:
        return default
    if not query:
        return default

    try:
        search_url, search_hits = _search_duckduckgo_html(
            query,
            max_results=max(1, int(args.web_max_results)),
            timeout_seconds=float(args.web_timeout_seconds),
        )
    except (OSError, ValueError, urllib_error.URLError) as exc:
        return replace(
            default,
            status="unavailable",
            search_url="",
            warning=f"web research unavailable: {exc}",
            action="Check network access for roadmap fallback web research or rerun with --disable-web-research.",
            error=str(exc),
        )

    if not search_hits:
        return replace(default, status="no_results", search_url=search_url)

    enriched_hits: list[WebResearchHit] = []
    fetched_urls: list[str] = []
    fetch_limit = max(0, min(int(args.web_fetch_limit), len(search_hits)))
    for hit in search_hits:
        excerpt = ""
        if len(fetched_urls) < fetch_limit:
            try:
                excerpt = _extract_web_excerpt(
                    _fetch_text_url(hit.url, timeout_seconds=float(args.web_timeout_seconds))
                )
                fetched_urls.append(hit.url)
            except (OSError, ValueError, urllib_error.URLError):
                excerpt = ""
        enriched_hits.append(replace(hit, excerpt=excerpt))

    return replace(
        default,
        status="used",
        search_url=search_url,
        fetched_urls=tuple(fetched_urls),
        results=tuple(enriched_hits),
    )


def _load_roadmap_research_notes(
    vault_root: Path,
    topic: RoadmapTopic,
    args: argparse.Namespace,
    *,
    selection_seed: str,
) -> tuple[list[ParsedNote], list[ParseIssue], RoadmapFallbackResearch]:
    roadmap_path = str(args.roadmap_file).strip()
    web_research = _load_web_research(topic, args)

    def research_status(current_notes: list[ParsedNote]) -> str:
        supporting_notes = [
            note for note in current_notes if note.rel_path != DEFAULT_ROADMAP_RELATIVE_PATH.as_posix()
        ]
        if supporting_notes:
            return "used"
        if web_research.results:
            return "web_only"
        return "no_supporting_notes"

    selected_paths: list[Path] = []
    for rel_path in topic.source_paths:
        try:
            safe_path = _ensure_under_root(vault_root, vault_root / rel_path, "roadmap topic source")
        except SurpriseMeError as exc:
            return [], [ParseIssue(path=rel_path, error=str(exc))], RoadmapFallbackResearch(
                enabled=True,
                status="unavailable",
                roadmap_path=roadmap_path,
                topic_slug=topic.slug,
                topic_title=topic.title,
                linked_note=topic.linked_note,
                goal=topic.goal,
                query="",
                terms=(),
                source_paths=topic.source_paths,
                added_paths=(),
                selection_seed=selection_seed,
                warning=str(exc),
                error=str(exc),
            )
        if safe_path.is_file():
            selected_paths.append(safe_path)

    notes, parse_issues = _load_notes(vault_root, selected_paths) if selected_paths else ([], [])
    query, terms = _build_topic_query(topic)
    if not _memory_retrieval_enabled(args) or not query or args.memory_note_limit <= 0:
        status = research_status(notes)
        return notes, parse_issues, RoadmapFallbackResearch(
            enabled=True,
            status=status,
            roadmap_path=roadmap_path,
            topic_slug=topic.slug,
            topic_title=topic.title,
            linked_note=topic.linked_note,
            goal=topic.goal,
            query=query,
            terms=terms,
            source_paths=tuple(note.rel_path for note in notes) or topic.source_paths,
            added_paths=(),
            selection_seed=selection_seed,
            web_research=web_research,
        )

    workspace = _resolve_memory_workspace(vault_root, args)
    db_path = _resolve_memory_db_path(args) or (workspace / ".memory_index.sqlite")
    index = MemoryIndex(workspace=workspace, db_path=db_path, source_roots=[vault_root])
    available, reason = index.is_available()
    if not available or index._requires_rebuild():
        warning = "memory index unavailable" if not reason else f"memory index unavailable: {reason}"
        if available:
            warning = "memory index unavailable: rebuild required for current workspace/source roots"
        status = research_status(notes)
        return notes, parse_issues, RoadmapFallbackResearch(
            enabled=True,
            status=status,
            roadmap_path=roadmap_path,
            topic_slug=topic.slug,
            topic_title=topic.title,
            linked_note=topic.linked_note,
            goal=topic.goal,
            query=query,
            terms=terms,
            source_paths=tuple(note.rel_path for note in notes) or topic.source_paths,
            added_paths=(),
            selection_seed=selection_seed,
            web_research=web_research,
            warning=warning,
            action="Rebuild local memory index for the canonical vault, then rerun surprise_me.",
            error=warning,
        )

    try:
        payload = memory_search(
            query,
            maxResults=max(1, min(args.memory_max_results, 6)),
            minScore=args.memory_min_score,
            workspace=workspace,
            db_path=db_path,
            source_roots=[vault_root],
            backend=args.memory_backend,
        )
    except Exception as exc:
        status = research_status(notes)
        return notes, parse_issues, RoadmapFallbackResearch(
            enabled=True,
            status=status,
            roadmap_path=roadmap_path,
            topic_slug=topic.slug,
            topic_title=topic.title,
            linked_note=topic.linked_note,
            goal=topic.goal,
            query=query,
            terms=terms,
            source_paths=tuple(note.rel_path for note in notes) or topic.source_paths,
            added_paths=(),
            selection_seed=selection_seed,
            web_research=web_research,
            warning=f"memory search failed: {exc}",
            action="Check the configured memory_system workspace/index and rerun surprise_me.",
            error=str(exc),
        )

    rows = payload.get("results", [])
    if not isinstance(rows, list):
        rows = []

    existing_paths = {note.rel_path for note in notes}
    added_paths: list[str] = []
    retrieval_hits: list[MemoryRetrievalHit] = []
    retrieved_paths: list[Path] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel_path = _normalize_memory_result_path(str(row.get("path") or "").strip(), workspace=workspace, vault_root=vault_root)
        if rel_path is None or not _allow_memory_result_path(rel_path):
            continue
        if rel_path in existing_paths or rel_path in added_paths:
            continue
        try:
            path = _ensure_under_root(vault_root, vault_root / rel_path, "roadmap retrieved context file")
        except SurpriseMeError:
            continue
        if not path.is_file():
            continue
        start_line = max(int(row.get("startLine") or 1), 1)
        end_line = max(int(row.get("endLine") or start_line), start_line)
        excerpt_payload = memory_get(
            str(row.get("path") or rel_path),
            from_=start_line,
            lines=max(4, min(args.memory_context_lines, 8)),
            workspace=workspace,
            source_roots=[vault_root],
        )
        retrieval_hits.append(
            MemoryRetrievalHit(
                path=rel_path,
                score=float(row.get("score") or 0.0),
                start_line=start_line,
                end_line=end_line,
                snippet=str(row.get("snippet") or "").strip(),
                excerpt=str(excerpt_payload.get("text") or "").strip(),
            )
        )
        retrieved_paths.append(path)
        added_paths.append(rel_path)
        if len(retrieved_paths) >= max(1, min(args.memory_note_limit, 3)):
            break

    retrieved_notes, retrieved_issues = _load_notes(vault_root, retrieved_paths) if retrieved_paths else ([], [])
    parse_issues.extend(retrieved_issues)
    if retrieved_notes:
        notes.extend(retrieved_notes)
        notes.sort(key=lambda item: item.rel_path)

    status = research_status(notes)
    return notes, parse_issues, RoadmapFallbackResearch(
        enabled=True,
        status=status,
        roadmap_path=roadmap_path,
        topic_slug=topic.slug,
        topic_title=topic.title,
        linked_note=topic.linked_note,
        goal=topic.goal,
        query=query,
        terms=terms,
        source_paths=tuple(note.rel_path for note in notes) or topic.source_paths,
        added_paths=tuple(added_paths),
        selection_seed=selection_seed,
        results=tuple(retrieval_hits),
        web_research=web_research,
        warning=str(payload.get("warning") or ""),
        action=str(payload.get("action") or ""),
        error=str(payload.get("error") or ""),
    )


def _build_roadmap_fallback_idea(
    topic: RoadmapTopic,
    research_notes: list[ParsedNote],
    research: RoadmapFallbackResearch,
) -> CandidateIdea | None:
    signals = _extract_signals(research_notes) if research_notes else []
    actionable = [
        signal
        for signal in signals
        if signal.kind in {"question", "task", "follow_up", "manual"}
        and not signal.note_path.endswith(DEFAULT_ROADMAP_RELATIVE_PATH.as_posix())
    ]
    if actionable:
        chosen = sorted(
            actionable,
            key=lambda item: (
                {"question": 0, "task": 1, "follow_up": 2, "manual": 3}.get(item.kind, 9),
                item.note_path,
                item.line,
                item.snippet,
            ),
        )[0]
        evidence = tuple(item.to_evidence() for item in actionable[:3])
        source_notes = tuple(
            sorted(
                dict.fromkeys(
                    [note.rel_path for note in research_notes if note.rel_path != DEFAULT_ROADMAP_RELATIVE_PATH.as_posix()]
                    or [note.rel_path for note in research_notes]
                )
            )
        )
        focus = chosen.snippet.rstrip(".")
        title = f"Create one scoped next step for {topic.title}: {focus[:72]}"
        rationale = (
            f"No new unreviewed workflow idea survived the normal ranking, so fallback research pivoted to roadmap topic "
            f"`{topic.title}` and found a concrete next-step signal in `{chosen.note_path}`."
        )
        expected_benefit = topic.goal or f"Turn dormant roadmap context for {topic.title} into one concrete follow-up."
        score = 30 + len(source_notes) * 4 + len(evidence) * 3 + len(research.added_paths) * 2
        return CandidateIdea(
            idea_id=_idea_id("roadmap", [topic.slug, chosen.note_path, focus]),
            idea_key=_idea_key("roadmap_research_follow_through", topic.slug, focus[:60]),
            title=title,
            category="roadmap_research_follow_through",
            score=score,
            rationale=rationale,
            expected_benefit=expected_benefit,
            risk_level="low",
            next_action="proposal_only",
            evidence=evidence,
            source_notes=source_notes,
        )

    if research.web_research is None or not research.web_research.results:
        return None

    chosen_hit = sorted(
        research.web_research.results,
        key=lambda item: (item.rank, item.url, item.title),
    )[0]
    focus = (chosen_hit.excerpt or chosen_hit.snippet or chosen_hit.title).rstrip(".")
    evidence = (
        Evidence(
            note_path=chosen_hit.url,
            line=1,
            snippet=(chosen_hit.snippet or chosen_hit.excerpt or chosen_hit.title)[:240],
        ),
    )
    source_notes = tuple(
        sorted(
            dict.fromkeys(
                [
                    *[note.rel_path for note in research_notes if note.rel_path != DEFAULT_ROADMAP_RELATIVE_PATH.as_posix()],
                    chosen_hit.url,
                ]
            )
        )
    ) or (chosen_hit.url,)
    title = f"Review {chosen_hit.title[:54]} for {topic.title}"
    if topic.slug == DEFAULT_WORKFLOW_WEB_FALLBACK_TOPIC_SLUG:
        idea_key_prefix = "workflow_web_research_follow_through"
        category = "workflow_web_research_follow_through"
        rationale = (
            f"No new unreviewed workflow idea survived the normal ranking, "
            f"so bounded web research surfaced a relevant external workflow angle at `{chosen_hit.url}`."
        )
    else:
        idea_key_prefix = "roadmap_research_follow_through"
        category = "roadmap_research_follow_through"
        rationale = (
            f"No new unreviewed workflow idea survived the normal ranking, so fallback research pivoted to roadmap topic "
            f"`{topic.title}` and bounded web research surfaced a relevant external angle at `{chosen_hit.url}`."
        )
    expected_benefit = topic.goal or f"Turn dormant roadmap context for {topic.title} into one concrete follow-up."
    score = 28 + len(source_notes) * 4 + len(research.web_research.fetched_urls) * 2
    return CandidateIdea(
        idea_id=_idea_id("roadmap-web", [topic.slug, chosen_hit.url, focus]),
        idea_key=_idea_key(idea_key_prefix, topic.slug, chosen_hit.title[:60], chosen_hit.url),
        title=title,
        category=category,
        score=score,
        rationale=rationale,
        expected_benefit=expected_benefit,
        risk_level="low",
        next_action="proposal_only",
        evidence=evidence,
        source_notes=source_notes,
    )


def _iter_deep_sleep_sidecars(staging_root: Path, limit: int) -> list[Path]:
    runs_dir = staging_root / DEEP_SLEEP_RUNS_RELATIVE_DIR
    if not runs_dir.is_dir():
        return []
    candidates = sorted(runs_dir.glob("DEEP-SLEEP-RUN-*.json"))
    if limit <= 0:
        return []
    return candidates[-limit:]


def _load_deep_sleep_findings(staging_root: Path, limit: int) -> tuple[list[DeepSleepFinding], list[ParseIssue], list[str]]:
    findings: list[DeepSleepFinding] = []
    parse_issues: list[ParseIssue] = []
    inputs: list[str] = []
    for path in _iter_deep_sleep_sidecars(staging_root, limit):
        safe_path = _ensure_under_root(staging_root, path, "deep-sleep sidecar")
        inputs.append(safe_path.relative_to(staging_root).as_posix())
        try:
            payload = json.loads(safe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parse_issues.append(ParseIssue(path=inputs[-1], error=f"failed to parse deep-sleep sidecar: {exc}"))
            continue
        run_id = str(payload.get("run_id") or safe_path.stem)
        for item in payload.get("connections", []):
            evidence_items = []
            for evidence in item.get("evidence", [])[:3]:
                evidence_items.append(
                    Evidence(
                        note_path=str(evidence.get("note_path") or inputs[-1]),
                        line=int(evidence.get("line") or 1),
                        snippet=str(evidence.get("snippet") or "").strip(),
                    )
                )
            findings.append(
                DeepSleepFinding(
                    run_id=run_id,
                    relation_type=str(item.get("relation_type") or "shared_theme"),
                    confidence=str(item.get("confidence") or "tentative"),
                    suggested_action=str(item.get("suggested_action") or "").strip(),
                    evidence=tuple(evidence_items),
                    source_notes=tuple(str(value) for value in item.get("source_notes", [])),
                )
            )
    findings.sort(
        key=lambda item: (
            -{"high": 2, "medium": 1, "tentative": 0}.get(item.confidence, 0),
            item.relation_type,
            item.run_id,
            item.source_notes,
        )
    )
    return findings, parse_issues, inputs


def _iter_workflow_experiment_sidecars(staging_root: Path, limit: int) -> list[Path]:
    runs_dir = staging_root / WORKFLOW_EXPERIMENT_RUNS_RELATIVE_DIR
    if not runs_dir.is_dir() or limit <= 0:
        return []
    return sorted(runs_dir.glob("WORKFLOW-EXPERIMENT-RUN-*.json"))[-limit:]


def _load_workflow_experiment_findings(
    staging_root: Path,
    limit: int,
) -> tuple[list[WorkflowExperimentFinding], list[ParseIssue], list[str]]:
    findings: list[WorkflowExperimentFinding] = []
    parse_issues: list[ParseIssue] = []
    inputs: list[str] = []
    for path in _iter_workflow_experiment_sidecars(staging_root, limit):
        safe_path = _ensure_under_root(staging_root, path, "workflow-experiment sidecar")
        inputs.append(safe_path.relative_to(staging_root).as_posix())
        try:
            payload = json.loads(safe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parse_issues.append(ParseIssue(path=inputs[-1], error=f"failed to parse workflow-experiment sidecar: {exc}"))
            continue
        selected = payload.get("selected_experiment")
        if not isinstance(selected, dict) or payload.get("status") != "succeeded":
            continue
        signals_payload = selected.get("signals")
        evidence_items: list[Evidence] = []
        if isinstance(signals_payload, list):
            for signal in signals_payload[:3]:
                if not isinstance(signal, dict):
                    continue
                evidence_items.append(
                    Evidence(
                        note_path=str(signal.get("note_path") or inputs[-1]),
                        line=int(signal.get("line") or 1),
                        snippet=str(signal.get("snippet") or "").strip(),
                    )
                )
        outputs = payload.get("outputs")
        report_path = str(outputs.get("run_report") or "") if isinstance(outputs, dict) else ""
        source_notes_payload = selected.get("source_notes")
        source_notes = (
            tuple(str(item) for item in source_notes_payload)
            if isinstance(source_notes_payload, list)
            else (inputs[-1],)
        )
        findings.append(
            WorkflowExperimentFinding(
                run_id=str(payload.get("run_id") or safe_path.stem),
                title=str(selected.get("title") or "Review one workflow experiment proposal").strip(),
                hypothesis=str(selected.get("hypothesis") or "").strip(),
                metric=str(selected.get("metric") or "").strip(),
                next_action=str(selected.get("next_action") or "").strip(),
                report_path=report_path,
                evidence=tuple(evidence_items),
                source_notes=source_notes,
            )
        )
    findings.sort(key=lambda item: (item.run_id, item.title), reverse=True)
    return findings, parse_issues, inputs


def _idea_id(prefix: str, parts: Iterable[str]) -> str:
    digest_basis = "|".join(parts)
    digest = sum(ord(ch) for ch in f"{prefix}:{digest_basis}") % 1_000_000_007
    return f"{prefix}-{digest:09d}"


def _idea_key(category: str, *identity_parts: str) -> str:
    basis = "-".join(part.strip().lower() for part in identity_parts if part.strip())
    normalized = re.sub(r"[^a-z0-9]+", "-", f"{category}-{basis}".lower()).strip("-")
    collapsed = re.sub(r"-{2,}", "-", normalized)
    return collapsed[:120] or "idea"


def _anchor_priority(anchor: str) -> tuple[int, str]:
    if anchor.endswith((".py", ".sh", ".json", ".sqlite")):
        return (0, anchor)
    if anchor.startswith("MC-"):
        return (1, anchor)
    return (2, anchor)


def _is_low_signal_anchor(anchor: str) -> bool:
    normalized = anchor.lower()
    if anchor.startswith("MC-"):
        return True
    if normalized.endswith(LOW_SIGNAL_ANCHOR_SUFFIXES):
        return True
    if normalized.startswith("/"):
        return True
    if "/" not in anchor:
        return False
    return not normalized.endswith((".py", ".sh"))


def _select_group_anchor(anchors: tuple[str, ...]) -> str | None:
    eligible = [anchor for anchor in anchors if not _is_low_signal_anchor(anchor)]
    if not eligible:
        return None
    return sorted(eligible, key=_anchor_priority)[0]


def _allow_anchor_review_helper(
    anchor: str,
    *,
    distinct_note_count: int,
    manual_hits: int,
    follow_up_hits: int,
) -> bool:
    normalized = anchor.lower()
    if _is_low_signal_anchor(anchor):
        return False
    return normalized.endswith((".py", ".sh")) and manual_hits >= 1 and follow_up_hits >= 1 and distinct_note_count >= 2


def _build_candidate_ideas(
    notes: list[ParsedNote],
    signals: list[EvidenceSignal],
    deep_sleep_findings: list[DeepSleepFinding],
    workflow_experiment_findings: list[WorkflowExperimentFinding],
) -> list[CandidateIdea]:
    candidates: dict[str, CandidateIdea] = {}

    def add(candidate: CandidateIdea) -> None:
        current = candidates.get(candidate.idea_id)
        if current is None or candidate.score > current.score:
            candidates[candidate.idea_id] = candidate

    # Bounded local source pack: conceptual lanes are seeded only when the
    # current local notes contain an explicit topic signal.  This preserves
    # the old friction behaviour for unrelated runs while preventing one
    # discarded operational entry from starving other lanes.
    local_text = "\n".join(note.title + "\n" + note.body for note in notes)
    for seed in matching_seeds(local_text):
        evidence = tuple(
            Evidence(note_path=note.rel_path, line=1, snippet=note.title)
            for note in notes
            if any(term in (note.title + " " + note.body).lower() for term in seed.trigger_terms)
        )[:3]
        add(
            CandidateIdea(
                idea_id=_idea_id("lane-seed", [seed.lane, seed.key, *[item.note_path for item in evidence]]),
                idea_key=_idea_key(f"lane:{seed.lane}", seed.key),
                title=seed.title,
                category=f"lane_seed:{seed.lane}",
                score=48,
                rationale=seed.hypothesis,
                expected_benefit=seed.metric,
                risk_level="low",
                next_action=seed.next_action,
                evidence=evidence,
                source_notes=tuple(sorted({item.note_path for item in evidence})),
            )
        )

    signal_groups: dict[str, list[EvidenceSignal]] = {}
    for signal in signals:
        anchor = _select_group_anchor(signal.anchors)
        if anchor is not None:
            signal_groups.setdefault(f"anchor:{anchor}", []).append(signal)

    for key, group in sorted(signal_groups.items()):
        anchor = key.split(":", 1)[1]
        distinct_notes = sorted({item.note_path for item in group})
        if len(distinct_notes) < 2:
            continue
        manual_hits = sum(1 for item in group if item.kind == "manual")
        follow_up_hits = sum(1 for item in group if item.kind in {"follow_up", "task", "question"})
        if not _allow_anchor_review_helper(
            anchor,
            distinct_note_count=len(distinct_notes),
            manual_hits=manual_hits,
            follow_up_hits=follow_up_hits,
        ):
            continue
        score = 28 + len(distinct_notes) * 7 + manual_hits * 3 + follow_up_hits * 4
        title = f"Draft a deterministic review helper for `{anchor}`"
        rationale = (
            f"Recent notes repeatedly revisit `{anchor}` across {len(distinct_notes)} notes, "
            "which suggests recurring manual context rebuilds or follow-up review."
        )
        next_action = "proposal_only" if anchor.startswith("MC-") else "ticket_recommendation"
        evidence = tuple(item.to_evidence() for item in sorted(group, key=lambda item: (item.note_path, item.line))[:3])
        add(
            CandidateIdea(
                idea_id=_idea_id("anchor", [anchor, *distinct_notes]),
                idea_key=_idea_key("anchor_review_helper", anchor),
                title=title,
                category="anchor_review_helper",
                score=score,
                rationale=rationale,
                expected_benefit="Reduce repeated verification and re-reading around one concrete work artifact.",
                risk_level="low",
                next_action=next_action,
                evidence=evidence,
                source_notes=tuple(distinct_notes),
            )
        )

    question_groups: dict[str, list[EvidenceSignal]] = {}
    for signal in signals:
        if signal.kind in {"question", "task"}:
            question_groups.setdefault(signal.snippet, []).append(signal)
    for snippet, group in sorted(question_groups.items()):
        distinct_notes = sorted({item.note_path for item in group})
        if len(distinct_notes) < 2:
            continue
        evidence = tuple(item.to_evidence() for item in sorted(group, key=lambda item: item.note_path)[:3])
        add(
            CandidateIdea(
                idea_id=_idea_id("tracking", [snippet, *distinct_notes]),
                idea_key=_idea_key("tracking_consolidation", snippet[:120]),
                title=f'Create one authoritative tracker for "{snippet[:72]}"',
                category="tracking_consolidation",
                score=55 + len(distinct_notes) * 6,
                rationale="The same unresolved follow-up appears in multiple notes, so one tracked owner/location would reduce duplication.",
                expected_benefit="Collapse repeated reminders into one maintained follow-up record.",
                risk_level="low",
                next_action="proposal_only",
                evidence=evidence,
                source_notes=tuple(distinct_notes),
            )
        )

    if workflow_experiment_findings:
        finding = workflow_experiment_findings[0]
        evidence = finding.evidence[:3] or (
            Evidence(
                note_path=finding.report_path or finding.run_id,
                line=1,
                snippet=finding.hypothesis or finding.title,
            ),
        )
        source_notes = finding.source_notes or (finding.report_path or finding.run_id,)
        add(
            CandidateIdea(
                idea_id=_idea_id("workflow-experiment", [finding.run_id, finding.title, *source_notes]),
                idea_key=_idea_key("workflow_experiment_follow_through", finding.title, *source_notes),
                title=f"Review proposed workflow experiment: {finding.title}",
                category="workflow_experiment_follow_through",
                score=64 + len(source_notes) * 2,
                rationale=(
                    "The pre-surprise-me workflow experiment worker already condensed recent friction "
                    "into one read-only experiment proposal."
                ),
                expected_benefit=finding.metric or "Turn recent workflow friction into one measurable approved experiment.",
                risk_level="low",
                next_action="proposal_only",
                evidence=evidence,
                source_notes=tuple(source_notes),
            )
        )

    if deep_sleep_findings:
        finding = deep_sleep_findings[0]
        evidence = finding.evidence[:3] or (
            Evidence(
                note_path=finding.run_id,
                line=1,
                snippet=finding.suggested_action or finding.relation_type,
            ),
        )
        source_notes = finding.source_notes or (finding.run_id,)
        relation_label = finding.relation_type.replace("_", " ")
        add(
            CandidateIdea(
                idea_id=_idea_id("deep-sleep", [finding.run_id, finding.relation_type, *source_notes]),
                idea_key=_idea_key("deep_sleep_follow_through", finding.relation_type, *source_notes),
                title=f"Turn recent deep-sleep {relation_label} review into one reusable checklist",
                category="deep_sleep_follow_through",
                score=60 + {"high": 12, "medium": 8, "tentative": 3}.get(finding.confidence, 0),
                rationale="Deep-sleep already surfaced a repeatable connection, so formalizing the morning review step is likely the fastest workflow gain.",
                expected_benefit="Reduce time spent interpreting repeated synthesis outputs and make follow-through more consistent.",
                risk_level="low",
                next_action="ticket_recommendation",
                evidence=evidence,
                source_notes=tuple(source_notes),
            )
        )

    manual_notes = sorted({signal.note_path for signal in signals if signal.kind == "manual"})
    if len(manual_notes) >= 3:
        evidence = tuple(
            signal.to_evidence()
            for signal in sorted((item for item in signals if item.kind == "manual"), key=lambda item: (item.note_path, item.line))[:3]
        )
        add(
            CandidateIdea(
                idea_id=_idea_id("manual-loops", manual_notes),
                idea_key=_idea_key("manual_loop_reduction", "recurring-manual-review-loops"),
                title="Create a short morning checklist for recurring manual review loops",
                category="manual_loop_reduction",
                score=35 + len(manual_notes) * 5,
                rationale="Recent notes spend repeated effort on manual checks and re-reading, which is a stable opportunity for a lightweight checklist or helper.",
                expected_benefit="Save operator time on repeated low-risk review steps.",
                risk_level="low",
                next_action="proposal_only",
                evidence=evidence,
                source_notes=tuple(manual_notes),
            )
        )

    return sorted(
        candidates.values(),
        key=lambda item: (-item.score, item.category, item.title, item.idea_id),
    )


def _registry_path(root: Path) -> Path:
    return root / IDEA_REGISTRY_RELATIVE_PATH


def _load_idea_registry_path(path: Path) -> IdeaRegistry:
    path = path.resolve()
    if not path.exists():
        return IdeaRegistry(path=path, entries={})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SurpriseMeError(f"failed to read idea registry {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SurpriseMeError(f"idea registry must contain a JSON object: {path}")
    raw_entries = payload.get("ideas", {})
    if not isinstance(raw_entries, dict):
        raise SurpriseMeError(f"idea registry ideas must contain a JSON object: {path}")
    entries: dict[str, RegistryEntry] = {}
    for idea_key, raw_entry in raw_entries.items():
        entry = _parse_registry_entry(str(idea_key), raw_entry)
        if entry is not None:
            entries[entry.idea_key] = entry
    return IdeaRegistry(path=path, entries=entries)


def _parse_registry_entry(idea_key: str, payload: object) -> RegistryEntry | None:
    if not isinstance(payload, dict):
        return None
    review_status = str(payload.get("review_status") or "unreviewed").strip().lower()
    if review_status not in REVIEW_STATUSES:
        review_status = "unreviewed"
    seen_idea_ids = tuple(
        sorted(
            dict.fromkeys(
                str(item).strip()
                for item in payload.get("seen_idea_ids", [])
                if str(item).strip()
            )
        )
    )
    source_notes = tuple(
        sorted(
            dict.fromkeys(
                str(item).strip()
                for item in payload.get("source_notes", [])
                if str(item).strip()
            )
        )
    )
    return RegistryEntry(
        idea_key=idea_key,
        category=str(payload.get("category") or "").strip(),
        title=str(payload.get("title") or "").strip() or idea_key,
        review_status=review_status,
        first_seen_run_id=str(payload.get("first_seen_run_id") or "").strip(),
        last_seen_run_id=str(payload.get("last_seen_run_id") or "").strip(),
        first_seen_at=str(payload.get("first_seen_at") or "").strip(),
        last_seen_at=str(payload.get("last_seen_at") or "").strip(),
        times_seen=max(int(payload.get("times_seen") or 0), 0),
        times_selected=max(int(payload.get("times_selected") or 0), 0),
        latest_idea_id=str(payload.get("latest_idea_id") or "").strip(),
        seen_idea_ids=seen_idea_ids,
        source_notes=source_notes,
        latest_expected_benefit=str(payload.get("latest_expected_benefit") or "").strip(),
        latest_next_action=str(payload.get("latest_next_action") or "").strip(),
        latest_rationale=str(payload.get("latest_rationale") or "").strip(),
        review_note=str(payload.get("review_note") or "").strip(),
        reviewed_at=str(payload.get("reviewed_at") or "").strip(),
    )


def load_idea_registry(root: Path) -> IdeaRegistry:
    return _load_idea_registry_path(_registry_path(root))


def _overlay_registry_path(root: Path) -> Path:
    return root / IDEA_REGISTRY_OVERLAY_RELATIVE_PATH


def _preferred_registry_write_path(root: Path) -> Path:
    primary = _registry_path(root)
    try:
        if primary.exists():
            stat_result = primary.stat()
            mode = stat_result.st_mode
            writable = False
            if stat_result.st_uid == os.geteuid():
                writable = bool(mode & 0o200)
            elif stat_result.st_gid == os.getegid():
                writable = bool(mode & 0o020)
            else:
                writable = bool(mode & 0o002)
            if writable:
                return primary
            return _overlay_registry_path(root)
        primary.parent.mkdir(parents=True, exist_ok=True)
        test_handle = (primary.parent / ".write_test").resolve()
        try:
            with test_handle.open("w", encoding="utf-8"):
                pass
        finally:
            if test_handle.exists():
                test_handle.unlink()
            return primary
    except OSError:
        return _overlay_registry_path(root)
    return _overlay_registry_path(root)


def write_idea_registry(registry: IdeaRegistry) -> None:
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "counts": registry.counts(),
        "ideas": {
            idea_key: entry.to_json()
            for idea_key, entry in sorted(registry.entries.items())
        },
    }
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        registry.path.write_text(content, encoding="utf-8")
        return
    except PermissionError:
        fallback_path = registry.path.with_name(IDEA_REGISTRY_OVERLAY_RELATIVE_PATH.name)
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        fallback_path.write_text(content, encoding="utf-8")


def _parse_run_timestamp(run_id: str) -> str:
    match = DATE_IN_NAME_RE.search(run_id)
    if match is None:
        return ""
    date_token = match.group(1)
    try:
        if len(run_id) >= len("SURPRISE-ME-RUN-YYYYMMDD-HHMMSS"):
            timestamp = run_id.rsplit("-", 1)[-1]
            parsed = datetime.strptime(f"{date_token}{timestamp}", "%Y%m%d%H%M%S")
        else:
            parsed = datetime.strptime(date_token, "%Y%m%d")
    except ValueError:
        return ""
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _registry_entry_from_history(
    idea: dict[str, object],
    *,
    run_id: str,
    run_seen_at: str,
    selected: bool,
) -> RegistryEntry | None:
    idea_key = str(idea.get("idea_key") or "").strip()
    if not idea_key:
        return None
    review_status = str(idea.get("review_status") or "unreviewed").strip().lower()
    if review_status not in REVIEW_STATUSES:
        review_status = "unreviewed"
    source_notes = tuple(
        sorted(
            dict.fromkeys(
                str(item).strip()
                for item in idea.get("source_notes", [])
                if str(item).strip()
            )
        )
    )
    idea_id = str(idea.get("idea_id") or "").strip()
    seen_idea_ids = (idea_id,) if idea_id else ()
    return RegistryEntry(
        idea_key=idea_key,
        category=str(idea.get("category") or "").strip(),
        title=str(idea.get("title") or "").strip() or idea_key,
        review_status=review_status,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        first_seen_at=run_seen_at,
        last_seen_at=run_seen_at,
        times_seen=1,
        times_selected=1 if selected else 0,
        latest_idea_id=idea_id,
        seen_idea_ids=seen_idea_ids,
        source_notes=source_notes,
        latest_expected_benefit=str(idea.get("expected_benefit") or "").strip(),
        latest_next_action=str(idea.get("next_action") or "").strip(),
        latest_rationale=str(idea.get("rationale") or "").strip(),
    )


def _merge_registry_entries(current: RegistryEntry, historical: RegistryEntry) -> RegistryEntry:
    first_seen_at = current.first_seen_at or historical.first_seen_at
    first_seen_run_id = current.first_seen_run_id or historical.first_seen_run_id
    if historical.first_seen_at and (not current.first_seen_at or historical.first_seen_at < current.first_seen_at):
        first_seen_at = historical.first_seen_at
        first_seen_run_id = historical.first_seen_run_id
    last_seen_at = current.last_seen_at or historical.last_seen_at
    last_seen_run_id = current.last_seen_run_id or historical.last_seen_run_id
    if historical.last_seen_at and (not current.last_seen_at or historical.last_seen_at > current.last_seen_at):
        last_seen_at = historical.last_seen_at
        last_seen_run_id = historical.last_seen_run_id
    merged_seen_idea_ids = tuple(sorted(dict.fromkeys(current.seen_idea_ids + historical.seen_idea_ids)))
    merged_source_notes = tuple(sorted(dict.fromkeys(current.source_notes + historical.source_notes)))
    latest_idea_id = current.latest_idea_id
    latest_expected_benefit = current.latest_expected_benefit
    latest_next_action = current.latest_next_action
    latest_rationale = current.latest_rationale
    latest_title = current.title
    latest_category = current.category
    if historical.last_seen_at and (not current.last_seen_at or historical.last_seen_at > current.last_seen_at):
        latest_idea_id = historical.latest_idea_id or latest_idea_id
        latest_expected_benefit = historical.latest_expected_benefit or latest_expected_benefit
        latest_next_action = historical.latest_next_action or latest_next_action
        latest_rationale = historical.latest_rationale or latest_rationale
        latest_title = historical.title or latest_title
        latest_category = historical.category or latest_category
    return RegistryEntry(
        idea_key=current.idea_key,
        category=latest_category,
        title=latest_title,
        review_status=current.review_status,
        first_seen_run_id=first_seen_run_id,
        last_seen_run_id=last_seen_run_id,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        times_seen=max(current.times_seen, historical.times_seen),
        times_selected=max(current.times_selected, historical.times_selected),
        latest_idea_id=latest_idea_id,
        seen_idea_ids=merged_seen_idea_ids,
        source_notes=merged_source_notes,
        latest_expected_benefit=latest_expected_benefit,
        latest_next_action=latest_next_action,
        latest_rationale=latest_rationale,
        review_note=current.review_note,
        reviewed_at=current.reviewed_at,
    )


def _load_registry_history_from_run_sidecars(root: Path) -> dict[str, RegistryEntry]:
    runs_dir = root / RUNS_RELATIVE_DIR
    if not runs_dir.is_dir():
        return {}
    entries: dict[str, RegistryEntry] = {}
    for sidecar_path in sorted(runs_dir.glob("SURPRISE-ME-RUN-*.json")):
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        run_id = str(payload.get("run_id") or sidecar_path.stem).strip() or sidecar_path.stem
        run_seen_at = _parse_run_timestamp(run_id)
        selected_idea = payload.get("selected_idea")
        selected_key = ""
        if isinstance(selected_idea, dict):
            selected_key = str(selected_idea.get("idea_key") or "").strip()
        raw_candidate_ideas = payload.get("candidate_ideas", [])
        if not isinstance(raw_candidate_ideas, list):
            raw_candidate_ideas = []
        for raw_idea in raw_candidate_ideas:
            if not isinstance(raw_idea, dict):
                continue
            historical_entry = _registry_entry_from_history(
                raw_idea,
                run_id=run_id,
                run_seen_at=run_seen_at,
                selected=str(raw_idea.get("idea_key") or "").strip() == selected_key,
            )
            if historical_entry is None:
                continue
            current = entries.get(historical_entry.idea_key)
            if current is None:
                entries[historical_entry.idea_key] = historical_entry
            else:
                entries[historical_entry.idea_key] = _merge_registry_entries(current, historical_entry)
    return entries


def _augment_registry_with_history(registry: IdeaRegistry, history_root: Path) -> IdeaRegistry:
    historical_entries = _load_registry_history_from_run_sidecars(history_root)
    if not historical_entries:
        return registry
    merged_entries = dict(registry.entries)
    for idea_key, historical_entry in historical_entries.items():
        current = merged_entries.get(idea_key)
        if current is None:
            merged_entries[idea_key] = historical_entry
        else:
            merged_entries[idea_key] = _merge_registry_entries(current, historical_entry)
    return IdeaRegistry(path=registry.path, entries=merged_entries)


def _merge_registry_entries_with_overlay(current: RegistryEntry, overlay: RegistryEntry) -> RegistryEntry:
    merged = _merge_registry_entries(current, overlay)
    return replace(
        merged,
        review_status=overlay.review_status,
        review_note=overlay.review_note,
        reviewed_at=overlay.reviewed_at,
    )


def load_idea_memory(vault_root: Path, staging_root: Path) -> IdeaRegistry:
    canonical_registry = _augment_registry_with_history(load_idea_registry(vault_root), vault_root)
    merged_entries = dict(canonical_registry.entries)
    canonical_overlay_registry = _load_idea_registry_path(_overlay_registry_path(vault_root))
    for idea_key, overlay_entry in canonical_overlay_registry.entries.items():
        current = merged_entries.get(idea_key)
        if current is None:
            merged_entries[idea_key] = overlay_entry
        else:
            merged_entries[idea_key] = _merge_registry_entries_with_overlay(current, overlay_entry)
    # Preserve backward compatibility for any legacy staged registry/history that has
    # not been promoted yet, but keep canonical memory authoritative.
    staged_registry = _augment_registry_with_history(load_idea_registry(staging_root), staging_root)
    for idea_key, staged_entry in staged_registry.entries.items():
        current = merged_entries.get(idea_key)
        if current is None:
            merged_entries[idea_key] = staged_entry
        else:
            merged_entries[idea_key] = _merge_registry_entries(current, staged_entry)
    staged_overlay_registry = _load_idea_registry_path(_overlay_registry_path(staging_root))
    for idea_key, staged_entry in staged_overlay_registry.entries.items():
        current = merged_entries.get(idea_key)
        if current is None:
            merged_entries[idea_key] = staged_entry
        else:
            merged_entries[idea_key] = _merge_registry_entries_with_overlay(current, staged_entry)
    return IdeaRegistry(path=_preferred_registry_write_path(staging_root), entries=merged_entries)


def _apply_registry_context(
    candidate_ideas: list[CandidateIdea],
    registry: IdeaRegistry,
    notes: list[ParsedNote],
) -> list[CandidateIdea]:
    enriched: list[CandidateIdea] = []
    for idea in candidate_ideas:
        entry = registry.entries.get(idea.idea_key)
        covered_by_context = _find_context_coverage_match(idea, notes)
        covered_by_existing = _find_semantic_review_match(
            idea,
            registry,
            exclude_idea_keys=frozenset({idea.idea_key}),
        )
        if entry is None:
            if covered_by_existing is not None:
                enriched.append(
                    replace(
                        idea,
                        review_status=covered_by_existing.review_status,
                    )
                )
                continue
            if covered_by_context is not None:
                enriched.append(replace(idea, review_status="discarded"))
            else:
                enriched.append(idea)
            continue
        if entry.review_status == "unreviewed" and covered_by_context is not None:
            enriched.append(
                replace(
                    idea,
                    review_status="discarded",
                    times_seen=entry.times_seen,
                    times_selected=entry.times_selected,
                )
            )
            continue
        effective_entry = entry
        if entry.review_status == "unreviewed" and covered_by_existing is not None:
            effective_entry = covered_by_existing
        enriched.append(
            replace(
                idea,
                review_status=effective_entry.review_status,
                times_seen=entry.times_seen,
                times_selected=entry.times_selected,
            )
        )
    return enriched


def _candidate_from_previous_registry_entry(entry: RegistryEntry) -> CandidateIdea:
    idea_id = entry.latest_idea_id or _idea_id("previous-idea", [entry.idea_key, entry.last_seen_run_id, entry.last_seen_at])
    source_notes = entry.source_notes or (entry.last_seen_run_id or entry.idea_key,)
    evidence_note = source_notes[0] if source_notes else entry.idea_key
    evidence_snippet = (
        f"Previous surprise-me idea `{entry.idea_key}` remained unreviewed"
        + (f" after `{entry.times_seen}` prior sighting(s)." if entry.times_seen else ".")
    )
    return CandidateIdea(
        idea_id=idea_id,
        idea_key=entry.idea_key,
        title=entry.title,
        category=entry.category or "previous_idea",
        score=50,
        rationale=entry.latest_rationale
        or "A previous surprise-me idea is still unreviewed, so it should be checked before proposing another one.",
        expected_benefit=entry.latest_expected_benefit or "Resolve existing workflow-improvement backlog before adding new ideas.",
        risk_level="low",
        next_action=entry.latest_next_action or "proposal_only",
        evidence=(
            Evidence(
                note_path=evidence_note,
                line=1,
                snippet=evidence_snippet,
            ),
        ),
        source_notes=source_notes,
        review_status=entry.review_status,
        times_seen=entry.times_seen,
        times_selected=entry.times_selected,
    )


def _build_previous_idea_candidates(registry: IdeaRegistry, notes: list[ParsedNote]) -> list[CandidateIdea]:
    raw_candidates = [
        _candidate_from_previous_registry_entry(entry)
        for entry in registry.entries.values()
        if entry.review_status == "unreviewed" and entry.times_selected == 0
    ]
    enriched = _apply_registry_context(raw_candidates, registry, notes)
    return sorted(
        enriched,
        key=lambda idea: (
            idea.review_status != "unreviewed",
            idea.times_selected,
            idea.times_seen,
            -idea.score,
            idea.category,
            idea.title,
            idea.idea_id,
        ),
    )


def _review_note_marks_existing_coverage(note: str) -> bool:
    lowered = note.strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in REDUNDANCY_REVIEW_MARKERS)


def _candidate_registry_tokens(idea: CandidateIdea) -> frozenset[str]:
    return _tokenize(
        " ".join(
            [
                idea.title,
                idea.category.replace("_", " "),
                idea.rationale,
                idea.expected_benefit,
                idea.next_action.replace("_", " "),
            ]
        )
    )


def _reviewed_entry_tokens(entry: RegistryEntry) -> frozenset[str]:
    return _tokenize(
        " ".join(
            [
                entry.title,
                entry.category.replace("_", " "),
                entry.latest_rationale,
                entry.latest_expected_benefit,
                entry.latest_next_action.replace("_", " "),
                entry.review_note,
            ]
        )
    )


def _find_semantic_review_match(
    idea: CandidateIdea,
    registry: IdeaRegistry,
    *,
    exclude_idea_keys: frozenset[str] = frozenset(),
) -> RegistryEntry | None:
    candidate_tokens = _candidate_registry_tokens(idea)
    if "checklist" not in candidate_tokens:
        return None
    for entry in registry.entries.values():
        if entry.idea_key in exclude_idea_keys:
            continue
        if entry.review_status == "unreviewed":
            continue
        if not _review_note_marks_existing_coverage(entry.review_note):
            continue
        entry_tokens = _reviewed_entry_tokens(entry)
        review_tokens = _tokenize(entry.review_note)
        overlap = candidate_tokens & entry_tokens
        review_note_overlap = candidate_tokens & review_tokens
        checklist_overlap = (candidate_tokens & (entry_tokens | review_tokens)) & CHECKLIST_COVERAGE_TOKENS
        if len(checklist_overlap) >= 2 and {"digest", "morning", "triage"} & (entry_tokens | review_tokens):
            return entry
        if "checklist" in entry_tokens and len(overlap) >= 3 and review_note_overlap:
            return entry
        if idea.category != "manual_loop_reduction":
            continue
        if not {"digest", "triage"} & review_tokens:
            continue
        manual_overlap = candidate_tokens & (entry_tokens | review_tokens) & MANUAL_LOOP_COVERAGE_TOKENS
        if len(manual_overlap) >= 2 and review_note_overlap:
            return entry
    return None


def _context_note_marks_existing_coverage(note: ParsedNote) -> bool:
    if note.note_type != "context":
        return False
    if _parse_current_workflow_rules(note):
        return True
    return _review_note_marks_existing_coverage(note.body)


def _find_current_workflow_rule_match(
    idea: CandidateIdea,
    note: ParsedNote,
) -> CurrentWorkflowRule | None:
    candidate_tokens = _candidate_registry_tokens(idea)
    if len(candidate_tokens) < 3:
        return None
    for rule in _parse_current_workflow_rules(note):
        if idea.category in rule.applies_to_categories:
            return rule
        overlap = candidate_tokens & rule.tokens
        if len(overlap) >= 3 and (rule.examples or rule.covers):
            return rule
    return None


def _find_context_coverage_match(idea: CandidateIdea, notes: list[ParsedNote]) -> ParsedNote | None:
    candidate_tokens = _candidate_registry_tokens(idea)
    if len(candidate_tokens) < 3:
        return None
    for note in notes:
        if _find_current_workflow_rule_match(idea, note) is not None:
            return note
        if not _context_note_marks_existing_coverage(note):
            continue
        note_tokens = note.tokens | _tokenize(note.title)
        if len(candidate_tokens & note_tokens) >= 3:
            return note
    return None


def _select_idea(candidate_ideas: list[CandidateIdea]) -> CandidateIdea | None:
    eligible = [idea for idea in candidate_ideas if idea.review_status == "unreviewed"]
    if not eligible:
        return None
    return sorted(
        eligible,
        key=lambda idea: (
            idea.times_selected,
            idea.times_seen,
            -idea.score,
            idea.category,
            idea.title,
            idea.idea_id,
        ),
    )[0]


def _selection_diagnostics(
    candidate_ideas: list[CandidateIdea],
    selected_idea: CandidateIdea | None,
) -> dict[str, object]:
    eligible = sorted(
        (idea for idea in candidate_ideas if idea.review_status == "unreviewed"),
        key=lambda idea: (
            idea.times_selected,
            idea.times_seen,
            -idea.score,
            idea.category,
            idea.title,
            idea.idea_id,
        ),
    )
    excluded = sorted(
        (idea for idea in candidate_ideas if idea.review_status != "unreviewed"),
        key=lambda idea: (idea.review_status, -idea.score, idea.category, idea.title, idea.idea_id),
    )
    selected_rank = 0
    if selected_idea is not None:
        for index, idea in enumerate(eligible, start=1):
            if idea.idea_id == selected_idea.idea_id:
                selected_rank = index
                break
    excluded_review_state_counts: dict[str, int] = {}
    for idea in excluded:
        excluded_review_state_counts[idea.review_status] = excluded_review_state_counts.get(idea.review_status, 0) + 1

    eligible_previously_seen_count = sum(1 for idea in eligible if idea.times_seen > 0)
    eligible_previously_selected_count = sum(1 for idea in eligible if idea.times_selected > 0)

    lane_health: dict[str, dict[str, object]] = {
        lane: {"attempted": True, "generated": 0, "eligible": 0, "selected": 0, "excluded": 0, "exclusions": {}}
        for lane in ("operational_workflow", "strategy_research", "backtest_data_reproducibility", "agent_workflow_architecture")
    }
    from scripts.workflow_suggestion_contract import classify_lane
    for idea in candidate_ideas:
        lane = classify_lane(idea.title + " " + idea.rationale, category_hint=idea.category).selected_lane
        info = lane_health[lane]
        info["generated"] = int(info["generated"]) + 1
        if idea.review_status == "unreviewed":
            info["eligible"] = int(info["eligible"]) + 1
        else:
            info["excluded"] = int(info["excluded"]) + 1
            exclusions = info["exclusions"]
            assert isinstance(exclusions, dict)
            exclusions[idea.review_status] = int(exclusions.get(idea.review_status, 0)) + 1
    if selected_idea is not None:
        selected_lane = classify_lane(selected_idea.title + " " + selected_idea.rationale, category_hint=selected_idea.category).selected_lane
        lane_health[selected_lane]["selected"] = 1

    if not candidate_ideas:
        summary = "No evidence-backed candidates met the deterministic threshold."
    elif not eligible:
        review_summary = ", ".join(
            f"{status} `{count}`" for status, count in sorted(excluded_review_state_counts.items())
        )
        summary = (
            "All candidates were excluded by registry review state before selection"
            f" ({review_summary})."
        )
    elif selected_idea is None:
        summary = "Eligible candidates existed, but no selection was recorded."
    elif selected_idea.times_selected > 0:
        summary = (
            f"Selected `{selected_idea.idea_key}` again because all `{len(eligible)}` eligible candidates had already been "
            f"surfaced, `{eligible_previously_selected_count}` had already won before, and this idea remained the highest-ranked fallback."
        )
    elif selected_idea.times_seen > 0:
        summary = (
            f"Selected previously seen idea `{selected_idea.idea_key}` because it stayed `unreviewed`, had been seen "
            f"`{selected_idea.times_seen}` time(s) without approval/discard, and still outranked the other `{max(len(eligible) - 1, 0)}` "
            "eligible candidate(s)."
        )
    else:
        summary = (
            f"Selected `{selected_idea.idea_key}` because it was unreviewed with no prior selection history and ranked first "
            "among eligible candidates."
        )

    return {
        "summary": summary,
        "candidate_count": len(candidate_ideas),
        "eligible_unreviewed_count": len(eligible),
        "excluded_reviewed_count": len(excluded),
        "eligible_previously_seen_count": eligible_previously_seen_count,
        "eligible_previously_selected_count": eligible_previously_selected_count,
        "excluded_review_state_counts": excluded_review_state_counts,
        "lane_health": lane_health,
        "run_health": "healthy_with_candidate" if selected_idea is not None else "healthy_but_empty",
        "selected_rank_among_eligible": selected_rank,
        "selected_previously_seen": bool(selected_idea.times_seen) if selected_idea is not None else False,
        "selected_previously_selected": bool(selected_idea.times_selected) if selected_idea is not None else False,
        "selected_history": (
            {
                "times_seen": selected_idea.times_seen,
                "times_selected": selected_idea.times_selected,
                "review_status": selected_idea.review_status,
            }
            if selected_idea is not None
            else None
        ),
        "eligible_candidates": [
            {
                "idea_id": idea.idea_id,
                "idea_key": idea.idea_key,
                "title": idea.title,
                "score": idea.score,
                "review_status": idea.review_status,
                "times_seen": idea.times_seen,
                "times_selected": idea.times_selected,
            }
            for idea in eligible[:5]
        ],
        "excluded_candidates": [
            {
                "idea_id": idea.idea_id,
                "idea_key": idea.idea_key,
                "title": idea.title,
                "score": idea.score,
                "review_status": idea.review_status,
                "times_seen": idea.times_seen,
                "times_selected": idea.times_selected,
            }
            for idea in excluded[:5]
        ],
    }


def _update_idea_registry(
    registry: IdeaRegistry,
    *,
    run_id: str,
    candidate_ideas: list[CandidateIdea],
    selected_idea: CandidateIdea | None,
) -> IdeaRegistry:
    now_iso = datetime.now(timezone.utc).isoformat()
    selected_key = selected_idea.idea_key if selected_idea is not None else ""
    updated = dict(registry.entries)
    for idea in candidate_ideas:
        current = updated.get(idea.idea_key)
        seen_idea_ids = set(current.seen_idea_ids if current is not None else ())
        seen_idea_ids.add(idea.idea_id)
        source_notes = tuple(sorted(dict.fromkeys((current.source_notes if current is not None else ()) + idea.source_notes)))
        review_status = current.review_status if current is not None else "unreviewed"
        review_note = current.review_note if current is not None else ""
        reviewed_at = current.reviewed_at if current is not None else ""
        updated[idea.idea_key] = RegistryEntry(
            idea_key=idea.idea_key,
            category=idea.category,
            title=idea.title,
            review_status=review_status,
            first_seen_run_id=current.first_seen_run_id if current is not None else run_id,
            last_seen_run_id=run_id,
            first_seen_at=current.first_seen_at if current is not None else now_iso,
            last_seen_at=now_iso,
            times_seen=(current.times_seen if current is not None else 0) + 1,
            times_selected=(current.times_selected if current is not None else 0) + (1 if idea.idea_key == selected_key else 0),
            latest_idea_id=idea.idea_id,
            seen_idea_ids=tuple(sorted(seen_idea_ids)),
            source_notes=source_notes,
            latest_expected_benefit=idea.expected_benefit,
            latest_next_action=idea.next_action,
            latest_rationale=idea.rationale,
            review_note=review_note,
            reviewed_at=reviewed_at,
        )
    return IdeaRegistry(path=registry.path, entries=updated)


def _next_run_id(staging_root: Path) -> str:
    base = datetime.now(timezone.utc).strftime("SURPRISE-ME-RUN-%Y%m%d-%H%M%S")
    candidate = base
    counter = 1
    runs_dir = staging_root / RUNS_RELATIVE_DIR
    while (runs_dir / f"{candidate}.md").exists() or (runs_dir / f"{candidate}.json").exists():
        candidate = f"{base}-{counter:02d}"
        counter += 1
    return candidate


def _ensure_output_paths(staging_root: Path, run_id: str) -> RunArtifacts:
    runs_dir = staging_root / RUNS_RELATIVE_DIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    report = runs_dir / f"{run_id}.md"
    sidecar = runs_dir / f"{run_id}.json"
    if report.exists() or sidecar.exists():
        raise SurpriseMeError(f"run artifacts already exist for {run_id}")
    return RunArtifacts(run_id=run_id, run_report=report, json_sidecar=sidecar)


def _write_new_file(path: Path, content: str) -> None:
    if path.exists():
        raise SurpriseMeError(f"refusing to overwrite existing file: {path}")
    path.write_text(content, encoding="utf-8")


def _render_run_report(
    run_id: str,
    lookback_days: int,
    status: str,
    selection_mode: str,
    notes: list[ParsedNote],
    context_files: list[str],
    memory_retrieval: MemoryRetrievalResult,
    roadmap_fallback: RoadmapFallbackResearch | None,
    deep_sleep_inputs: list[str],
    workflow_experiment_inputs: list[str],
    candidate_ideas: list[CandidateIdea],
    selected_idea: CandidateIdea | None,
    registry_before: IdeaRegistry,
    registry_after: IdeaRegistry,
    selection_diagnostics: dict[str, object],
    parse_issues: list[ParseIssue],
    artifacts: RunArtifacts,
) -> str:
    output_files = [
        artifacts.run_report.relative_to(artifacts.run_report.parents[3]).as_posix(),
        artifacts.json_sidecar.relative_to(artifacts.json_sidecar.parents[3]).as_posix(),
    ]
    lines = [
        "---",
        "type: surprise_me_run",
        f"date: {datetime.now(timezone.utc).date().isoformat()}",
        "project: mission-control",
        f"run_id: {run_id}",
        f"lookback_days: {lookback_days}",
        "mode: deterministic",
        f"status: {status}",
        f"selection_mode: {selection_mode}",
        f"notes_scanned: {len(notes)}",
        f"candidate_ideas: {len(candidate_ideas)}",
        f"selected_idea: {selected_idea.idea_id if selected_idea else 'none'}",
        "---",
        "",
        f"# {run_id}",
        "",
        "## Summary",
        f"- Status: `{status}`",
        f"- Selection mode: `{selection_mode}`",
        f"- Notes scanned: `{len(notes)}`",
        f"- Candidate ideas generated: `{len(candidate_ideas)}`",
        f"- Selected idea: `{selected_idea.title if selected_idea else 'none'}`",
        f"- Deep-sleep inputs read: `{len(deep_sleep_inputs)}`",
        f"- Workflow-experiment inputs read: `{len(workflow_experiment_inputs)}`",
        f"- Memory retrieval: `{memory_retrieval.status}` / added `{len(memory_retrieval.added_paths)}` note(s)",
        (
            "- Idea registry before selection: "
            f"`{registry_before.counts().get('total', 0)}` tracked / `{registry_before.counts().get('unreviewed', 0)}` unreviewed / "
            f"`{registry_before.counts().get('approved', 0)}` approved / `{registry_before.counts().get('discarded', 0)}` discarded"
        ),
        (
            "- Idea registry after run: "
            f"`{registry_after.counts().get('total', 0)}` tracked / `{registry_after.counts().get('unreviewed', 0)}` unreviewed / "
            f"`{registry_after.counts().get('approved', 0)}` approved / `{registry_after.counts().get('discarded', 0)}` discarded"
        ),
    ]
    if parse_issues:
        lines.append(f"- Degraded parse handling: `{len(parse_issues)}` input(s)")
    lines.extend(
        [
            "",
            "## Inputs",
            f"- Lookback days: `{lookback_days}`",
            f"- Source notes: {', '.join(f'`{note.rel_path}`' for note in notes) if notes else 'none'}",
            f"- Context files: {', '.join(f'`{item}`' for item in context_files) if context_files else 'none'}",
            f"- Memory retrieval status: `{memory_retrieval.status}`",
            f"- Memory retrieval backend: `{memory_retrieval.backend}`",
            f"- Memory retrieval query: {memory_retrieval.query if memory_retrieval.query else 'none'}",
            f"- Memory retrieval added notes: {', '.join(f'`{item}`' for item in memory_retrieval.added_paths) if memory_retrieval.added_paths else 'none'}",
            f"- Deep-sleep sidecars: {', '.join(f'`{item}`' for item in deep_sleep_inputs) if deep_sleep_inputs else 'none'}",
            f"- Workflow-experiment sidecars: {', '.join(f'`{item}`' for item in workflow_experiment_inputs) if workflow_experiment_inputs else 'none'}",
            f"- Idea registry: `{registry_after.path.relative_to(registry_after.path.parents[3]).as_posix()}`",
        ]
    )
    if roadmap_fallback is not None:
        lines.append(
            f"- Roadmap fallback topic: `{roadmap_fallback.topic_title}` / status `{roadmap_fallback.status}`"
        )
    if memory_retrieval.warning:
        lines.append(f"- Memory retrieval warning: {memory_retrieval.warning}")
    if memory_retrieval.action:
        lines.append(f"- Memory retrieval action: {memory_retrieval.action}")
    if parse_issues:
        lines.append("- Parse issues:")
        for issue in parse_issues:
            lines.append(f"  - `{issue.path}`: {issue.error}")
    lines.extend(["", "## Retrieved Context Pack"])
    if memory_retrieval.results:
        for item in memory_retrieval.results:
            lines.append(
                f"- `{item.path}:{item.start_line}` score `{item.score:.3f}`: {item.snippet or item.excerpt[:120]}"
            )
            if item.excerpt:
                lines.append(f"  - Excerpt: {item.excerpt.replace(chr(10), ' ')[:240]}")
    else:
        lines.append("- No retrieved context added.")
    lines.extend(["", "## Roadmap Fallback Research"])
    if roadmap_fallback is None:
        lines.append("- Not used.")
    else:
        lines.append(f"- Topic: `{roadmap_fallback.topic_title}`")
        lines.append(f"- Status: `{roadmap_fallback.status}`")
        lines.append(f"- Roadmap note: `{roadmap_fallback.roadmap_path}`")
        lines.append(f"- Linked note: `{roadmap_fallback.linked_note or 'none'}`")
        lines.append(f"- Goal: {roadmap_fallback.goal or 'none'}")
        lines.append(f"- Selection seed: `{roadmap_fallback.selection_seed}`")
        lines.append(f"- Query: {roadmap_fallback.query or 'none'}")
        lines.append(
            f"- Source notes: {', '.join(f'`{item}`' for item in roadmap_fallback.source_paths) if roadmap_fallback.source_paths else 'none'}"
        )
        lines.append(
            f"- Retrieved notes added: {', '.join(f'`{item}`' for item in roadmap_fallback.added_paths) if roadmap_fallback.added_paths else 'none'}"
        )
        if roadmap_fallback.web_research is not None:
            lines.append(f"- Web research status: `{roadmap_fallback.web_research.status}`")
            lines.append(f"- Web research provider: `{roadmap_fallback.web_research.provider}`")
            lines.append(f"- Web research query: {roadmap_fallback.web_research.query or 'none'}")
            lines.append(f"- Web search URL: {roadmap_fallback.web_research.search_url or 'none'}")
            lines.append(
                f"- Web pages fetched: {', '.join(f'`{item}`' for item in roadmap_fallback.web_research.fetched_urls) if roadmap_fallback.web_research.fetched_urls else 'none'}"
            )
            if roadmap_fallback.web_research.warning:
                lines.append(f"- Web research warning: {roadmap_fallback.web_research.warning}")
            if roadmap_fallback.web_research.action:
                lines.append(f"- Web research action: {roadmap_fallback.web_research.action}")
        if roadmap_fallback.warning:
            lines.append(f"- Warning: {roadmap_fallback.warning}")
        if roadmap_fallback.action:
            lines.append(f"- Action: {roadmap_fallback.action}")
        if roadmap_fallback.results:
            lines.append("- Research hits:")
            for item in roadmap_fallback.results:
                lines.append(
                    f"  - `{item.path}:{item.start_line}` score `{item.score:.3f}`: {item.snippet or item.excerpt[:120]}"
                )
        if roadmap_fallback.web_research is not None and roadmap_fallback.web_research.results:
            lines.append("- Web research hits:")
            for item in roadmap_fallback.web_research.results:
                lines.append(
                    f"  - `#{item.rank}` `{item.url}`: {item.title}"
                )
                if item.snippet:
                    lines.append(f"    - Snippet: {item.snippet}")
                if item.excerpt:
                    lines.append(f"    - Excerpt: {item.excerpt}")
    lines.extend(["", "## Candidate Ideas"])
    if candidate_ideas:
        for idea in candidate_ideas[:5]:
            lines.append(
                f"- `{idea.idea_id}` / `{idea.idea_key}` score `{idea.score}` `{idea.category}` `{idea.review_status}`: {idea.title}"
            )
            for item in idea.evidence:
                lines.append(f"  {item.to_markdown()}")
            lines.append(f"  - Rationale: {idea.rationale}")
            lines.append(f"  - Expected benefit: {idea.expected_benefit}")
            lines.append(f"  - Recommended next action: `{idea.next_action}`")
            if idea.times_seen or idea.times_selected:
                lines.append(
                    f"  - Prior registry history: seen `{idea.times_seen}` time(s), selected `{idea.times_selected}` time(s)"
                )
    else:
        lines.append("- No evidence-backed improvement candidates met the deterministic threshold.")
    lines.extend(["", "## Selection Diagnostics"])
    lines.append(f"- Summary: {selection_diagnostics.get('summary', 'none')}")
    lines.append(
        f"- Eligible unreviewed candidates: `{selection_diagnostics.get('eligible_unreviewed_count', 0)}` / "
        f"excluded by review state: `{selection_diagnostics.get('excluded_reviewed_count', 0)}`"
    )
    excluded_review_state_counts = selection_diagnostics.get("excluded_review_state_counts", {})
    if isinstance(excluded_review_state_counts, dict) and excluded_review_state_counts:
        lines.append(
            "- Excluded review-state breakdown: "
            + ", ".join(
                f"`{status}` `{count}`" for status, count in sorted(excluded_review_state_counts.items())
            )
        )
    lines.append(f"- Run health: `{selection_diagnostics.get('run_health', 'unknown')}`")
    lane_health = selection_diagnostics.get("lane_health", {})
    if isinstance(lane_health, dict):
        lines.append("- Lane health:")
        for lane, info in sorted(lane_health.items()):
            if isinstance(info, dict):
                lines.append(
                    f"  - `{lane}` generated `{info.get('generated', 0)}`, eligible `{info.get('eligible', 0)}`, "
                    f"selected `{info.get('selected', 0)}`, excluded `{info.get('excluded', 0)}`"
                )
    lines.append(
        f"- Eligible candidates with prior registry history: seen before `{selection_diagnostics.get('eligible_previously_seen_count', 0)}` / "
        f"selected before `{selection_diagnostics.get('eligible_previously_selected_count', 0)}`"
    )
    if selected_idea is not None:
        lines.append(
            f"- Selected rank among eligible candidates: `{selection_diagnostics.get('selected_rank_among_eligible', 0)}`"
        )
        selected_history = selection_diagnostics.get("selected_history")
        if isinstance(selected_history, dict):
            lines.append(
                f"- Selected idea registry history: seen `{selected_history.get('times_seen', 0)}` time(s), "
                f"selected `{selected_history.get('times_selected', 0)}` time(s), "
                f"review status `{selected_history.get('review_status', 'unknown')}`"
            )
    eligible_candidates = selection_diagnostics.get("eligible_candidates", [])
    if isinstance(eligible_candidates, list) and eligible_candidates:
        lines.append("- Eligible candidate order:")
        for item in eligible_candidates:
            if not isinstance(item, dict):
                continue
            lines.append(
                "  - "
                f"`{item.get('idea_key', '')}` score `{item.get('score', 0)}` "
                f"seen `{item.get('times_seen', 0)}` selected `{item.get('times_selected', 0)}`: "
                f"{item.get('title', '')}"
            )
    lines.extend(["", "## Selected Improvement"])
    if selected_idea is None:
        lines.append("- No high-confidence improvement tonight.")
    else:
        lines.append(f"- Title: {selected_idea.title}")
        lines.append(f"- Category: `{selected_idea.category}`")
        lines.append(f"- Idea key: `{selected_idea.idea_key}`")
        lines.append(f"- Review status: `{selected_idea.review_status}`")
        lines.append(f"- Risk level: `{selected_idea.risk_level}`")
        lines.append(f"- Expected benefit: {selected_idea.expected_benefit}")
        lines.append(f"- Action taken: `{selected_idea.next_action}`")
        lines.append(f"- Why this one: {selected_idea.rationale}")
    lines.extend(["", "## Follow-Up Recommendation"])
    if selected_idea is None:
        if candidate_ideas:
            lines.append("- All current high-confidence ideas are already reviewed or were previously surfaced; wait for new evidence or update registry statuses.")
        else:
            if roadmap_fallback is not None:
                lines.append("- Re-run after more recent notes exist or expand the roadmap-linked research context.")
            else:
                lines.append("- Re-run after more recent notes or deep-sleep outputs exist.")
    else:
        lines.append(
            f"- Review `{selected_idea.idea_key}` and set its registry status to `approved`, `discarded`, or leave it `unreviewed`."
        )
    lines.extend(["", "## Output Files"])
    for item in output_files:
        lines.append(f"- `{item}`")
    lines.append(f"- `{registry_after.path.relative_to(registry_after.path.parents[3]).as_posix()}`")
    return "\n".join(lines) + "\n"


def _sidecar_payload(
    run_id: str,
    args: argparse.Namespace,
    status: str,
    selection_mode: str,
    notes: list[ParsedNote],
    context_files: list[str],
    memory_retrieval: MemoryRetrievalResult,
    roadmap_fallback: RoadmapFallbackResearch | None,
    deep_sleep_inputs: list[str],
    workflow_experiment_inputs: list[str],
    candidate_ideas: list[CandidateIdea],
    selected_idea: CandidateIdea | None,
    registry_before: IdeaRegistry,
    registry_after: IdeaRegistry,
    selection_diagnostics: dict[str, object],
    parse_issues: list[ParseIssue],
    artifacts: RunArtifacts,
) -> dict[str, object]:
    local_note_only = not (
        selected_idea is not None
        and roadmap_fallback is not None
        and roadmap_fallback.web_research is not None
        and roadmap_fallback.web_research.status == "used"
    )
    workflow_suggestion = (
        build_workflow_suggestion(
            title=selected_idea.title,
            hypothesis=selected_idea.rationale,
            metric=selected_idea.expected_benefit,
            next_action=selected_idea.next_action,
            safety_boundary="local-note-only proposal; no web retrieval, schedules, ticket mutation, or secrets",
            source_notes=selected_idea.source_notes,
            evidence=(asdict(item) for item in selected_idea.evidence),
            category_hint=selected_idea.category,
        )
        if selected_idea is not None and local_note_only
        else None
    )
    return {
        "workflow_suggestion": workflow_suggestion,
        "workflow_suggestion_diagnostics": {
            "eligible": workflow_suggestion is not None,
            "code": (
                "local_note_contract_emitted"
                if workflow_suggestion is not None
                else "web_evidence_excluded"
                if selected_idea is not None and not local_note_only
                else "no_suggestion"
            ),
        },
        "run_id": run_id,
        "mode": args.mode,
        "status": status,
        "selection_mode": selection_mode,
        "degraded": bool(parse_issues),
        "lookback_days": args.lookback_days,
        "notes_scanned": len(notes),
        "candidate_ideas": [item.to_json() for item in candidate_ideas],
        "selected_idea": selected_idea.to_json() if selected_idea is not None else None,
        "selection_diagnostics": selection_diagnostics,
        "summary": selected_idea.title if selected_idea is not None else "no high-confidence improvement tonight",
        "evidence_signals": [
            asdict(item)
            for idea in candidate_ideas[:3]
            for item in idea.evidence
        ],
        "expected_benefit": selected_idea.expected_benefit if selected_idea is not None else "",
        "risk_level": selected_idea.risk_level if selected_idea is not None else "none",
        "action_taken": selected_idea.next_action if selected_idea is not None else "proposal_only",
        "artifacts_created": [
            artifacts.run_report.as_posix(),
            artifacts.json_sidecar.as_posix(),
        ],
        "follow_up_recommendation": (
            f"Review `{selected_idea.idea_key}` and set the registry status to approved, discarded, or keep it unreviewed."
            if selected_idea is not None
            else (
                "All current high-confidence ideas are already reviewed or were previously surfaced; gather new evidence or update the registry."
                if candidate_ideas
                else (
                    (
                        "Gather more workflow web research evidence before acting."
                        if roadmap_fallback.topic_slug == DEFAULT_WORKFLOW_WEB_FALLBACK_TOPIC_SLUG
                        else "Gather more roadmap-linked research evidence before acting."
                    )
                    if roadmap_fallback is not None
                    else "Gather more recent evidence before acting."
                )
            )
        ),
        "inputs": {
            "vault_root": str(Path(args.vault_root).resolve()),
            "staging_root": str(Path(args.staging_root).resolve()),
            "include_sessions": bool(args.include_sessions),
            "include_work_logs": bool(args.include_work_logs),
            "include_decisions": bool(args.include_decisions),
            "context_files": context_files,
            "memory_retrieval_enabled": _memory_retrieval_enabled(args),
            "memory_backend": str(args.memory_backend),
            "memory_context_lines": int(args.memory_context_lines),
            "memory_max_results": int(args.memory_max_results),
            "memory_min_score": float(args.memory_min_score),
            "memory_note_limit": int(args.memory_note_limit),
            "memory_retrieval": memory_retrieval.to_json(),
            "roadmap_file": str(args.roadmap_file),
            "roadmap_fallback": roadmap_fallback.to_json() if roadmap_fallback is not None else None,
        "web_research_enabled": _web_research_enabled(args),
        "source_pack": source_pack_metadata(),
            "web_search_provider": str(args.web_search_provider),
            "web_max_results": int(args.web_max_results),
            "web_fetch_limit": int(args.web_fetch_limit),
            "web_timeout_seconds": float(args.web_timeout_seconds),
            "include_deep_sleep_runs": bool(args.include_deep_sleep_runs),
            "deep_sleep_limit": args.deep_sleep_limit,
            "deep_sleep_inputs": deep_sleep_inputs,
            "include_workflow_experiment_runs": bool(args.include_workflow_experiment_runs),
            "workflow_experiment_limit": args.workflow_experiment_limit,
            "workflow_experiment_inputs": workflow_experiment_inputs,
        },
        "notes": [
            {
                "path": note.rel_path,
                "type": note.note_type,
                "date": note.note_date.isoformat() if note.note_date else None,
                "anchors": list(note.anchors),
            }
            for note in notes
        ],
        "parse_issues": [asdict(item) for item in parse_issues],
        "idea_registry": {
            "path": registry_after.path.as_posix(),
            "counts_before_selection": registry_before.counts(),
            "counts_after_run": registry_after.counts(),
        },
        "outputs": {
            "run_report": artifacts.run_report.as_posix(),
            "json_sidecar": artifacts.json_sidecar.as_posix(),
            "idea_registry": registry_after.path.as_posix(),
        },
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    vault_root = Path(args.vault_root).resolve()
    staging_root = Path(args.staging_root).resolve()
    if not vault_root.is_dir():
        raise SurpriseMeError(f"vault root not found: {vault_root}")
    if not staging_root.exists():
        raise SurpriseMeError(f"staging root not found: {staging_root}")
    if args.lookback_days < 1:
        raise SurpriseMeError("--lookback-days must be >= 1")
    if args.deep_sleep_limit < 0:
        raise SurpriseMeError("--deep-sleep-limit must be >= 0")
    if args.workflow_experiment_limit < 0:
        raise SurpriseMeError("--workflow-experiment-limit must be >= 0")
    if args.memory_max_results < 1:
        raise SurpriseMeError("--memory-max-results must be >= 1")
    if args.memory_note_limit < 0:
        raise SurpriseMeError("--memory-note-limit must be >= 0")
    if args.memory_context_lines < 1:
        raise SurpriseMeError("--memory-context-lines must be >= 1")
    if args.memory_min_score < 0.0 or args.memory_min_score > 1.0:
        raise SurpriseMeError("--memory-min-score must be between 0 and 1")
    if not str(args.roadmap_file).strip():
        raise SurpriseMeError("--roadmap-file must not be empty")
    if args.web_max_results < 1:
        raise SurpriseMeError("--web-max-results must be >= 1")
    if args.web_fetch_limit < 0:
        raise SurpriseMeError("--web-fetch-limit must be >= 0")
    if args.web_timeout_seconds <= 0:
        raise SurpriseMeError("--web-timeout-seconds must be > 0")
    if not any((args.include_sessions, args.include_work_logs, args.include_decisions)):
        raise SurpriseMeError("at least one include flag is required")

    effective_context_files = _effective_context_files(vault_root, staging_root, args)
    selected_paths = _iter_allowed_paths(vault_root, staging_root, args)
    registry_before = load_idea_memory(vault_root, staging_root)
    notes, parse_issues = _load_notes(vault_root, selected_paths, staging_root=staging_root)
    initial_signals = _extract_signals(notes)
    memory_notes, memory_issues, memory_retrieval = _load_memory_context_notes(
        vault_root,
        selected_paths,
        notes,
        initial_signals,
        args,
    )
    if memory_notes:
        notes.extend(memory_notes)
        notes.sort(key=lambda item: item.rel_path)
    parse_issues.extend(memory_issues)
    deep_sleep_findings: list[DeepSleepFinding] = []
    deep_sleep_inputs: list[str] = []
    if args.include_deep_sleep_runs:
        deep_sleep_findings, deep_sleep_issues, deep_sleep_inputs = _load_deep_sleep_findings(
            staging_root,
            args.deep_sleep_limit,
        )
        parse_issues.extend(deep_sleep_issues)
    workflow_experiment_findings: list[WorkflowExperimentFinding] = []
    workflow_experiment_inputs: list[str] = []
    if args.include_workflow_experiment_runs:
        workflow_experiment_findings, workflow_experiment_issues, workflow_experiment_inputs = _load_workflow_experiment_findings(
            staging_root,
            args.workflow_experiment_limit,
        )
        parse_issues.extend(workflow_experiment_issues)

    candidate_ideas = _build_previous_idea_candidates(registry_before, notes)
    selected_idea = _select_idea(candidate_ideas)
    roadmap_fallback: RoadmapFallbackResearch | None = None
    selection_mode = "previous_idea" if selected_idea is not None else "evidence"
    if selected_idea is None:
        signals = _extract_signals(notes)
        raw_candidate_ideas = _build_candidate_ideas(
            notes,
            signals,
            deep_sleep_findings,
            workflow_experiment_findings,
        )
        candidate_ideas = candidate_ideas + _apply_registry_context(raw_candidate_ideas, registry_before, notes)
        selected_idea = _select_idea(candidate_ideas)
    if selected_idea is None:
        selection_seed = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def try_fallback_topic(topic: RoadmapTopic, mode: str) -> bool:
            nonlocal candidate_ideas, roadmap_fallback, selected_idea, selection_mode
            roadmap_notes, roadmap_note_issues, roadmap_fallback = _load_roadmap_research_notes(
                vault_root,
                topic,
                args,
                selection_seed=selection_seed,
            )
            parse_issues.extend(roadmap_note_issues)
            fallback_candidate = _build_roadmap_fallback_idea(topic, roadmap_notes, roadmap_fallback)
            if fallback_candidate is not None:
                fallback_candidates = _apply_registry_context([fallback_candidate], registry_before, notes)
                candidate_ideas = candidate_ideas + fallback_candidates
                selected_idea = _select_idea(candidate_ideas)
                if selected_idea is not None and selected_idea.idea_id == fallback_candidates[0].idea_id:
                    selection_mode = mode
            return selected_idea is not None

        workflow_web_topic = _default_workflow_web_fallback_topic()
        if _web_research_enabled(args):
            try_fallback_topic(workflow_web_topic, "web_research_fallback")

        if selected_idea is None:
            roadmap_topics, roadmap_issues, _roadmap_path = _load_roadmap_topics(vault_root, str(args.roadmap_file).strip())
            parse_issues.extend(roadmap_issues)
            topic = _select_roadmap_topic(roadmap_topics, selection_seed)
            if topic is not None:
                try_fallback_topic(topic, "roadmap_research_fallback")
            elif roadmap_fallback is None:
                try_fallback_topic(workflow_web_topic, "web_research_fallback")
            if roadmap_fallback.status == "no_supporting_notes" and roadmap_fallback.web_research is None:
                roadmap_fallback = replace(
                    roadmap_fallback,
                    status="no_topics",
                    warning="no roadmap topics parsed",
                )
    if selected_idea is None:
        selection_mode = "none"
    selection_diagnostics = _selection_diagnostics(candidate_ideas, selected_idea)

    run_id = _next_run_id(staging_root)
    artifacts = _ensure_output_paths(staging_root, run_id)
    status = "succeeded" if selected_idea is not None else "no_findings"
    updated_registry = _update_idea_registry(
        registry_before,
        run_id=run_id,
        candidate_ideas=candidate_ideas,
        selected_idea=selected_idea,
    )
    report = _render_run_report(
        run_id=run_id,
        lookback_days=args.lookback_days,
        status=status,
        selection_mode=selection_mode,
        notes=notes,
        context_files=effective_context_files,
        memory_retrieval=memory_retrieval,
        roadmap_fallback=roadmap_fallback,
        deep_sleep_inputs=deep_sleep_inputs,
        workflow_experiment_inputs=workflow_experiment_inputs,
        candidate_ideas=candidate_ideas,
        selected_idea=selected_idea,
        registry_before=registry_before,
        registry_after=updated_registry,
        selection_diagnostics=selection_diagnostics,
        parse_issues=parse_issues,
        artifacts=artifacts,
    )
    _write_new_file(artifacts.run_report, report)

    sidecar = _sidecar_payload(
        run_id=run_id,
        args=args,
        status=status,
        selection_mode=selection_mode,
        notes=notes,
        context_files=effective_context_files,
        memory_retrieval=memory_retrieval,
        roadmap_fallback=roadmap_fallback,
        deep_sleep_inputs=deep_sleep_inputs,
        workflow_experiment_inputs=workflow_experiment_inputs,
        candidate_ideas=candidate_ideas,
        selected_idea=selected_idea,
        registry_before=registry_before,
        registry_after=updated_registry,
        selection_diagnostics=selection_diagnostics,
        parse_issues=parse_issues,
        artifacts=artifacts,
    )
    _write_new_file(artifacts.json_sidecar, json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    write_idea_registry(updated_registry)
    return {
        "run_id": run_id,
        "status": status,
        "notes_scanned": len(notes),
        "selection_mode": selection_mode,
        "candidate_connections": 1 if selected_idea is not None else 0,
        "candidate_ideas": len(candidate_ideas),
        "selected_idea_id": selected_idea.idea_id if selected_idea is not None else "",
        "output_file": artifacts.run_report.as_posix(),
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = run(args)
    except SurpriseMeError as exc:
        print(
            json.dumps(
                {
                    "run_id": "",
                    "status": "failed",
                    "notes_scanned": 0,
                    "candidate_connections": 0,
                    "candidate_ideas": 0,
                    "selected_idea_id": "",
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
