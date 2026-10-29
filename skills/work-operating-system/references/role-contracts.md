# Role contracts

Roles separate proposing, doing, observing, verifying, and approving. A role may
produce a handoff but cannot enlarge its own authority or approve its own
consequential work.

| Role | Inputs | Outputs | Authority | Stop conditions |
| --- | --- | --- | --- | --- |
| `validation_scout` | Work item, current state, constraints, prior evidence | Ranked next-task proposal with rationale, dependencies, expected evidence, and risks | Read permitted context; write a staged proposal only | Missing context, scope expansion, or proposal would require consequential action |
| `executor` | Approved work item and scoped handoff | Changed artifacts, execution log, validation results, unresolved risks | Only the declared local/external writes in the permission profile | Ambiguous authority, unexpected destructive/irreversible effect, secret exposure, or failed safety check |
| `friction_observer` | Work-item history, handoffs, corrections, failures, stale artifacts | Staged friction report with evidence and suggested remedy | Read-only analysis; no ticket, code, data, or permission mutation | Evidence is incomplete, finding is speculative, or remedy changes scope |
| `reviewer` | Original contract, executor output, evidence, and observer findings | Independent acceptance decision or blocked/escalated finding | Verify and classify; cannot silently repair or approve its own work | Acceptance criteria unmet, evidence unverifiable, conflict of interest, or human approval required |

## Handoff format

Every handoff contains: `work_item_id`, sender and recipient role, status,
completed outcome, artifact paths, evidence links, open risks, authority still
available, and next action. It contains no secrets, tokens, or unredacted
personal data.

## Conflict handling

1. A reviewer compares the claim with the frozen work-item contract and records
   the discrepancy.
2. An executor may clarify or produce a separately scoped correction, but may
   not rewrite the acceptance criteria after seeing results.
3. The decision owner resolves scope or risk disagreements. If the decision
   owner is unavailable, the state is `escalated` or `human_approval_required`.
4. A scout or observer may propose a follow-up ticket; neither may create
   authority by implication.

## Example flow

For a proposed strategy-validation diagnostic, the scout reads the frozen
candidate and proposes a warm-started long/short attribution run. The decision
owner approves that bounded work item. The executor runs it and writes a
compact report plus provenance. The observer notes missing warm-up evidence or
venue differences without changing the run. The reviewer checks the declared
gates, classifies the result as `completed` or `blocked`, and sends any paper
promotion question to the human decision owner. No role places orders or
changes the strategy from this flow.
