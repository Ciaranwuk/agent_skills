#!/usr/bin/env python3
"""Deterministic deep-sleep link synthesis worker."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_VAULT_ROOT = Path("/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record")
DEFAULT_STAGING_ROOT = Path("/home/cwilson/projects/_vault_staging")
RUNS_RELATIVE_DIR = Path("00_Inbox/deep_sleep/runs")
CLUSTERS_RELATIVE_DIR = Path("00_Inbox/deep_sleep/clusters")
ALLOWED_RELATION_TYPES = (
    "decision_affects_log",
    "session_continues_plan",
    "repeated_open_question",
    "duplicate_or_near_duplicate_idea",
    "shared_theme",
    "follow_up_gap",
)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "use",
    "was",
    "what",
    "when",
    "which",
    "with",
}
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")
DECISION_TOKEN_RE = re.compile(r"\bDEC-\d{8}-[A-Za-z0-9][A-Za-z0-9-]*")
DATE_IN_NAME_RE = re.compile(r"(\d{8})")
TOKEN_RE = re.compile(r"[A-Za-z0-9]{4,}")
ANCHOR_RE = re.compile(
    r"(?:\bMC-\d{4}\b|\bcronjob-[a-z0-9]+\b|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:md|py|sh|sqlite))"
)
DECISION_CONTEXT_KEYWORDS = frozenset(
    {
        "adopt",
        "adopted",
        "affects",
        "align",
        "aligned",
        "apply",
        "applied",
        "because",
        "blocked",
        "blocking",
        "carry",
        "change",
        "changed",
        "choose",
        "chose",
        "continue",
        "continuing",
        "drives",
        "due",
        "followup",
        "follow",
        "impact",
        "impacts",
        "implement",
        "implemented",
        "keep",
        "keeps",
        "preserve",
        "rollout",
        "shapes",
        "therefore",
        "unblock",
        "use",
        "using",
    }
)
PLAN_CONTEXT_KEYWORDS = frozenset(
    {
        "add",
        "added",
        "adjust",
        "adjusted",
        "continue",
        "continuing",
        "debug",
        "document",
        "documented",
        "fix",
        "fixed",
        "implement",
        "implemented",
        "investigate",
        "migrate",
        "migrated",
        "plan",
        "planned",
        "preserve",
        "reduce",
        "refine",
        "review",
        "ship",
        "shipped",
        "tighten",
        "track",
        "tracked",
        "update",
        "updated",
        "verify",
        "work",
    }
)
NEGATION_CONTEXT_PHRASES = (
    "incidental",
    "no plan",
    "no follow-up",
    "no follow up",
    "no active impact",
    "routine checklist",
)
REFERENCE_BOOKKEEPING_PREFIXES = (
    "backlink",
    "backlinks",
    "context",
    "decision",
    "decisions",
    "link",
    "links",
    "ref",
    "refs",
    "reference",
    "references",
    "related",
    "see also",
)
VIRTUAL_VAULT_ROOTS = (
    "00_Inbox/",
    "10_Decisions/",
    "20_Work_Logs/",
    "30_Projects/",
    "40_Ideas/",
    "50_Retros/",
    "99_Assets/",
)
GENERIC_DOC_BASENAMES = {"readme.md", "index.md", "start_here.md"}
STRONG_ANCHOR_SUFFIXES = {".py", ".sh", ".sqlite", ".json", ".service", ".timer"}


class DeepSleepError(Exception):
    """Raised when the deterministic worker cannot complete safely."""


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
    open_questions: tuple[str, ...]
    unchecked_tasks: tuple[str, ...]
    decision_refs: tuple[str, ...]
    outgoing_links: tuple[str, ...]
    anchors: tuple[str, ...]
    frontmatter_ok: bool


@dataclass(frozen=True)
class CandidateConnection:
    connection_id: str
    confidence: str
    relation_type: str
    source_notes: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    justification: str
    suggested_action: str
    cluster_key: str

    def to_json(self) -> dict[str, object]:
        return {
            "connection_id": self.connection_id,
            "confidence": self.confidence,
            "relation_type": self.relation_type,
            "source_notes": list(self.source_notes),
            "evidence": [asdict(item) for item in self.evidence],
            "justification": self.justification,
            "suggested_action": self.suggested_action,
            "cluster_key": self.cluster_key,
        }


@dataclass(frozen=True)
class Cluster:
    cluster_id: str
    slug: str
    note_paths: tuple[str, ...]
    connections: tuple[CandidateConnection, ...]
    theme_tokens: tuple[str, ...]
    high_confidence_present: bool


@dataclass
class ParseIssue:
    path: str
    error: str


@dataclass
class RunArtifacts:
    run_id: str
    run_report: Path
    json_sidecar: Path
    cluster_files: list[Path] = field(default_factory=list)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deep_sleep_link_synthesis")
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


def _iter_allowed_paths(vault_root: Path, args: argparse.Namespace) -> list[Path]:
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=max(args.lookback_days - 1, 0))
    selected: list[Path] = []

    if args.include_sessions:
        inbox = vault_root / "00_Inbox"
        if inbox.is_dir():
            for path in sorted(inbox.glob("SESSION-*.md")):
                note_date = _file_date_from_name(path)
                if note_date is not None and start_date <= note_date <= today:
                    selected.append(path)

    if args.include_work_logs:
        logs = vault_root / "20_Work_Logs"
        if logs.is_dir():
            for path in sorted(logs.glob("LOG-*.md")):
                note_date = _file_date_from_name(path)
                if note_date is not None and start_date <= note_date <= today:
                    selected.append(path)

    if args.include_decisions:
        decisions = vault_root / "10_Decisions"
        if decisions.is_dir():
            for path in sorted(decisions.glob("DEC-*.md")):
                note_date = _file_date_from_name(path)
                if note_date is not None and start_date <= note_date <= today:
                    selected.append(path)

    for rel_path in sorted(dict.fromkeys(args.context_file)):
        context_path = (vault_root / rel_path).resolve()
        if not context_path.is_file():
            raise DeepSleepError(f"context file not found: {rel_path}")
        if vault_root.resolve() not in context_path.parents and context_path != vault_root.resolve():
            raise DeepSleepError(f"context file escapes vault root: {rel_path}")
        selected.append(context_path)

    deduped: dict[str, Path] = {}
    for path in selected:
        deduped[path.resolve().as_posix()] = path.resolve()
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


def _extract_links(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in WIKILINK_RE.finditer(text):
        found.append(match.group(1).strip())
    for match in MARKDOWN_LINK_RE.finditer(text):
        found.append(match.group(1).strip())
    for match in DECISION_TOKEN_RE.finditer(text):
        found.append(match.group(0).strip())
    return tuple(sorted(dict.fromkeys(item for item in found if item)))


def _extract_anchors(text: str) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(match.group(0).strip("`") for match in ANCHOR_RE.finditer(text))))


def _decision_rel_path(value: str) -> str | None:
    candidate = value.strip()
    if candidate.startswith("10_Decisions/") and candidate.endswith(".md"):
        return candidate
    if candidate.startswith("DEC-"):
        return f"10_Decisions/{candidate}.md"
    if "DEC-" in candidate and candidate.endswith(".md"):
        return f"10_Decisions/{Path(candidate).name}"
    return None


def _parse_note(vault_root: Path, path: Path) -> ParsedNote:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeepSleepError(f"failed to read note {path}: {exc}") from exc
    raw_lines = text.splitlines()
    metadata, body_lines, frontmatter_ok = _extract_frontmatter(raw_lines)
    rel_path = path.relative_to(vault_root).as_posix()
    note_type = metadata.get("type") or _infer_note_type(path.name)
    note_date = _parse_date_token(metadata["date"].replace("-", "")) if "date" in metadata else _file_date_from_name(path)
    body = "\n".join(body_lines).strip()
    title = _extract_title(body_lines, path.stem)
    outgoing_links = _extract_links(text)
    anchors = _extract_anchors(text)
    decision_refs = tuple(
        sorted(
            dict.fromkeys(
                ref for item in outgoing_links if (ref := _decision_rel_path(item)) is not None
            )
        )
    )
    open_questions = tuple(_normalize_text(item) for item in _extract_section_items(body_lines, "Open Questions"))
    unchecked_tasks = tuple(_normalize_text(item) for item in _extract_unchecked_tasks(body_lines))
    tokens = _tokenize("\n".join(body_lines))
    return ParsedNote(
        path=path,
        rel_path=rel_path,
        note_type=note_type,
        note_date=note_date,
        title=title,
        body=body,
        lines=tuple(raw_lines),
        tokens=tokens,
        open_questions=open_questions,
        unchecked_tasks=unchecked_tasks,
        decision_refs=decision_refs,
        outgoing_links=outgoing_links,
        anchors=anchors,
        frontmatter_ok=frontmatter_ok,
    )


def _infer_note_type(name: str) -> str:
    if name.startswith("SESSION-"):
        return "session"
    if name.startswith("LOG-"):
        return "log"
    if name.startswith("DEC-"):
        return "decision"
    return "context"


def _find_evidence(note: ParsedNote, preferred_terms: Iterable[str]) -> Evidence:
    terms = [term.strip().lower() for term in preferred_terms if term and term.strip()]
    for index, line in enumerate(note.lines, start=1):
        lowered = line.lower()
        if any(term in lowered for term in terms):
            return Evidence(note_path=note.rel_path, line=index, snippet=line.strip())
    for index, line in enumerate(note.lines, start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith("---"):
            return Evidence(note_path=note.rel_path, line=index, snippet=stripped)
    return Evidence(note_path=note.rel_path, line=1, snippet=note.title)


def _matching_lines(note: ParsedNote, preferred_terms: Iterable[str]) -> list[tuple[int, str]]:
    terms = [term.strip().lower() for term in preferred_terms if term and term.strip()]
    matches: list[tuple[int, str]] = []
    for index, line in enumerate(note.lines, start=1):
        lowered = line.lower()
        if any(term in lowered for term in terms):
            matches.append((index, line.strip()))
    return matches


def _decision_search_terms(decision_rel_path: str) -> list[str]:
    basename = Path(decision_rel_path).name
    stem = Path(decision_rel_path).stem
    return [decision_rel_path, basename, stem]


def _anchor_support_terms(anchor: str) -> tuple[str, ...]:
    terms = [anchor]
    if "/" in anchor:
        anchor_path = Path(anchor)
        # Match concrete file references, not generic stems like "cli" or "index".
        terms.append(anchor_path.name)
    return tuple(dict.fromkeys(term for term in terms if term))


def _anchor_strength(anchor: str) -> int:
    normalized = anchor.strip().strip("`")
    lowered = normalized.lower()
    if re.fullmatch(r"MC-\d{4}", normalized) or lowered.startswith("cronjob-"):
        return 1
    if not ("/" in normalized or "." in Path(normalized).name):
        return 0
    if normalized.startswith(VIRTUAL_VAULT_ROOTS):
        return 0
    path = Path(normalized)
    suffix = path.suffix.lower()
    if suffix == ".md" and path.name.lower() in GENERIC_DOC_BASENAMES:
        return 0
    if suffix in STRONG_ANCHOR_SUFFIXES:
        return 3
    if suffix == ".md":
        return 1
    return 2


def _residual_tokens(text: str, removable_terms: Iterable[str]) -> frozenset[str]:
    lowered = text.lower()
    for term in removable_terms:
        stripped = term.strip().lower()
        if not stripped:
            continue
        lowered = lowered.replace(stripped, " ")
    return _tokenize(lowered)


def _contains_context_keyword(tokens: Iterable[str], keywords: frozenset[str]) -> bool:
    return any(token in keywords for token in tokens)


def _contains_negating_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in NEGATION_CONTEXT_PHRASES)


def _line_looks_like_bookkeeping(line: str, removable_terms: Iterable[str]) -> bool:
    stripped = line.strip().strip("-* ").strip()
    lowered = stripped.lower()
    if ":" in lowered:
        prefix = lowered.split(":", 1)[0].strip()
        if prefix in REFERENCE_BOOKKEEPING_PREFIXES:
            return True
    residual = _residual_tokens(lowered, removable_terms)
    return len(residual) <= 1 and not _contains_context_keyword(residual, DECISION_CONTEXT_KEYWORDS | PLAN_CONTEXT_KEYWORDS)


def _decision_reference_evidence(note: ParsedNote, decision: ParsedNote) -> Evidence | None:
    decision_terms = _decision_search_terms(decision.rel_path)
    decision_tokens = _tokenize("\n".join((decision.title, decision.body)))
    for line_number, line in _matching_lines(note, decision_terms):
        line_tokens = _residual_tokens(line, decision_terms)
        shared_decision_tokens = line_tokens & decision_tokens
        if _line_looks_like_bookkeeping(line, decision_terms):
            continue
        if _contains_context_keyword(line_tokens, DECISION_CONTEXT_KEYWORDS) or len(shared_decision_tokens) >= 2:
            return Evidence(note_path=note.rel_path, line=line_number, snippet=line)
    note_shared_tokens = (note.tokens & decision_tokens) - _tokenize(" ".join(decision_terms))
    if len(note_shared_tokens) >= 3:
        return _find_evidence(note, decision_terms)
    return None


def _note_type_rank(note_type: str) -> int:
    return {"session": 2, "log": 1}.get(note_type, 0)


def _shared_context_tokens(left: ParsedNote, right: ParsedNote, removable_terms: Iterable[str]) -> frozenset[str]:
    removable = _tokenize(" ".join(removable_terms))
    return (left.tokens & right.tokens) - removable


def _anchor_supports_plan_continuation(
    left: ParsedNote,
    right: ParsedNote,
    anchor: str,
    shared_anchors: Iterable[str],
) -> tuple[Evidence, Evidence] | None:
    support_terms = _anchor_support_terms(anchor)
    left_matches = _matching_lines(left, support_terms)
    right_matches = _matching_lines(right, support_terms)
    if not left_matches or not right_matches:
        return None
    shared_context_terms = [term for item in shared_anchors for term in _anchor_support_terms(item)]
    shared_context = _shared_context_tokens(left, right, shared_context_terms)
    for left_line_number, left_line in left_matches:
        left_tokens = _residual_tokens(left_line, support_terms)
        left_active = _contains_context_keyword(left_tokens, PLAN_CONTEXT_KEYWORDS)
        if _line_looks_like_bookkeeping(left_line, support_terms) or _contains_negating_phrase(left_line):
            continue
        for right_line_number, right_line in right_matches:
            right_tokens = _residual_tokens(right_line, support_terms)
            right_active = _contains_context_keyword(right_tokens, PLAN_CONTEXT_KEYWORDS)
            if _line_looks_like_bookkeeping(right_line, support_terms) or _contains_negating_phrase(right_line):
                continue
            if (left_active and right_active) or ((left_active or right_active) and len(shared_context) >= 2):
                return (
                    Evidence(note_path=left.rel_path, line=left_line_number, snippet=left_line),
                    Evidence(note_path=right.rel_path, line=right_line_number, snippet=right_line),
                )
    return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or "cluster"


def _connection_id(prefix: str, note_paths: Iterable[str]) -> str:
    digest_basis = "|".join(sorted(note_paths))
    digest = sum(ord(ch) for ch in f"{prefix}:{digest_basis}") % 1_000_000_007
    return f"{prefix}-{digest:09d}"


def _confidence_rank(value: str) -> int:
    return {"tentative": 0, "medium": 1, "high": 2}[value]


def _candidate_key(connection: CandidateConnection) -> tuple[tuple[str, ...], str, str]:
    return connection.source_notes, connection.relation_type, connection.justification


def _build_connections(notes: list[ParsedNote]) -> list[CandidateConnection]:
    by_rel = {note.rel_path: note for note in notes}
    candidates: dict[tuple[tuple[str, ...], str, str], CandidateConnection] = {}

    def add(candidate: CandidateConnection) -> None:
        key = _candidate_key(candidate)
        current = candidates.get(key)
        if current is None or _confidence_rank(candidate.confidence) > _confidence_rank(current.confidence):
            candidates[key] = candidate

    recent_notes = [note for note in notes if note.note_type in {"session", "log", "decision"}]
    decision_reference_groups: dict[str, list[tuple[ParsedNote, Evidence]]] = defaultdict(list)
    for note in recent_notes:
        if note.note_type not in {"session", "log"}:
            continue
        for decision_ref in note.decision_refs:
            decision = by_rel.get(decision_ref)
            if decision is None:
                continue
            note_evidence = _decision_reference_evidence(note, decision)
            if note_evidence is None:
                continue
            decision_reference_groups[decision.rel_path].append((note, note_evidence))

    for decision_rel_path, references in sorted(decision_reference_groups.items()):
        decision = by_rel.get(decision_rel_path)
        if decision is None:
            continue
        unique_notes = {
            note.rel_path: (note, evidence)
            for note, evidence in references
        }
        note_types = {note.note_type for note, _ in unique_notes.values()}
        reference_count = len(unique_notes)
        best_note, best_evidence = sorted(
            unique_notes.values(),
            key=lambda item: (
                _note_type_rank(item[0].note_type),
                item[0].note_date or date.min,
                item[0].rel_path,
            ),
            reverse=True,
        )[0]
        if reference_count < 2 and best_note.note_type != "session":
            continue
        decision_evidence = _find_evidence(decision, [decision.title, "## Decision", "# DEC-"])
        note_kind = "session" if best_note.note_type == "session" else "work log"
        recurring_phrase = "session and work log" if note_types == {"session", "log"} else f"{reference_count} recent notes"
        add(
            CandidateConnection(
                connection_id=_connection_id("decision-affects-log", [best_note.rel_path, decision.rel_path]),
                confidence="medium" if reference_count <= 2 else "high",
                relation_type="decision_affects_log",
                source_notes=tuple(sorted((best_note.rel_path, decision.rel_path))),
                evidence=(best_evidence, decision_evidence),
                justification=f"The decision appears in {recurring_phrase}, so the strongest recent {note_kind} reference likely reflects active context rather than one-off bookkeeping.",
                suggested_action=f"Review whether one authoritative recent note should summarize the impact of `{decision.rel_path}` instead of repeating multiple references.",
                cluster_key=_slugify(f"decision-{decision.path.stem}"),
            )
        )

    open_question_groups: dict[str, list[ParsedNote]] = defaultdict(list)
    for note in recent_notes:
        for question in note.open_questions:
            if len(question) >= 18:
                open_question_groups[question].append(note)
    for question, grouped_notes in sorted(open_question_groups.items()):
        unique_notes = sorted({note.rel_path: note for note in grouped_notes}.values(), key=lambda item: item.rel_path)
        if len(unique_notes) < 2:
            continue
        source_notes = tuple(note.rel_path for note in unique_notes[:3])
        evidence = tuple(_find_evidence(note, [question]) for note in unique_notes[:3])
        add(
            CandidateConnection(
                connection_id=_connection_id("repeated-open-question", source_notes),
                confidence="high",
                relation_type="repeated_open_question",
                source_notes=source_notes,
                evidence=evidence,
                justification="Multiple recent notes carry the same open question, which suggests unresolved follow-up that spans sessions or logs.",
                suggested_action="Consolidate the repeated question into a single tracked follow-up with owner and next checkpoint.",
                cluster_key=_slugify(question),
            )
        )

    task_groups: dict[str, list[ParsedNote]] = defaultdict(list)
    for note in recent_notes:
        for task in note.unchecked_tasks:
            if len(task) >= 18:
                task_groups[task].append(note)
    for task, grouped_notes in sorted(task_groups.items()):
        unique_notes = sorted({note.rel_path: note for note in grouped_notes}.values(), key=lambda item: item.rel_path)
        if len(unique_notes) < 2:
            continue
        source_notes = tuple(note.rel_path for note in unique_notes[:3])
        evidence = tuple(_find_evidence(note, [task]) for note in unique_notes[:3])
        add(
            CandidateConnection(
                connection_id=_connection_id("follow-up-gap", source_notes),
                confidence="medium",
                relation_type="follow_up_gap",
                source_notes=source_notes,
                evidence=evidence,
                justification="The same unchecked follow-up appears in multiple notes, so the work is persisting without a clear closure path.",
                suggested_action="Pick one authoritative note to own the action and link the others back to it.",
                cluster_key=_slugify(task),
            )
        )

    pair_notes = [note for note in recent_notes if note.note_type in {"session", "log"}]
    for index, left in enumerate(pair_notes):
        for right in pair_notes[index + 1 :]:
            shared_anchors = sorted(set(left.anchors) & set(right.anchors))
            if not shared_anchors:
                continue
            strong_shared_anchors = [anchor for anchor in shared_anchors if _anchor_strength(anchor) >= 2]
            supported_anchor_evidence = [
                (anchor, evidence)
                for anchor in strong_shared_anchors
                if (evidence := _anchor_supports_plan_continuation(left, right, anchor, strong_shared_anchors)) is not None
            ]
            if supported_anchor_evidence:
                anchor_terms = [anchor for anchor, _ in supported_anchor_evidence]
                relation_type = "session_continues_plan"
                confidence = "medium"
                evidence = supported_anchor_evidence[0][1]
                justification = "The notes refer to the same active artifact and describe substantive follow-through, indicating the session is continuing an existing plan."
            else:
                if not strong_shared_anchors:
                    continue
                shared_tokens = _shared_context_tokens(left, right, strong_shared_anchors)
                if len(shared_tokens) < 3:
                    continue
                anchor_terms = strong_shared_anchors
                relation_type = "shared_theme"
                confidence = "tentative"
                evidence = (
                    _find_evidence(left, anchor_terms[:3]),
                    _find_evidence(right, anchor_terms[:3]),
                )
                justification = "The notes share concrete identifiers plus overlapping context, indicating a theme worth review."
            cluster_key = _slugify("-".join(shared_anchors[:4]))
            source_notes = tuple(sorted((left.rel_path, right.rel_path)))
            add(
                CandidateConnection(
                    connection_id=_connection_id(relation_type.replace("_", "-"), source_notes),
                    confidence=confidence,
                    relation_type=relation_type,
                    source_notes=source_notes,
                    evidence=evidence,
                    justification=justification,
                    suggested_action="Review whether these notes should be cross-linked or summarized together in a future manual note.",
                    cluster_key=cluster_key,
                )
            )

    return sorted(
        candidates.values(),
        key=lambda item: (
            -_confidence_rank(item.confidence),
            item.relation_type,
            item.source_notes,
            item.connection_id,
        ),
    )


def _build_clusters(connections: list[CandidateConnection]) -> list[Cluster]:
    grouped: dict[str, list[CandidateConnection]] = defaultdict(list)
    for connection in connections:
        if _confidence_rank(connection.confidence) >= _confidence_rank("medium"):
            grouped[connection.cluster_key].append(connection)
    clusters: list[Cluster] = []
    for key, items in sorted(grouped.items()):
        if len(items) < 2 or not any(item.confidence == "high" for item in items):
            continue
        note_paths = sorted({path for item in items for path in item.source_notes})
        token_counts = Counter(token for item in items for token in key.split("-") if token)
        theme_tokens = tuple(token for token, _ in token_counts.most_common(5))
        clusters.append(
            Cluster(
                cluster_id=_connection_id("cluster", note_paths + [key]),
                slug=_slugify(key),
                note_paths=tuple(note_paths),
                connections=tuple(
                    sorted(
                        items,
                        key=lambda item: (
                            -_confidence_rank(item.confidence),
                            item.relation_type,
                            item.connection_id,
                        ),
                    )
                ),
                theme_tokens=theme_tokens,
                high_confidence_present=True,
            )
        )
    return clusters


def _render_run_report(
    run_id: str,
    lookback_days: int,
    status: str,
    notes: list[ParsedNote],
    context_files: list[str],
    connections: list[CandidateConnection],
    clusters: list[Cluster],
    parse_issues: list[ParseIssue],
    artifacts: RunArtifacts,
) -> str:
    theme_counts = Counter(
        token
        for note in notes
        if note.note_type in {"session", "log", "decision"}
        for token in note.tokens
    )
    top_themes = [token for token, _ in theme_counts.most_common(8)]
    recent_inputs = [note.rel_path for note in notes]
    output_files = [
        artifacts.run_report.relative_to(artifacts.run_report.parents[3]).as_posix(),
        artifacts.json_sidecar.relative_to(artifacts.json_sidecar.parents[3]).as_posix(),
    ] + [path.relative_to(path.parents[3]).as_posix() for path in artifacts.cluster_files]

    lines = [
        "---",
        "type: deep_sleep_run",
        f"date: {datetime.now(timezone.utc).date().isoformat()}",
        "project: mission-control",
        f"run_id: {run_id}",
        f"lookback_days: {lookback_days}",
        "mode: deterministic",
        f"status: {status}",
        f"notes_scanned: {len(notes)}",
        f"clusters_found: {len(clusters)}",
        f"candidate_connections: {len(connections)}",
        "---",
        "",
        f"# {run_id}",
        "",
        "## Summary",
        f"- Status: `{status}`",
        f"- Notes scanned: `{len(notes)}`",
        f"- Candidate connections: `{len(connections)}`",
        f"- Cluster notes created: `{len(artifacts.cluster_files)}`",
    ]
    if parse_issues:
        lines.append(f"- Degraded parse handling: `{len(parse_issues)}` note(s) skipped or partially parsed")
    lines.extend(
        [
            "",
            "## Inputs",
            f"- Lookback days: `{lookback_days}`",
            f"- Source notes: {', '.join(f'`{item}`' for item in recent_inputs) if recent_inputs else 'none'}",
            f"- Context files: {', '.join(f'`{item}`' for item in context_files) if context_files else 'none'}",
        ]
    )
    if parse_issues:
        lines.append("- Parse issues:")
        for issue in parse_issues:
            lines.append(f"  - `{issue.path}`: {issue.error}")
    lines.extend(["", "## Clusters"])
    if clusters:
        for cluster in clusters:
            lines.append(
                f"- `{cluster.cluster_id}` theme `{', '.join(cluster.theme_tokens) or cluster.slug}` across {len(cluster.note_paths)} note(s) and {len(cluster.connections)} connection(s)"
            )
    else:
        lines.append("- No cluster notes met the V1 threshold.")
    lines.extend(["", "## Candidate Connections"])
    if connections:
        for connection in connections:
            lines.append(
                f"- `{connection.connection_id}` `{connection.confidence}` `{connection.relation_type}` between "
                + ", ".join(f"`{path}`" for path in connection.source_notes)
            )
            for item in connection.evidence:
                lines.append(f"  {item.to_markdown()}")
            lines.append(f"  - Justification: {connection.justification}")
            lines.append(f"  - Suggested action: {connection.suggested_action}")
    else:
        lines.append("- No evidence-backed candidate connections were found in the current window.")
    lines.extend(["", "## Emerging Themes"])
    if top_themes:
        for theme in top_themes:
            lines.append(f"- `{theme}`")
    else:
        lines.append("- No stable themes extracted.")
    lines.extend(["", "## Open Questions"])
    if parse_issues:
        lines.append("- Should the skipped or partially parsed notes be normalized so future runs can include them?")
    else:
        lines.append("- No new worker-level open questions from this run.")
    lines.extend(["", "## Recommended Manual Follow-Ups"])
    if connections:
        for connection in connections[:5]:
            lines.append(f"- Review `{connection.connection_id}` and decide whether to add manual backlinks or a summary note.")
    else:
        lines.append("- No manual follow-up required beyond monitoring future nightly runs.")
    lines.extend(["", "## Output Files"])
    for item in output_files:
        lines.append(f"- `{item}`")
    return "\n".join(lines) + "\n"


def _render_cluster_note(run_id: str, cluster: Cluster, run_report_rel: str) -> str:
    lines = [
        "---",
        "type: deep_sleep_cluster",
        f"date: {datetime.now(timezone.utc).date().isoformat()}",
        "project: mission-control",
        f"run_id: {run_id}",
        f"cluster_id: {cluster.cluster_id}",
        "---",
        "",
        f"# {cluster.cluster_id}",
        "",
        "## Summary",
        f"- Theme: `{', '.join(cluster.theme_tokens) or cluster.slug}`",
        f"- Notes: `{len(cluster.note_paths)}`",
        f"- Connections: `{len(cluster.connections)}`",
        "",
        "## Notes",
    ]
    for path in cluster.note_paths:
        lines.append(f"- `{path}`")
    lines.extend(["", "## Connections"])
    for connection in cluster.connections:
        lines.append(f"- `{connection.connection_id}` `{connection.confidence}` `{connection.relation_type}`")
        for item in connection.evidence:
            lines.append(f"  {item.to_markdown()}")
        lines.append(f"  - Justification: {connection.justification}")
    lines.extend(
        [
            "",
            "## Source Run",
            f"- `{run_report_rel}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _load_notes(vault_root: Path, selected_paths: list[Path], include_decisions: bool) -> tuple[list[ParsedNote], list[ParseIssue]]:
    notes: list[ParsedNote] = []
    parse_issues: list[ParseIssue] = []
    by_rel_path: dict[str, ParsedNote] = {}

    for path in selected_paths:
        rel_path = path.relative_to(vault_root).as_posix()
        try:
            note = _parse_note(vault_root, path)
        except DeepSleepError as exc:
            parse_issues.append(ParseIssue(path=rel_path, error=str(exc)))
            continue
        if not note.frontmatter_ok:
            parse_issues.append(ParseIssue(path=rel_path, error="frontmatter parse degraded; body parsed without full metadata"))
        notes.append(note)
        by_rel_path[note.rel_path] = note

    if include_decisions:
        pending = sorted(
            {
                ref
                for note in notes
                if note.note_type in {"session", "log"}
                for ref in note.decision_refs
                if ref not in by_rel_path
            }
        )
        for rel_path in pending:
            path = vault_root / rel_path
            if not path.is_file():
                parse_issues.append(ParseIssue(path=rel_path, error="linked decision referenced but missing"))
                continue
            try:
                note = _parse_note(vault_root, path)
            except DeepSleepError as exc:
                parse_issues.append(ParseIssue(path=rel_path, error=str(exc)))
                continue
            if not note.frontmatter_ok:
                parse_issues.append(ParseIssue(path=rel_path, error="frontmatter parse degraded; body parsed without full metadata"))
            notes.append(note)
            by_rel_path[note.rel_path] = note

    notes.sort(key=lambda item: item.rel_path)
    return notes, parse_issues


def _ensure_output_paths(staging_root: Path, run_id: str) -> RunArtifacts:
    runs_dir = staging_root / RUNS_RELATIVE_DIR
    clusters_dir = staging_root / CLUSTERS_RELATIVE_DIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    clusters_dir.mkdir(parents=True, exist_ok=True)
    report = runs_dir / f"{run_id}.md"
    sidecar = runs_dir / f"{run_id}.json"
    if report.exists() or sidecar.exists():
        raise DeepSleepError(f"run artifacts already exist for {run_id}")
    return RunArtifacts(run_id=run_id, run_report=report, json_sidecar=sidecar)


def _next_run_id(staging_root: Path) -> str:
    base = datetime.now(timezone.utc).strftime("DEEP-SLEEP-RUN-%Y%m%d-%H%M%S")
    candidate = base
    counter = 1
    runs_dir = staging_root / RUNS_RELATIVE_DIR
    while (runs_dir / f"{candidate}.md").exists() or (runs_dir / f"{candidate}.json").exists():
        candidate = f"{base}-{counter:02d}"
        counter += 1
    return candidate


def _write_new_file(path: Path, content: str) -> None:
    if path.exists():
        raise DeepSleepError(f"refusing to overwrite existing file: {path}")
    path.write_text(content, encoding="utf-8")


def _sidecar_payload(
    run_id: str,
    args: argparse.Namespace,
    status: str,
    notes: list[ParsedNote],
    connections: list[CandidateConnection],
    clusters: list[Cluster],
    parse_issues: list[ParseIssue],
    artifacts: RunArtifacts,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "mode": args.mode,
        "status": status,
        "degraded": bool(parse_issues),
        "lookback_days": args.lookback_days,
        "notes_scanned": len(notes),
        "clusters_found": len(clusters),
        "candidate_connections": len(connections),
        "inputs": {
            "vault_root": str(Path(args.vault_root).resolve()),
            "staging_root": str(Path(args.staging_root).resolve()),
            "include_sessions": bool(args.include_sessions),
            "include_work_logs": bool(args.include_work_logs),
            "include_decisions": bool(args.include_decisions),
            "context_files": list(args.context_file),
        },
        "notes": [
            {
                "path": note.rel_path,
                "type": note.note_type,
                "date": note.note_date.isoformat() if note.note_date else None,
                "decision_refs": list(note.decision_refs),
            }
            for note in notes
        ],
        "connections": [item.to_json() for item in connections],
        "clusters": [
            {
                "cluster_id": cluster.cluster_id,
                "slug": cluster.slug,
                "note_paths": list(cluster.note_paths),
                "connection_ids": [item.connection_id for item in cluster.connections],
                "theme_tokens": list(cluster.theme_tokens),
            }
            for cluster in clusters
        ],
        "parse_issues": [asdict(item) for item in parse_issues],
        "outputs": {
            "run_report": artifacts.run_report.as_posix(),
            "json_sidecar": artifacts.json_sidecar.as_posix(),
            "cluster_files": [path.as_posix() for path in artifacts.cluster_files],
        },
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    vault_root = Path(args.vault_root).resolve()
    staging_root = Path(args.staging_root).resolve()
    if not vault_root.is_dir():
        raise DeepSleepError(f"vault root not found: {vault_root}")
    if args.lookback_days < 1:
        raise DeepSleepError("--lookback-days must be >= 1")
    if not any((args.include_sessions, args.include_work_logs, args.include_decisions)):
        raise DeepSleepError("at least one include flag is required")

    selected_paths = _iter_allowed_paths(vault_root, args)
    notes, parse_issues = _load_notes(vault_root, selected_paths, include_decisions=args.include_decisions)
    connections = _build_connections(notes)
    clusters = _build_clusters(connections)

    run_id = _next_run_id(staging_root)
    artifacts = _ensure_output_paths(staging_root, run_id)
    run_report_rel = artifacts.run_report.relative_to(staging_root).as_posix()

    for cluster in clusters:
        cluster_path = staging_root / CLUSTERS_RELATIVE_DIR / f"{run_id}-{cluster.slug}.md"
        cluster_content = _render_cluster_note(run_id, cluster, run_report_rel)
        _write_new_file(cluster_path, cluster_content)
        artifacts.cluster_files.append(cluster_path)

    status = "succeeded" if connections else "no_findings"
    report_content = _render_run_report(
        run_id=run_id,
        lookback_days=args.lookback_days,
        status=status,
        notes=notes,
        context_files=list(args.context_file),
        connections=connections,
        clusters=clusters,
        parse_issues=parse_issues,
        artifacts=artifacts,
    )
    _write_new_file(artifacts.run_report, report_content)

    sidecar = _sidecar_payload(run_id, args, status, notes, connections, clusters, parse_issues, artifacts)
    _write_new_file(artifacts.json_sidecar, json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    return {
        "run_id": run_id,
        "status": status,
        "notes_scanned": len(notes),
        "candidate_connections": len(connections),
        "output_file": artifacts.run_report.as_posix(),
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = run(args)
    except DeepSleepError as exc:
        print(
            json.dumps(
                {
                    "run_id": "",
                    "status": "failed",
                    "notes_scanned": 0,
                    "candidate_connections": 0,
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
