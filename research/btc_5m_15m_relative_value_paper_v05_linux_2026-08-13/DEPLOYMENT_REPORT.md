# AWS Linux paper v0.5 deployment report

Deployed at `2026-08-13T06:28:04Z` (`2026-08-13 14:28:04+08:00`).

## Immutable identities

- Release commit (`.deployment-revision`):
  `e7fec37b68452ae8b4b755326ed5282d211e1542`
- Frozen implementation revision (`.implementation-revision`, preregistration
  `repository_head`): `99bdbd67c7a4e82347bbf3d3b78e7980e15d323c`
- Runtime archive SHA-256:
  `7e5587a0ef849a00c2650aa0ba6a23bd8337a3076d9e21d618309ac1d8d08e4d`
- Frozen strategy SHA-256:
  `c96f89c87db0ec4e28b500f56ea32d7da9d9087fbc11ce738a7e737fa2921825`
- Evidence track: `btc-paper-v05-20260813` (prospective-only after
  `2026-08-13T06:00:00Z`)
- Archive inspection verified 1,479 runtime text files (`.py`, `.sh`, `.json`,
  `.toml`, `.service`, `.timer`) had zero CRLF sequences, both revision
  markers matched, and the strategy bytes matched the preregistration before
  upload. Two known CRLF legacy blobs (`.env.example`, one `thoughts/` note)
  are outside the runtime slice and were left untouched.

## AWS host

- Region: `eu-west-1`
- Instance: `i-045bb69f9cba2dadd` (shared with the untouched v0.4 track)
- Isolation: `/opt/poly-mm-v05` (root-owned read-only) and
  `/var/lib/poly-mm-v05` (service-owned), service account `polybotv05`
- The private Git credential was never copied to the host. A private
  AES-256-encrypted S3 transfer bucket delivered the content-addressed
  archive; the object and bucket were deleted immediately after extraction
  and hash acceptance on the host.
- systemd hardening: `ProtectSystem=strict`, read-only install root, IMDS
  blocked via `IPAddressDeny=169.254.169.254` plus
  `AWS_EC2_METADATA_DISABLED=true`, empty capability bounding set.

## Verification before start

- Local focused suite: 169 btc_twap tests passed on Windows.
- Full local regression matched the `main` baseline failure set exactly
  (206 known Windows-environment failures, zero introduced by v0.5; the
  affected files are unrelated `fcntl`/`py_clob_client`/permission suites).
- Ruff E/F/I passed on every file changed by the v0.5 branch.
- Bootstrap `--validate-only`: `valid=true`, `paper_only=true`,
  `public_only=true`, `new_orders_disabled=true`, `orders_submitted=0`,
  `authenticated_endpoints_used=0`.
- Bootstrap asserted both revision markers and the strategy hash on-host
  before installing units; the run was staged-only and started manually.

## Runtime acceptance

- `polymm-btc-twap-paper-v05.service`: active and enabled
- `polymm-btc-twap-paper-v05-health.timer`: active and enabled
- `polymm-btc-twap-paper-v04.service` and its health timer: still active;
  v0.4 report root untouched (208 reports at acceptance)
- Service PID 56220 environment contained only standard systemd variables,
  `PYTHONUNBUFFERED`, and `AWS_EC2_METADATA_DISABLED`; no credential,
  wallet, signing, or proxy variables existed.
- chrony: Amazon Time Sync `169.254.169.123`, system offset ~13 microseconds,
  leap status Normal.
- First health snapshot: `healthy=true`, fresh heartbeat, empty
  `guard_missing_fields`/`guard_unsafe_fields`, `paper_only_guard_valid=true`,
  phase `capturing`, 46.6 GB free disk.
- Public capture grew ~58 KB over a ten-second probe immediately after start.
- First target expiry: `1786604400` = `2026-08-13T07:00:00Z`
  (`15:00+08:00`); the capture root opened at `06:27:37Z`, about 32 minutes
  of lead time.

## First-cycle observations (monitored 06:28Z-07:25Z)

- Cycle `1786604400` (07:00:00Z expiry): all four tau reports were generated
  at ~07:08Z with schema `btc-5m-15m-relative-value-pilot-report.v2` and
  `verified_report_v2=true`. Every tau, including TAU240, reported
  `predictor_points_available=300` and `raw_shadow_model.available=true`
  (the first capture root opened 32 minutes early, covering the full
  lookback). Shadow decisions were `no_trade` with clean reason codes.
- Cycle `1786605300` (07:15:00Z expiry): the capture root opened at
  `07:06:36Z`, 35 seconds after the left edge of the TAU240 predictor
  window — exactly the late-start geometry that starved v0.4. Same-host,
  same-cycle comparison of the two code lines:
  - v0.4 (old resampler): `predictor_points_available=0`,
    `available=false`, `causal_predictor_history_missing`
  - v0.5 (leading-prefix resampler): `predictor_points_available=263`,
    `available=true`, empty cycle reason codes
  The previously disabled quarter of the decision surface is confirmed
  recovered, while internal gaps over five seconds still fail closed.
- Qualified and OOS tracks correctly failed closed with
  `past_only_calibration_insufficient`: the prospective track has no
  strictly-prior v0.5 calibration observations yet, and pre-v0.5 evidence
  is banned from training by the preregistration.
- Health monitor: 10 snapshots in the first hour, all `healthy=true` with
  empty `failures`; the per-field guard block reported
  `paper_only=true`, `public_only=true`, `new_orders_disabled=true`,
  `orders_submitted=0`, `authenticated_endpoints_used=0`, no missing and
  no unsafe fields.
- Resource headroom with both tracks running on the shared t3.small:
  ~200 MB RSS per service, ~1.1 GB memory available, load average ~0.4,
  46.6 GB free disk.
- v0.4 remained healthy and untouched throughout (224 reports and
  growing; its health snapshot stayed `healthy=true`).
- Zero `error`, `exception`, `traceback`, or `failed` entries appeared in
  the v0.5 service journal during the monitoring window.

## Interpretation boundary

At deployment time there is no finalized v0.5 cycle, so there is no v0.5
shadow, qualified, or OOS PnL. `null` is not zero. The v0.5 lane is
prospective-only: pre-v0.5 tracks (including all v0.4 evidence) never enter
training, calibration, qualified PnL, or OOS claims. The frozen promotion
gates remain 2,000 resolved markets, 500 simulated trades, 500 explainable
fills, cluster-bootstrap lower bound above zero, OOS Brier beating the
market baseline, ECE at or below 0.05, and the single-event concentration
cap in `PREREGISTRATION.json`.
