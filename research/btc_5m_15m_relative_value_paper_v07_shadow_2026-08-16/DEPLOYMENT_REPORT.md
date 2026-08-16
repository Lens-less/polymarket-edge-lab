# BTC 5m/15m relative-value paper v0.7 shadow deployment report

Updated on 2026-08-16 after external review, independent correction, and an
Amazon Linux 2023/XFS canary on the EC2 host that already runs the frozen v0.6
public-paper capture service.

## Final release and artifacts

- GitHub `main` code/deployment commit: `4061913f13279721af0a48ce063ea5caa5310517`
- Frozen v0.7 implementation baseline: `c557160d0c98e195a988f4353bbe19a3b00b3576`
- Final deployment artifact:
  `s3://poly-quant-artifacts-998948477566-eu-west-1/deploy/poly-mm-v07/2026-08-16/poly-mm-v07-git-release-4061913f13279721af0a48ce063ea5caa5310517-lf.tar.gz`
- Artifact format: sparse, shallow Git checkout with the required runtime,
  research, deployment, and test paths; the installed tree retains `.git`.
- Artifact size: `3,087,212` bytes
- Artifact SHA-256:
  `096a46b6c5f09eb103fd7acb71de445e925ec963012c06b4dacff0931d394703`
- S3 server-side encryption: `AES256`
- Release-diff high-confidence secret findings: `0`
- Workspace credential-regex finding files: `0`

The source bundle supplied to ChatGPT Pro was:

- baseline: `36a09c4d9c49b540ea3a61b181fae50bcfa3a32b`
- file: `poly-mm-v07-deployment-36a09c4-20260816T1228Z.zip`
- size: `1,329,957` bytes
- SHA-256:
  `f61437c485652ab7a1936c11ad5d282167750d7497ee6a6f0a21c36f44bd3ffa`
- high-confidence secret findings before upload: `0`

## External review and corrections

ChatGPT Pro deployment review:
<https://chatgpt.com/c/6a81b3fd-3a3c-83ec-bdda-d7056e03d5c3>

The reviewer returned `NO-GO` for the submitted adapter and supplied a
`44,232` byte patch with SHA-256
`1c14a5f6b5ec5462402084e339f86c42c411be5359e2a8904bd9cb894a134aa8`.
The important findings were independently reproduced and corrected:

- the first normal post-cutoff capture may lack the exact 15-minute opening
  boundary, so it becomes a history seed rather than a train case
- V6 summary claims are no longer trusted as the integrity proof;
  `CaptureStore.audit_integrity()` rechecks manifests and raw checksums
- V6 service status SHA-256, phase, evidence-track ID, safety flags, config
  schema, settlement regime, and generated time are revalidated
- complete source-tree identities are checked before and after reflink
  projection; symlinks, hard links, path escape, and source mutation fail closed
- only the immediately preceding capture is used as history, avoiding quadratic
  history amplification
- impossible builder authority claims are suppressed on the no-journal track
- health now fails for failed status, invalid source validation, non-null
  qualified PnL, true-edge/100-trade/prelabel guard drift, future heartbeat, or
  authenticated endpoint use
- bootstrap now requires a real clean Git checkout and XFS on both V6 and V7
  roots; marker-only source archives are rejected

The external patch was not applied blindly. Its silent skipping of bad
post-cutoff captures conflicted with the stricter current contract, so the
integrated implementation keeps all post-cutoff schema, integrity, failure, and
provenance violations fail-closed. A scoped independent re-review ended `GO`.

## Host and activation result

- Region: `eu-west-1`
- Instance: `i-045bb69f9cba2dadd`
- Host: Amazon Linux 2023, `t3.small`
- CPU credit mode: `standard`
- Install root: `/opt/poly-mm-v07`
- Runtime root: `/var/lib/poly-mm-v07`
- V6 source root: `/var/lib/poly-mm-v06` (read-only to V7 units)

An earlier automation briefly changed CPU credits to `unlimited` without the
required cost authorization. The responsible agent corrected this immediately;
the final and verified mode is `standard`. The low-credit resource risk remains.

Final activation completed before the frozen prospective cutoff:

- deployment revision:
  `4061913f13279721af0a48ce063ea5caa5310517`
- implementation revision:
  `c557160d0c98e195a988f4353bbe19a3b00b3576`
- installed Git worktree: clean after the two known release markers were written
- V6 static config/unit combined SHA-256 before and after:
  `eb61f901c4fffebca8eed07e8e1bfcc8b4a2a9d42b1d45dde4dd26e850e7486a`
- V6 PID before and after: `148862`
- XFS and `cp --reflink=always` positive-path probe: passed
- `systemd-analyze verify`: no V7 unit error
- performance timer: `enabled/active`
- health timer: `enabled/active`
- manual performance canary: `success`
- manual health canary: `success`

Two pre-activation attempts were rejected without touching the active install:
the instance role could not read a new S3 object directly, and a Windows-created
Git index required a Linux checkout refresh. The active old V7 timers remained
running during both preflight failures. The final artifact was transferred via a
short-lived presigned URL and verified by SHA-256 before activation.

## Verification

Local acceptance on the integrated release:

- all V7 tests: `133 passed`
- focused shadow/deployment tests after the final phase allowlist fix:
  `45 passed`
- Ruff `E`, `F`, and `I`: passed
- Python compileall: passed
- Bash syntax: passed
- systemd calendar parsing for `*:0/30` and `*:0/5`: passed
- `git diff --check`: passed

The full Windows repository suite was also attempted earlier, but collection is
not a valid cross-platform gate for POSIX-only modules (`fcntl`) and legacy tests
whose optional `py_clob_client` dependency was absent. That attempt is not
reported as a product pass. The focused Linux/systemd canary above is the
deployment evidence.

## State immediately before the prospective cutoff

At `2026-08-16T14:54:28Z`:

- `phase`: `warming_up`
- `source_summary_file_count`: `175`
- `source_finalized_count`: `0`
- `history_seed_count`: `0`
- `case_count`: `0`
- `orders_submitted`: `0`
- `authenticated_endpoints_used`: `0`
- `live`: `false`
- `qualified_net_pnl`: `null`
- `true_edge`: `false`
- `positive_100_trade_check`: `false`

At `2026-08-16T14:54:30.327824Z`, health was `healthy=true`, source V6
was active, shadow validate-only returned `0`, and free space was
`20,653,350,912` bytes versus a `12,884,901,888` byte minimum.

The first scheduled performance firing on the final release started at
`2026-08-16T15:00:49Z` and completed successfully at `15:00:54Z`. It
correctly remained at zero cases because no capture that started at or after
the `15:00:00Z` cutoff had finalized yet. The next scheduled performance
firing was `2026-08-16T15:30:06Z`.

## Evidence boundary

This is a prospective public-data counterfactual/shadow track. It cannot place
orders, read credentials, or prove realized trading PnL. Because this track has
no prelabel lock journal, `true_edge` must remain `false`, qualified PnL must
remain `null`, and the authoritative 100-trade gate must remain `false` even if
diagnostic counterfactual PnL becomes positive.

The first ordinary post-cutoff V6 capture is expected to be history-only when it
started after its own 15-minute opening. The next builder-eligible expiry becomes
train, followed by validation and test. No pre-cutoff capture may be used to make
that first case appear eligible.
