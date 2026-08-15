# v0.7 independent acceptance record

Status: **complete for code acceptance, failed for profitability acceptance**.
ChatGPT Pro outputs were treated as advisory only. The delivered v0.7 paper-only
track plus a local Codex hardening patch were independently verified, but the
economic evidence still does **not** establish a true edge or a qualified
100-trade positive PnL claim.

## Baseline before any v0.7 patch

- Branch/commit: `codex/settlement-regime-v06` at
  `c557160d0c98e195a988f4353bbe19a3b00b3576`.
- Isolated worktree: `codex/v07-profitability` at the same commit.
- Strategy-focused Windows/Python 3.14 suite: **245 passed** in 3.15 seconds.
- `compileall -q src scripts`: passed.
- Full Windows suite after excluding the five known import-time `fcntl` files:
  **1,245 passed, 206 failed, 12 skipped, 1 deselected, 10 teardown errors**.
  The failures are baseline platform/data issues: POSIX descriptor and mode
  guarantees unavailable on Windows, missing legacy dry-run logs, and open
  SQLite handles at teardown. They are not a usable green gate and must be
  compared against the post-change run, not hidden.
- Unfiltered Windows collection: five import errors from Linux-only `fcntl`.
- Frozen tracked-tree fingerprints (SHA-256 of canonical `git ls-tree -r`
  output at `c557160d`):
  - v0.5 research: `9d6414fbc4edf752f1ec359143d067fc2ec64ccdf62b1b5edd4d1c0a298a4e52`;
  - v0.6 research: `9e1d92676f0732756e61c8ff715d05107de8ad6faf6d605a3b680f014a7a0c8c`;
  - v0.5 deployment assets: `90f324028dd8b706578f6d6cc567ea4de2f91fffc54e21db47007d6058bfd962`;
  - v0.6 deployment assets: `9e88536e6ee3b189c1da09ee6047bbb6587bb4e408c88479b4745d6bf5c67641`.
  Post-change verification must reproduce these values exactly.

## Model correctness gates

- [ ] v0.5/v0.6 behavior and frozen evidence identities remain unchanged.
- [ ] v0.7 derives both binary outcomes from one shared terminal 60-second TWAP.
- [ ] Strike ordering makes the impossible split state exactly zero by
      construction, including after calibration or market shrinkage.
- [ ] Equal strikes make both split states impossible.
- [ ] Relative-value economics are expressed as the probability of the terminal
      TWAP lying between the two strikes; redundant marginal machinery does not
      reintroduce impossible states.
- [ ] All predictor, oracle, book, fee, and calibration inputs are causal at the
      decision timestamp.
- [ ] A local rolling mean is labeled as a proxy, not an exact reconstruction of
      Chainlink's undisclosed TWAP implementation.
- [ ] Volatility/model uncertainty does not collapse on one-second tick
      quantization and is not confused with Monte Carlo sampling error.
- [ ] Any numeric floor, lambda, clipping bound, veto, or tau is selected ex ante
      or train-only, not tuned on the known 48 v0.6 shadow attempts.

## Economic and execution gates

- [ ] Full walked ask depth, dynamic crypto taker fees, latency, partial fills,
      leg delay, hedge failure, and unwind remain in every economic attempt.
- [ ] Structural payoff floors are checked before predictive edge; a theoretical
      floor is not called arbitrage unless both legs are executable after fees.
- [ ] Market baseline uses only decision-time executable surfaces.
- [ ] Failed attempts remain in PnL and cannot be selected away.
- [ ] No live orders, signing, credentials, authenticated endpoints, proxies,
      chain writes, or production changes are introduced.

## Evidence gates

- [ ] Synthetic/fixture tests are explicitly non-economic evidence.
- [ ] The user-facing 100-trade check requires at least 100 distinct settled
      expiry clusters and 100 explainable locked-OOS economic attempts.
- [ ] Positive total PnL is accompanied by expiry-cluster bootstrap 95% lower
      bound above zero and max single-event contribution at most 20%.
- [ ] OOS Brier beats the executable-market baseline for both horizons and ECE
      is at most 0.05.
- [ ] Neighboring preregistered settings are mostly positive without test-set
      selection.
- [ ] Existing 500-attempt promotion gates are not lowered.
- [ ] Insufficient evidence is serialized as `null` plus reasons, never zero or
      a fabricated profitable classification.

## Engineering gates

- [ ] Delivered hashes and attachment contents verified.
- [ ] Patch applies cleanly in the isolated worktree.
- [ ] Changed files reviewed for dependencies, lockfiles, executable flows, and
      hidden scope expansion.
- [ ] New focused tests pass.
- [ ] The 245-test baseline suite remains green with all new relevant tests.
- [ ] Linux/POSIX full or appropriately scoped regression passes.
- [ ] Ruff E/F/I on all changed Python files passes.
- [ ] `python -m compileall -q src scripts` passes.
- [ ] `git diff --check` passes.

## Final independent verification summary

- Patch provenance:
  - ChatGPT Pro implementation accepted only after three correction rounds and
    ZIP/hash validation.
  - Codex applied one additional local hardening fix in
    `src/edge_lab/data_store.py` plus the matching test expectation in
    `tests/test_edge_lab_data_store.py`.
- Local hardening reason:
  - Metadata-only integrity-cache reuse could miss same-size tampering on the
    Windows, WSL/DrvFS, and mixed-filesystem paths used here.
  - The final accepted behavior is fail-closed: always recompute integrity
    instead of trusting metadata-only cache keys.
- Independent test matrix:
  - Windows Python 3.13.5: `tests/test_btc_twap_relative_value_v07_*.py`
    plus `tests/test_edge_lab_data_store.py` => **76 passed**.
  - Linux/WSL Python 3.12.3 on `/mnt/c`: same scoped suite =>
    **76 passed** in a temporary dependency environment.
  - Ruff gate on changed Python files: `python -m ruff check --select E,F,I`
    => passed on the checked local environment.
  - Build gate: `python -m compileall` for changed strategy/evaluation/replay
    modules plus `data_store.py` => passed on Windows and WSL.
  - Patch hygiene: `git diff --check` => passed.
- Scope verification:
  - No new dependencies, lockfile edits, live-order paths, signing,
    credentials, proxies, authenticated endpoints, chain writes, database
    migrations, or production configuration changes were introduced.
  - v0.7 remains a separate paper-only track; v0.5/v0.6 trading behavior is not
    modified by the new strategy files.
- Acceptance verdict:
  - Engineering acceptance for the delivered paper-only code: **pass**.
  - User profitability acceptance ("find a real edge" and "positive PnL after
    100 trades"): **fail / unproven** with current evidence.

## Profitability conclusion

The existing evidence was independently reconstructed from all 48 appendix
rows, grouped by the 24 expiry IDs. It exactly reproduces total net PnL
`+87.523635`, cluster mean `+3.646818125`, sample SD `12.237525799`, and
cluster t-statistic `1.459910073`. A 200,000-resample expiry-cluster bootstrap
with seed 712 gives a one-sided fifth percentile of `-0.153821583` USDC per
cluster and only `0.94168` bootstrap probability of a positive mean. The
largest positive cluster is `48.5237%` of total net PnL. Equal-cluster-weighted
Brier is worse than the executable-market diagnostic on both horizons: model
versus market is `0.288421` versus `0.176509` for 5m and `0.206083` versus
`0.150203` for 15m.

Therefore the current 24 independent expiry clusters do **not** meet the
100-independent-trade requirement or prove a true edge. No final report may
upgrade that status without new qualifying evidence.
