# BTC 5m/15m relative-value paper v0.7 shadow deployment spec

Status: deployment-only shadow track. This is not a profitability claim.

Mode

- `prospective_actual_market_counterfactual_shadow`
- reads only finalized v0.6 runs from `/var/lib/poly-mm-v06/.../runs`
- writes only under `/var/lib/poly-mm-v07`
- never places orders, never reads credentials, never enables live trading

Frozen boundaries

- Install root: `/opt/poly-mm-v07`
- Data root: `/var/lib/poly-mm-v07`
- Source root remains read-only: `/var/lib/poly-mm-v06`
- Frozen preregistration path:
  `/opt/poly-mm-v07/research/btc_5m_15m_relative_value_counterfactual_v07_2026-08-15/PREREGISTRATION.json`
- Frozen preregistration SHA-256:
  `de79f3e4d43b513a7c71e3196877ee110f04f7d31cec4b3cd060f6184f541bfe`

Prospective gate

- Only source captures with `capture_started_at_ms >= 1786892400000` may enter.
- Absolute cutoff: `2026-08-16T15:00:00Z`.

Selection policy

- decision taus: `60, 120, 180, 240`
- `train_case_count = 1`
- `validation_case_count = 1`
- `maximum_cases = 102`
- `snapshot_mode = reflink_required`
- `minimum_free_bytes = 12884901888`
- The configured V6 source root and V7 data root must be on the same XFS
  filesystem, proven by device identity and a real cross-root reflink probe.
- The independent V7 service user receives only read/traverse POSIX ACLs on
  the exact V6 runs and service-status paths (including inherited ACLs for
  future files); the systemd unit additionally mounts the source tree
  read-only.

Source-attempt accounting

- A finalized source attempt whose own `capture_error` is non-null is never
  projected, never admitted to a cohort, and never included in PnL or trade
  counts.
- Such attempts remain visible in the status denominator, rejection counts,
  and recent rejection evidence. They are a health warning, not permission to
  select only favorable outcomes.
- Schema, integrity, safety, settlement-regime, path, or provenance violations
  still fail the entire refresh closed.
- Any source rejection keeps qualified PnL null, true edge false, and the
  authoritative 100-trade gate false on this no-journal track.

What this track can and cannot say

- It can measure prospective public-data counterfactual PnL after the cutoff.
- It cannot create a prelabel lock journal retroactively.
- It cannot satisfy true-edge gates.
- It cannot produce non-null qualified PnL.
- It cannot be presented as live trading or real production profitability.

Operational shape

- `polymm-btc-twap-paper-v07-performance.service` runs the shadow builder as a
  oneshot with a 30-minute timer.
- `polymm-btc-twap-paper-v07-health.service` writes health snapshots every
  5 minutes.
- The runner is responsible for lock-based overlap prevention.
