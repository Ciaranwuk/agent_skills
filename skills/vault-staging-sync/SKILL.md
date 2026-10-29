---
name: vault-staging-sync
description: Promote markdown files from `/home/cwilson/projects/_vault_staging` into the canonical `llm_work_record` vault using the deterministic sync script, merge same-day session/work-log collisions additively, optionally prune promoted staging files, and reset `_vault_staging/INDEX.md` to the empty-state template. Use when asked to sync, promote, clean, or reset the work-record staging area.
---

# Vault Staging Sync

Use this skill when the task is to move staged work-record files from `_vault_staging` into the canonical `llm_work_record` vault safely and repeatably. The canonical vault is the normal write destination; staging is only the fallback when canonical writes are unavailable.

## Default Paths

- Staging root: `/home/cwilson/projects/_vault_staging`
- Canonical vault: `/mnt/c/Users/ciara/OneDrive/Documents/llm_work_record`
- Script: `/home/cwilson/projects/agent_skills/scripts/vault_staging_sync.py`

## Workflow

1. Confirm the request is a staging-vault promotion/cleanup task.
2. Run the deterministic script:
   `python3 /home/cwilson/projects/agent_skills/scripts/vault_staging_sync.py`
3. If the user wants to keep staged files, add `--keep-staging-files`.
4. If the user wants to preserve the current staging index text, add `--skip-index-reset`.
5. If the script reports non-mergeable conflicts, stop and review those paths manually instead of overwriting canonical content.

## Behavior

- New staged files are copied into the canonical vault.
- `00_Inbox/SESSION-*.md` and `20_Work_Logs/LOG-*.md` collisions are merged additively and idempotently.
- `INDEX.md` rows are appended only when missing from the canonical index.
- By default, promoted staged files are deleted after sync, staging directories are preserved, and `_vault_staging/INDEX.md` is rewritten to the explicit empty-state template.
- Keep staged files when the user requests a fallback copy; successful promotion does not require deleting the fallback.

## Validation

- Read the JSON summary from the script output.
- If this was an interactive Codex task, update the canonical session/work-log/index notes after the filesystem sync completes.
