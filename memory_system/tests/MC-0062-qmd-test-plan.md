# MC-0062 QMD Search Test Plan

Scope: `memory_system` only. This plan targets the upcoming QMD-backed `memory_search` adapter for MC-0058, MC-0059, and MC-0061 without touching vault data.

## High-risk areas

1. Subprocess invocation and classification
   - Non-zero exit status
   - `OSError` during spawn
   - `subprocess.TimeoutExpired`
2. Response decoding
   - Stdout is not valid JSON
   - Stdout is valid JSON but not an object
   - Missing required top-level keys or wrong types
3. Fallback behavior
   - QMD unavailable before spawn
   - QMD runtime failure with builtin fallback enabled
   - QMD runtime failure with fallback disabled
4. Contract parity
   - Successful QMD search matches builtin top-level shape
   - Result row shape matches builtin field names and types
   - Empty-results shape remains deterministic

## Recommended test seams

Implement the adapter so tests can patch one narrow boundary instead of patching the whole backend:

- `_build_qmd_command(...) -> list[str]`
- `_run_qmd_search(...) -> CompletedProcess[str]` or equivalent
- `_decode_qmd_payload(stdout: str) -> dict[str, Any]`
- `_normalize_qmd_results(payload: dict[str, Any]) -> dict[str, Any]`

If fallback is configurable, expose it explicitly on the backend or `memory_search(...)` so tests do not rely on environment variables.

## Deterministic test matrix

### Success and parity

1. `test_search_qmd_success_matches_builtin_top_level_shape`
   - Mock a successful QMD subprocess response.
   - Assert exact top-level keys match builtin success payload:
     - `results`
     - `provider`
     - `model`
     - `citations`
     - `mode`
   - Assert `provider == "qmd"` and `mode` is the intended QMD mode.

2. `test_search_qmd_success_result_rows_match_builtin_result_shape`
   - Mock one QMD result row.
   - Assert each row has:
     - `path: str`
     - `startLine: int`
     - `endLine: int`
     - `score: int | float`
     - `snippet: str`
     - `source: str`
   - Assert no extra transport-only fields leak into the API response.

3. `test_search_qmd_empty_results_is_not_reported_as_unavailable`
   - Mock a successful empty QMD payload.
   - Assert `results == []`.
   - Assert `disabled` and `unavailable` are absent.

### Failure classification

4. `test_search_qmd_subprocess_nonzero_exit_returns_deterministic_unavailable_payload`
   - Mock `subprocess.run` returning non-zero with stderr.
   - Assert:
     - `results == []`
     - `disabled is True`
     - `unavailable is True`
     - `provider == "qmd"`
     - `warning` and `action` stay deterministic
     - `error` includes a stable classification string, not raw traceback text

5. `test_search_qmd_spawn_oserror_returns_deterministic_unavailable_payload`
   - Mock `subprocess.run` raising `OSError`.
   - Assert deterministic unavailable payload and stable operator-facing error text.

6. `test_search_qmd_timeout_returns_deterministic_unavailable_payload`
   - Mock `subprocess.run` raising `subprocess.TimeoutExpired`.
   - Assert timeout classification is explicit in `error` or `details`.
   - Assert the configured timeout value is passed to the subprocess layer.

### Response decoding

7. `test_search_qmd_malformed_json_returns_deterministic_unavailable_payload`
   - Mock stdout containing invalid JSON.
   - Assert unavailable payload, empty results, and a stable malformed-response classification.

8. `test_search_qmd_non_object_json_returns_deterministic_unavailable_payload`
   - Mock stdout as `[]` or `"text"`.
   - Assert the adapter rejects it instead of passing it through.

9. `test_search_qmd_missing_required_keys_returns_deterministic_unavailable_payload`
   - Mock JSON object missing `results` or containing wrong field names.
   - Assert validation failure is deterministic.

10. `test_search_qmd_invalid_result_row_shape_returns_deterministic_unavailable_payload`
   - Mock a row with wrong types, for example `startLine: "1"`.
   - Assert the adapter rejects the payload instead of coercing silently unless coercion is a deliberate spec choice.

### Fallback behavior

11. `test_search_qmd_runtime_failure_falls_back_to_builtin_when_enabled`
   - Prepare a valid builtin index in a temp workspace.
   - Mock a QMD runtime failure.
   - Assert search returns builtin results and builtin top-level shape.
   - Assert there is a visible indication of fallback only if the API contract explicitly allows extra metadata.

12. `test_search_qmd_runtime_failure_without_fallback_stays_unavailable`
   - Same setup, fallback disabled.
   - Assert unavailable payload remains QMD-owned and does not silently switch providers.

13. `test_search_qmd_preflight_unavailable_can_use_builtin_fallback_when_enabled`
   - Mock missing `qmd` binary or preflight failure.
   - Assert behavior matches the selected fallback policy.

## Minimal verification commands

Run the current `memory_system` suite:

```bash
cd /home/cwilson/projects/agent_skills
python3 -m unittest memory_system.tests.test_memory_system
```

Run only QMD-related tests after MC-0062 lands:

```bash
cd /home/cwilson/projects/agent_skills
python3 -m unittest memory_system.tests.test_memory_system -k qmd
```

Run one specific future failure-path test while iterating:

```bash
cd /home/cwilson/projects/agent_skills
python3 -m unittest memory_system.tests.test_memory_system.MemorySystemTests.test_search_qmd_timeout_returns_deterministic_unavailable_payload
```

## Current gaps

- The current `QmdSearchBackend` does not spawn a subprocess, so failure-path tests above cannot execute yet.
- No contract exists yet for whether QMD failures should fall back automatically to builtin search.
- No decoder/validator seam exists for QMD JSON normalization, so malformed-payload tests should be added only after that seam is introduced.
