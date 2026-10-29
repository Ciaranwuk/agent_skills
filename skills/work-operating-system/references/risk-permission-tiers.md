# Risk and permission tiers

Risk is assigned before execution. The tier is a constraint, not a grant: the
effective permission is the intersection of the tier, the work-item profile,
the domain policy, and the runtime/operator approval. When uncertain, use the
higher tier and ask.

| Tier | Typical work | Reads | Local writes | External writes/messages | Financial, destructive, or permission actions | Human gate |
| --- | --- | --- | --- | --- | --- | --- |
| Low | Read-only analysis, drafts, deterministic reports | Declared workspace/context; no secret values | New, bounded, recoverable staged artifacts | None | Deny | Not normally, but required if scope or privacy is unclear |
| Medium | Reversible local implementation or controlled data transformation | Declared inputs and approved configuration; secrets only through approved runtime interfaces | Scoped repo/test/staging changes with reviewable diff | None by default | Deny | Review before promotion, merge, deletion, or any external side effect |
| High | External communication, production changes, personal-data handling, live or financial activity | Minimum necessary, explicitly approved sources | Approved target only with rollback plan | Explicitly approved and logged | Deny by default; no trades, payments, destructive actions, permission changes, or irreversible actions without explicit human approval | Required before action and again when material scope changes |

## Non-negotiable boundaries

- Never copy tokens, API keys, auth headers, or secret-bearing screenshots into
  prompts, artifacts, repositories, or durable notes.
- Deletion, overwrite, credential use, permission changes, external messages,
  trades, payments, and production mutations are consequential by default.
- Reversibility must be demonstrated, not assumed; a backup or rollback plan
  does not remove the approval requirement for a high-risk action.
- A skill, role, ticket, or agent cannot grant itself a permission that the
  runtime or operator has not granted.

## Escalation rules

Escalate when the target, authority, identity, data sensitivity, side effect,
rollback path, or required evidence is ambiguous. Stop immediately on secret
exposure, unexpected external effect, destructive behavior, conflicting
instructions, or evidence that the work has crossed its declared scope.

## Negative examples

- A scout must not turn a suggestion into a ticket mutation or a live run.
- An executor must not send a client email merely because a draft is complete.
- A reviewer must not approve a trade, merge, payment, deletion, or permission
  change as part of “verification”.
- A medium-risk local write must not overwrite an existing dataset when a new
  staged output would suffice.
