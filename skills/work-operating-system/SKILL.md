---
name: work-operating-system
description: Define, execute, verify, and record general work items with explicit scope, permissions, evidence, escalation, and completion states. Use for cross-domain work where safety and handoffs matter.
---

# Work Operating System

Use this contract for work that may span research, software, administration, or other domains. A domain skill adds specialist rules; this skill never grants permissions by itself.

## Work-item contract

Every item must declare:

- `work_item_id`, `title`, `desired_outcome`, and `scope`
- `owner` and `decision_owner`
- `risk_tier` and `permission_profile`
- `inputs`, `assumptions`, and `constraints`
- `planned_artifacts` and `required_evidence`
- `stop_conditions`, `rollback_plan`, and `escalation_rule`
- `dependencies`, `status`, and `next_action`

The canonical template is [`references/work-item-contract.yaml`](references/work-item-contract.yaml).
The completion checklist is [`references/completion-contract.yaml`](references/completion-contract.yaml).
Role boundaries are defined in [`references/role-contracts.md`](references/role-contracts.md), and risk/permission tiers in [`references/risk-permission-tiers.md`](references/risk-permission-tiers.md).

## Bounded scout/observer pilot

The pilot contract is [`references/pilot-contract.yaml`](references/pilot-contract.yaml).
Run [`scripts/run_read_only_pilot.py`](scripts/run_read_only_pilot.py) with a JSON
work item, optional JSON history, and a new output directory. It emits only a
scout proposal and friction report; it does not mutate tickets, repositories,
datasets, schedules, permissions, or external systems. Existing output files
are never overwritten. Proposals remain `human_approval_required` until the
decision owner accepts them.

## Lifecycle

`proposed` → `approved` → `in_progress` → `verification` → `completed`

Alternative terminal states are `blocked`, `escalated`, `rejected`, and `human_approval_required`. A completion claim is invalid unless the required evidence, changed artifacts, unresolved risks, and durable record are present.

## Authority boundaries

Default to deny for consequential actions. Proposal, execution, verification, and approval are separate responsibilities when risk warrants it. External messages, financial actions, destructive changes, permission changes, and irreversible operations require explicit human approval unless a narrower domain policy says otherwise.

## Handoff

Pass a compact handoff containing the work-item ID, current status, completed outcome, artifact paths, evidence links, open risks, and the next action. Never pass secrets or tokens in prompts, artifacts, or durable notes.

## Review

Before marking an item complete, independently check the acceptance criteria, inspect the actual changed artifacts, confirm no prohibited action occurred, and record any follow-up ticket. If evidence is insufficient, use `blocked`, `escalated`, or `human_approval_required` rather than claiming success.

## Completion gate

Populate the completion contract for every terminal state. A result may be marked `completed` only when the outcome, artifact inventory, risk-appropriate evidence, unresolved risks, decision owner, and durable record are present. The contract is a checklist, not an authorization mechanism; permission and human-approval requirements still come from the assigned risk tier and domain policy.
