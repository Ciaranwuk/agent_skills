"""Bounded, deterministic local seed topics for workflow suggestions.

The source pack is deliberately data-only: it never reads files, performs web
requests, mutates tickets, or schedules work.  The worker supplies local-note
text and turns matching topics into proposal candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class LaneSeed:
    lane: str
    key: str
    title: str
    trigger_terms: tuple[str, ...]
    hypothesis: str
    metric: str
    next_action: str


SEEDS: tuple[LaneSeed, ...] = (
    LaneSeed("operational_workflow", "workflow-suggestion-contract", "Define a bounded workflow-suggestion contract", ("workflow-suggestion", "lane-aware", "suggestion-lane"), "A deterministic lane contract will reduce silent empty runs.", "healthy runs with visible lane diagnostics", "Review one read-only lane contract proposal."),
    LaneSeed("strategy_research", "strategy-factory-funnel", "Design the next strategy-factory research funnel", ("strategy", "factory", "research", "paper"), "A lane-specific research funnel will produce more diverse, auditable candidates.", "time from idea to first evidence", "Review the candidate manifest and choose one bounded research experiment."),
    LaneSeed("backtest_data_reproducibility", "backtest-provenance-pack", "Strengthen backtest dataset provenance", ("backtest", "dataset", "provenance", "reproducibility"), "A shared provenance pack will make retained backtests reproducible without duplicating large data.", "replay success with matching dataset identity", "Review a local-only provenance checklist."),
    LaneSeed("agent_workflow_architecture", "agent-authority-boundaries", "Audit agent authority boundaries", ("agent", "architecture", "orchestration", "contract"), "Explicit role and authority boundaries will reduce unsafe or duplicated agent work.", "unapproved side effects detected", "Review one role/tool contract for missing guardrails."),
)


def matching_seeds(text: str, *, max_per_lane: int = 1) -> list[LaneSeed]:
    tokens = set(re.findall(r"[a-z0-9_-]+", text.lower()))
    result: list[LaneSeed] = []
    per_lane: dict[str, int] = {}
    for seed in SEEDS:
        if per_lane.get(seed.lane, 0) >= max_per_lane:
            continue
        if any(term.replace("-", "_") in tokens for term in seed.trigger_terms):
            result.append(seed)
            per_lane[seed.lane] = per_lane.get(seed.lane, 0) + 1
    return result


def source_pack_metadata() -> dict[str, object]:
    return {
        "version": "1.0",
        "source_policy": "local_note_text_only",
        "web_retrieval": False,
        "max_candidates_per_lane": 1,
        "cadence": "nightly",
        "lanes": sorted({seed.lane for seed in SEEDS}),
        "seed_keys": [seed.key for seed in SEEDS],
    }
