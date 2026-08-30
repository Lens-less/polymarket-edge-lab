# Live Probe Result Schema

Schema version: `polymm-live-probe-result.v0.2`

This is a sanitized live-probe receipt that `polymm doctor` consumes for diagnostics. It must never include private keys, signatures, passphrases, or raw credential material.

In v0.2.0, local JSON is not authoritative proof. A `READY` field, or a self-asserted `provenance_attestation`, cannot unlock Canary because offline `doctor` has no external attestation verifier. The document remains advisory until such a verifier is implemented and reviewed.

## Top-level fields

- `schema_version`
- `report_kind` = `live_probe_result`
- `generated_at`
- `status`
- `checks`

## Check item shape

- `check_id`
- `label`
- `status` = `READY` or `BLOCKED`
- `detail`
- `evidence`

## Required check IDs

- `geoblock`
- `credentials_signing`
- `balance_allowance`
- `market_constraints`
- `user_stream`
- `heartbeat`
- `reconciliation`
