# Official settlement and fee semantics audit

Checked against current official documentation on 2026-08-16 before final v0.7
acceptance.

## Primary sources

- Polymarket Chainlink TWAP documentation:
  https://docs.polymarket.com/market-data/chainlink-twap
- Polymarket fee documentation:
  https://docs.polymarket.com/trading/fees
- Gamma event for the settled 5-minute market opening at epoch 1786759800:
  https://gamma-api.polymarket.com/events?slug=btc-updown-5m-1786759800
- Gamma event for the same-expiry 15-minute market opening at epoch 1786759200:
  https://gamma-api.polymarket.com/events?slug=btc-updown-15m-1786759200

## Verified settlement contract

The two Gamma descriptions name the same resolution source,
`https://data.chain.link/streams/btc-usd-twap-60s-streams`, and both end at
`2026-08-15T02:15:00Z`. The 5-minute title covers 10:10PM-10:15PM ET and the
15-minute title covers 10:00PM-10:15PM ET. Each resolves Up when the Chainlink
TWAP at the end of its titled interval is at least its opening Chainlink TWAP.

Therefore a same-expiry pair has two opening strikes but one shared terminal
60-second TWAP. A model using separate 30-second and 60-second terminal values
is structurally wrong for this regime.

Let `T` be the shared terminal TWAP, `K5` the 5-minute opening TWAP, and `K15`
the 15-minute opening TWAP:

- If `K5 < K15`, `5m Down / 15m Up` is impossible. The only split state is
  `5m Up / 15m Down` for `K5 <= T < K15`.
- If `K15 < K5`, `5m Up / 15m Down` is impossible. The only split state is
  `5m Down / 15m Up` for `K15 <= T < K5`.
- If the strikes are equal, both split states are impossible.

The implementation enforces these states by construction and regression tests,
not merely through correlation or calibration.

## TWAP replication limitation

The official TWAP documentation defines the 30- and 60-second labels as
lookback windows. It does not publish enough custom-feed sampling, weighting,
rounding, or missing-input detail to prove that a local spot-price rolling mean
exactly reconstructs settlement. The local value must remain labeled as a proxy
with causal basis/residual evidence, model uncertainty, and fail-closed gaps.

The official subscription behavior is forward-looking: a client receives the
next update and has no snapshot, history, or replay after a disconnect. Missing
updates cannot be silently reconstructed from the current observation.

## Fee contract

The official current crypto fee formula is:

```text
fee = shares * 0.07 * p * (1 - p)
```

It applies to taker matches; makers pay no fee. Fees are rounded to five decimal
places, with `0.00001` as the minimum charged amount and smaller computed fees
rounded to zero. The two sampled Gamma markets report `feesEnabled=true`.

Profitability evaluation must therefore use captured market fee metadata and
compute the dynamic taker fee for every walked price level/match. Aggregating
all shares at a midpoint, using one unrounded aggregate fee, or assuming zero
fees can overstate executable PnL. The v0.7 structural lane applies a
conservative per-level calculation rather than relying on midpoint fills.

## Consequences for acceptance

1. v0.6 remains frozen as incident/evidence history; the shared-terminal model
   is a separate paper-only evidence track.
2. Structural payoff floors are evaluated before predictive Monte Carlo. When
   strike ordering makes one split impossible, the complementary cross-market
   pair has a deterministic one-USDC payoff floor per paired share, but it is an
   executable arbitrage only where walked asks, rounded fees, latency, and
   execution risk remain below that floor.
3. Any residual predictive edge must beat decision-time executable market
   probabilities after all costs and remain strictly OOS. The current 24
   independent expiry clusters cannot establish that claim.
