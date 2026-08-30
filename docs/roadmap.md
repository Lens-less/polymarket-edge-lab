# 项目路线图

当前软件线是 `v0.2.0` 的 GitHub source-checkout 离线开发预览。仓库已经实现 V0.2 的模块化单体、离线预览台和 fail-closed 门禁；真正的 live canary 仍然取决于明确的当前环境授权、风险预算、资格与对账证据。

BTC 5m/15m 结构线已于 2026-08-22 **STRUCTURAL STOP**。旧的“v1.0 核心做市已实现”路线图作废；`SmartMarketMaker`、TUI 和 `run_mm.py` / `run_tui.py` 已删除，不能当作策略入口。

> **当前状态：结构线关闭，策略实盘 NO-GO，执行探针 NO-GO，不建议实盘交易。**

权威结论：
[STRUCTURAL_LINE_STOP.md](../research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md)。
[LIVE_COMPLETION_SPEC.md](../research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md)
里的 WP-E2 / S0 / S1 / E0 / E1 / L 不再为这条线执行。数字门槛没有放宽，是提前触发停手。

## 现有软件面

| 面 | 当前状态 | 说明 |
|---|---|---|
| `WP0` | 已实现 | 合同、目录、类型、配置、venue adapter tests、基线 |
| `WP1` | 已实现 | 统一经济模型、机会 schema、扫描、排名和解释 |
| `WP2` | 已实现 | 套利与 maker 策略、回放和 Research Gate |
| `WP3` | 已实现 | 现实 Paper、Shadow、证据包和 Shadow Gate |
| `WP4` | 已实现 | 实盘 adapter、订单生命周期、账本、对账、kill |
| `WP5` | 已实现 | 单页交易台、前端测试和 Playwright E2E |
| `WP6` | 已实现 | doctor、fault drills、runbook、Canary-ready 和 `LIVE_BLOCKED` / `LIVE_CANARY_READY` 报告；`scan` / `replay` / `shadow` / `status` / `desk` 现在提供确定性的固定验收/演示报告和本地预览表面 |
| `Final` | 外部阻塞 | 全量 live canary 仍需要用户明确授权、风险预算和外部环境条件 |

这里的“已实现”表示仓库里已经有软件实现和离线验证面，不表示已经获得真实 live 授权，也不表示已经证明盈利。

## Track A / Track B 语义

固定可复算验收场景不是利润声明，而是路径声明。

- Track A 是正向可执行场景：`Research -> Replay -> Paper -> Shadow` 全链路完成后，最终状态应是 `GO`
- Track B 是负向门禁场景：`Research -> Replay -> Paper -> Shadow` 全链路完成后，最终状态应是 `NO_GO`
- 两条 track 都必须能完整走通中间阶段；差别在于最后的 gate 结果是否应该放行
- Track B 的 `NO_GO` 不是失败，它证明门禁会正确阻止不应放行的路径

## 不要做

- 不要为结构线换主机、打 SNS、跑 Gate 0、采 200 expiry
- 不要写双 maker ProbeVenue 或 12 USDC 结构探针
- 不要解除 `assert_new_orders_disabled()`
- 不要把奖励线当成结构线的自动后备；那需要全新预注册
- 不要恢复通用做市机器人或 TUI
- 不要把 `paper`、`shadow`、公开 touch、或任何未对账的报告写成已验证盈利

## 若还想在 Polymarket 上继续

那是一个新问题，不是本路线图的下一步。必须先另开 track、写新的预注册，并满足当前环境的 live 授权、风险预算、资格和对账条件。

当前仓库里没有 `validated_profitable` 策略，默认 live 状态也仍然是 `LIVE_BLOCKED`。

---

*最后更新：2026-08-31*
