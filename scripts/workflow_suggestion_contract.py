"""Versioned, deterministic contract for lane-aware workflow suggestions.

V1 classifies only caller-provided local-note evidence.  It performs no I/O and
has no authority to retrieve from the web, schedule work, mutate tickets, or
resolve secrets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Iterable, Mapping


CONTRACT_NAME = "workflow_suggestion"
CONTRACT_VERSION = "1.0"
LANES = (
    "operational_workflow",
    "strategy_research",
    "backtest_data_reproducibility",
    "agent_workflow_architecture",
)

# Capabilities are explicit in the artifact so a downstream consumer does not
# have to infer authority from producer names or prose.  V1 is descriptive: it
# can propose a review action, but it cannot perform one.
V1_AUTHORITY: Mapping[str, bool] = {
    "web_retrieval": False,
    "scheduler_mutation": False,
    "ticket_mutation": False,
    "trading_execution": False,
}

_LANE_TERMS: Mapping[str, tuple[str, ...]] = {
    "operational_workflow": (
        "checklist", "handoff", "manual", "morning", "operations", "review", "triage",
    ),
    "strategy_research": (
        "alpha", "hypothesis", "literature", "paper", "research", "strategy", "thesis",
    ),
    "backtest_data_reproducibility": (
        "backtest", "dataset", "provenance", "reproducibility", "reproduce", "seed", "snapshot",
    ),
    "agent_workflow_architecture": (
        "agent", "architecture", "contract", "orchestration", "prompt", "schema", "skill", "worker",
    ),
}

_CATEGORY_LANES: Mapping[str, str] = {
    "anchor_review_helper": "operational_workflow",
    "manual_loop_reduction": "operational_workflow",
    "tracking_consolidation": "operational_workflow",
    "roadmap_research_follow_through": "strategy_research",
    "workflow_web_research_follow_through": "strategy_research",
    "workflow_experiment_follow_through": "agent_workflow_architecture",
}


@dataclass(frozen=True)
class LaneDiagnostic:
    code: str
    message: str
    selected_lane: str
    category_hint: str
    matched_terms: tuple[str, ...]
    lane_scores: Mapping[str, int]


def classify_lane(text: str, *, category_hint: str = "") -> LaneDiagnostic:
    """Classify text deterministically; ties follow the documented LANES order."""
    normalized = " ".join(re.findall(r"[a-z0-9_-]+", text.lower()))
    tokens = frozenset(normalized.replace("-", "_").split())
    scores = {
        lane: sum(1 for term in terms if term.replace("-", "_") in tokens)
        for lane, terms in _LANE_TERMS.items()
    }
    hinted_lane = _CATEGORY_LANES.get(category_hint)
    if hinted_lane is not None:
        selected = hinted_lane
        code = "category_hint_matched"
        message = "Known producer category selected the lane."
    else:
        selected = max(LANES, key=lambda lane: scores[lane])
        if scores[selected] > 0:
            code = "local_terms_matched"
            message = "Local-note terms selected the highest-scoring lane."
        else:
            selected = "operational_workflow"
            code = "defaulted_operational"
            message = "No lane terms matched; V1 used the conservative operational default."
    matched = tuple(term for term in _LANE_TERMS[selected] if term.replace("-", "_") in tokens)
    return LaneDiagnostic(code, message, selected, category_hint, matched, scores)


def build_workflow_suggestion(
    *,
    title: str,
    hypothesis: str,
    metric: str,
    next_action: str,
    safety_boundary: str,
    source_notes: Iterable[str],
    evidence: Iterable[Mapping[str, object]],
    category_hint: str = "",
) -> dict[str, object]:
    """Return the stable V1 JSON-ready envelope shared by both workers."""
    normalized_source_notes = sorted(dict.fromkeys(source_notes))
    normalized_evidence = [dict(item) for item in evidence]
    diagnostic = classify_lane(
        " ".join((title, hypothesis, metric, next_action)), category_hint=category_hint
    )
    return {
        "contract": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "lane": diagnostic.selected_lane,
        "suggestion": {
            "title": title,
            "hypothesis": hypothesis,
            "metric": metric,
            "next_action": next_action,
            "safety_boundary": safety_boundary,
            "source_notes": normalized_source_notes,
            "evidence": normalized_evidence,
        },
        "diagnostics": {
            **asdict(diagnostic),
            "lane_scores": {lane: diagnostic.lane_scores[lane] for lane in LANES},
            "input_scope": "local_notes_only",
            "side_effects": "proposal_artifact_only",
            "authority": dict(V1_AUTHORITY),
            "source_note_count": len(normalized_source_notes),
            "evidence_count": len(normalized_evidence),
        },
    }
