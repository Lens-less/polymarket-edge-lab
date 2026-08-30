# Strategy Performance Report Schema

Schema version: `polymm-strategy-performance-report.v0.2`

This schema documents the JSON payload returned by `PortfolioBook.performance()`. The in-memory `PerformanceReport` dataclass keeps `Decimal` values; when the payload is serialized to JSON, decimal values must become decimal strings and `None` values must remain JSON `null`.

If a higher-level orchestration layer wraps the report, keep the payload below unchanged and add envelope metadata outside it.

## Core payload fields

- `as_of`
- `metrics`

## Field details

`as_of`

- Type: UTC timestamp string
- Source: `PerformanceReport.as_of`
- Meaning: snapshot timestamp for the performance calculation

`metrics`

- Type: object
- Source: `PortfolioBook.performance()`
- Meaning: a fixed strategy-performance metric map keyed by the fields below

## Metric fields

All monetary, ratio, drawdown, exposure, and rate metrics are emitted as decimal strings in JSON when present. Missing metrics are emitted as JSON `null`.

- `realized_net_pnl`
- `trading_net_pnl`
- `incentive_pnl`
- `return_on_allocated_capital`
- `profit_factor`
- `max_drawdown`
- `expected_net_edge`
- `realized_net_edge`
- `edge_realization_ratio`
- `fill_rate`
- `maker_fill_rate`
- `adverse_selection_bps`
- `capital_utilization`
- `unmatched_leg_exposure_seconds`
- `unresolved_reconciliation_count`

## Metric semantics

- `realized_net_pnl`: realized trading PnL plus confirmed incentives.
- `trading_net_pnl`: realized trading PnL after fees, slippage, unwind cost, and onchain cost.
- `incentive_pnl`: confirmed incentives only.
- `return_on_allocated_capital`: `realized_net_pnl / allocated_capital`, or `null` when no capital is provided.
- `profit_factor`: gross profit divided by gross loss across the realized timeline, or `null` when gross loss is zero.
- `max_drawdown`: peak-to-trough drawdown across the realized timeline, or `null` when there is no timeline.
- `expected_net_edge`: sum of approved execution plans' expected net edge, or `null` when that sum is zero.
- `realized_net_edge`: realized net PnL over the selected horizon.
- `edge_realization_ratio`: `realized_net_edge / expected_net_edge`, or `null` when expected edge is zero.
- `fill_rate`: filled orders divided by active-or-terminal orders, or `null` when no eligible orders exist.
- `maker_fill_rate`: maker fills divided by maker orders, or `null` when no maker orders exist.
- `adverse_selection_bps`: weighted adverse-selection basis points across positions, or `null` when no basis-point data exists.
- `capital_utilization`: current exposure divided by allocated capital, or `null` when no capital is provided.
- `unmatched_leg_exposure_seconds`: sum of unmatched-leg exposure seconds from open orders.
- `unresolved_reconciliation_count`: unresolved reconciliation count carried by the portfolio snapshot.

## JSON shape

```json
{
  "as_of": "2026-08-30T12:00:00Z",
  "metrics": {
    "realized_net_pnl": "2.34",
    "trading_net_pnl": "1.84",
    "incentive_pnl": "0.50",
    "return_on_allocated_capital": "0.0234",
    "profit_factor": "1.17",
    "max_drawdown": "0.16",
    "expected_net_edge": "2",
    "realized_net_edge": "2.34",
    "edge_realization_ratio": "1.17",
    "fill_rate": "1",
    "maker_fill_rate": "1",
    "adverse_selection_bps": "5",
    "capital_utilization": "0.20",
    "unmatched_leg_exposure_seconds": "12",
    "unresolved_reconciliation_count": 0
  }
}
```

## Serialization rule

When this payload is written as JSON, preserve decimal precision by serializing every `Decimal` as a decimal string. Do not round-trip through binary float.
