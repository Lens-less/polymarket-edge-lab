# Fault Drill Result Schema

Schema version: `polymm-fault-drill-result.v0.2`

This document is the offline-safe receipt that `polymm doctor` consumes for drill diagnostics. In v0.2.0, a local `READY` value remains advisory and cannot unlock Canary without an external attestation verifier.

## Top-level fields

- `schema_version`
- `report_kind` = `fault_drill_result`
- `generated_at`
- `status`
- `drills`

## Drill item shape

- `drill_id`
- `status` = `READY` or `BLOCKED`
- `detail`
- `evidence`

## Required drill IDs

- `kill_switch_manual_reset`
- `restart_recovery`
- `user_stream_disconnect_cancel_all`
- `heartbeat_timeout_blocks_new_orders`
- `reconciliation_mismatch_blocks_new_orders`
