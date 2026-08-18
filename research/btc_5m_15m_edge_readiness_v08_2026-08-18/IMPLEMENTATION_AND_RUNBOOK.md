# v0.8 readiness implementation and runbook

## Current decision

Strategy live: **NO-GO**. Execution probe: **NO-GO**. The repaired Gate 0 and
double-maker shadow code are paper-only. The runtime dependency and generic
client calls use official `polymarket-client 0.6.0`, but no double-maker venue
adapter or armed systemd probe unit is distributed. The compatibility boundary
remains an unconditional hard stop; the eight checks are diagnostics only until
a reviewed Track-C adapter owns a provenance-bound release capability.

## Implemented surfaces

- `btc_twap_continuous_rtds.py`: public-only continuous official 60s RTDS
  capture, exact topic/symbol validation, source/receipt/monotonic clocks,
  connection epochs, gaps, immutable raw batches, status hash, and pure health
  evaluation.
- Existing compact pair capture now retains CLOB HTTP full-book/clock/market fee
  evidence, polls both conditions' public taker-only trades, retains monotonic
  timestamps, and treats a single recorder-leg failure as dirty at v0.7 intake.
- `btc_twap_relative_value_readiness.py`: co-terminal payoff validator,
  rule-compatible structural ladder, robust neutral-shadow prerequisites,
  cohort coverage, and semantically separate probe/live gates.
- `build_btc_twap_executable_upper_bound.py`: exact-count, full-window Gate-0
  report builder. It scans `ttc=300..0`, emits taker/taker, maker/taker, and
  maker/maker diagnostics plus `b(t)`, and refuses to label a partial local set
  as the reported 41.
- `btc_twap_structural_shadow.py`: double-maker public-trade replay using the
  existing `QueueScenario`, separate single-leg dispositions, exact ledger
  accounting, strike/rule-bound direction derivation, and
  remove-best/20-expiry-rolling/concentration robustness gates.
- `build_btc_twap_structural_shadow_report.py`: strict offline capture adapter
  and 3x3 replay report. It verifies the rolling journal itself, requires exact
  coverage of every admitted/finalized cohort, re-derives every economic input
  from finalized CaptureStore manifests, binds primary/supporting capture-tree
  SHA-256 identities, and refuses caller-supplied receipt/completeness booleans.
- `btc_twap_locked_shadow_v08.py`: rolling predecision admission and receipt
  chain with O_EXCL files, fsync, hashes, permanent first-attempt identity, and
  denominator-preserving outcomes.
- The shared paper-track watcher now monitors the v0.8 heartbeat, capture
  failure rate, projected retention, disk, memory, and CPU credits; a failed
  v0.8 health check exits nonzero and the watcher pages the configured,
  confirmed SNS subscription.
- `btc_twap_execution_probe.py`: injected, default-inert harness with fresh
  plan-bound confirmation, authenticated readiness contract, authoritative fill
  reconciliation, post-only maker, actual-cost/full-depth FOK recheck,
  sell-side unwind, persistent kill switch, 12/10 USDC caps, and no venue adapter.

## Deployment order

1. Capacity first. The observed t3.small CPU-credit exhaustion is an external
   runtime constraint. Do not stack the new recorder on that saturated instance.
   Resize or replace the old RTDS ingest, then prove CPU, memory, disk latency,
   and free-space headroom during a full K15-to-close window.
   The machine capacity verdict must show capture failures <5%, free disk
   >=10 GiB, retained projection <=1 GiB/day, available memory >=2 GiB, and no
   exhausted burstable CPU credits. Otherwise all statistical/probe promotion
   stops.
2. Install the public continuous RTDS unit from
   `deploy/aws/edge_readiness_v08`, but do not enable any order path. Verify the
   status hash, `phase=capturing`, age <=15 seconds, zero disconnect/error/invalid
   observations, and maximum source gap <=2 seconds.
3. Run four complete common-expiry cohorts. Require official RTDS coverage from
   before K15 open through close plus four-token L2/full-depth anchors, both
   public trade streams, fee/rule snapshots, and all clocks. Any single failure
   stays as a dirty receipt and resets neither identity nor denominator.
