# BTC 5m/15m relative-value paper v0.7 shadow deployment report

Updated on 2026-08-17 UTC after external review, independent correction,
deployment, scheduled-run observation, Amazon Linux/XFS validation, and the
capture-capacity incident correction described below.

## Outcome

V0.7 is deployed as a read-only prospective public-data
counterfactual/shadow monitor. It does not submit orders, use credentials, call
authenticated endpoints, or modify the frozen V0.6 evidence tree. A performance
job runs every 30 minutes and a health job every 5 minutes.

Codex heartbeat automation `v7-30` performs a read-only safety check every 10
minutes and reports state changes or failures back to the current task. Its
prompt forbids server, repository, cloud-configuration, service, and order
mutations; server-side systemd remains the authoritative scheduler.

## 2026-08-17 capture-capacity correction

The repeated V0.6 capture failures were reproduced as shared persistence
backpressure while the `t3.small` CPU credit balance was effectively zero.
V0.7 also re-read and re-hashed every rejected source tree on every refresh,
which made the cost grow with the failure count. The corrective deployment:

- stopped and disabled the redundant rawcap producer while retaining its
  non-destructive 15-minute maintenance timer
- moved rawcap compression forward to 10 minutes after finalization
- cached fully audited rejected-source identities under the V0.7 runtime root;
  any source inventory change invalidates the cache and forces a full re-audit
- separated expected per-case evidence gaps from safety/contract failures,
  cached them by the complete manifest-case commitment, and prevented excluded
  cases from entering later case history chains
- retained the 30-minute V0.7 server timer after cache-hit refreshes fell to
  17 seconds, keeping status inside its frozen 45-minute freshness contract
- raised the V0.6 new-cycle disk guard from 12 GiB to a 15 GiB soft stop while
  retaining the 12 GiB global hard floor
- preserved each `RecorderWorkerError` type and code in future capture summaries
- quarantined the obsolete V1 `deploy/aws/v07_shadow` asset set outside active
  deployment paths

The isolated V0.6 acceptance cycle completed at `2026-08-17T02:21:01Z` with
`capture_error = null`, no recorder-leg failures, clean integrity, 98 manifests,
and 27,955 records. CPU credits recovered from approximately zero to above 10.
The first V0.7 rejected-tree cache build took 150 seconds and its immediate hit
took 17 seconds. After the case-data-quality cache was added, its build took 41
seconds and the dual-cache hit took 20 seconds. The first unattended timer run
then completed successfully in 22 seconds. At that canary, 46 post-cutoff
attempts contained 7 clean captures, 39 historical capture errors, 4 explicit
case-level boundary gaps, and `case_count = cohort_admission_count = 2`. Orders
and authenticated calls remained zero throughout.

This is **not** evidence that the strategy makes money. There is no pre-label
lock journal, no qualified V0.7 trade, and no qualified PnL. The enforced state
remains:

- `qualified_net_pnl = null`
- `true_edge = false`
- `true_edge_gate = false`
- `positive_100_trade_check = false`
- `orders_submitted = 0`
- `authenticated_endpoints_used = 0`

The user's two economic acceptance criteria are therefore not met yet.

## Source and release identities

- GitHub `main` code/deployment commit:
  `5b6708a2ce30b74b5d164fc33f8f195ae65e9689`
- Frozen V0.7 implementation baseline:
  `c557160d0c98e195a988f4353bbe19a3b00b3576`
- Installed server commit at the time of this update:
  `5b6708a2ce30b74b5d164fc33f8f195ae65e9689`
- Installed Git status: clean

The initial source bundle supplied to ChatGPT Pro was:

- baseline: `36a09c4d9c49b540ea3a61b181fae50bcfa3a32b`
- size: `1,329,957` bytes
- SHA-256:
  `f61437c485652ab7a1936c11ad5d282167750d7497ee6a6f0a21c36f44bd3ffa`
- high-confidence secret findings: `0`

The final ACL-review source bundle was:

