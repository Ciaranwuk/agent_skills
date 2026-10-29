# Agent Skills Repo

This repo contains a modular Telegram runtime plus two supporting subsystems:
- `heartbeat_system`: best-effort runtime/system eventing and heartbeat execution.
- `memory_system`: local markdown indexing and retrieval (`search` and `get`).

Current default context mode is `legacy` (`CHANNEL_CONTEXT_MODE=legacy`). Durable context mode (`durable`) is available behind explicit runtime config.

## Repository Map

- `channel_core`: provider-agnostic contracts and single-cycle processing (`process_once`).
- `telegram_channel`: Telegram API client, update parsing, adapter, and durable cursor floor state.
- `channel_runtime`: config parsing, orchestration wiring, polling loop, telemetry payloads.
- `heartbeat_system`: heartbeat CLI/API, scheduler runtime, event dedupe/state store, system-event queue.
- `memory_system`: SQLite FTS index builder plus safe file retrieval from configured source roots.
- `scripts/`: helper scripts (for example: `scripts/run_telegram_channel_checks.sh`).
- `artifacts/`: generated logs and verification outputs.

## Deep Sleep V1 Worker

Purpose:
- `scripts/deep_sleep_link_synthesis.py` is the deterministic first-pass worker for the Mission Control deep-sleep cron flow.
- It reads a bounded recent window of work-record markdown plus explicit read-only context files and writes additive synthesis artifacts into staging only.

Current safety boundary:
- Reads only markdown from the configured vault root plus the requested context files.
- Writes only new files under `_vault_staging/00_Inbox/deep_sleep/`.
- Does not edit or delete canonical-vault notes.
- Does not call the network.
- Does not implement assisted/LLM mode yet.

Primary CLI shape:

```bash
cd /home/cwilson/projects/agent_skills
python3 scripts/deep_sleep_link_synthesis.py \
  --vault-root /mnt/c/Users/ciara/OneDrive/Documents/llm_work_record \
  --staging-root /home/cwilson/projects/_vault_staging \
  --lookback-days 7 \
  --include-sessions \
  --include-work-logs \
  --include-decisions \
  --context-file START_HERE.md \
  --context-file 30_Projects/PROFILE-ciaran.md \
  --mode deterministic
```

CLI/runtime contract:
- Required selection: set at least one of `--include-sessions`, `--include-work-logs`, or `--include-decisions`.
- `--lookback-days` must be `>= 1`.
- `--context-file` values are vault-relative markdown paths and must remain under `--vault-root`.
- When `60_How_To/HOWTO-current-workflows.md` exists in the canonical vault or staged overlay, `surprise-me` auto-includes it as read-only context so already-owned workflows can suppress redundant checklist ideas before selection.
- `--mode` currently accepts only `deterministic`; assisted/LLM mode is intentionally out of scope for V1.
- Stdout emits one JSON object per invocation:
  - success: `{"run_id","status","notes_scanned","candidate_connections","output_file"}`
  - failure: the same payload shape with `status: "failed"` plus an `error` field
- Exit codes:
  - `0` when the worker completes and writes artifacts
  - `1` when the worker refuses unsafe input or cannot complete safely

Output contract:
- Every run writes exactly one markdown run report named `DEEP-SLEEP-RUN-YYYYMMDD-HHMMSS.md` under `_vault_staging/00_Inbox/deep_sleep/runs/`.
- Every run writes one JSON sidecar with the same stem beside that report for machine-readable follow-up tooling.
- Optional cluster notes are written under `_vault_staging/00_Inbox/deep_sleep/clusters/` only when the worker finds at least two related connections and one is high confidence.
- The worker refuses to overwrite an existing report or sidecar for the same run id.
- Run status is `succeeded` when evidence-backed connections exist and `no_findings` when the scan completes without candidate connections.
- Parse problems produce degraded output with explicit `parse_issues` in the report and sidecar; missing roots or unsafe inputs fail the run.

Deterministic V1 scope:
- Scans recent `SESSION-*`, `LOG-*`, and optionally in-window `DEC-*` notes.
- Pulls in older `DEC-*` notes only when recent session/work-log notes explicitly reference them.
- Emits conservative candidate connections such as `decision_affects_log`, `session_continues_plan`, `repeated_open_question`, and `shared_theme`.
- Keeps promotion into the canonical vault out of scope; review remains manual.

Mission Control boundary:
- Mission Control should schedule this worker as an opaque command and record its invocation metadata, retries, and run history.
- The worker's only durable outputs are staged artifacts under `_vault_staging/00_Inbox/deep_sleep/`; canonical-vault mutation remains explicitly out of scope.
- Current planning guidance is to run it after the nightly vault sync with at least a 30-minute buffer, for example `01:30 Europe/London` if sync remains at `01:00`.

Operator review rubric:
- `useful`: most findings are `medium` or `high` confidence, they collapse repeated references into a smaller set of concrete follow-ups, and at least one suggested action feels promotion-worthy.
- `noisy-but-safe`: outputs stay in staging and remain auditable, but the run is dominated by obvious restatements, weak `shared_theme` items, or pairwise note overlap that does not change operator understanding.
- `promote-worthy`: a run produces one or more connections or clusters that justify a new manual note, backlink, or consolidated summary in the canonical vault.
- If a run is `noisy-but-safe`, keep the artifacts for audit, tighten heuristics before widening scope, and do not promote anything into the canonical vault automatically.

## Surprise-Me V1 Worker