4. Export the deployed v0.7 manifest containing the claimed 41 unique clean
   common expiries and run:

   ```text
   python scripts/build_btc_twap_executable_upper_bound.py --manifest MANIFEST --output /var/lib/poly-mm-v08/reports/gate-0-upper-bound-41.json --expected-clean-attempts 41
   ```

   If the count is not exactly 41, stop and reconcile the seven-attempt
   difference from the prior 48 rather than changing the expected count.
   `--decision-tau-seconds` remains diagnostic-only and must not be used on the
   formal 41-expiry report path. Any precomputed maker fill-cap summary is also
   diagnostic-only; only builder-recomputed caps from verified captured future
   public trades can participate in a formal Gate 0 authority claim.
5. If and only if the maker/maker Gate 0 aggregate is positive and averages at
   least 0.5 USDC/expiry, lock the v0.8 inclusion policy before the new track
   start, then use the
   rolling `admit`, `decision`, and `finalize` commands. Store the journal on a
   dedicated volume and copy receipt hashes to an independent log destination;
   the local chain alone is not an external signature.
6. Accumulate at least 200 unique common expiries and run all three queue
   scenarios plus all three single-leg dispositions. Stop unless the neutral
   double-maker total, remove-best-expiry, remove-best-direction, and every
   registered 20-expiry rolling window are positive and concentration is at
   most 20%. The neutral row is permanently `passive_wait`; taker-hedge and
   FAK-unwind remain sensitivity rows. Build the provenance-bound report with:

   ```text
   python scripts/build_btc_twap_structural_shadow_report.py --input STRUCTURAL_INPUT.json --journal-root /var/lib/poly-mm-v08/data/locked-shadow-v08 --preregistration /opt/poly-mm-v08/research/btc_5m_15m_edge_readiness_v08_2026-08-18/PREREGISTRATION.json --output-dir /var/lib/poly-mm-v08/reports/structural-shadow
   ```

   Each attempt must name its finalized primary `capture_root`; it may name
   finalized overlapping `history_capture_roots` solely to complete the K15 to
   close official-RTDS window. The input carries exact-decimal four-token books,
   public book/trade events, rule-hash-bound opening TWAP strikes, terminal
   outcomes, and any hedge/unwind books, but the builder independently re-derives
   all of them and rejects any omission or mismatch. Its `attempt_id` must be
   the journal's canonical common-expiry ID. The predecision receipt must match
   the canonical locked-action payload hash, while the finalized outcome must
   commit to the manifest-bound source-evidence hash and exact neutral replay
   PnL. Dirty/no-trade rows remain explicit zero-PnL denominator rows.
   Strategy-live evidence additionally needs 14 consecutive UTC days at 20
   USDC/day or more while peak deployed capital stays at or below 2,000 USDC.
7. Only after steps 4–6 pass, review the already migrated official-SDK client
   boundary, then perform pUSD balance/allowance reconciliation, signature and
   terminal-state drills, user stream/cancel-on-disconnect validation, and the
   target-host geoblock check. The current maker+FOK harness is not a substitute
   for a double-maker probe.

## Probe arming boundary

Even after every machine gate reports eligible, no final order may be submitted
until the user explicitly accepts a maximum account loss of 12 USDC for the
exact plan hash and supplies a second fresh confirmation. The confirmation
expires within five minutes and its nonce/phrase are never persisted in clear.

## Known evidence gaps

- The 41 deployed capture trees are not present in this checkout, so the actual
  Gate-0 report cannot be honestly generated here.
- No host deployment or four complete new cohorts were available in this local
  Windows session, so the capacity watcher and confirmed SNS delivery still
  require target-host evidence.
- No 200-expiry public trade tape exists in this checkout, so the revised neutral
  double-maker shadow gate cannot be claimed here.
- The structural report adapter is covered by a synthetic finalized CaptureStore
  tree, including tamper and omitted-flow rejection, but no deployed v0.8 tree
  is available locally to produce real shadow evidence.
- The official unified SDK dependency/client boundary is migrated, but no
  production double-maker venue is enabled. Authenticated reads, pUSD,
  signatures, user WebSocket fills, cancel races, terminal status, and
  target-host geoblock remain deliberately unproven and closed.
- The locked journal is local append-only evidence, not a third-party timestamp
  or adversary-resistant signature.
- Targeted strict type checks and the portable Windows suite pass. Linux/Python
  3.12 release validation remains required to exercise the real `fcntl` lock
  and POSIX durability/performance paths that are intentionally skipped on
  Windows.