- baseline: `5b6708a2ce30b74b5d164fc33f8f195ae65e9689`
- file: `poly-mm-v07-final-review-5b6708a-20260817-source.zip`
- size: `1,096,531` bytes
- entries: `211`
- SHA-256:
  `34a06bdae36a1dae20b1933d9dbef2dbf1ea93292b4328da236c4bb3ec351c56`
- high-confidence secret findings: `0`
- forbidden archive entries: `0`

The original Git release archive remains preserved in encrypted S3:

- size: `3,087,212` bytes
- SHA-256:
  `096a46b6c5f09eb103fd7acb71de445e925ec963012c06b4dacff0931d394703`
- server-side encryption: `AES256`

Immutable incremental bundles used for later corrections were also uploaded
with `AES256` and verified before use. The final ACL increment was `6,482`
bytes with SHA-256
`6aa87f43e42ef36fc1bef1379bd5dd623eb6541372f5e2ee0cef8743c0b248a4`.
The final Linux test found that the sparse release omitted the three frozen
V0.5 research files asserted by the V0.7 asset test. Their exact Git blobs were
packaged as a `4,619` byte, zero-high-confidence-secret archive with SHA-256
`e421eee6b84e7257e2c98932f457141630385da3296c7bd78b70a12e9bc2c77c`,
uploaded with `AES256`, verified on the host, written to the local object store,
and added to the sparse checkout without changing the deployed commit or Git
status.

## External review and independent corrections

ChatGPT Pro review conversation:
<https://chatgpt.com/c/6a81b3fd-3a3c-83ec-bdda-d7056e03d5c3>

Implementation conversation:
<https://chatgpt.com/c/6a7fe211-89a8-83ec-88dc-083193386308>

Earlier quantitative audit:
<https://chatgpt.com/c/6a7fe366-7ce4-83ec-9afa-f193454d6eb7>

The external reviewer initially returned `NO-GO` and supplied a `44,232` byte
patch with SHA-256
`1c14a5f6b5ec5462402084e339f86c42c411be5359e2a8904bd9cb894a134aa8`.
The patch was not applied blindly. Findings were reproduced and corrected while
keeping stricter fail-closed behavior where appropriate. Important corrections
included:

- using the first normal post-cutoff capture as a history seed when it lacks
  the exact 15-minute opening boundary
- independently auditing manifests and raw checksums instead of trusting a V6
  summary claim
- binding V6 status, configuration, evidence track, settlement regime, timing,
  target identity, and safety flags
- checking complete source-tree identity before and after reflink projection
- rejecting symlinks, hard links, non-regular files, path escape, and mutation
- using only the immediately preceding capture as history
- suppressing builder authority on the no-journal track
- verifying that V6 and V7 share one XFS device and that a real cross-root
  reflink works
- keeping explicit capture failures visible in the attempt denominator without
  projecting them into a cohort
- requiring `capture_error` to contain exactly `error_type` and `error_code`,
  with `error_code = capture_failed`
- running full tree identity checks before a failed attempt can take the visible
  non-fatal rejection path

ChatGPT Pro verified the completed V0.7 source package as `140/140` tests and
returned `GO for read-only 30-minute prospective shadow monitoring`. That GO
explicitly does not assert profitability or true edge.

Two independent Codex reviews subsequently challenged the privileged ACL
repair design. They first found a pathname TOCTOU and then a missing
same-filesystem constraint. The final helper uses no-follow `openat`, stable
inherited file descriptors, pre/post device-inode-type-link checks, root-device
confinement at every directory and file boundary, and same-FD ACL rollback on
failure. Both independent final reviews returned `GO`.

The final ACL build was returned to ChatGPT Pro for one more narrow review. It
verified the `1,096,531` byte package and SHA-256, ran all `142/142` V0.7 tests
on Linux, ran helper probes for symlink, hardlink, and wrong-device rejection,
and returned `GO for this ACL deployment fix`. Its container lacked real ACL
xattr tools, which it disclosed; the actual grant/read/no-write canary was run
independently on the Amazon Linux host.

## Host and systemd boundaries

