# V0.2 Strategy Performance Report

This report summarizes the fixed acceptance CLI outputs for Track A and Track B. It is offline evidence only.

Links: [JSON](strategy_performance_report.json) · [Schema](strategy_performance_report_schema.md) · [Acceptance report](v0_2_acceptance_report.md)

## Summary

- Source command: `polymm acceptance --track all`
- Generated at: `2026-08-30T00:00:00Z`
- Live status: `LIVE_BLOCKED`
- Real profit claimed: `false`

## Track A

| Field | Value |
| --- | --- |
| Strategy | `track_a.complete_set.arbitrage.v0_2` |
| Research | `GO` |
| Replay | `CONFIRMED` |
| Paper | `CONFIRMED` |
| Paper simulated realized net PnL | `2.5` |
| Shadow | `GO` |
| Shadow realized net PnL | `null` |

## Track B

| Field | Value |
| --- | --- |
| Strategy | `track_b.maker.quote.v0_2` |
| Research | `GO` |
| Replay | `CONFIRMED` |
| Paper | `CONFIRMED` |
| Paper simulated realized net PnL | `0` |
| Shadow | `NO_GO` |
| Shadow expected net edge | `-0.12` |
| Shadow realized net PnL | `null` |

## Notes

- Paper and shadow are simulated acceptance evidence, not live profit.
- The Track B shadow result is a valid negative gate result.
