#!/usr/bin/env python3
"""Emit deterministic proposal-only scout and observer reports."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any

REQUIRED = ("work_item_id", "title", "desired_outcome", "scope", "risk_tier", "permission_profile", "required_evidence", "stop_conditions", "decision_owner", "status")

def load(path: Path | None, default: Any) -> Any:
    if path is None:
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)

def build_reports(work_item: dict[str, Any], history: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    missing = [field for field in REQUIRED if not work_item.get(field)]
    evidence = work_item.get("required_evidence") or []
    completed = set(work_item.get("completed_evidence") or [])
    outstanding = [item for item in evidence if item not in completed]
    dependencies = work_item.get("dependencies") or []
    next_action = ("complete_missing_contract_fields" if missing else "resolve_dependencies" if dependencies else "collect_outstanding_evidence" if outstanding else "request_human_approval")
    authority = ["read_declared_work_item", "read_declared_history", "write_new_staged_json_reports"]
    proposal = {"pilot": "bounded-read-only-scout", "work_item_id": work_item.get("work_item_id"), "status": "human_approval_required", "recommended_next_action": next_action, "rationale": {"missing_contract_fields": missing, "outstanding_evidence": outstanding, "dependencies": dependencies}, "expected_evidence": outstanding or ["decision_owner_approval"], "risks": ["Advisory output must not mutate the work item.", "Scope expansion and parameter changes are prohibited."], "authority_used": authority, "next_state": "human_approval_required"}
    findings = []
    if missing:
        findings.append({"id": "missing-contract-fields", "severity": "high", "evidence": missing, "suggested_remedy": "Complete the work-item contract before execution."})
    if outstanding:
        findings.append({"id": "missing-evidence", "severity": "medium", "evidence": outstanding, "suggested_remedy": "Assign a bounded evidence task without changing acceptance criteria."})
    for entry in history:
        if entry.get("status") in {"blocked", "escalated", "human_approval_required"}:
            findings.append({"id": "unresolved-handoff", "severity": "medium", "evidence": entry, "suggested_remedy": "Escalate to or clarify with the decision owner."})
    observer = {"pilot": "bounded-read-only-friction-observer", "work_item_id": work_item.get("work_item_id"), "findings": findings, "finding_count": len(findings), "authority_used": authority, "mutations_performed": [], "next_state": "human_approval_required" if findings else "completed"}
    return proposal, observer

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-item", type=Path, required=True)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    work_item = load(args.work_item, {})
    history = load(args.history, [])
    if not isinstance(work_item, dict) or not isinstance(history, list):
        raise SystemExit("work item must be an object and history must be an array")
    proposal, observer = build_reports(work_item, history)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (("scout_proposal.json", proposal), ("friction_report.json", observer)):
        with (args.output_dir / name).open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    print(json.dumps({"status": "ok", "output_dir": str(args.output_dir), "files": ["scout_proposal.json", "friction_report.json"]}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
