---
name: trading-strategy-validation
description: Formalize and execute crypto trading strategy research validation. Use when Codex needs to turn a strategy idea into a staged testing plan, run or review backtests, decide when tweaks are allowed, generate decision-ready artifacts including equity/balance/drawdown charts, or classify a candidate as rejected, validation-only, paper-ready, or ready for live-sized review.
---

# Trading Strategy Validation

Use this skill to keep strategy research from becoming ad hoc curve-fitting. Treat every strategy as moving through explicit gates, with artifacts and decisions recorded before promotion.

## Workflow

1. Define the hypothesis, target venue, universe, data source, edge mechanism, allowed parameters, and invalidation rules before running broad scans.
2. Run a smoke test on a tiny deterministic fixture or shortest practical real-data window.
3. Run only the pre-declared broad scan grid. Do not add new knobs after seeing the first results unless you mark it as a new hypothesis.
4. Freeze the best candidate that clears headline gates and record its exact config, dataset, commit, metrics, and artifact links.
5. Run the validation gauntlet in `references/pipeline.md`.
6. Generate standard visual artifacts with `scripts/generate_backtest_charts.py` for any run artifact that has `equity_curve` and `trades`.
7. Decide one status: `reject`, `diagnose`, `validation-only`, `paper-dry-run`, `paper-ready`, or `live-review`.
8. Record behavior-changing choices as decision notes and create Mission Control tickets for unresolved blockers.

## Required Artifacts

Every frozen candidate needs:

- Config/spec: exact parameters, sizing, universe, venue assumptions, and data cut.
- Metrics: net Sharpe, return, max drawdown, trade count, turnover, win rate, profit factor, fees/funding/slippage assumptions.
- Charts: equity, realized balance or cumulative closed PnL, drawdown, and optional monthly/annual return heatmap.
- Validation reports: costs, capacity/liquidity, walk-forward or calendar splits, path risk, data quality, portfolio fit, and venue/execution parity.
- Decision record: promote, hold, or reject with rationale and consequences.

## Tweak Rules

Only tweak at gate boundaries:

- Before broad scan: tune implementation bugs and pre-registered parameter ranges.
- After broad scan: freeze one candidate or register a new hypothesis.
- After validation failure: diagnose first, then create a new ticket for a classified change.
- After venue/out-of-sample failure: do not tune directly on the failed venue window unless the ticket is explicitly a diagnosis or venue-adaptation experiment.

Classify every tweak as `hypothesis_change`, `risk_control_change`, `execution_fix`, `data_repair`, or `venue_adaptation`.

## Graph Generation

Run:

```bash
python3 /home/cwilson/projects/agent_skills/skills/trading-strategy-validation/scripts/generate_backtest_charts.py \
  --run-artifact /path/to/backtest_artifact.json \
  --output-dir /path/to/report/charts
```

The script writes SVG files and a small JSON manifest. If no balance curve exists, it builds a realized-balance series from closed trades where possible.

## References

- Read `references/pipeline.md` for the gate checklist and promotion rules.
- Read `references/report-artifact-contract.md` before adding new strategy runners or report formats.