Purpose:
- `scripts/workflow_experiment_proposal.py` is the read-only pre-surprise-me worker for the "one experiment" loop: it scans recent session/work-log notes for repeated workflow friction and writes one staged proposal for a human-approved experiment.
- `scripts/surprise_me_workflow_improvement.py` is the deterministic proposal-first worker for the Mission Control surprise-me flow.
- It reads a bounded recent window of vault markdown, can supplement that with a small set of memory-system-retrieved canonical-vault notes, and writes exactly one staged workflow-improvement proposal per successful run.
- It can also read the latest staged workflow-experiment proposal sidecar so the pre-run experiment signal can compete with ordinary evidence-backed ideas.
- When the normal evidence-backed proposal lane finds nothing new, it falls back to one deterministic pseudo-random workstream topic from `Technology_Roadmap.md`, researches that topic using the linked roadmap note plus bounded memory retrieval, and can add bounded web-search evidence before emitting one fallback suggestion. If no roadmap topic is available, it still performs a bounded generic workflow web-search fallback before returning `no_findings`.
- `scripts/surprise_me_telegram_notifier.py` formats one concise Telegram summary from a staged `SURPRISE-ME-RUN-*.json` sidecar and sends it through the existing Telegram Bot API client.
- `scripts/run_surprise_me_nightly.py` is the preferred nightly wrapper: it runs the worker, records notification metadata in the run artifacts, and stays silent by default so morning triage remains the only scheduled Telegram delivery path.

Current safety boundary:
- Reads only markdown from the configured vault root, explicitly requested context files, and optional deep-sleep sidecars under `_vault_staging/00_Inbox/deep_sleep/runs/`.
- Memory retrieval is bounded and additive: it searches the local `memory_system` index against the canonical vault, adds only a capped set of markdown notes, and falls back to recent-only behavior when the index/backend is unavailable.
- Fallback research is also bounded and additive: it reads one roadmap topic plus its linked note when available, optionally augments that with a small memory-system context pack, can run a capped web-search pass (`duckduckgo`, top `3` hits, top `2` fetches by default), and records the selected topic/query/source notes plus web provenance in the staged report and sidecar. If the roadmap has no usable topic, the same web limits apply to a generic workflow-improvement query.
- The workflow-experiment pre-run is bounded and additive: it writes only `_vault_staging/00_Inbox/workflow_experiments/runs/WORKFLOW-EXPERIMENT-RUN-*.md` plus a JSON sidecar, and `surprise-me` reads that sidecar only when invoked with `--include-workflow-experiment-runs`.
- Uses the canonical vault as the durable surprise-me memory base for prior run history and synced registry state, with staged registry updates overlaid for pending same-cycle changes that have not synced yet.
- Writes only new run files under `_vault_staging/00_Inbox/surprise_me/runs/` plus a pending additive idea registry update at `_vault_staging/00_Inbox/surprise_me/idea_registry.json`.
- Does not edit the canonical vault, production repos, or Mission Control state.
- The worker only calls the network inside the no-proposal fallback lane unless `--disable-web-research` is set; web failures degrade to diagnostics rather than failing the run.
- Scheduled runs should not notify directly; optional Telegram send remains available only as an explicit wrapper opt-in for manual/debug use.
- Keeps all outputs reviewable as markdown plus JSON only.

Primary CLI shape:

```bash
cd /home/cwilson/projects/agent_skills
python3 scripts/workflow_experiment_proposal.py \
  --vault-root /mnt/c/Users/ciara/OneDrive/Documents/llm_work_record \
  --staging-root /home/cwilson/projects/_vault_staging \
  --lookback-days 7 \
  --include-sessions \
  --include-work-logs \
  --context-file START_HERE.md \
  --context-file 30_Projects/PROFILE-ciaran.md \
  --mode deterministic

python3 scripts/run_surprise_me_nightly.py \
  --vault-root /mnt/c/Users/ciara/OneDrive/Documents/llm_work_record \
  --staging-root /home/cwilson/projects/_vault_staging \
  --lookback-days 7 \
  --include-sessions \
  --include-work-logs \
  --context-file START_HERE.md \
  --context-file 30_Projects/PROFILE-ciaran.md \
  --include-workflow-experiment-runs \
  --workflow-experiment-limit 1 \
  --include-deep-sleep-runs \
  --deep-sleep-limit 3 \
  --mode deterministic
```

CLI/runtime contract:
- `scripts/run_surprise_me_nightly.py` returns exit code `1` only when the worker fails or refuses unsafe input.
- Scheduled runs skip Telegram notification by default and record notification status as `skipped` with reason `morning-digest-owned`.
- Telegram notification failure is still non-fatal when direct notification is explicitly enabled, and it is surfaced in stderr/stdout JSON so Mission Control run history can still show the problem.
- The notifier uses `SURPRISE_ME_TELEGRAM_BOT_TOKEN` first, then falls back to `CHANNEL_TOKEN`.
- For sparse manual/debug environments, the notifier also supports a gitignored local env file via `AGENT_SKILLS_ENV_FILE` or `agent_skills/.env.local`.
- Enable direct send only with `--telegram-notify`, then set the destination chat with `--notify-chat-id` or `SURPRISE_ME_TELEGRAM_CHAT_ID`.
- Required selection: set at least one of `--include-sessions`, `--include-work-logs`, or `--include-decisions`.
- `--lookback-days` must be `>= 1`.
- `--deep-sleep-limit` must be `>= 0`.
- `--workflow-experiment-limit` must be `>= 0`.
- Memory retrieval is enabled by default for the canonical vault and records its status in the run report/sidecar; disable it with `--disable-memory-retrieval`.
- `--memory-max-results` must be `>= 1`.
- `--memory-note-limit` must be `>= 0`.
- `--memory-min-score` must be between `0` and `1`.
- `--roadmap-file` must be a non-empty vault-relative markdown path; default is `Technology_Roadmap.md`.
- Web research is enabled by default for the no-proposal fallback lane; disable it with `--disable-web-research`.
- `--web-max-results` must be `>= 1`.
- `--web-fetch-limit` must be `>= 0`.
- `--web-timeout-seconds` must be `> 0`.
- `--context-file` values are vault-relative markdown paths and must remain under `--vault-root`.
- Deep-sleep inputs are optional and are read only from staged `DEEP-SLEEP-RUN-*.json` files.
- The default memory lookup uses `/home/cwilson/projects/.memory_index.sqlite`; override with `--memory-workspace` and/or `--memory-db-path` if you maintain the index elsewhere.
- Stdout emits one JSON object per invocation in the same style as deep-sleep:
  - success: `{"run_id","status","notes_scanned","candidate_connections","output_file",...}`
  - failure: the same core payload shape with `status: "failed"` plus an `error` field
