# 项目路线图

本仓库的主线是 BTC 5m/15m 结构对研究，不是通用做市产品。
旧的“v1.0 核心做市已实现”路线图已作废：`SmartMarketMaker`、TUI 和
`run_mm.py` / `run_tui.py` 已删除，不能当作策略入口。

> **当前状态：策略实盘 NO-GO，执行探针 NO-GO，不建议实盘交易。**

权威顺序只在
[LIVE_COMPLETION_SPEC.md](../research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md)。
数字门槛冻结在
[PREREGISTRATION.json](../research/btc_5m_15m_edge_readiness_v08_2026-08-18/PREREGISTRATION.json)，
不得放宽。

## 允许的下一包（不要跳）

1. WP-H0 仓库卫生（本会话）
2. WP-E2.0 历史采集失败分类（本会话；未完成不得采购主机）
3. WP-E2 主机容量 + SNS + compact capture（仅当 E2.0 主因是容量）
4. WP-S0 冻结 v0.8，开 v0.9，锁死 exact count 后再跑 Gate 0
5. 仅当 Gate 0 PASS 才进入 WP-S1 / E0 / E1

跳过 WP-S0/S1 直接写下单适配器，或把 `DRY_RUN=false` / 八项布尔审计当成放行，都违反合同。

## 明确不做

- 不恢复通用做市机器人或 TUI
- 不把 v0.6 +87.52、公开 touch、Gate 0 上界写成利润
- 不把事后数出来的清洁到期数填进 `--expected-clean-attempts`

---

*最后更新：2026-08-22*
