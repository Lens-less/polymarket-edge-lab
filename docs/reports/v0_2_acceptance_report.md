# V0.2 Acceptance Report

This is the versioned summary of the fixed, offline acceptance CLI output for the v0.2.0 source preview. It is preview-conformance evidence, not full SPEC completion.

Links: [JSON](v0_2_acceptance_report.json) · [Strategy performance report](strategy_performance_report.md) · [Live readiness report](live_readiness_report.md)

## Result

- Track A: `GO`
- Track B: `NO_GO`
- Final CI: `PASS`
- Live status: `LIVE_BLOCKED`
- Never submitted real orders: `true`
- Real funds changed: `false`

## Track A

| Field | Value |
| --- | --- |
| Research | `GO` |
| Replay | `CONFIRMED` |
| Paper | `CONFIRMED` |
| Paper simulated realized net PnL | `2.5` |
| Shadow | `GO` |
| Shadow realized net PnL | `null` |

## Track B

| Field | Value |
| --- | --- |
| Research | `GO` |
| Replay | `CONFIRMED` |
| Paper | `CONFIRMED` |
| Paper simulated realized net PnL | `0` |
| Shadow | `NO_GO` |
| Shadow expected net edge | `-0.12` |
| Shadow realized net PnL | `null` |

## Notes

- Paper and shadow values are offline acceptance evidence, not live profit.
- Track B is an expected negative gate result and remains useful evidence.
- The commands use fixed acceptance scenarios; they are not production market/config-backed scan, replay, or shadow workflows.

## SPEC §18 Mapping

| Item | Status | Claim class |
| --- | --- | --- |
| WP0-WP6 preview code and docs surface | PARTIAL | INTERNAL |
| Deterministic Research / Replay / Paper / Shadow acceptance paths exist | PASS | INTERNAL |
| Production market/config-backed scanner, replay, shadow, and packaged desk | DEFERRED | NOT_CLAIMED |
| Track A and Track B fixed acceptance scenes each reach Research -> Replay -> Paper -> Shadow | PASS | INTERNAL |
| Same strategy logic across the fixed Replay / Paper / Shadow scenes | PASS | INTERNAL |
| Local trade-desk preview completes a synthetic opportunity -> plan -> confirm -> order -> fill -> PnL flow | PASS | INTERNAL |
| All ordinary CI checks | PASS | INTERNAL |
| No known high-priority correctness, security, or funds-risk defects | PASS | INTERNAL |
| Legacy live hard gate is not bypassed by a boolean or environment variable | PASS | INTERNAL |
| Offline doctor rejects unsigned or self-asserted all-green live evidence | PASS | INTERNAL |
| No live credentials appear in browser or logs | PASS | INTERNAL |
| If a real-money Canary cannot run, a complete LIVE_BLOCKED report is produced instead of fake completion | PASS | INTERNAL |
| External provenance-attestation verification | DEFERRED | NOT_CLAIMED |
| Real-money Canary execution | EXTERNAL | NOT_CLAIMED |
| Real profitability claim | EXTERNAL | NOT_CLAIMED |
| LIVE_LIMITED qualification | EXTERNAL | NOT_CLAIMED |
