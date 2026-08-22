# BTC 5m/15m capture failure taxonomy (WP-E2.0)

Status: **classification complete**; **WP-E2 host remediation not started**.
Date: 2026-08-22
Authority: [LIVE_COMPLETION_SPEC.md](LIVE_COMPLETION_SPEC.md) WP-E2.0.
This note classifies already reported capture failures. It does not authorize
orders, host purchase, or a Gate 0 economic decision.

## 1. Question

Was the V0.7 source-capture failure rate `14/15 = 93.3%` mainly capacity, or
a parser / window-miss bug that a bigger instance cannot fix?

Answer: **primary bucket is `capacity_backpressure`.** A new non-burstable
host is the next authorized WP-E2 action. This session does not change AWS.

## 2. Evidence that may be used

This checkout does **not** contain the 15 post-cutoff capture trees, attempt
receipts, or unsanitized `RecorderWorkerError.error_code` values. WP-E2.0
therefore uses only SHA-bound deployment reports already in git:

| Artifact | Role |
|---|---|
| [DEPLOYMENT_REPORT.md](../btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/DEPLOYMENT_REPORT.md) | Primary. Heartbeat `2026-08-16T19:02:17Z`: 15 post-cutoff attempts, 1 clean, 14 rejected capture errors. |
| Same report, 2026-08-17 capacity correction | Isolated V0.6 cycle at `2026-08-17T02:21:01Z` reached `capture_error = null` after stopping rawcap and reducing persistence load. Later canary: 46 post-cutoff attempts = 7 clean + 39 historical capture errors + 4 explicit case-level boundary gaps. |
| Same report, 2026-08-17 11:10 UTC repair | Future-opening schedule applied to every capture, not only bootstrap. That is a separate window-miss class, not the 14/15 source-error count. |
| [V06_REVIEW.md](../v06_review_2026-08-15/V06_REVIEW.md) | Host trend: t3.small standard credits falling ~23/h toward exhaustion; rawcap ~5.3 GiB/day; CLOB WS ~30s disconnects. |
| Current recorder contract | `src/edge_lab/network_safety.py:safe_error_details` and `src/edge_lab/btc_twap_relative_value_v07_shadow.py:_validated_source_capture_error` force top-level `capture_error.error_code = capture_failed`. The 14/15 summaries therefore cannot be recounted by raw error code. |

No raw capture-summary files, no attempt receipts, and no restored 41 v0.8
trees were used. Missing trees remain unrestorable by default.

## 3. Universe

The classified universe is the **15 post-cutoff source attempts** in the
unattended V0.7 heartbeat:

- `source_post_cutoff_attempt_count = 15`
- `source_finalized_clean_count = 1` (not a failure)
- `source_rejected_capture_error_count = 14`
- failure rate `14/15 = 93.3%`

Later numbers in the same report are **not** this universe:

- Isolated acceptance cycle `capture_error = null` is a capacity-repair canary,
  not a 15th success inside the 14/15 window.
- `46` post-cutoff attempts / `7` clean / `39` historical errors / `4`
  boundary gaps is a later mixed stock after the repair. The 4 gaps belong in
  `expected_evidence_gap` and are recorded below as out-of-universe context.
- The 11:10 UTC future-opening miss is a scheduler defect on an otherwise
  different failure mode (warm-up never reaching a test case). It is not one
  of the 14 rejected source errors.

Unknown-bucket rule: any failure that cannot be placed from inspected evidence
must not be dumped into capacity. The 9 uninspected members of the 14 inherit
the inspected dual-leg pattern plus contemporaneous host saturation, and are
counted as capacity with that caveat, not as a unique software root cause.

## 4. Counts

| Bucket | In-universe count | Share of 15 | Share of 14 failures | How assigned |
|---|---:|---:|---:|---|
| `capacity_backpressure` | 14 | 93.3% | 100% | 5/14 inspected: two `RecorderWorkerError` legs, clean manifest/raw integrity, while CPU credit ~0 and disk 148 MiB from the 15 GiB soft stop. 9/14 uninspected inherit the same heartbeat class; error codes were sanitized to `capture_failed`. |
| `window_miss` | 0 | 0% | 0% | Not present in the 14/15 source-error count. Documented separately below. |
| `protocol_or_parser` | 0 | 0% | 0% | Inspected failures had clean integrity arrays. No schema/parser crash was reported for these 14. |
| `expected_evidence_gap` | 0 | 0% | 0% | Not in the 14 rejected capture errors. |
| clean / not a failure | 1 | 6.7% | n/a | `source_finalized_clean_count = 1` |
| unknown | 0 | 0% | 0% | Uninspected 9 stay in capacity with explicit caveat, not a fifth bucket. |

Unknown bucket of the 14 failures: **0%** (< 10% gate).
Inspected fraction of the 14: **5/14 = 35.7%** had dual-leg recorder detail.
Uninspected fraction: **9/14 = 64.3%**, still labeled capacity because the
report presents them as the same `source_rejected_capture_error` class in one
heartbeat, not as mixed parser or window codes.

## 5. Representative receipts

Local receipt paths are not in this checkout. The durable references are:

