# Report Artifact Contract

## Minimum Run Artifact Fields

Strategy runners should write JSON with:

- `run_id` or stable report identifier.
- `strategy_id`.
- `dataset_id`.
- `venue`.
- `start_ts` and `end_ts`.
- `metrics` object.
- `equity_curve` list of `{ "ts_utc": "...", "equity_usd": number }`.
- `trades` list with `exit_ts_utc` and `net_pnl_usd` where closed-trade balance is required.

## Standard Chart Bundle

Each validation report should include:

- `equity.svg`: account equity over time.
- `drawdown.svg`: drawdown from running equity peak.
- `balance.svg`: realized balance from `balance_curve` if present, otherwise starting equity plus cumulative closed trade PnL.
- `chart_manifest.json`: source artifact, generated files, point counts, and warnings.

## Report Markdown

Each `summary.md` should link the chart files and include:

- Candidate status.
- Headline metrics.
- Data and venue assumptions.
- Validation pass/fail table.
- Decision and next tickets.

Do not paste secrets, API keys, auth headers, account identifiers, or unredacted screenshots into strategy artifacts or vault notes.
