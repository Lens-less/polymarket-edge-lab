# Recovery Audit

- Generated at: `2026-08-14T15:42:34Z`
- Scope: diagnostic-only audit of the v0.5 gap period after the 5m settlement regime diverged from the frozen 30s universe.

## Facts

- Expected v0.5 cycles seen in the incident window: 69
- Cycles with canonical reports/summaries: 39
- Cycles missing canonical report or summary output: 30
- `tau120` captures with enough dual-surface evidence for diagnostic replay: 10
- Synthetic actions reviewed: 3
- Dust diagnostic wins observed: `+13.626271 USDC`, `+10.365131 USDC`

## Boundary

- These diagnostics do not alter `btc-paper-v05-20260813`.
- The reconstructed outputs are not equivalent to the missing real-time information set.
- No diagnostic result may enter `qualified` evidence, calibration data, OOS metrics, or promotion claims.