| Sample | Durable reference | Observed | Bucket |
|---|---|---|---|
| 5 most recent of the 14 | DEPLOYMENT_REPORT.md “Current prospective state and data-quality blocker”, heartbeat `2026-08-16T19:02:17Z` | two `RecorderWorkerError` legs; manifest/raw integrity arrays clean; top-level `error_code` sanitized | `capacity_backpressure` |
| remaining 9 of the 14 | same heartbeat: `source_rejected_capture_error_count = 14` | no per-attempt code retained | `capacity_backpressure` (inherited class, not unique root cause) |
| 1 clean attempt | same heartbeat: `source_finalized_clean_count = 1` | clean source attempt in the 15 | not a failure |
| isolated V0.6 canary | DEPLOYMENT_REPORT.md “2026-08-17 capture-capacity correction”, `2026-08-17T02:21:01Z` | `capture_error = null`, 0 recorder-leg failures, 98 manifests, 27,955 records; CPU credits recovered from ~0 to >10 after stopping rawcap / caching rejected trees | corroborates capacity, out of 14/15 universe |
| 4 later boundary gaps | same correction paragraph, first unattended timer canary | “4 explicit case-level boundary gaps”, cached separately from safety/contract failures | `expected_evidence_gap` (out of universe) |
| future-opening miss | DEPLOYMENT_REPORT.md “2026-08-17 11:10 UTC operational repair” | capture started after frozen 300s lookback and after 15m open, so V7 train/validation slid forward and never reached a test case | `window_miss` (out of universe; scheduler already patched) |

Host telemetry attached to the 14/15 window:

- instance `i-045bb69f9cba2dadd`, `t3.small`, credit mode `standard`
- CPUCreditBalance latest five-minute average ~`0.000068`
- CPU utilization pinned near T3 baseline ~20%
- free disk `16,261,636,096` bytes vs V6 15 GiB soft stop (~148 MiB margin)
- V0.7 re-read/re-hashed every rejected source tree on every refresh

## 6. Why this is not parser or window-miss as the main cause

`protocol_or_parser` would require WS/REST schema failure, clock-anomaly
rejection, or a non-capacity recorder crash with dirty integrity. The five
inspected failures had **clean** integrity and two recorder legs. CLOB WS
~30s disconnects existed, but V0.6’s first accepted cycle already survived
24 disconnects with `recorder_leg_failures=0`. Disconnect-without-capacity
is therefore not the 93.3% explanation.

`window_miss` did happen later as a bootstrap-only future-opening bug. That
bug produced a sliding warm-up, not the 14 `capture_error` rejections. The
11:10 UTC repair already applies future-opening to every capture. No additional
parser/window code fix is required before WP-E2 host work.

`expected_evidence_gap` appears only in the later 4 boundary gaps. Those rows
are dirty-but-expected and must not be spent as script bugs or as capacity
failures.

The capacity assignment is the strongest remaining hypothesis because:

1. inspected failures match dual-leg recorder death under host saturation;
2. the same report reproduced the failures as shared persistence backpressure
   plus CPU-credit exhaustion;
3. removing rawcap / compressing earlier / caching rejected trees produced a
   clean isolated cycle on the same instance once credits recovered.

It is **not** a uniquely proved per-attempt software root cause. The summaries
deliberately dropped the underlying recorder code.

## 7. Will a new host move 93.3% to < 5%?

**Yes, that is the aligned WP-E2 bet; it is not guaranteed from this
checkout.**

Why a host change can work:

- The 14/15 failures occurred on a credit-exhausted `t3.small` with ~148 MiB
  to the disk soft stop and a growing rejected-tree hash tax.
- Compact capture (no rawcap full frames) plus non-burstable CPU and ≥10 GiB
  free disk removes the two saturation mechanisms named in the report.
- The isolated canary already showed `capture_error = null` once persistence
  load and credits recovered.

Why a host change can still fail:

- 9/14 attempts have no unsanitized `error_code`. If those were a second
  latent bug, failure rate could stall around 40% after capacity is fixed.
- CLOB WS ~30s disconnects may remain. On paper they are dirty/cost; they
  become a live cancel-all requirement only after WP-E0.
- SNS still has 0 confirmed subscriptions; that is an E2 alarm gap, not the
  14/15 classifier.

If a new host only reduces failure rate to about 40%, that is still a capture
problem. It is **not** a Gate 0 economic STOP and must not start WP-S0.

## 8. WP-E2 host spec (authorized only because primary bucket is capacity)

Do not execute this session. Minimum from LIVE_COMPLETION_SPEC WP-E2:

- retire or migrate the exhausted `t3.small` recorder stack
- non-preemptible, non-exhaustible credit (or unlimited non-burstable)
- available memory ≥ 2 GiB
- `/var/lib/poly-mm-v08` free disk ≥ 10 GiB
- projected daily capture ≤ 1 GiB; no rawcap full frames on the saturated disk
- compact pair capture only: four tokens, 30s full-depth anchors, 5s public
  taker poll, top-of-book changes, official 60s RTDS
- `persist_raw_clob_frames=false`, `persist_reconstructed_full_depth_frames=false`
- one recorder-leg failure dirties the whole cohort
- SNS topic with at least one **Confirmed** subscription
- **stop sanitizing** future capture-summary recorder codes: persist
  `RecorderWorkerError.error_type` and `error_code`

## 9. Code fixes not required before buying a host

No parser or window-miss patch is the main 14/15 action. Keep these as
follow-through on the new host, not as a reason to delay WP-E2:

- already done: future-opening on every capture; rejected-tree cache;
  rawcap producer stopped
- still required at deploy time: unsanitized recorder codes in summaries;
  confirmed SNS; compact-capture config actually running

## 10. Decision

| Gate | Result |
|---|---|
| Classification covers all 15 attempts in the 14/15 universe | yes |
| Unknown bucket < 10% of classified failures | yes (0%) |
| Primary bucket | `capacity_backpressure` |
| Capacity repair aligned with main cause? | **yes** |
| Start WP-E2 host remediation next session? | **yes, after user provides host access / cost approval** |
| Claim WP-E2 complete? | **no** |
| Start WP-S0 / WP-E0? | **no** |

Strategy live remains **NO-GO**. Execution probe remains **NO-GO**.
