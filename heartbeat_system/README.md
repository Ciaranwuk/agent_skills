# Heartbeat System (Phase 0/1 Lane A)

## Scope
This package currently provides scaffold and boundaries for the heartbeat runtime:
- provider-agnostic contracts (`HeartbeatRequest`, `HeartbeatResponse`, `HeartbeatResponder`)
- minimal config dataclass (`HeartbeatConfig`)
- CLI entrypoint and command parser
- API placeholders with friendly fallback/stub responses for not-yet-implemented runner functions
- deterministic `NullResponder` adapter for tests and offline use

Core modules in this phase avoid model vendor SDK imports.

## CLI
Run from repository root:

```bash
python -m heartbeat_system --help
```

Commands:
- `run-once`
- `status`
- `last-event`
- `wake`
- `enable`
- `disable`

For Phase 0/1, non-`run-once` commands return deterministic stub payloads when runtime wiring is not present.
For `run-once`, API wiring defaults to `NullResponder` when no responder is provided, so local/offline runs remain deterministic and avoid adapter-provider coupling.

## Examples
```bash
python3 -m heartbeat_system run-once --disabled
# {"contract":"heartbeat.operator","contract_version":"1.0","contract_metadata":{"name":"heartbeat.operator","version":"1.0"},"ok":true,"status":"skipped","reason":"disabled","run_reason":"manual","output_text":"","error":"","error_code":null,"error_reason":null,"event":{"event_id":"evt_...","ts_ms":...,"status":"skipped","reason":"disabled","run_reason":"manual","output_text":"","error":"","dedupe_suppressed":false},"counters":{"ran":0,"skipped":1,"failed":0,"deduped":0}}

python3 -m heartbeat_system status
# {"contract":"heartbeat.operator","contract_version":"1.0","contract_metadata":{"name":"heartbeat.operator","version":"1.0"},"ok":true,"status":"idle","enabled":true,"running":false,"in_flight":false,"next_due_ms":null,"pending_wake_reason":null,"last_run_reason":null,"scheduler_diagnostics":null,"counters":{"ran":0,"skipped":0,"failed":0,"deduped":0},"last_event_present":false,"ingest_diagnostics":{"history_limit":20,"recent":[],"counters":{"total":0,"manual":0,"scheduler":0,"delivered":0,"suppressed":0}},"store_load_warning":null,"error_code":null,"error_reason":null}

python3 -m heartbeat_system last-event
# {"contract":"heartbeat.operator","contract_version":"1.0","contract_metadata":{"name":"heartbeat.operator","version":"1.0"},"ok":true,"status":"empty","event":null,"error_code":null,"error_reason":null}

python3 -m heartbeat_system wake --reason manual
# {"contract":"heartbeat.operator","contract_version":"1.0","contract_metadata":{"name":"heartbeat.operator","version":"1.0"},"ok":false,"status":"not-running","accepted":false,"reason":"manual","wake_reason":"manual","queue_size":0,"replaced_reason":null,"error":"","error_code":"runtime-not-started","error_reason":"runtime-not-started"}

python3 -m heartbeat_system enable
# {"contract":"heartbeat.operator","contract_version":"1.0","contract_metadata":{"name":"heartbeat.operator","version":"1.0"},"ok":true,"status":"ok","enabled":true,"previous_enabled":true,"applied_to_scheduler":false,"error_code":null,"error_reason":null}

python3 -m heartbeat_system disable
# {"contract":"heartbeat.operator","contract_version":"1.0","contract_metadata":{"name":"heartbeat.operator","version":"1.0"},"ok":true,"status":"ok","enabled":false,"previous_enabled":true,"applied_to_scheduler":false,"error_code":null,"error_reason":null}
```

If `heartbeat_system.runner` does not yet expose run-once implementation, the CLI prints a structured, friendly error payload instead of crashing.
