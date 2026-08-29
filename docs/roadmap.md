# 项目路线图

本仓库的 BTC 5m/15m 结构线已于 2026-08-22 **STRUCTURAL STOP**。
旧的“v1.0 核心做市已实现”路线图作废；`SmartMarketMaker`、TUI 和
`run_mm.py` / `run_tui.py` 已删除，不能当作策略入口。

> **当前状态：结构线关闭，策略实盘 NO-GO，执行探针 NO-GO，不建议实盘交易。**

权威结论：
[STRUCTURAL_LINE_STOP.md](../research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md)。
[LIVE_COMPLETION_SPEC.md](../research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md)
里的 WP-E2 / S0 / S1 / E0 / E1 / L **不再为这条线执行**。数字门槛没有放宽，是提前触发停手。

## 不要做

- 不要为结构线换主机、打 SNS、跑 Gate 0、采 200 expiry
- 不要写双 maker ProbeVenue 或 12 USDC 结构探针
- 不要解除 `assert_new_orders_disabled()`
- 不要把奖励线当成结构线的自动后备；那需要全新预注册
- 不要恢复通用做市机器人或 TUI
- 不要把 v0.6 +87.52、公开 touch、Gate 0 上界写成利润

## 若还想在 Polymarket 上继续

那是一个新问题，不是本路线图的下一步。必须先另开 track 并写新的预注册。
当前仓库里没有 `validated_profitable` 策略。

---

*最后更新：2026-08-22*
