# Edge Discovery Metrics

As of: `2026-07-24T11:27:10.995744Z`

所有当前策略的严格盈利验证均未通过。理论奖励、price touch、公开流量和同步快照候选不计为成交或 PnL。

| 策略族 | 实验 | 数据级别 | 结论 | 可认证成交 | PnL |
|---|---|---:|---|---:|---|
| constraints | `current-20260724-standard-v6-security-final:constraints` | L1 | insufficient_data | 0 | null (read-only synchronized snapshot screening has no executable fill ledger) |
| latency | `capture-latency-fddaaadb3cdbf2fc8c93` | L2 | insufficient_data | 0 | null (receive-time co-movement and price touches are not executable settled fills) |
| legacy_mm | `baseline:canonical-audit` | mixed_L0_L1 | rejected | 0 | null (legacy evidence has no authenticated, settled, reconciled fill ledger) |
| neg_risk_augmented | `current-20260724-augmented-v5-security-final` | L1 | insufficient_data | 0 | null (read-only synchronized snapshot screening has no executable fill ledger) |
| neg_risk_standard | `current-20260724-standard-v6-security-final:neg_risk_standard` | L1 | insufficient_data | 0 | null (read-only synchronized snapshot screening has no executable fill ledger) |
| rewards | `selective-liquidity-rewards-20260724T112636.637029Z` | L1 | promising_not_validated | 0 | null (theoretical reward scenarios and public price touches are not fills or realized PnL) |
| weather | `weather-public-experiment` | L1 | insufficient_data | 0 | null (public L1 price history has no queue-aware, executable fill ledger) |

## Sample and fill coverage

| 策略族 | Train count | Validation count | Test/OOS count | Actual fills | Explainable fills |
|---|---:|---:|---:|---:|---:|
| constraints | None | None | None | 0 | 0 |
| latency | None | None | 1 | 0 | 0 |
| legacy_mm | None | None | None | 0 | 0 |
| neg_risk_augmented | None | None | None | 0 | 0 |
| neg_risk_standard | None | None | None | 0 | 0 |
| rewards | None | None | None | 0 | 0 |
| weather | 58 | 21 | 21 | 0 | 0 |

## Required metric contract

Every strategy row in `BACKTEST_RESULTS.json` includes `null + reason` when unavailable for: `initial_capital`, `pnl`, `return`, `fees`, `rewards`, `max_drawdown`, `sharpe`, `sortino`, `hit_rate`, `turnover`, `capacity`, `one_leg_cvar`, `forecast_alpha`, `maker_spread_pnl`, `adverse_selection`, `confidence_interval_95`, `confidence_interval`, `pnl_concentration`.

## 统一结论

- 严格验证盈利策略：0。
- 当前可认证/可解释成交：0。
- 实盘交易：禁用。
- 结论：不建议实盘交易。