- Exit codes:
  - `0` when the worker completes and writes artifacts
  - `1` when the worker refuses unsafe input or cannot complete safely

Output contract:
- Every run writes exactly one markdown run report named `SURPRISE-ME-RUN-YYYYMMDD-HHMMSS.md` under `_vault_staging/00_Inbox/surprise_me/runs/`.
- Every run writes one JSON sidecar with the same stem beside that report.
- Every run updates `_vault_staging/00_Inbox/surprise_me/idea_registry.json` with the latest merged registry state so the next vault sync can promote it into canonical memory.
- Successful runs select at most one evidence-backed improvement proposal, while still recording alternate candidates for review.
- Successful runs now record `selection_mode` as the normal evidence lane, the roadmap-research fallback lane, or the generic web-research fallback lane.
- Ideas now carry a stable `idea_key` plus review state (`unreviewed`, `approved`, `discarded`).
- The worker will not reselect ideas already marked `approved` or `discarded`, and it prefers fresh unreviewed ideas over ones already surfaced in prior runs.
- When `60_How_To/HOWTO-current-workflows.md` exists in the vault or staging overlay, the worker auto-loads it as durable context and suppresses candidates whose workflow is explicitly marked as already covered there.
- The worker refuses to overwrite an existing report or sidecar for the same run id.

Idea review workflow:
- Inspect the registry with:

```bash
cd /home/cwilson/projects/agent_skills
python3 scripts/surprise_me_idea_registry.py \
  --vault-root /mnt/c/Users/ciara/OneDrive/Documents/llm_work_record \
  --staging-root /home/cwilson/projects/_vault_staging \
  list
```

- Update one idea status with:

```bash
cd /home/cwilson/projects/agent_skills
python3 scripts/surprise_me_idea_registry.py \
  --vault-root /mnt/c/Users/ciara/OneDrive/Documents/llm_work_record \
  --staging-root /home/cwilson/projects/_vault_staging \
  set-status \
  --idea-key manual-loop-reduction-recurring-manual-review-loops \
  --status approved \
  --note "Approved for future rollout discussion."
```

- Use `--idea-id` instead of `--idea-key` when you only have the run artifact id.
- Run status is `succeeded` when one proposal is selected and `no_findings` only when the normal lane plus the no-proposal fallback research complete without a deterministic candidate.
- The worker reads configuration from CLI flags only; Telegram settings live in the wrapper/notifier layer rather than the worker itself.
- `no_findings` is a successful, non-paging outcome: the worker exits `0` and still writes the markdown report plus JSON sidecar so operators can see that the scan completed cleanly.
- Morning triage consults the current surprise-me idea registry before surfacing a staged workflow suggestion, so discarded or explicitly covered ideas do not reappear as active follow-ups.
- Degraded parse handling for individual notes or deep-sleep sidecars is also non-fatal: the run completes, records `parse_issues`, and remains reviewable from staging.
- Unsafe inputs, unreadable required roots, or artifact collisions fail closed with exit code `1`.
- When direct notification is explicitly enabled, the notifier message always includes the run id and staged markdown report path so the morning review can open the artifact directly.

Operator expectations:
- Treat each run as a morning review artifact, not as an automation that edits the vault, repo, or Mission Control state.
- Expect at most one selected proposal per run; alternate ideas remain in the report for manual triage.
- If Telegram delivery is enabled, notify only after the worker exits `0` and use the exact sidecar from that run rather than re-discovering the latest staged file later.
- Prefer closing `MC-0092` only when the selected idea reduces a repeated manual loop across recent notes; if the top result is mostly a ticket/file restatement, treat that run as `noisy-but-safe` and tighten heuristics before widening scope.

Surprise-me review rubric:
- `useful`: the selected idea condenses a repeated manual review or handoff pattern into one clear morning action and feels materially better than rereading the raw notes.
- `noisy-but-safe`: the run is auditable and staged-only, but the selected idea mostly restates a ticket id, file path, or already-obvious artifact with little workflow leverage.
- `noisy-but-safe`: generic `manual_loop_reduction` output is still noise when it does not name a concrete checklist, brief, or operator handoff artifact to create next.
- `promote-worthy`: the selected idea is concrete enough to become a follow-up ticket, checklist, or reviewed prototype request without substantial reinterpretation.

## Morning Triage V1 Worker

Purpose:
- `scripts/morning_triage_digest.py` is the deterministic first-pass worker for the Mission Control morning-triage flow.
- It consolidates the latest overnight cron state plus the most relevant staged worker artifacts into one operator-facing morning digest.
- `scripts/morning_triage_telegram_notifier.py` formats one concise Telegram summary from a staged `MORNING-TRIAGE-RUN-*.json` sidecar and sends it through the existing Telegram Bot API client.
- `scripts/run_morning_triage_digest.py` is the preferred scheduled wrapper when morning Telegram delivery is enabled: it runs the worker first, then notifies from the exact sidecar that run produced.

Current safety boundary:
- Reads only the configured Mission Control SQLite database plus staged JSON/markdown artifacts under `_vault_staging/00_Inbox/`.
- Writes only new files under `_vault_staging/00_Inbox/morning_triage/runs/`.
- Does not edit the canonical vault, production repos, or Mission Control state.
- The worker itself does not call the network.
- The notifier performs one narrow outbound Telegram `sendMessage` call only after the worker completes.
- Does not use model-assisted summarization in V1.

