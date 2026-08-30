# Gate Report Schema

Schema version: `polymm-gate-report.v0.2`

`GateReport` is the shared machine-readable contract for:

- `RESEARCH`
- `SHADOW`
- `CANARY`
- `LIMITED_LIVE`

## Top-level fields

- `schema_version`
- `report_kind` = `gate_report`
- `generated_at`
- `gate`
- `status`
- `strategy_id`
- `summary`
- `blocking_check_ids`
- `remaining_conditions`
- `checks`
- `context`

## Status rules

- `RESEARCH`: `PASS` or `NO_GO`
- `SHADOW`: `PASS` or `NO_GO`
- `CANARY`: `LIVE_BLOCKED` or `LIVE_CANARY_READY`
- `LIMITED_LIVE`: `LIVE_LIMITED_READY` or `NO_GO`

`LIVE_CANARY_READY` is reserved for an externally verified path. The v0.2.0 offline `doctor` does not verify provenance attestations, so unsigned or self-asserted local probe/drill JSON always remains fail-closed.

## Check shape

Each item in `checks` contains:

- `check_id`
- `label`
- `status` = `READY` or `BLOCKED`
- `detail`
- `evidence`

## Canary check IDs

- `explicit_user_authorization`
- `geoblock`
- `credentials_signing`
- `balance_allowance`
- `market_constraints`
- `user_stream`
- `heartbeat`
- `reconciliation`
- `risk_config`
- `qualification`
- `kill_restart_drills`
