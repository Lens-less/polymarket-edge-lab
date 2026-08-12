# BTC 5m/15m TWAP relative-value paper strategy v0.4

This is the repository-local strategy specification referenced by the v0.4
preregistration. It restates the previously frozen trading semantics and changes
only the prospective Linux clock-evidence source.

## Scope and safety

- Asset: BTC.
- Pair only markets with the same expiry, one 5-minute and one 15-minute market.
- Settlement regime: Chainlink 30-second TWAP for 5-minute markets and
  Chainlink 60-second TWAP for 15-minute markets.
- Public-data paper replay only. Never load credentials, sign transactions, call
  authenticated trading endpoints, or submit orders.
- v0.4 observations and PnL begin after the preregistration timestamp. Earlier
  tracks may be used only for operational diagnostics.

## Frozen decision and data gates

- Decision ticks: tau 240, 180, 120, and 60 seconds; admissible tau range
  45 through 240 seconds.
- Maximum spread on each leg: 0.03.
- Maximum Chainlink staleness: 5,000 ms.
- Maximum book staleness: 750 ms.
- Require complete four-token signal and delayed-execution book surfaces.
- Predictor history is resampled causally; missing observations are never
  imputed.
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
- Calibration: train-only isotonic calibration. Development calibration requires
  at least 20 past points per horizon. Raw-probability development shadow output
  is reported separately and is never qualified evidence.
- Execution: conservative taker replay with a 250 ms taker delay, 750 ms maximum
  leg delay, depth walking, fees, partial fills, legging, and unwind handling.
- Minimum net expected PnL per pair: 0.015 after applying a 1.25 uncertainty
  multiplier.
- Paper bankroll: 10,000 USDC. Pair risk: 25 USDC. Maximum same-expiry risk:
  100 USDC. Maximum total open risk: 250 USDC.
- Primary exit: hold to settlement.

## Walk-forward validation and promotion

- Chronological, never shuffled: 5 training days, 1 validation day, 1 locked
  test day. Parameters are selected only on training and validation data.
- Minimum 2,000 resolved markets, 500 simulated trades, 500 explainable fills,
  and 99.5% market coverage with zero unknown resolution mappings.
- OOS Brier must beat the market probability, OOS ECE must be at most 0.05,
  OOS net PnL must be positive, and the 95% bootstrap lower bound of mean net
  PnL must exceed zero.
- Maximum single-event PnL share: 20%. Direction exposure must remain below a
  single leg. Signal-strength/net-EV monotonicity is required.
- Missing economic evidence is null, never zero. Tests or small positive shadow
  samples do not support a profitability or live-trading claim.
