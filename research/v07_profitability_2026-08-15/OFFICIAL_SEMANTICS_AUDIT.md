# Official settlement and fee semantics audit

Checked on 2026-08-15 before accepting any v0.7 design.

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

The two Gamma market descriptions both name the exact same resolution source,
`https://data.chain.link/streams/btc-usd-twap-60s-streams`, and both end at
`2026-08-15T02:15:00Z`. The 5-minute title covers 10:10PM-10:15PM ET; the
15-minute title covers 10:00PM-10:15PM ET. Each resolves Up when the Chainlink
TWAP at the end of its titled interval is at least the Chainlink TWAP at the
beginning of that interval.

Therefore a same-expiry pair has two opening strikes but one shared terminal
60-second TWAP. A model with independent or separately defined 30-second and
60-second terminal values is structurally wrong for this regime.

Let `T` be the shared terminal TWAP, `K5` the 5-minute opening TWAP, and `K15`
the 15-minute opening TWAP:

- If `K5 < K15`, `5m Down / 15m Up` is impossible. The only split state is
  `5m Up / 15m Down` for `K5 <= T < K15`.
- If `K15 < K5`, `5m Up / 15m Down` is impossible. The only split state is
  `5m Down / 15m Up` for `K15 <= T < K5`.
- If the strikes are equal, both split states are impossible.

This should be enforced by construction and by tests, not merely encouraged by
correlation or calibration.

## TWAP replication limitation

Polymarket's official TWAP documentation says the 30- and 60-second labels are
lookback windows, not publication cadences. It also says Chainlink does not
currently publish the custom feed's sampling boundaries, weighting, rounding,
or missing-input behavior. A local spot-price rolling mean must therefore not be
presented as an exact reconstruction of the settlement feed. It is a proxy that
needs causal basis/residual evidence, model uncertainty, and fail-closed gaps.

The RTDS stream has no snapshot, history, or replay after a disconnect. Missing
updates cannot be silently backfilled from a current observation.

## Fee contract

The official fee documentation states that crypto takers pay
`fee = shares * 0.07 * p * (1 - p)` at match time; makers pay no fee. The two
sample Gamma markets report `feesEnabled=true`. Any profitability result must
use the captured market fee metadata and the dynamic fee formula at the actual
executable prices. It must not use midpoint fills or a fee-free assumption.

## Consequences for acceptance

1. v0.6 remains frozen as incident/evidence history; a shared-terminal model is
   a new evidence track.
2. Structural no-arbitrage bounds should be checked before using a predictive
   Monte Carlo edge. When strike ordering makes one split impossible, buying
   the complementary cross-market pair has a deterministic payoff floor of one
   USDC per paired share. It is a true executable arbitrage only if walked ask
   depth plus fees and execution risk cost less than that floor.
3. Any residual predictive edge must beat executable market probabilities after
   all costs and must remain strictly OOS. The current 24 independent expiry
   clusters cannot establish that claim.

