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
books, and captured dynamic fees. For quantity `q`, it walks every ask level and
applies the captured fee schedule with actual per-level rounding.

If total all-in cost per pair is `c`, the structural net floor is `1-c`.
`c <= 1` proves only a non-negative floor. Positive edge needs `c < 1`. The
execution probe additionally requires `c <= 0.99` after the actual passive fill
and a new current full-depth FOK hedge quote.

## Gate 0

Before promotion work, the hindsight diagnostic enumerates both cross-pair
directions and every jointly executable depth breakpoint at one fixed decision
time per common expiry. It can select no-trade at zero. It uses captured
decision-time books, fee/rule metadata, and the realized common terminal label.
It does not truncate the upper bound at the older v0.7 25 USDC sizing budget.
The non-floor direction remains visible as an unrestricted hindsight diagnostic,
but only the strike-consistent structural direction contributes to the
structural Gate 0. Aggregate structural executable PnL `<= 0` stops that route.
A positive result is not edge evidence and never enters locked-OOS counts.

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

## Maker-assisted execution

Maker assistance is not alpha. The plan compares making the 5m leg and making
the 15m leg, binding the chosen pair and both orientations to the validator
hash. After a passive fill it cancels all remaining orders, reconciles the
authenticated user-fill ledger, and rejects overfills, unknown orders,
duplicate conflicts, and an open-order remainder.

Only then may it obtain a current full-depth FOK quote for the other leg. Actual
maker notional and fee plus current hedge notional and fee must still be at most
`0.99` per matched pair, and submitted buy-order notional must remain at most
10 USDC. Otherwise it performs the single preregistered sell-side emergency
unwind. Ambiguous FOK state engages the persistent kill switch; it must not guess
whether the hedge filled.

The harness has an injected venue interface but no production adapter. It is
therefore testable without credentials and cannot submit a real order as shipped.

## Promotion boundaries

Positive neutral-shadow realized PnL plus four complete common-expiry cohorts is
only one part of execution-probe eligibility. Health, Gate 0, authenticated
read, user fill stream, all failure drills, immutable probe preregistration, and
current hedge depth are also mandatory.

Strategy live requires at least 100 clean prelabel common-expiry cohorts, at
least 100 explainable structural economic attempts, complete actual execution
costs/reconciliation, no more than 20% single-expiry PnL concentration, and a
one-sided 95% common-expiry cluster-bootstrap mean lower bound above zero. The
Oracle basis/split-probability model stays action-disabled in a separate research
namespace and can never act as an automatic fallback or contribute counts to
the structural gate.