- Region: `eu-west-1`
- Instance: `i-045bb69f9cba2dadd`
- Host: Amazon Linux 2023, `t3.small`
- CPU credit mode: `standard`
- Install root: `/opt/poly-mm-v07`
- Runtime root: `/var/lib/poly-mm-v07`
- V6 source root: `/var/lib/poly-mm-v06` (read-only to V7 runtime units)
- Runtime user: `polybotv07` with supplementary public-source group
  `polybotv06`

An earlier deployment automation briefly changed CPU credits to `unlimited`
without the required cost authorization. It was immediately restored to
`standard`; the final verified mode is `standard`. The near-zero-credit
capacity risk remains.

The performance service has no network address family beyond `AF_UNIX`, denies
all IP traffic, loads no environment file, retains no capabilities, and has
`ReadOnlyPaths=/var/lib/poly-mm-v06`. It writes only under
`/var/lib/poly-mm-v07`.

Two pre-activation install attempts failed closed before changing the active
install: the instance role could not directly read the new S3 object, and a
Windows-created Git index required a Linux checkout refresh. The final transfer
used a short-lived presigned URL and was verified by SHA-256 before activation.

V6 checkpoints are atomically replaced with mode `0600`. A separate root
oneshot repairs only read ACL metadata on single-link JSON checkpoints in
finalized post-cutoff attempts before each performance refresh. It retains only
`CAP_DAC_READ_SEARCH` and `CAP_FOWNER`, has no network access, is confined to
the exact V6 runs path, and never changes capture content. The V7 runtime user
was verified to read but not write a repaired checkpoint.

At `2026-08-16T19:04Z`, after the first unattended post-fix refresh:

- source ACL unit: `Result=success`, `ExecMainStatus=0`
- performance service: `Result=success`, `ExecMainStatus=0`
- health: `healthy=true`, `failures=[]`
- performance timer: enabled and active
- health timer: enabled and active
- V6 source service: active; its PID was not restarted by this deployment
- installed Git tree: clean
- most recent unattended refresh: started at `19:00:32Z`, completed at
  `19:02:18Z`, `Result=success`, `ExecMainStatus=0`
- next performance timer: `19:30:08Z`
- free bytes at the final manual canary: `18,775,560,192`
- required minimum: `12,884,901,888`

## Scheduled-run incident record

The monitoring path was observed rather than accepted from manual canaries
alone.

- The initial 15:00 scheduled run completed successfully with no post-cutoff
  finalized capture yet.
- The first failed post-cutoff capture showed that hard-failing every explicit
  `capture_error` would permanently stop monitoring. The corrected status keeps
  failed attempts visible while excluding them from projection, cohort, PnL,
  cluster, and 100-trade counts.
- A later unattended run completed successfully on the visible-rejection build.
- The 18:00 scheduled run failed closed with `PermissionError`. Health became
  unhealthy with `status_phase_unhealthy` and `source_accounting_invalid`; it
  did not retain an old success or generate economic claims.
- Root cause was directly reproduced: atomic V6 checkpoint replacement left
  `user:polybotv07:r-x` present but with `mask::---`, making the named entry
  ineffective.
- The final ACL helper was deployed and its manual performance and health
  canaries succeeded. A formerly unreadable checkpoint now has
  `user:polybotv07:r--`; an explicit V7-user write test still fails.

- The first unattended post-fix performance timer fired at `19:00:32Z` and
  completed at `19:02:18Z` with `Result=success` and `ExecMainStatus=0`.
  Its dependent ACL unit also returned success, the following health snapshot
  was healthy with no failures, and the next performance run remained queued
  for `19:30:08Z`.

## Verification

Local final code acceptance:

- V7 model/evaluation/replay/assets/counterfactual/shadow/deployment suite:
  `142` collected, `141 passed`, `1 skipped`
- the skip is the deliberately Linux-only FD/device/path-swap ACL test
- Ruff `E`, `F`, and `I`: passed
- Python compilation: passed
- Bash syntax: passed
- `git diff --check`: passed
- independent spec review: `GO`
- independent security/standards review: `GO` after two evidenced corrections

Server Linux acceptance:

