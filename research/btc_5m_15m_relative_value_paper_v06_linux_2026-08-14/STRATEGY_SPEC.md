# BTC 5m/15m TWAP relative-value paper strategy v0.6

This is a prospective-only transfer test of the v0.5 strategy in the
`chainlink_twap_60s_5m_and_60s_15m.v1` settlement regime. Trading, model,
calibration, execution, and promotion parameters remain identical to v0.5.
Only the frozen settlement source and evidence-track identity change.

## Scope and safety

- Asset: BTC; same-expiry 5-minute/15-minute pairs only.
- Settlement: Chainlink 60-second BTC/USD TWAP for both horizons, matched by
  normalized identity through the frozen registry.
- Public-data paper replay only. Never load credentials, configure proxies,
  sign transactions, call authenticated endpoints, or submit orders.
- v0.5 and earlier observations are incident context only. They never enter
  v0.6 training, calibration, OOS metrics, or qualified PnL.
- Unknown regimes remain capture-eligible in rawcap but are quarantined from
  every v0.6 strategy and evidence output.

## Frozen decision and data gates

- Decision ticks: tau 240, 180, 120, and 60 seconds; admissible range 45–240.
- Maximum spread on each leg: 0.03.
- Maximum Chainlink staleness: 5,000 ms; maximum book staleness: 750 ms.
- Require complete four-token signal and delayed-execution surfaces.
- At decision time, walk both selected ask books to the full target quantity.
- Predictor history is causal. A stale leading prefix is allowed; internal
  gaps or stale non-prefix observations reject the candidate.
- Persist reconstructed top-five books every 250 ms.

## Clock evidence

- chronyd is locked to Amazon Time Sync Service 169.254.169.123.
- Before each capture, take three read-only `/usr/bin/chronyc -n tracking`
  measurements and select minimum uncertainty, breaking ties by freshness.
- Uncertainty is `abs(last_offset) + abs(root_delay)/2 + root_dispersion`.
- Maximum uncertainty: 100 ms; maximum measurement age: 1,200 seconds.
- Correct receipt timestamps by the measured offset. Missing, stale, malformed,
  unlocked, or over-threshold clock evidence fails closed.

## Model, calibration, and execution replay

- Forward volatility: EWMA regime method.
- Distribution: standardized residual bootstrap, 20,000 Monte Carlo paths.
- Per-tau isotonic calibration uses only strictly prior raw v0.6 observations.
  Require 20 unique expiry clusters per horizon-plus-tau slice before
  calibrated qualified evidence is allowed.
- Raw-probability shadow output is never qualified evidence.
- Conservative taker replay: 250 ms taker delay, 750 ms maximum leg delay,
  depth walking, dynamic fees, partial fills, legging, and immediate unwind of
  excess first-leg inventory after hedge failure.
- Minimum adjusted net expected PnL per pair: 0.015; uncertainty multiplier:
  1.25.
- Paper bankroll: 10,000 USDC. Pair risk: 25 USDC. Same-expiry cap: 100 USDC.
  Total-open cap: 250 USDC. Primary exit: hold to settlement.

## Walk-forward and promotion gates

- Five training days, one embargo validation day, one locked test day.
  Parameters are selected on train only; validation may veto but never retune.
- At least 2,000 resolved markets, 500 simulated trades, 500 explainable fills,
  99.5% coverage, and zero unknown resolution mappings.
- Market baseline: `best_ask_up / (best_ask_up + best_ask_down)`. OOS Brier
  must beat it; OOS ECE must be at most 0.05.
- Qualified economic-attempt PnL includes execution failures and remains null
  until all gates pass.
- 5,000 expiry-cluster bootstrap resamples, seed 712; 95% mean-PnL lower bound
  must exceed zero. Maximum single-event PnL share: 20%.
- Direction exposure must remain below a single leg; signal-strength/net-EV
  monotonicity is required. Dust is diagnostic only; missing evidence is null.

## Parallel non-canonical hypotheses

The service records, but never acts on, A1/A4 veto inputs, market-probability
shrinkage grids, and 1.25×/1.5×/2× signal-depth buffers. B1 full dual-depth
walking and B2 timeout/unwind are documented as existing canonical controls,
not falsely presented as new candidates. Any candidate requires a separate
future preregistration before it may alter an action.