Primary CLI shape:
- `scripts/morning_triage_digest.py` reads:
  - `--db-path` pointing at `mission_control/data/mission_control.sqlite`
  - `--staging-root` pointing at `_vault_staging`
  - `--lookback-hours` with a deterministic default of `12`
  - `--job-name` as a repeatable optional filter; default source set is `vault-staging-sync-daily`, `deep-sleep-link-synthesis-nightly`, and `surprise-me-workflow-improvement-nightly`
  - `--mode deterministic`

```bash
cd /home/cwilson/projects/agent_skills
python3 scripts/run_morning_triage_digest.py \
  --db-path /home/cwilson/projects/mission_control/data/mission_control.sqlite \
  --staging-root /home/cwilson/projects/_vault_staging \
  --notify-chat-id 5946121750 \
  --lookback-hours 12 \
  --mode deterministic
```

CLI/runtime contract:
- `scripts/run_morning_triage_digest.py` returns exit code `1` only when the worker fails or refuses unsafe input.
- Telegram notification failure is non-fatal to the combined scheduled run, but it is surfaced in stderr/stdout JSON so Mission Control run history can still show the problem.
- The notifier uses `MORNING_TRIAGE_TELEGRAM_BOT_TOKEN` first, then falls back to `CHANNEL_TOKEN`.
- For scheduled runs with a sparse host env, the notifier also supports a gitignored local env file via `AGENT_SKILLS_ENV_FILE` or `agent_skills/.env.local`.
- Set the destination chat either with `--notify-chat-id` or `MORNING_TRIAGE_TELEGRAM_CHAT_ID`.
- Primary inputs should be:
  - `--db-path` pointing at `mission_control/data/mission_control.sqlite`
  - `--staging-root` pointing at `_vault_staging`
  - `--lookback-hours` with a deterministic default of `12`
  - `--job-name` as a repeatable optional filter; default source set is `vault-staging-sync-daily`, `deep-sleep-link-synthesis-nightly`, and `surprise-me-workflow-improvement-nightly`
  - `--mode deterministic`
- Stdout should emit one JSON object per invocation:
  - success with attention: `{"run_id","status":"attention_required","jobs_considered","urgent_items","output_file"}`
  - clean run: `{"run_id","status":"nothing_urgent","jobs_considered","urgent_items","output_file"}`
  - failure: the same core payload shape with `status: "failed"` plus an `error` field
- Exit codes:
  - `0` when the digest is generated successfully, even if the content reports failures or blocked runs
  - `1` when the worker refuses unsafe input or cannot complete safely

Output contract:
- Every run should write exactly one markdown digest named `MORNING-TRIAGE-RUN-YYYYMMDD-HHMMSS.md` under `_vault_staging/00_Inbox/morning_triage/runs/`.
- Every run should write one JSON sidecar with the same stem beside that digest for later notifier/UI reuse.
- The worker should refuse to overwrite an existing digest or sidecar for the same run id.
- JSON sidecars should include:
  - run metadata: `run_id`, `status`, `generated_at`, `lookback_hours`
  - source coverage: `jobs_considered`, `job_statuses`, `artifacts_considered`
  - ranked findings: `urgent_items` and `follow_ups`
  - output metadata: markdown path plus related staged artifact paths
  - degraded parsing details under `parse_issues`
- Missing optional worker sidecars should degrade cleanly into a Mission-Control-only summary rather than failing the run.

Deterministic V1 scope:
- Treat Mission Control as the source of truth for cron schedule/run status, especially failures, timeouts, blocked lanes, and missing expected overnight runs.
- Read the existing `tickets` table from Mission Control to add a compact open-ticket snapshot to the digest, without mutating any ticket state.
- Only escalate blocked tickets into urgent morning attention when the blocked ticket was updated inside the review window; older unresolved blockers stay visible in the ticket snapshot without paging every morning.
- Read the latest relevant staged sidecars from deep-sleep and surprise-me only to enrich successful-job summaries with concrete findings and artifact links.
- When surprise-me succeeds, surface the selected idea directly in the digest as one workflow suggestion with a human-readable triage prompt, expected benefit, rationale, and source-note footprint so the operator does not need to open a separate surprise-me review lane first.
- Rank output failure-first:
  - failed or timed out cron runs that do not already have a later success in the same review window
  - expected overnight jobs missing a recent run in the lookback window
  - recently updated blocked tickets or degraded workflows needing manual review
  - successful deep-sleep and surprise-me highlights
  - an explicit short `nothing urgent` close when no higher-priority item exists
- Keep notifier behavior, promotion automation, ticket mutation, and Mission Control UI persistence out of scope for V1.

Operator expectations:
- This is a read-only morning review artifact, not an automation that edits tickets, the vault, or worker schedules.
- The digest should link directly to the staged deep-sleep and surprise-me reports it references instead of rephrasing them at length.
- A failure-heavy night should produce a short, obvious checklist of what needs attention first.
- A clean night should degrade to a compact summary plus at most one clearly labeled workflow suggestion from surprise-me when one exists.
- If Telegram delivery is enabled, notify only after the worker exits `0` and use the exact sidecar from that run rather than re-discovering the latest staged file later.
- Once the same job records a later success in the review window, older failed attempts should not continue to page the operator as active urgent items.

Operator review rubric:
- `useful`: the first urgent item points to a real overnight blocker or degraded workflow, and the digest makes the next review action obvious without opening several separate tools first.
- `noisy-but-safe`: the digest remains read-only and auditable, but mostly restates healthy runs or repeats low-value warnings that do not change what the operator should do next.
- `promote-worthy`: the digest is concise enough to become the default morning starting point because it catches failures early and links directly to the staged artifact or run history that needs review.

## Quick Start (Runtime)

