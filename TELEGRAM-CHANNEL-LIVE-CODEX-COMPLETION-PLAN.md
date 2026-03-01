# Telegram Channel Live Codex Completion Plan (Execution-Ready)

Date: 2026-02-27  
Project: openclaw-channels (agent_skills Telegram runtime)  
Mode: Planning only (no implementation in this document)
Related explainer: `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md`

## 1) Objective

Finish the existing Telegram channel adapter/runtime so it can run as a robust live bridge to a Codex CLI-backed orchestrator, while preserving the provider-agnostic `channel_core` boundary and Telegram-first minimal scope.

## 2) Current Status Baseline

Implemented from prior TG plan:
- `TG-P0` to `TG-P3` are functionally complete in code/tests for the minimal transport/runtime slice.
- Existing validation indicates strong baseline coverage for happy paths and selected failure paths.
- `TG-QA-3` remains partially blocked in environment quality tooling (`ruff`/`black` availability), and live Codex integration has not yet been implemented.

What is still missing for target outcome:
- Live Codex orchestrator integration (current default behavior is echo-oriented).
- Robust delivery semantics under restart/failure (durable cursor/idempotency posture).
- Production-oriented observability/error taxonomy.
- Explicit handoff lifecycle for potentially long-running Codex responses.
- Live smoke/release evidence for Telegram -> Codex -> Telegram loop.

## 3) Scope and Non-Goals

In scope:
- Telegram polling text path (`getUpdates` + `sendMessage`) as current baseline.
- Codex CLI orchestrator integration through explicit runtime/orchestrator boundary.
- Reliability, observability, release hardening required for controlled live use.

Out of scope:
- Webhook mode.
- Media/callback/buttons.
- Multi-channel fanout.
- Distributed queue/broker.
- Heartbeat or memory schema redesign.

## 4) Design Constraints (Must Hold)

1. `channel_core/*` remains provider-agnostic and free of Telegram/Codex transport details.
2. Telegram transport logic stays in `telegram_channel/*`.
3. Codex process/session lifecycle logic sits behind `OrchestratorPort`, not inside adapter code.
4. Heartbeat and memory hooks stay best-effort and non-fatal.
5. No secrets in artifacts/tests/logs.

## 5) Detailed Task Board

## Phase TG-LIVE-A: Baseline Readiness and Guardrails

### TG-LIVE-A1 Quality Gate Closure
- Objective: close outstanding readiness gate for lint/format in this environment.
- Owner lane: `test` + `git-quality`
- Tasks:
  1. Validate availability/version pinning for lint/format tooling used by this repo.
  2. Execute lint/format checks and capture deterministic output artifact.
  3. Record pass/fail gate in readiness note.
- Acceptance criteria:
  - Quality gate command set is reproducible and documented.
  - No unresolved blocker remains for readiness signaling.

### TG-LIVE-A2 Live Mode Safety Guard
- Objective: require explicit operator intent for live operation.
- Owner lane: `backend`
- Tasks:
  1. Introduce live-mode flag contract in runtime config.
  2. Enforce non-empty allowlist when live mode is enabled.
  3. Keep non-live/dev behavior backward compatible.
- Acceptance criteria:
  - Live mode cannot start with empty allowlist.
  - Deterministic config errors and tests cover guard behavior.

Dependencies: none  
Risk level: LOW

## Phase TG-LIVE-B: Codex Orchestrator Integration

### TG-LIVE-B1 Codex Orchestrator Port Implementation Plan
- Objective: define and add a Codex-backed `OrchestratorPort` implementation seam.
- Owner lane: `architecture` -> `backend`
- Tasks:
  1. Specify sync request/response contract and failure taxonomy.
  2. Define per-session binding (`telegram:<chat_id>` continuity).
  3. Preserve fallback to default orchestrator for controlled rollback.
- Acceptance criteria:
  - Clear interface contract for handoff request/result/error paths.
  - Transport remains unaware of Codex subprocess details.

### TG-LIVE-B2 Session Worker Lifecycle
- Objective: stabilize long-running live behavior per chat/session.
- Owner lane: `backend`
- Tasks:
  1. Add worker lifecycle policy (spawn/reuse/timeout/idle-evict/restart).
  2. Define per-session serialization model to preserve conversational ordering.
  3. Add bounded queue/backpressure policy and overload behavior.
- Acceptance criteria:
  - One chat/session cannot interleave responses out-of-order.
  - Timeouts/restarts are deterministic and observable.

### TG-LIVE-B3 Handoff Failure Semantics
- Objective: make failure behavior machine-actionable and operator-readable.
- Owner lane: `backend`
- Tasks:
  1. Map Codex process failures to normalized runtime codes.
  2. Define retryability class (`transient`/`non-transient`) and operator actions.
  3. Ensure heartbeat events carry consistent context keys.
- Acceptance criteria:
  - Each failure class has stable code + expected runtime reaction.
  - No uncaught exception escapes loop path.

Dependencies: TG-LIVE-A2  
Risk level: MEDIUM

## Phase TG-LIVE-C: Delivery Semantics and Resilience

### TG-LIVE-C1 Ack/Idempotency Policy Hardening
- Objective: avoid silent update loss and duplicate side effects.
- Owner lane: `backend`
- Tasks:
  1. Define explicit ack policy for production live mode.
  2. Clarify semantics for no-outbound outcomes vs failed-outbound outcomes.
  3. Align service results with policy for operator diagnostics.
- Acceptance criteria:
  - Policy is explicit, tested, and documented.
  - Failure paths do not silently discard actionable inbound updates.

