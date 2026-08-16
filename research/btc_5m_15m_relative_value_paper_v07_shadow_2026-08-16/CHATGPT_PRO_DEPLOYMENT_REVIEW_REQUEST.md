# ChatGPT Pro deployment review request

## Background and objective

The repository contains a paper-only BTC 5-minute/15-minute Polymarket
relative-value strategy. V0.6 collects public market data continuously. V0.7
fixes the shared-terminal settlement model and adds stricter structural and
predictive evidence gates, but its authoritative entry point is an offline
counterfactual builder rather than a live service.

The immediate objective is to deploy a lightweight V0.7 shadow evaluator on an
existing Amazon Linux 2023 host. It must inspect finalized public V0.6 captures
every 30 minutes, retain a future-only evidence cohort, and report actual-market
counterfactual performance without placing orders or claiming proven edge.

## Current architecture and boundaries

- V0.6 remains the sole continuous public-data capture service and its evidence
  tree under `/var/lib/poly-mm-v06` is immutable and read-only to V0.7.
- V0.7 uses a separate `/opt/poly-mm-v07` code tree and
  `/var/lib/poly-mm-v07` data tree.
- Only captures started at or after `2026-08-16T15:00:00Z` may enter the new
  cohort.
- XFS reflink copies are mandatory. Hard links and ordinary-copy fallback are
  forbidden.
- The first eligible expiry is train, the second validation, and subsequent
  expiries are test, with at most 102 total cases.
- No production pre-label lock journal exists for this track. Therefore
  `true_edge=false`, the 100-trade user gate is false, and qualified PnL is null
  regardless of any malformed upstream claim.
- V0.7 is paper-only, public-only, offline during evaluation, and has no access
  to credentials or authenticated endpoints.
- Existing V0.5, V0.6, raw capture, and watcher services must not be stopped,
  rewritten, or restarted by the V0.7 evaluator.

## Review scope

Review the following implementation as an external senior engineer:

- `src/edge_lab/btc_twap_relative_value_v07_shadow.py`
- `scripts/run_btc_twap_relative_value_v07_shadow.py`
- `deploy/aws/paper_v07/`
- `research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/`
- `tests/test_btc_twap_relative_value_v07_shadow.py`
- `tests/test_btc_twap_relative_value_v07_deployment_assets.py`

Trace the adapter into the existing V0.7 builder and the V0.6 capture schemas.
Pay particular attention to:

1. whether a post-cutoff V0.6 capture is truly compatible with the V0.7
   manifest and builder;
2. filesystem race, symlink, hard-link, mutation, and path-escape risks;
3. evidence leakage from pre-cutoff data or repeated expiries;
4. fail-closed handling of unresolved, failed, incomplete, stale, or malformed
   captures;
5. status fields that could overstate profitability or true edge;
6. systemd permissions, network isolation, resource limits, timer overlap, and
   bootstrap idempotency;
7. whether the health check can run under the declared hardening;
8. correctness of the 30-minute performance cadence and 5-minute health cadence.

## Required deliverables

Provide:

1. a concise architecture verdict;
2. actionable findings sorted by severity, with exact file and line references;
3. a minimal complete patch for every merge-blocking issue;
4. tests for every proposed behavioral fix;
5. a deployment checklist and rollback checklist;
6. a plain statement of what the deployed track can and cannot prove.

If no merge-blocking defect exists, say so explicitly and list the remaining
operational risks. Avoid broad rewrites or new dependencies.

## Required verification

At minimum, reason about or run when possible:

- focused shadow runtime and deployment asset tests;
- Ruff `E,F,I` for changed Python files;
- Python compile checks;
- `git diff --check`;
- `systemd-analyze verify` semantics for all units;
- a Linux smoke run that remains offline and submits zero orders.

## Forbidden actions and claims

- Do not place orders, enable live execution, use credentials, access private
  endpoints, deploy to the host, change AWS resources, push Git, or modify V0.6
  evidence.
- Do not call shadow/counterfactual PnL real trading PnL.
- Do not claim true edge, qualified PnL, or satisfaction of the 100-trade gate
  without builder-authoritative prospective evidence.
- Do not treat mock tests as production verification.
- Do not weaken the frozen cutoff, safety flags, preregistration hash, evidence
  splits, or resource isolation.

## Acceptance criteria

The implementation is acceptable only if it:

- cannot submit an order or access a network from the evaluator units;
- cannot modify V0.6 or admit a pre-cutoff capture;
- selects at most one successful finalized capture per common expiry;
- fails closed on safety, integrity, settlement, schema, freshness, disk, and
  reflink violations;
- writes truthful status that always distinguishes diagnostic performance from
  qualified edge;
- runs at the requested cadence without overlapping itself;
- is reproducibly deployable from an immutable Git commit and cleanly
  removable without affecting V0.6.