Run from repo root:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_TOKEN="<REDACTED_BOT_TOKEN>"
export CHANNEL_MODE="poll"
export CHANNEL_ACK_POLICY="always"
export CHANNEL_POLL_INTERVAL_S="2.0"
export CHANNEL_ALLOWED_CHAT_IDS=""
export CHANNEL_LIVE_MODE="false"
export CHANNEL_CONTEXT_MODE="legacy"
export CHANNEL_CONTEXT_STRICT_IO="false"
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
```

Run one cycle:

```bash
python3 -m channel_runtime --once
```

Run continuously:

```bash
python3 -m channel_runtime
```

This command is a foreground poll loop. It is healthy for it to keep the terminal occupied and emit one JSON payload per polling cycle; use Ctrl+C to stop it, or use the restart helper/systemd service below for normal background operation.

If JSON output stops, restart with cycle tracing enabled to see the last completed stage on stderr while keeping stdout as JSON:

```bash
CHANNEL_RUNTIME_TRACE_CYCLES=1 python3 -m channel_runtime
```

Trace stages include fetch, orchestrator, send, and ack boundaries. If the final trace line is `fetch:start`, investigate Telegram networking; if it is `orchestrator:start`, investigate Codex/acquisition/voice-note processing for the referenced update; if it is `send:start`, investigate Telegram send/TTS.

Apply a new Codex timeout in continuous mode:

```bash
export CHANNEL_CODEX_TIMEOUT_S="30.0"
bash scripts/restart_channel_runtime.sh
```

`scripts/restart_channel_runtime.sh` gracefully stops the existing runtime process tracked in
`.channel_runtime/channel_runtime.pid` and starts a new one with current environment values.

## Durable Telegram Runtime Service

Purpose:
- Keep the Telegram runtime alive across WSL restarts and shell exits.
- Reuse the same repo-local startup path for manual restarts and supervised runs.
- Fail fast when the host still cannot resolve the external domains the runtime depends on.

Foreground startup wrapper:
- `scripts/run_channel_runtime_foreground.sh`
- Loads `AGENT_SKILLS_ENV_FILE` when set, otherwise falls back to `agent_skills/.env.local`.
- Preserves already-exported shell env values instead of overwriting them from the file.
- Runs DNS preflight through `scripts/check_channel_runtime_dns.py` before `python3 -m channel_runtime`.

DNS preflight defaults:
- `api.telegram.org`
- `openai.com`
- `www.youtube.com`

Optional startup env knobs:
- `CHANNEL_RUNTIME_PYTHON_BIN` (`python3` default)
- `CHANNEL_RUNTIME_DNS_PREFLIGHT` (`true` default; accepts `true/false`, `1/0`, `yes/no`, `on/off`)
- `CHANNEL_RUNTIME_DNS_PREFLIGHT_HOSTS` (comma- or space-separated host list)
- `CHANNEL_RUNTIME_DNS_PREFLIGHT_PORT` (`443` default)
- `CHANNEL_RUNTIME_LOG_MAX_BYTES` (`104857600` default; set `0` to disable restart-helper log rollover)

Preferred host install shape:
- Copy [`ops/systemd/telegram-channel-runtime.service`](/home/cwilson/projects/agent_skills/ops/systemd/telegram-channel-runtime.service) to `/etc/systemd/system/`.
- Ensure the runtime env is available either through:
  - `/home/cwilson/projects/agent_skills/.env.local`, or
  - a host env file exposed as `AGENT_SKILLS_ENV_FILE` via a systemd drop-in or unit edit
- Run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-channel-runtime.service
sudo systemctl status telegram-channel-runtime.service
```

The service runs `scripts/run_channel_runtime_foreground.sh`, so manual `bash scripts/restart_channel_runtime.sh`
and supervised startup now share the same env-loading and DNS-preflight behavior.

Disk-space alert:
- `ops/systemd/disk-space-alert.service` and `ops/systemd/disk-space-alert.timer` check `/mnt/c` hourly,
  which is the laptop's Windows-mounted data disk in this environment.
- The default threshold is 10 GB. `DISK_ALERT_THRESHOLD_GB`, `DISK_ALERT_PATH`, and
  `DISK_ALERT_STATE_PATH` can override the defaults.
- The first low-space check sends a Telegram warning to `DISK_ALERT_CHAT_ID` (defaulting to the configured
  operator chat), repeated checks are suppressed, and a recovery message is sent after space is restored.
- Install and enable it with:

```bash
sudo cp ops/systemd/disk-space-alert.service ops/systemd/disk-space-alert.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now disk-space-alert.timer
systemctl status disk-space-alert.timer
```

## Telegram YouTube Transcript Path Status

Current direction:
- Do not treat the Telegram runtime as the preferred execution path for YouTube transcript retrieval or other network-sensitive jobs such as Binance/API pulls.
- Use Telegram, if needed, only for lightweight capture or reminders, then perform the actual retrieval and analysis directly in this local Codex environment.
- Keep raw Telegram media support out of scope; the runtime still only parses `message.text`.

Retained implementation status:
- `channel_runtime/codex_orchestrator.py` now accepts a narrow acquisition/message-preparation seam ahead of sandboxed `codex exec`.
- `channel_runtime/acquisition.py` is the privileged acquisition boundary. It detects allowlisted jobs (currently YouTube transcript acquisition), writes a bounded local artifact, and returns prompt-safe text to the orchestrator.
- `channel_runtime/youtube_transcript.py` performs the transcript-specific fetch/format logic used by the acquisition boundary.
- If transcript fetch fails or the dependency is missing, the operator gets a direct deterministic reply instead of a generic Codex error.
- This path is now best understood as legacy/diagnostic machinery, not the recommended operator workflow for routine transcript retrieval.

Acquisition modes:
- `CHANNEL_ACQUISITION_MODE="inline"` (default): runtime performs allowlisted acquisition directly in the parent Telegram runtime process before handing text to sandboxed Codex.
- `CHANNEL_ACQUISITION_MODE="command"`: runtime invokes one explicit host-side command for allowlisted acquisition jobs and persists the returned artifact locally before handing text to sandboxed Codex.
- `CHANNEL_ACQUISITION_MODE="disabled"`: acquisition seam is off; inbound text is passed through unchanged.