### TG-LIVE-C2 Durable Cursor/Offset Store
- Objective: survive restart without replay ambiguity.
- Owner lane: `backend`
- Tasks:
  1. Add minimal durable committed-offset persistence.
  2. Enforce monotonic floor for fetched updates.
  3. Emit stale/replayed drop counters.
- Acceptance criteria:
  - Restart preserves delivery cursor behavior.
  - Duplicate processing risk materially reduced.

### TG-LIVE-C3 Retry and Rate-Limit Behavior Refinement
- Objective: improve Telegram API resilience behavior in live mode.
- Owner lane: `backend`
- Tasks:
  1. Support rate-limit guidance (`retry_after`) in retry policy.
  2. Separate fetch/send retry budgets where needed.
  3. Add bounded cycle time budget to prevent stall cascades.
- Acceptance criteria:
  - Retry paths are deterministic and tuned for live polling.
  - Runtime remains responsive under transient API issues.

Dependencies: TG-LIVE-B1, TG-LIVE-B2  
Risk level: MEDIUM

## Phase TG-LIVE-D: Observability and Operator UX

### TG-LIVE-D1 Structured Error Details
- Objective: move beyond string-only errors for automation and triage.
- Owner lane: `backend`
- Tasks:
  1. Add backward-compatible structured error payload fields.
  2. Standardize context keys: `update_id`, `chat_id`, `session_id`, `layer`, `operation`.
  3. Keep existing `errors[]` for compatibility.
- Acceptance criteria:
  - Operators and automation can route by stable codes/fields.

### TG-LIVE-D2 Runtime Metrics/Events Contract
- Objective: provide a minimal telemetry contract for live operation.
- Owner lane: `architecture` -> `backend`
- Tasks:
  1. Define required counters/timers (fetch, send, retry, drops, queue depth, worker restarts).
  2. Clarify heartbeat emission outcomes (`disabled` vs `emit-failed`).
  3. Update runbook triage matrix with new telemetry cues.
- Acceptance criteria:
  - Metrics/events needed for go/no-go and rollback are present.

Dependencies: TG-LIVE-B3, TG-LIVE-C1  
Risk level: MEDIUM

## Phase TG-LIVE-E: Validation and Controlled Rollout

### TG-LIVE-E1 Test Expansion (Completion Gate)
- Objective: close high-risk test gaps before live enablement.
- Owner lane: `test`
- Mandatory additions:
  1. Adapter send-error wrapping, invalid `ack_update` IDs, skipped-only offset behavior.
  2. Parser missing chat/user and coercion edge branches.
  3. API invalid JSON/shape and transient `ok=false` retry semantics.
  4. Runtime wrapper exception branches and full CLI exit code matrix.
  5. Integration tests covering multi-cycle ack/send/drop invariants.
- Acceptance criteria:
  - Two consecutive stable passes for target suites.
  - No flaky tests in completion set.

### TG-LIVE-E2 Live Smoke Protocol
- Objective: prove minimal real-world viability with strict blast-radius controls.
- Owner lane: `test` + `documentation`
- Tasks:
  1. One allowlisted chat canary.
  2. Execute scripted scenarios: happy path, Telegram transient failure, Codex timeout/failure, allowlist drop.
  3. Record redacted evidence artifact with outcomes and rollback decision.
- Acceptance criteria:
  - Telegram -> Codex -> Telegram round-trip succeeds in canary.
  - Failure scenarios match expected behavior and recovery paths.

### TG-LIVE-E3 Final Readiness Review
- Objective: independent go/no-go before expanding allowlist.
- Owner lane: `git-quality`
- Tasks:
  1. Validate boundary rules and residual risks.
  2. Verify runbook reproducibility in clean shell.
  3. Produce signed-off readiness artifact with explicit rollout recommendation.
- Acceptance criteria:
  - No blocking defects.
  - Residual risks documented as low/acceptable for planned rollout stage.

Dependencies: TG-LIVE-A..D complete  
Risk level: MEDIUM

## 6) Dependency Graph (Summary)

1. TG-LIVE-A1 + A2
2. TG-LIVE-B1 -> B2 -> B3
3. TG-LIVE-C1/C2/C3 (after B1/B2)
4. TG-LIVE-D1/D2 (after B3 + C1)
5. TG-LIVE-E1 -> E2 -> E3

## 7) Rollout Strategy

1. Stage 0: non-live/dev mode only, expanded test coverage, quality gates green.
2. Stage 1: live mode enabled for exactly one allowlisted canary chat.
3. Stage 2: small allowlist cohort, monitor failure/timeout/drop metrics.
4. Stage 3: intended allowlist after successful readiness review.

Rollback triggers:
- sustained adapter fetch/send failure rate above threshold,
- repeated Codex handoff timeout beyond threshold,
- queue saturation or worker restart churn,
- unexpected duplicate/drop behavior in canary evidence.

## 8) Deliverables Checklist

- New/updated architecture note for Codex orchestrator boundary.
- End-to-end architecture/fit explainer for core, adapter, runtime, and heartbeat interaction:
  - `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md`
- Backend implementation note per phase (when executed later).
- Test validation artifact with two-pass stability evidence.
- Updated operator runbook with live mode and telemetry triage.
- Final readiness assessment artifact (go/no-go).

## 9) Delegation Plan for Execution (When Approved)

Per substantive phase:
1. `architecture` drafts phase design and acceptance contract.
2. `backend` implements phase tasks.
3. `test` creates/runs phase validation and failure-injection suites.
4. `documentation` updates runbook and operator playbooks.
5. `git-quality` performs readiness gate and release recommendation.

Parent orchestrator role:
- Integrate outputs,
- enforce boundaries/acceptance criteria,
- resolve conflicts,
- maintain vault logs/index/decision notes.
