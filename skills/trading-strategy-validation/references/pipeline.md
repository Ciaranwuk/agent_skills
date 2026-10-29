# Strategy Validation Pipeline

## Gate 0: Pre-Registration

Record before scanning:

- Strategy hypothesis and expected edge.
- Target venue and fallback proxy venue.
- Universe construction, inclusions, exclusions, market cap/liquidity filters, and survivorship assumptions.
- Data source, time range, bar frequency, fees, funding, borrow, and slippage assumptions.
- Parameter grid and allowed implementation fixes.
- Invalidation rules, including minimum trades and maximum tolerated drawdown/path-risk flags.

Promotion: proceed only when the hypothesis and scan space are specific enough that later changes are auditable.

## Gate 1: Smoke

Run a tiny deterministic fixture or narrow real-data slice.

Required checks:

- No lookahead.
- Orders, fills, costs, stops, stale bars, and symbol mappings behave as expected.
- Artifact schema is parseable.
- Metrics are non-null and deterministic.

Promotion: proceed only when implementation behavior is explainable.

## Gate 2: Broad Scan

Run the pre-registered grid only.

Required checks:

- Same dataset and cost assumptions across variants.
- Table sorted by primary metric plus return, max drawdown, trades, turnover, and capacity proxy.
- Record the full result matrix, not only the best row.

Promotion: freeze one or more candidates. Any new parameter, signal, or filter after this point starts a new hypothesis or clearly labeled diagnosis ticket.

## Gate 3: Frozen Candidate Validation

Run:

- Calendar or walk-forward splits.
- Cost/slippage/funding stress.
- Liquidity and participation stress.
- Path risk: MAE, MFE, gap/wick/stale-bar review.
- Data quality: missing bars, bad candles, symbol lifecycle, external OHLC spot checks.
- Portfolio fit: correlation, downside/crisis correlation, overlap with current strategy book.
- Robustness comparison against nearby variants and simple baselines.

Promotion: proceed only if the candidate survives directionally across validation, not merely on the headline in-sample result.

## Gate 4: Venue And Execution Parity

Run:

- Venue symbol support, long/short availability, product type, margin mode, min notional, tick/step size, max order, max position, leverage cap.
- Fees, funding/borrow, spread/slippage, and rejected-order behavior.
- Research-to-execution replay from research artifact into order/intention events.
- Stale-symbol force-exit or skip behavior.

Promotion: proceed only when unavailable symbols, rejected orders, and execution differences are explicit in artifacts.

## Gate 5: Paper Dry Run

Generate daily expected-order artifacts without live risk:

- Target weights and current weights.
- Intended orders.
- Skipped/rejected symbols and reasons.
- Estimated fees, funding, spread, and participation.
- Post-order exposure checks.
- Daily live-vs-research drift report.

Promotion: paper-ready only after a clean dry-run window and explicit go/no-go thresholds.

## Gate 6: Decision

Use these statuses:

- `reject`: failed core hypothesis or cannot be made operationally viable.
- `diagnose`: failure is informative but needs a scoped investigation.
- `validation-only`: useful research signal but not paper-ready.
- `paper-dry-run`: ready for observation-only order generation.
- `paper-ready`: ready for tiny paper deployment after operator approval.
- `live-review`: only after paper performance, drift, costs, and operational constraints are acceptable.