Acquisition safety contract:
- Only deterministic allowlisted job types should use the acquisition seam.
- Current default allowlist is `youtube-transcript`.
- The general spawned Codex worker remains sandboxed; it should consume local artifacts, not broad network access.
- Future networked jobs such as crypto market-data acquisition should not be added to the Telegram runtime by default; prefer direct local execution here unless there is a strong reason to re-open the runtime-side boundary.

Command-mode contract:
- `CHANNEL_ACQUISITION_COMMAND` should point to a deterministic host-side helper.
- The helper reads one JSON request on stdin and writes one JSON response on stdout.
- Optional helper-only proxy settings for YouTube transcript fetches:
  - `CHANNEL_YOUTUBE_TRANSCRIPT_PROXY_HTTP_URL`
  - `CHANNEL_YOUTUBE_TRANSCRIPT_PROXY_HTTPS_URL`
- These proxy settings are consumed only by the repo-local YouTube acquisition helper path; they do not widen general runtime or Codex networking.
- The repo-local helper entrypoint is:

```bash
python3 /home/cwilson/projects/agent_skills/scripts/run_channel_runtime_acquisition_helper.py
```

- Current helper behavior:
  - accepts only `youtube-transcript`
  - rejects unknown job types with structured stderr JSON
  - when helper-only proxy env vars are set, passes them to `youtube-transcript-api` as a narrow proxy config for transcript fetches
  - fetches the transcript and returns one bounded JSON artifact payload on stdout
  - when `export_dir` is provided, writes the full transcript markdown file there and returns only metadata plus `export_path` on stdout
- Current request shape:

```json
{
  "job_type": "youtube-transcript",
  "source_url": "https://youtu.be/dQw4w9WgXcQ",
  "message_text": "Please review this video https://youtu.be/dQw4w9WgXcQ"
}
```

- Current response shape:

```json
{
  "job_type": "youtube-transcript",
  "artifact": {
    "source_url": "https://youtu.be/dQw4w9WgXcQ",
    "video_id": "dQw4w9WgXcQ",
    "transcript_text": "transcript text",
    "segment_count": 42,
    "truncated": false,
    "language_code": "en"
  }
}
```

- Full transcript export request shape:

```json
{
  "job_type": "youtube-transcript",
  "source_url": "https://youtu.be/dQw4w9WgXcQ",
  "export_dir": "/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record/00_Inbox/youtube_transcripts"
}
```

- Full transcript export response shape omits `transcript_text` so the full transcript is written to markdown instead of returned through helper stdout:

```json
{
  "job_type": "youtube-transcript",
  "artifact": {
    "source_url": "https://youtu.be/dQw4w9WgXcQ",
    "video_id": "dQw4w9WgXcQ",
    "segment_count": 42,
    "truncated": false,
    "language_code": "en",
    "export_path": "/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record/00_Inbox/youtube_transcripts/YOUTUBE-dQw4w9WgXcQ-20260620-120000.md",
    "transcript_text_included": false
  }
}
```

- Alternatively set `CHANNEL_YOUTUBE_TRANSCRIPT_EXPORT_DIR` and send `"export_full_transcript": true`.

Legacy prerequisite, only if this path is re-tested:

```bash
python3 -m pip install youtube-transcript-api
```

Boundary notes:
- Supported input is a YouTube link sent as plain text, optionally with surrounding text.
- Unsupported cases such as raw Telegram video uploads, missing captions, or blocked videos return a clear reply and do not change transport behavior.
- If you want a transcript or API data now, prefer asking for it directly in this environment instead of relying on the Telegram runtime path.

## Telegram Voice Notes V1

Behavior:
- Telegram `message.voice` payloads are downloaded through the Bot API `getFile` path and normalized into the existing inbound text flow.
- When the configured/local `whisper` CLI is available, the runtime transcribes the downloaded audio and forwards the transcript to the existing orchestrator path as text.
- When voice-note handling is disabled, Whisper is unavailable, Telegram file lookup/download fails, or transcription fails, the runtime sends a direct deterministic Telegram reply instead of silently dropping the update.

Host prerequisite:

```bash
python3 -m pip install openai-whisper
```

Notes:
- You may need `ffmpeg` installed on the host for local transcription to work reliably.
- The first `whisper` run downloads the selected model into the local cache automatically; you do not need to commit model files into this repo.

Host/runtime knobs:
- `CHANNEL_VOICE_NOTES_ENABLED` (`true` default)
- `CHANNEL_VOICE_NOTE_WHISPER_COMMAND` (`whisper` default; may be an absolute path)
- `CHANNEL_VOICE_NOTE_WHISPER_MODEL` (`turbo` default)
- `CHANNEL_VOICE_NOTE_LANGUAGE` (optional)
- `CHANNEL_VOICE_NOTE_TRANSCRIBE_TIMEOUT_S` (`120` default)
- `CHANNEL_VOICE_NOTE_MAX_CHARS` (`4000` default)
- `CHANNEL_VOICE_NOTE_TEMP_DIR` (`.channel_runtime/voice_notes` default)

## Telegram Text To Speech V1

Behavior:
- `/speak <text>` and `/tts <text>` are intercepted before Codex execution and mark the outbound reply for Telegram voice delivery.
- The Telegram adapter keeps `OutboundMessage.text` as the canonical fallback and attempts local speech synthesis only when `telegram_tts.enabled` metadata is present.
- Local synthesis failures fall back to a plain text reply; transient Telegram `sendVoice` delivery failures still raise so `ack_policy=on-success` can safely retry instead of risking duplicate text plus audio.

Host prerequisite:

```bash
sudo apt-get install ffmpeg espeak-ng
```

Example runtime setup:

```bash
export CHANNEL_TTS_ENABLED="true"
export CHANNEL_TTS_COMMAND="python3 /home/cwilson/projects/agent_skills/scripts/run_channel_runtime_tts_helper.py"
```

