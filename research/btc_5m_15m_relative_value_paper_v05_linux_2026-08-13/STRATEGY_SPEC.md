# BTC 5m/15m TWAP relative-value paper strategy v0.5

This repository-local strategy specification is referenced by the v0.5
preregistration. It preserves the v0.4 trading parameters and upgrades only the
prospective evidence, calibration, and runtime-enforcement rules for the new
Linux paper track.

## Scope and safety

- Asset: BTC.
- Pair only markets with the same expiry, one 5-minute and one 15-minute
  market.
- Settlement regime: Chainlink 30-second TWAP for 5-minute markets and
  Chainlink 60-second TWAP for 15-minute markets.
- Public-data paper replay only. Never load credentials, configure proxies,
  sign transactions, call authenticated trading endpoints, or submit orders.
- v0.5 observations and PnL begin only after
  `2026-08-13T06:00:00Z`. Pre-v0.5 tracks, including v0.4 and earlier, may be
  used only for operational diagnostics and never for training or PnL.

## Frozen decision and data gates

- Decision ticks: tau 240, 180, 120, and 60 seconds; admissible tau range
  45 through 240 seconds.
- Maximum spread on each leg: 0.03.
- Maximum Chainlink staleness: 5,000 ms.
- Maximum book staleness: 750 ms.
- Require complete four-token signal and delayed-execution book surfaces.
- Predictor history is resampled causally; missing observations are never
  imputed.
- A leading predictor-history prefix older than 5 seconds is accepted, but any
  internal predictor gap or non-prefix observation older than 5 seconds rejects
  the affected candidate signal.
- Persist reconstructed top-five books every 250 ms.

## Clock evidence

- Source: chronyd locked to Amazon Time Sync Service 169.254.169.123.
- Before each capture, run three read-only `/usr/bin/chronyc -n tracking`
  measurements and select minimum uncertainty, breaking ties by freshness.
- Uncertainty is `abs(last_offset) + abs(root_delay)/2 + root_dispersion`.
- Maximum uncertainty: 100 ms. Maximum measurement age: 1,200 seconds.
- Correct causal receipt timestamps by the measured system-time offset. Any
  missing, stale, malformed, unlocked, or over-threshold clock evidence fails
  the affected decision closed.

## Model, calibration, and execution replay

- Forward volatility: EWMA regime method.
- Distribution: standardized residual bootstrap with 20,000 Monte Carlo paths.
- Calibration: per-tau isotonic calibration trained only on strictly prior raw
  calibration observations. Current validation and locked-test observations
  never participate in their own calibration.
- Require at least 20 unique expiry clusters for each horizon-plus-tau
  calibration slice before qualified calibrated evidence is allowed.
- Raw-probability development shadow output is reported separately and is never
  qualified evidence.
- Execution: conservative taker replay with a 250 ms taker delay, 750 ms
  maximum leg delay, depth walking, dynamic fees, partial fills, legging, and
  unwind handling.
- Minimum net expected PnL per pair: 0.015 after applying a 1.25 uncertainty
  multiplier.
- Paper bankroll: 10,000 USDC. Pair risk: 25 USDC. Maximum same-expiry risk:
  100 USDC. Maximum total open risk: 250 USDC.
- The four frozen decision ticks cap one expiry at exactly four 25 USDC
  decisions (100 USDC). Captures are finalized after settlement grace before
  the next report cycle, so the 250 USDC total-open cap is also checked as an
  invariant and never relaxed by report-time outcomes.
- Primary exit: hold to settlement.

## Walk-forward validation and qualified evidence

- Chronological rolling daily walk-forward: 5 training days, 1 embargo
  validation day, and 1 locked current test day. Parameters are selected only
  on training data; validation may veto but never retune the locked test.
- Minimum 2,000 resolved markets, 500 simulated trades, 500 explainable fills,
  and 99.5% market coverage with zero unknown resolution mappings.
- Market baseline probability is `best_ask_up / (best_ask_up + best_ask_down)`.
  OOS Brier must beat that baseline and OOS ECE must be at most 0.05.
- Qualified economic attempt PnL is conservative and includes execution
  failures. Qualified net PnL remains null until every promotion gate passes.
- Bootstrap inference uses 5,000 expiry-cluster resamples with seed 712. The
  95% lower bound of mean net PnL must exceed zero.
- Maximum single-event PnL share: 20%. Direction exposure must remain below a
  single leg. Signal-strength/net-EV monotonicity is required.
- Dust outcomes are diagnostics only and never change canonical status.
- Missing economic evidence is null, never zero. Tests or small positive shadow
  samples do not support a profitability or live-trading claim.
