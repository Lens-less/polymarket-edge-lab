# ChatGPT Pro independent task: adversarial edge and evidence audit

## Role and independence

Act as an independent quantitative-engineering reviewer. Do not assume the
implementation engineer's proposed v0.7 model is correct. Use only the attached
sanitized bundle. Your job is to determine whether any claimed edge is real,
whether the 100-trade requirement can be satisfied without statistical sleight
of hand, and what minimum evidence would falsify or support the thesis.

Baseline commit: `c557160d0c98e195a988f4353bbe19a3b00b3576`.
Current evidence: 48 settled development-shadow attempts over 24 independent
expiry clusters, `+87.523635` USDC, cluster t-stat about `1.46`, largest cluster
48.5% of profit, and qualified PnL `null`. Both horizons now settle from the same
Chainlink BTC/USD 60-second TWAP at the same expiry, while the legacy model still
uses separate 30s/60s terminal proxies.

## Questions to answer

1. Derive the actual payoff state space for a same-expiry 5m/15m pair sharing one
   terminal 60s TWAP and two opening strikes. Identify structurally impossible
   outcomes for each strike ordering and the implications for relative-value
   pricing, hedging, and apparent model edge.
2. Audit the existing strategy, replay, calibration, scoreboards, OOS metrics,
   and promotion logic for leakage, duplicated tau observations, survivorship,
   cost omissions, market-probability construction errors, calibration misuse,
   and PnL concentration.
3. Test the hypothesis that any apparent edge is merely strike-order/path
   structure already reflected in executable market prices. State what residual
   source of edge could remain after fees, latency, book depth, legging, and
   market-implied probabilities.
4. Examine the repository's other summarized research tracks for an already
   supportable, public-data, execution-aware edge. Do not promote results that
   the repository itself classifies as L0/L1, insufficient, rejected, or lacking
   authenticated fills/reward payouts.
5. Define a preregistered, statistically defensible evaluation that requires at
   least 100 distinct settled expiry clusters and 100 explainable locked-OOS
   economic attempts with positive net PnL. Positive total PnL alone is not a
   true-edge finding: require cluster bootstrap 95% lower bound above zero,
   concentration at most 20%, complete fees/depth/delay/legging evidence, both
   horizon Brier scores better than the executable-market baseline, ECE at most
   0.05, and neighboring-parameter robustness. Explain power and likely sample
   needs without p-hacking the current sample.
6. Produce concrete falsification tests and a go/no-go decision. If current data
   cannot establish an edge, say so plainly and distinguish the engineering
   path to collect evidence from a profitable-strategy claim.

## Optional bounded code work

You may provide a small patch only if it materially improves independent
cluster accounting, power/uncertainty reporting, structural-arbitrage checks, or
evidence-gate enforcement. Do not implement a competing trading model in this
conversation. Any code must preserve frozen v0.5/v0.6 behavior and all safety
guards, include tests, and use no new dependencies.

## Required deliverable

Return a downloadable ZIP named similarly to `poly-mm-edge-audit-delivery.zip`
containing:

- `EDGE_AUDIT.md`: findings ordered by severity, exact file/symbol references,
  derivations, falsification plan, sample-size/power discussion, ranked edge
  hypotheses, and a final `validated / promising / insufficient / rejected`
  classification;
- `changes.patch` plus repository-relative files if you made a bounded patch;
- `TEST_RESULTS.md` for any commands run;
- `SHA256SUMS.txt` for every delivered file.

State explicitly in chat whether the present bundle proves a true edge and
whether it contains 100 independent positive-PnL locked-OOS trades.

## Prohibitions

Do not request or use credentials; do not access authenticated endpoints, AWS,
the paper host, wallets, or user data; do not place orders or deploy; do not
commit, push, or open a PR; do not change frozen evidence; do not call synthetic
fixtures, raw shadow rows, duplicated tau rows, or historical tuning a valid
100-trade result; and do not promise future profit.