Host/runtime knobs:
- `CHANNEL_TTS_ENABLED` (`false` default)
- `CHANNEL_TTS_COMMAND` (empty default; local helper command)
- `CHANNEL_TTS_TIMEOUT_S` (`30` default)
- `CHANNEL_TTS_MAX_CHARS` (`2000` default)
- `CHANNEL_TTS_TEMP_DIR` (`.channel_runtime/tts` default)
- `CHANNEL_TTS_VOICE` (optional voice override passed to the helper)
- `CHANNEL_TTS_LANGUAGE` (optional helper hint)

## Telegram Context Management (P3)

Context env vars and defaults:
- `CHANNEL_CONTEXT_MODE` (`legacy` default; `durable` enables persistent transcript + compaction path)
- `CHANNEL_CONTEXT_WINDOW_TOKENS` (`28000`)
- `CHANNEL_CONTEXT_RESERVE_TOKENS` (`8000`)
- `CHANNEL_CONTEXT_KEEP_RECENT_TOKENS` (`12000`)
- `CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS` (`1200`)
- `CHANNEL_CONTEXT_MIN_GAIN_TOKENS` (`800`)
- `CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S` (`300`)
- `CHANNEL_CONTEXT_STRICT_IO` (`false`)
- `CHANNEL_CONTEXT_MANUAL_COMPACT` (`false`)

Effective behavior notes:
- Durable path is active only when `CHANNEL_CONTEXT_MODE=durable`.
- `CHANNEL_CONTEXT_STRICT_IO=true` makes malformed durable transcript lines fail load (`context-store-load-error`) instead of being skipped.
- `/ctx inspect` and `/ctx compact` are intercepted only when all are true:
  - `CHANNEL_ORCHESTRATOR_MODE=codex`
  - `CHANNEL_CONTEXT_MODE=durable`
  - `CHANNEL_CONTEXT_MANUAL_COMPACT=true`
- `/runtime env` is intercepted under the same operator-control gate and writes a safe allowlisted env snapshot to `artifacts/channel_runtime/runtime_env_diagnostics.log`.
- `/runtime youtube-probe <youtube-url>` is intercepted under the same operator-control gate and writes a runtime-side transcript probe record to `artifacts/channel_runtime/youtube_probe_diagnostics.log`.
- Parsed policy values `CHANNEL_CONTEXT_MIN_GAIN_TOKENS`, `CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S`, and `CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS` are reserved for subsequent wiring; current codex durable runtime uses fixed compaction policy inputs (`min_compaction_gain_tokens=0`, `cooldown_window_s=0.0`).

Operator command output fields (`/ctx inspect` and `/ctx compact`):
- `session_id`, `status`, `reason`, `tokens_before`, `tokens_after`, `gained_tokens`, `turns_before`, `turns_after`

Operator command output fields (`/runtime env`):
- `status`, `reason`, `env_keys`, `env_keys_set`, `log_path`

Operator command output fields (`/runtime youtube-probe <youtube-url>`):
- `status`, `reason`, `probe_code`, `video_id`, `dns_failures`, `log_path`

Rollback to legacy context mode:

```bash
export CHANNEL_CONTEXT_MODE="legacy"
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
bash scripts/restart_channel_runtime.sh
```

Legacy-path deprecation contract (TG-CTX-P4-T03):
- `CHANNEL_CONTEXT_MODE=legacy` is the emergency kill-switch and remains supported for one release cycle after 2026-03-05.
- Removal gate for legacy mode:
  - durable-mode canary and full rollout stay stable for the full release cycle;
  - no unresolved sev-1/sev-2 incidents attributable to durable context;
  - operators validate rollback/runbook flows without requiring legacy mode.
- If durable behavior regresses during the retention window, operators should immediately roll back with the command block above.

P3 runtime payload additions for context triage:
- `telemetry.context.mode`
- `telemetry.context.compaction.*`
- `telemetry.context.tokens.*`
- `runtime_digest.context_mode`
- `runtime_digest.context_compaction.*`
- `runtime_digest.context_tokens.*`
- heartbeat event `context.telemetry_digest.context_*` fields

## heartbeat_system (Need To Know)

Purpose:
- Provides heartbeat run control (`run-once`, scheduler start/stop APIs) and a session-scoped system-event bus.
- Used by `channel_runtime` for best-effort failure publication (it does not gate runtime success).

Key entrypoints:
- CLI: `python3 -m heartbeat_system ...`
- API: `heartbeat_system.api.run_once`, `get_status`, `get_last_event`, `wake`, `enable_heartbeat`, `disable_heartbeat`, `publish_system_event`

Typical usage patterns:

```bash
# One heartbeat run (manual reason)
python3 -m heartbeat_system run-once --reason manual

# Runtime/heartbeat visibility
python3 -m heartbeat_system status
python3 -m heartbeat_system last-event

# Wake request for active scheduler
python3 -m heartbeat_system wake --reason manual
```

Where runtime uses it:
- `channel_runtime.runner.HeartbeatEventEmitter` publishes runtime failure diagnostics through `heartbeat_system.api.publish_system_event`.
- Emission is attempted for cycle failures, adapter/orchestrator diagnostics, and process-once exceptions.

Failure semantics (best-effort, non-fatal):
- Heartbeat emission failures are swallowed and tracked as `heartbeat_emit_failures` telemetry.
- Runtime cycle completion does not depend on heartbeat publish success.
- If heartbeat runtime/scheduler is not running, `wake` returns a structured `not-running` payload instead of crashing.

## memory_system (Need To Know)

Purpose:
- Local markdown memory service for indexing and retrieval.
- Supports three-step workflow: `rebuild` index -> `search` snippets -> `get` safe file content.

Index/search/get workflow:
1. Build or sync SQLite FTS index from allowed markdown roots.
2. Query index with `search` (returns ranked snippets and line spans).
3. Read source content with `get` (full file or line window).

