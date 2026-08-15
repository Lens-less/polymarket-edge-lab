# v0.7 independent acceptance record

Recorded: 2026-08-16 (Asia/Shanghai)

Status: **engineering acceptance passed; profitability acceptance failed /
unproven**.

ChatGPT Pro's artifacts were advisory. Codex verified the archive and hashes,
applied the patch in an isolated worktree, challenged the evidence and security
boundaries through multiple correction rounds, reviewed the final source, and
ran independent Windows and Linux gates. The result is a truthful paper-only
v0.7 evaluation track. It is not evidence that the strategy can currently make
money.

## Immutable identities

- Source baseline:
  `c557160d0c98e195a988f4353bbe19a3b00b3576`.
- Accepted feature commit:
  `802f52a20408443836819116012762648347ed13`.
- Reviewed merge commit on local `main` before the final evidence-only commit:
  `a0b3dbd9572d1bc2699159bc199df27666989dff`.
- Accepted v7 archive:
  `poly-mm-v07-pro-replacement-v7-dbb904a5.zip`, 246,311 bytes,
  SHA-256
  `dbb904a5ea84233188425b2d849ad74c9af7157875b15d741826e0ec048767b8`.
- Archive contents: 29 exact/case-fold unique regular entries, no unsafe paths
  or symlinks, CRC clean, and 28/28 declared payload hashes matched.
- Patch identity: SHA-256
  `be2b319776ec68121c902d83212e0d40fbe732f0eff58619064e62913f80fa17`.
- Exact external delivery report: SHA-256
  `0aefb261344c19c0723a2b85eddd731ba5f4c4ccca09e604ba58260045ccd459`.

Before the two final test-only follow-ups, all 14 repository files applied from
the archive were byte-identical to the packaged copies. The accepted feature
commit contains only the independently reviewed v7.1 timeout annotation and
v7.2 platform-aware cache-read expectation in addition to that content.

## Accepted implementation

- Both binary outcomes use one shared terminal 60-second TWAP. Strike ordering
  makes the impossible split state zero by construction; equal strikes make
  both split states impossible.
- Structural opportunities are evaluated before predictive opportunities and
  do not depend on Monte Carlo. Sizing walks exact depth, fees, risk costs, and
  breakpoints instead of shrinking an otherwise profitable quantity through a
  generic risk budget.
- The predictive lane uses causal inputs, coherent market shrinkage, uncertainty
  controls, and distinct predictive promotion gates.
- The evaluation ledger retains causal no-fill and failed attempts, derives
  expiry clusters from common expiry, requires pre-label locks, and keeps
  insufficient economic evidence as `null` with reasons.
- The public evaluator is fail-closed. The high-level builder independently
  revalidates capture roots, configuration, the pre-expiry journal, receipts,
  replay, and reconciliation rather than accepting a generic caller-provided
  authority flag.
- The provenance guard prevents supported API misuse; it is not an external
  signature or proof against an actor controlling local Python execution or
  source. That residual trust boundary is explicit.
- Windows filesystems without `st_rdev` fall back to full manifest/content
  validation with no metadata cache reuse. The POSIX metadata-token bytes and
  existing fast path remain unchanged.
- No dependency or lockfile changed. No live-order, signing, credential,
  authenticated endpoint, proxy, chain-write, database migration, deployment,
  server, or production-configuration path was introduced or exercised.

## Independent verification

### Focused and compatibility gates

- Windows Python 3.13.5 on merged `main`: v0.7 plus data-store suite — **107
  passed, 1 skipped** in 53.87 seconds.
- Windows Python 3.14.6: the same 108 tests — **107 passed, 1 skipped** in
  68.71 seconds.
- Linux/WSL Python 3.12.3: the same 108 tests — **106 passed, 2 failed**.
  All 89 new v0.7 tests passed. The two failures are the pre-existing
  same-size POSIX metadata-cache tamper tests; both reproduce unchanged on the
  untouched `c557160d` baseline in the same filesystem.
- Linux integration/compatibility selection — **288 passed**.
- Linux safety/network-mocking/recorder selection — **76 passed**.
- `tests/test_edge_lab_data_store.py` on Windows — **18 passed, 1 skipped**.
- Ruff 0.16.x `--select E,F,I` on all 11 changed Python files — passed.
- `compileall -q src scripts` under Python 3.11.15, 3.13.5, and 3.14.6 —
  passed.
- `git diff --check` — passed.
- Two independent final read-only reviews (repository standards and task spec)
  — no actionable findings.

### Full Linux regression

A fresh native-Linux Git clone with the exact accepted files produced:

```text
1578 passed, 26 failed, 11 skipped, 1 deselected in 569.73s
```

The untouched baseline produced 1,488 passes and 25 failures before the 90 new
tests. The final run's failures comprise four tests requiring absent legacy
dry-run logs, two pre-existing same-size metadata-cache cases, one host-memory
pressure expectation, 18 legacy live-network tests in a network-unreachable
environment, and one performance ratio at 5.02x against a 5x threshold. The
last test subsequently passed five consecutive runs on the final tree and five
consecutive runs on the untouched baseline. It is recorded as host timing
noise, not silently counted as a green full-suite run.

The frozen v0.5/v0.6 strategy/deployment trees and recorder source remain
identical to the baseline. The only modified pre-existing production file is
`src/edge_lab/data_store.py`, limited to the reviewed cross-platform
integrity-cache fallback.

## Economic acceptance

The independent audit reconstructed every available v0.6 development-shadow
row and found:

| Metric | Result |
|---|---:|
| Economic attempts | 48 |
| Independent expiry clusters | 24 |
| Net PnL | +87.523635 USDC |
| Cluster t-statistic | 1.459910073 |
| One-sided bootstrap 5th percentile | -0.153821583 USDC/cluster |
| Bootstrap probability mean > 0 | 0.94168 |
| Largest positive cluster / net PnL | 48.5237% |
| 5m Brier, model vs market | 0.288421 vs 0.176509 |
| 15m Brier, model vs market | 0.206083 vs 0.150203 |

This fails the preregistered minimum of 100 distinct settled expiries and 100
explainable locked-OOS attempts, the positive cluster-bootstrap lower bound,
the concentration limit, and both market-relative Brier gates. The machine
verdict therefore remains:

```text
evidence_status = counterfactual_insufficient
predictive_true_edge_gate_satisfied = false
structural_true_edge_gate_satisfied = false
true_edge_gate_satisfied = false
positive_net_pnl_user_check_passed = false
qualified_net_pnl = null
predictive_qualified_net_pnl = null
structural_qualified_net_pnl = null
```

The user's two profitability criteria are **not met by current evidence**. No
paper, fixture, synthetic, Wine, counterfactual, or shadow result is represented
as real-money or production profitability. A future claim requires prospective,
preregistered, independently settled evidence through the included fail-closed
builder; code correctness alone cannot manufacture an edge or 100-trade PnL.

## Residual risks

- No prospective 100-expiry locked-OOS journal exists yet.
- Capture receipts are local and are not externally signed or timestamped.
- The local TWAP is a proxy because official sampling, weighting, rounding, and
  gap semantics are not public.
- Fees, latency, partial fills, sequential two-leg execution, hedge failure,
  unwind, and model drift still need prospective evidence under actual market
  conditions.
- Two POSIX same-size metadata-cache tests fail on the baseline and final tree;
  the accepted Windows missing-stat fallback is fail-closed, but the older
  POSIX fast path retains this known baseline limitation.
- Legacy live-network and absent-runtime-data tests prevent an unconditional
  green full-suite statement in the isolated Linux environment.
