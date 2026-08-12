# AWS Linux paper v0.4 deployment report

Deployed at `2026-08-12T16:59:27Z` (`2026-08-13 00:59:27+08:00`).

## Immutable identities

- Runtime commit: `ef52ca76165873e4db0307114c7906d964199510`
- Runtime archive SHA-256:
  `ed00227b89ae73195b934c3ad2cd223a6faccb1aa24025c21ee1289bd9d3cdca`
- Frozen strategy SHA-256:
  `11abd4022c7769344b32e17bdec2e45c46ce9a5185e88459ae2a8e38520eb132`
- Archive inspection verified 138 Linux text files had zero CRLF sequences and
  the strategy bytes matched the preregistration before upload.

## AWS host

- Region: `eu-west-1`
- Instance: `i-045bb69f9cba2dadd`
- Instance type: `t3.small`
- AMI: Amazon Linux 2023 `ami-062a8901a5ddcf280`
- Root storage: encrypted 50 GiB gp3; 47 GiB available after deployment
- Instance profile: `poly-quant-ec2-ssm-profile`, limited to
  `AmazonSSMManagedInstanceCore`
- Security group: `sg-00f7286f8f96ec934`, zero inbound rules
- IMDSv2 required; hop limit 1; metadata tags disabled
- API stop and termination protection enabled
- No SSH key or inbound SSH rule was created. Administration uses SSM.

The private Git repository credential was never copied to the host. A private,
AES-256-encrypted S3 transfer bucket delivered the content-addressed archive;
all four temporary objects and the bucket were deleted after acceptance.

## Runtime acceptance

- `chronyd.service`: active and enabled
- `polymm-btc-twap-paper-v04.service`: active and enabled
- `polymm-btc-twap-paper-v04-health.timer`: active and enabled
- Runtime phase: `capturing`
- PID at acceptance: `28554`
- Current first expiry: `1786555800` = `2026-08-13 01:30:00+08:00`
- First finalized summary is expected only after the six-minute settlement grace
  following that expiry.
- Heartbeat health: valid hash, process alive, no failures, `last_error=null`
- Latest direct chrony evidence: offset `-0.000004366s`, uncertainty `1ms`,
  Amazon reference `169.254.169.123`, leap status `Normal`
- Paper guard: `paper_only=true`, `public_only=true`,
  `new_orders_disabled=true`, `orders_submitted=0`,
  `authenticated_endpoints_used=0`
- Service process environment keys contained only standard systemd variables and
  `PYTHONUNBUFFERED`; no credential, wallet, signing, or proxy variables existed.
- Public capture grew from 2,406,692 to 2,492,071 bytes over a ten-second probe.
- Two distinct active CLOB partial streams and two distinct active RTDS partial
  streams were observed, confirming both redundant recorder sessions are writing.
- No `error`, `exception`, `traceback`, or `failed` entry appeared in the service
  journal during acceptance.

## Verification

- Local focused suite: 70 passed.
- Amazon Linux focused suite: 65 passed.
- Ruff E/F/I, Python compilation, shell syntax, and two-axis standards/spec review
  passed.
- The complete Windows test suite remains non-actionably blocked during collection
  by Linux-only `fcntl` imports and a missing local `py_clob_client`; the affected
  deployment slice passed on Amazon Linux with `py_clob_client` installed.

## Monitoring

- The instance writes `health-latest.json` every five minutes and retains
  timestamped snapshots under `/var/lib/poly-mm/monitor/history/`.
- The health document surfaces runtime state, frozen clock-policy failures,
  CLOB/RTDS connection counters, redundant-leg degradation, development-shadow
  PnL, qualified PnL, and promotion gaps once a validation summary exists.
- Codex heartbeat automation `btc-paper-aws-30` performs an independent read-only
  AWS/SSM inspection every 30 minutes in the deployment task.

## Interpretation boundary

At deployment time, there is no finalized v0.4 cycle and therefore no v0.4
shadow or qualified PnL. `null` is not zero. One overnight run is an operational
shakeout, not enough evidence for live trading; the frozen promotion gates remain
2,000 resolved markets, 500 simulated trades, 500 explainable fills, and the OOS
quality/profitability constraints in `PREREGISTRATION.json`.

Two failed pre-start source trees remain recoverably isolated at
`/opt/poly-mm-failed-0d409ed-crlf` and
`/opt/poly-mm-failed-242c2b8-strategy-hash`. They contain no strategy observations
and may be removed after the successful service has remained stable.
