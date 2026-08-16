# BTC 5m/15m relative-value paper v0.7 shadow deployment report

Generated on 2026-08-16 after deploying the prospective-only v0.7 shadow track to
the live EC2 host that already runs v0.6 public-paper capture.

## Release and artifact

- Remote `main` commit: `f519af657cabe5ade3fee6969e4c812ad8fe3603`
- Frozen preregistration baseline: `c557160d0c98e195a988f4353bbe19a3b00b3576`
- Deployment artifact: `s3://poly-quant-artifacts-998948477566-eu-west-1/deploy/poly-mm-v07/2026-08-16/poly-mm-v07-deploy-f519af657cabe5ade3fee6969e4c812ad8fe3603.tar.gz`
- Deployment artifact size: `806,486` bytes
- Deployment artifact SHA-256: `5ec4d91ff52f7359c08777d12b83bd8751fce8e99a6320520f2286dfb344d61a`

## Host and bootstrap result

- Region: `eu-west-1`
- Instance: `i-045bb69f9cba2dadd`
- Host type: `t3.small`
- Credit mode after deployment: `unlimited`
- Install root: `/opt/poly-mm-v07`
- Data root: `/var/lib/poly-mm-v07`
- Source root preserved read-only: `/var/lib/poly-mm-v06`

Bootstrap succeeded from a source archive because the instance cannot clone the
private GitHub repository directly. The bootstrap run completed all of the
following:

- extracted the exact release artifact under `/opt/poly-mm-v07`
- wrote `.deployment-revision` = `f519af657cabe5ade3fee6969e4c812ad8fe3603`
- wrote `.implementation-revision` = `c557160d0c98e195a988f4353bbe19a3b00b3576`
- rebuilt the venv and installed the package editable
- validated the frozen v0.7 shadow config
- installed the health/performance systemd units

## Verification

Local merged-main verification before deployment:

- `pytest tests/test_btc_twap_relative_value_v07_shadow.py tests/test_btc_twap_relative_value_v07_deployment_assets.py tests/test_btc_twap_relative_value_v07_counterfactual.py -q` -> `58 passed`
- `ruff check ...` on all V7 shadow files -> passed
- `python -m compileall src/edge_lab/btc_twap_relative_value_v07_shadow.py scripts/run_btc_twap_relative_value_v07_shadow.py` -> passed
- `git diff --check c2eb29f3cc527ebd53b9d9f9cece7558ac70ef1d..HEAD` -> passed

Server verification after bootstrap:

- `systemd-analyze verify` on all v0.7 shadow units -> no v0.7 unit errors
- `polymm-btc-twap-paper-v07-performance.timer` -> enabled and active
- `polymm-btc-twap-paper-v07-health.timer` -> enabled and active
- first manual `performance.service` run -> success
- first manual `health.service` run -> success

Observed timer schedule immediately after enable:

- performance timer next trigger: `2026-08-16 13:30:06 UTC`
- health timer next trigger: `2026-08-16 13:25:00 UTC`

## First observed runtime state

At `2026-08-16T13:22:42Z`, the deployed shadow status recorded:

- `mode`: `prospective_actual_market_counterfactual_shadow`
- `phase`: `warming_up`
- `source_finalized_count`: `169`
- `source_eligible_count`: `0`
- `projected_count`: `0`
- `case_count`: `0`
- `report_status`: `warming_up`
- `orders_submitted`: `0`
- `live`: `false`
- `true_edge`: `false`
- `qualified_net_pnl`: `null`

At `2026-08-16T13:22:43.356177Z`, the health snapshot recorded:

- `healthy`: `true`
- `source_v06_active`: `true`
- `free_bytes`: `21,159,469,056`
- `minimum_free_bytes`: `12,884,901,888`

This is the expected initial state because the frozen cutoff is
`2026-08-16T15:00:00Z`, so no post-cutoff eligible v0.6 captures existed yet.

## Important boundary

This deployment is a prospective-only shadow track. It is not authorized to:

- place orders
- read trading credentials
- claim qualified PnL
- claim proven edge
- override the v0.6 public-paper safety boundary

Until post-cutoff captures accumulate and the track leaves `warming_up`, this
deployment should be treated as an infrastructure and evidence-collection lane,
not as proof of profitability.
