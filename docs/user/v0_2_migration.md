# V0.2 Migration Notes

V0.2 moves the main `polymm` entrypoint to `src.profit_system.cli:main` and replaces ad hoc readiness checks with a fail-closed WP6 surface.

## What changed

- `polymm doctor` now emits a machine-readable Canary `GateReport`, and offline inputs cannot authorize live.
- The default environment reports `LIVE_BLOCKED` instead of implying readiness.
- `polymm scan`, `polymm replay`, and `polymm shadow` emit deterministic offline acceptance reports rather than real market/config-backed scans or replays.
- `polymm desk` and `polymm status` expose operator-facing summaries and local preview surfaces without enabling live mutation.
- Canary configuration now lives under `config/v0.2/` with tiny numeric budgets and no secrets in repo.

## Migration steps

1. Update any local scripts that assumed `polymm` had no subcommands.
2. Replace old live-readiness shortcuts with `polymm doctor`.
3. Store sanitized live probe and fault drill results as JSON documents instead of mixing them into shell notes.
4. Keep secrets outside repo-managed config. The canary template intentionally references only sanitized report paths.
5. Treat this checkout as a source-based preview; `uv build` is for validation only, not a promise of PyPI or standalone wheel support.

## Compatibility notes

- There is no `--live` fast path.
- Missing external evidence is a valid completed outcome and surfaces as `LIVE_BLOCKED`.
- Decimal values in JSON outputs are serialized as strings to stay stable and lossless.
- The preview commands are deterministic and local-first; they do not authorize live trading or replace fresh market evidence.