- pre-ACL final V7 suite: `140 passed` in `498.56s`
- final Linux deployment suite, including FD path-swap, wrong-device, and ACL
  rollback behavior: `9 passed` in `2.24s`
- the first complete `142`-test run exposed the sparse-release omission above:
  `141 passed`, `1` asset-file failure; the failure was retained rather than
  misreported as a pass
- after restoring those exact frozen Git blobs, the complete suite passed:
  `142 passed` in `340.79s`; the installed Git tree remained clean and both V7
  timers plus the V6 source service remained active
- `systemd-analyze verify`: no V7 unit error; the only output was an unrelated
  legacy `acpid.socket` `/var/run` warning
- optional pytest asyncio/timeout plugins are absent on the host, producing two
  configuration warnings and one unknown-timeout-mark warning; local tests did
  have timeout enforcement

ChatGPT Pro acceptance:

- final complete pre-ACL V0.7 source bundle: `140/140 passed`
- final ACL V0.7 source bundle: `142/142 passed`
- final ACL deployment verdict: `GO`
- package size, SHA-256, entry count, and CRC: matched
- its environment lacked pytest-timeout, which it disclosed rather than
  claiming timeout-enforced tests

## Current prospective state and data-quality blocker

At the unattended `2026-08-16T19:02:17Z` heartbeat:

- `phase`: `warming_up`
- `source_summary_file_count`: `191`
- `source_post_cutoff_attempt_count`: `15`
- `source_finalized_clean_count`: `1`
- `source_rejected_capture_error_count`: `14`
- `history_seed_count`: `1`
- `projected_count`: `1`
- `case_count`: `0`
- `cohort_admission_count`: `0`
- `qualified_net_pnl`: `null`
- `true_edge`: `false`
- `positive_100_trade_check`: `false`
- `orders_submitted`: `0`
- `authenticated_endpoints_used`: `0`
- `data_quality_complete`: `false`

The source capture failure rate is therefore `14/15 = 93.3%`. The five most
recent inspected failures all had two `RecorderWorkerError` legs while their
manifest/raw integrity arrays were clean. The summaries deliberately sanitize
the underlying recorder error code, so this report does not claim a uniquely
proved software root cause.

Host telemetry is a severe correlated risk. CloudWatch showed CPU credit
balance near zero (latest observed five-minute average `~0.000068`) and CPU
utilization pinned near the T3 baseline (`~20%`) while credit mode remained
`standard`. Enabling `unlimited`, resizing the instance, or stopping another
track would affect cost or existing services and was not inferred from the V7
deployment authorization.

No V0.7 economic case can be admitted until enough clean finalized captures
exist. The data recorder and host-capacity issue is therefore the immediate
blocker, ahead of model tuning.

For comparison, the latest V0.6 validation summary generated at
`2026-08-16T15:36:14Z` reports `126` development-shadow trades, `122` settled,
and `+179.16470800` net shadow PnL. That attractive headline is not qualified
evidence: there were only `2` qualified economic attempts, their observed net
PnL was `-10.01115600`, the 95% bootstrap lower value was `-10.01115600`, the
maximum single-event PnL share was `1`, and `qualified_net_pnl` remained null.
Thus even the old track's positive result after more than 100 shadow trades does
not satisfy the user's 100-trade acceptance criterion.

## Evidence boundary and next work

This track can prove that a fixed, paper-only V0.7 implementation processed
prospective public captures under explicit safety and provenance checks. It
cannot prove actual fills, market impact, live slippage, realized trading PnL,
or true edge.

Required next work, in order:

1. restore reliable source capture and retain the underlying safe recorder
   error code for diagnosis
2. establish a prospective pre-label lock/journal protocol before test actions
3. collect at least 100 independent settled expiry clusters and 100 explainable
   economic attempts on one frozen track
4. require positive qualified PnL, a positive cluster-bootstrap lower bound,
   acceptable concentration, and model-vs-market calibration
5. quantify two-leg execution, partial-fill, unwind, and latency loss before any
   separately authorized tiny live experiment

Until those gates pass, the correct claim is: **the V0.7 monitoring
infrastructure is deployed, but profitable edge is not established**.