Key commands:

```bash
cd /home/cwilson/projects/agent_skills

# Rebuild default workspace index
python3 -m memory_system rebuild --force

# Search local memory
python3 -m memory_system search "telegram timeout" --max-results 5 --min-score 0.2

# Read a file from workspace root
python3 -m memory_system get README.md --from 1 --lines 40

# Include an extra source root and use canonical key for secondary root files
python3 -m memory_system --source-root /tmp/extra_docs search "heartbeat"
python3 -m memory_system --source-root /tmp/extra_docs get root1:notes.md
```

DB path pattern:
- Default: `<workspace>/.memory_index.sqlite`
- Override with `--db <path>` on any command.

Safety notes:
- Source roots are explicit: primary `--workspace` plus optional repeated `--source-root`.
- Index scan excludes noisy directories (`.git`, `node_modules`, `.venv`, `venv`, `dist`, `build`, `__pycache__`).
- `get` is constrained to allowed roots and markdown files only:
  - denies absolute paths
  - denies `..` traversal
  - denies symlink path components
  - denies non-`.md` targets
- When search index is missing/unavailable, `search` returns a deterministic unavailable payload (does not throw by default).

Runtime usage note:
- In default orchestrator mode, enabling `CHANNEL_ENABLE_MEMORY_HOOK=true` triggers a best-effort `memory_search(..., maxResults=1)` lookup and appends `memory: <snippet>` only when a result exists.

## How Components Fit Together

- `telegram_channel` fetches updates, parses inbound messages, sends replies, and manages cursor acks.
- `channel_core` executes a single deterministic `fetch -> orchestrate -> send -> ack` cycle.
- `channel_runtime` composes adapter + orchestrator + policies, runs one cycle or loop, and emits telemetry.
- `memory_system` is an optional lookup dependency used by runtime default orchestrator when memory hook is enabled.
- `heartbeat_system` is a side-channel for runtime/system events and heartbeat operations; runtime treats it as best-effort and non-blocking.

## Codex Mode Controls

```bash
export CHANNEL_ORCHESTRATOR_MODE="codex"
export CHANNEL_CODEX_TIMEOUT_S="20.0"
export CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="false"
# Default remains workspace-write. Set config-default to let Codex config
# default_permissions, such as workspace-network, control the sandbox/profile.
export CHANNEL_CODEX_SANDBOX_MODE="workspace-write"
export CHANNEL_CODEX_DEFAULT_PERMISSIONS=""
export CHANNEL_CODEX_ADD_DIRS="/mnt/d,/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record"
export CHANNEL_CODEX_SEARCH="false"
export CHANNEL_CODEX_PROFILE=""
export CHANNEL_CODEX_MODEL="gpt-5.6-luna"
```

Behavior:
- `CHANNEL_ORCHESTRATOR_MODE="default"`: echo-style response path (optionally memory-enriched).
- `CHANNEL_ORCHESTRATOR_MODE="codex"`: invokes `codex exec` via `channel_runtime.codex_orchestrator`.
- In codex mode, allowlisted acquisition can run before `codex exec`, but the spawned Codex worker remains sandboxed by default.
- `CHANNEL_CODEX_SANDBOX_MODE="workspace-write"` preserves the default `codex exec --sandbox workspace-write` behavior. Use `config-default` to omit `--sandbox` and let Codex config, including `default_permissions`, apply. `danger-full-access` is rejected by the runtime env path.
- `CHANNEL_CODEX_DEFAULT_PERMISSIONS="workspace-network"` adds `-c default_permissions="workspace-network"` for scoped network opt-in when supported by the installed Codex CLI/config.
- `CHANNEL_CODEX_ADD_DIRS` is an optional comma-separated list of absolute paths passed as repeated `--add-dir` arguments. The installed service grants Codex write access to `/mnt/d` and `/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record`; this does not require the Linux mount itself to appear writable to the parent Python process.
- `CHANNEL_CODEX_SEARCH="true"` adds top-level `--search`, enabling model web search separately from shell/Python network access.
- `CHANNEL_CODEX_PROFILE` optionally passes `--profile <name>` to layer a Codex config profile.
- `CHANNEL_CODEX_MODEL` sets the first `--model <model>` choice; it defaults to `gpt-5.6-luna`. If that invocation fails, the runtime retries with `gpt-5.4`, then once without `--model` so Codex can use its configured available model.
- `CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="true"`: sends minimal user-facing fallback text on codex failures/timeouts.
- `CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="false"`: suppresses fallback text; diagnostics still recorded.

Scoped network tryout:

```bash
export CHANNEL_ORCHESTRATOR_MODE="codex"
export CHANNEL_CODEX_SANDBOX_MODE="config-default"
export CHANNEL_CODEX_DEFAULT_PERMISSIONS="workspace-network"
export CHANNEL_CODEX_SEARCH="true"
```

## Testing

```bash
python3 -m unittest discover -s channel_core/tests -p 'test_*.py'
python3 -m unittest discover -s telegram_channel/tests -p 'test_*.py'
python3 -m unittest discover -s channel_runtime/tests -p 'test_*.py'
python3 -m unittest discover -s heartbeat_system/tests -p 'test_*.py'
python3 -m unittest discover -s memory_system/tests -p 'test_*.py'
```

Or:

```bash
bash scripts/run_telegram_channel_checks.sh
```

## Additional Docs

- [TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md](TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md)
- [TELEGRAM-CHANNEL-OPERATOR-RUNBOOK.md](TELEGRAM-CHANNEL-OPERATOR-RUNBOOK.md)
- [TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md](TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md)
- [TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md](TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md)

## Context Schema Docs

- Durable session metadata contract (`schema_version = 1`):
  - `artifacts/schemas/context_session.schema.json`
- Durable transcript entry contract (`schema_version = 1`):
  - `artifacts/schemas/context_transcript_entry.schema.json`
