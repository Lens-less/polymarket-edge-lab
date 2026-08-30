# V0.2 Operator Runbook

This runbook covers the source-checkout, offline-safe WP6 preview surface added for V0.2. Run the commands from the repository root after `uv sync --locked --extra dev`. It does not authorize live trading by itself, and `polymm doctor` is intentionally fail-closed.

## Commands

```text
polymm doctor
polymm scan --strategy arbitrage
polymm replay --strategy maker
polymm shadow --strategy maker
polymm desk
polymm status
```

No `--live` switch exists. In v0.2.0, `scan`, `replay`, `shadow`, `desk`, and `status` are deterministic acceptance/demo reports or local previews, not production market/config-backed workflows. Canary would require reviewed configuration, external evidence, externally verifiable probe/drill provenance, and explicit human authorization; the offline preview cannot produce that authorization.

## What `doctor` checks

`polymm doctor` produces a machine-readable `GateReport` for the Canary gate. The exact conditions are:

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

The command never reads or prints credential values. It only consumes sanitized JSON/config inputs. Unsigned local probe and drill documents are advisory: editing them to `READY`, including adding self-asserted attestation fields, cannot make offline `doctor` return `LIVE_CANARY_READY`.

## Default behavior

Running `polymm doctor` with no arguments loads `config/v0.2/canary.template.json` and returns `LIVE_BLOCKED`.

That is expected. The template has tiny budget defaults but leaves live authorization, strategy qualification, live probes, and drills unresolved on purpose.

## Config flow

1. Copy the template from `config/v0.2/canary.template.json`.
2. Replace placeholder strategy and whitelist values.
3. Record reviewed authorization fields only after a human approves the exact plan.
4. Generate sanitized live probe and fault-drill JSON using the schemas under `docs/reports/`.
5. Run `polymm doctor --config path/to/canary.json` for a diagnostic blocker report.
6. Treat any local `READY` receipt as advisory until a future external attestation verifier is implemented and separately reviewed.

## Expected statuses

- `RESEARCH`: `PASS` or `NO_GO`
- `SHADOW`: `PASS` or `NO_GO`
- `CANARY`: `LIVE_BLOCKED` or `LIVE_CANARY_READY`
- `LIMITED_LIVE`: `LIVE_LIMITED_READY` or `NO_GO`

`LIVE_CANARY_READY` is reserved by the schema for a future externally verified path; offline v0.2.0 `doctor` remains `LIVE_BLOCKED`. Software completion is not profitability completion. `LIVE_LIMITED_READY` still requires the exact profitability and reconciliation evidence from the spec.

## Fault drills

The fault drill result must cover:

- `kill_switch_manual_reset`
- `restart_recovery`
- `user_stream_disconnect_cancel_all`
- `heartbeat_timeout_blocks_new_orders`
- `reconciliation_mismatch_blocks_new_orders`

If any one of them is missing or blocked, Canary remains `LIVE_BLOCKED`.
