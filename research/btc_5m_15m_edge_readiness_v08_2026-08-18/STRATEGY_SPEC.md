# BTC 5m/15m shared-terminal structural readiness v0.8

Status: strategy live **NO-GO**; execution probe **NO-GO**. This specification
defines evidence and controls only. It does not enable credentials or orders.

## Settlement invariant

For one 5m market and one 15m market with the same close, `T` is the single
official BTC/USD 60-second TWAP at that common close. `K5` and `K15` are the
separate official opening 60-second TWAP strikes. Both legs compare the same
`T` to their own strike.

| Strike order | Feasible terminal states | Structural pair | Payoff by feasible state |
| --- | --- | --- | --- |
| `K5 < K15` | both Up; 5m Up/15m Down; both Down | 5m Up + 15m Down | 1, 2, 1 |
| `K5 > K15` | both Up; 5m Down/15m Up; both Down | 5m Down + 15m Up | 1, 2, 1 |
| `K5 = K15` | both Up; both Down | compare both cross-pairs | 1, 1 |

The co-terminal validator must reject any pair without matching close,
official 60-second topics, exact strikes, current rule hashes, four fresh token
books, matching captured tick/minimum-size rules, and captured dynamic fees.
Prediction-only `[0.05, 0.95]` price and `0.03` spread filters never apply to
the structural route: the only interesting regime naturally has one leg near
zero and the other near one.

If total all-in cost per pair is `c`, the structural net floor is `1-c`.
`c <= 1` proves only a non-negative floor. Positive edge needs `c < 1`. The
Gate 0 reports three costs: two takers, one maker plus one FOK taker, and two
makers. The maker+FOK shape cannot be live-eligible because its cost is
`1 + b + hedge_fee` for non-negative implicit split probability `b`. The
`c <= 0.99` probe buffer therefore belongs only to the two-maker shape.

## Gate 0

Before promotion work, the hindsight diagnostic scans every integer second
from `ttc=300` through `ttc=0` for each common expiry, enumerates both
cross-pair directions and every jointly modeled depth breakpoint, and records
the best `ttc`. It can select no-trade at zero. It uses captured
decision-time books, fee/rule metadata, and the realized common terminal label.
It does not truncate the upper bound at the older v0.7 25 USDC sizing budget.
The non-floor direction remains visible as an unrestricted hindsight diagnostic,
but only the strike-consistent structural direction contributes to the
structural Gate 0. It persists all three execution shapes plus the full `b(t)`
curve and count of `b < 0` observations. The decision route is two makers;
aggregate PnL `<= 0` or average PnL below `0.5 USDC/expiry` stops the route.
A positive result is only an optimistic upper bound and never enters locked-OOS
counts; actual fills must be established by the neutral queue replay.

The local repository does not contain the deployed 41 capture trees. Runtime
generation must use the exact-count command in `PREREGISTRATION.json`; any
observed count other than 41 aborts instead of silently changing the claim.

## Capture and cohort policy

Official RTDS capture runs continuously before K15 opens, through K5 open, and
through the common close. Only the decision and passive-order window is near K5
open. A disconnect, missing update, clock anomaly, malformed source record, or
missing L2/trade/fee/rule receipt makes that cohort dirty. There is no
interpolation from the current value and no historical replay assumption.

The rolling v0.8 journal locks a deterministic inclusion rule before the track
starts. Each future common expiry is admitted before its first decision. The
first admitted capture attempt is permanent; later cleaner attempts cannot
replace it. Dirty, no-trade, causal no-fill, partial, and failed outcomes remain
in the denominator. There is no 102-case ceiling and tau aliases do not count as
independent samples.

Capture capacity is itself a promotion gate. Failure rate must be at most 5%,
free disk at least 10 GiB, projected retained data at most 1 GiB/day, available
memory at least 2 GiB, and burstable CPU credits must not be exhausted. The
v0.8 compact policy retains only the paired four tokens, 30-second full-depth
anchors, 5-second public taker polls, and compact top-of-book changes; raw CLOB
frames and 250 ms reconstructed full-depth frames are disabled.

## Double-maker shadow execution

Both structural legs rest post-only at captured bids. `QueueScenario` supplies
optimistic, neutral, and pessimistic public-trade queue boundaries. The neutral
case is the promotion route. Every maker fill is recorded through the existing
ledger, with dynamic maker/taker fees and terminal payouts.

If only one leg fills, the preregistered paper replay accounts three policies
separately: keep waiting passively, cancel and buy the missing leg as an FOK
taker (including the captured 250 ms delay and fee), or cancel and flatten the
filled leg with FAK. These are scenario diagnostics, not interchangeable rows.
No maker+FOK result can authorize live trading.

The existing live harness still models one maker plus FOK and has no production
adapter. It is therefore intentionally ineligible for this revised double-maker
route and cannot submit a real order as shipped.

## Promotion boundaries

At least 200 unique common expiries are required in neutral double-maker shadow.
Total PnL, PnL without the best expiry, PnL without the best direction, and every
registered rolling-window minimum must remain positive; single-expiry
concentration must be at most 20%. Four fresh complete cohorts are still needed
for a later metered probe, but cannot substitute for the 200-expiry shadow gate.
Health, Gate 0, authenticated read, user fill stream, all failure drills,
immutable probe preregistration, and a purpose-built double-maker probe are also
mandatory.

Strategy live requires at least 200 clean prelabel common-expiry cohorts, at
least 200 explainable structural economic attempts, complete actual execution
costs/reconciliation, no more than 20% single-expiry PnL concentration, and a
one-sided 95% common-expiry cluster-bootstrap mean lower bound above zero. The
Oracle basis/split-probability model stays action-disabled in a separate research
namespace and can never act as an automatic fallback or contribute counts to
the structural gate.
