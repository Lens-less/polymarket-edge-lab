# 文档中心

Polymarket Edge Lab 是公开数据研究与成交感知回放项目，不是可实盘运行的做市机器人，也不是 Polymarket 官方项目。当前软件版本是 `v0.2.0` 的 GitHub source-checkout 离线开发预览；`src/profit_system` 是新的模块化单体，`src/edge_lab` 仍保留为冻结研究与兼容边界。研究目录中出现的 `v0.5`–`v0.8` 是实验协议版本，两者不是同一版本轴。wheel 和 sdist 仅用于构建验证，不代表 PyPI 发布或独立 wheel 安装支持。

## 从这里开始

- [快速开始](user/getting-started.md) — 安装并完成离线验收
- [复现指南](user/reproduction.md) — 从任意检出目录复算固定证据
- [配置说明](user/configuration.md) — CLI、固定配置和可选环境变量
- [安全边界](user/safety.md) — 网络、写盘、凭证和下单边界
- [证据与执行模式](user/trading-modes.md) — L0–L4 证据等级
- [常见问题](user/faq.md)
- [开发环境](developer/setup.md)
- [架构说明](developer/architecture.md)
- [V0.2 迁移说明](user/v0_2_migration.md)
- [V0.2 运行手册](user/v0_2_operator_runbook.md)
- [V0.2 live readiness report](reports/live_readiness_report.md)
- [V0.2 strategy performance report](reports/strategy_performance_report.md)
- [V0.2 acceptance report](reports/v0_2_acceptance_report.md)
- [门禁报告 Schema](reports/gate_report_schema.md)
- [Live 预检结果 Schema](reports/live_probe_result_schema.md)
- [故障演练结果 Schema](reports/fault_drill_result_schema.md)
- [贡献指南](../CONTRIBUTING.md)

## 当前权威结论

- [最终研究报告](../research/edge_discovery_2026-07-24/FINAL_REPORT.md)
- [复现说明](../research/edge_discovery_2026-07-24/REPRODUCTION.md)
- [实盘就绪审计](../research/edge_discovery_2026-07-24/LIVE_READINESS.md)
- [BTC 结构线停手证明](../research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md)
- [最终投资决策](../STRATEGY_DECISION_2026-08-22.md)
- [数据存储政策](../DATASETS.md)
- [V0.2 迁移说明](user/v0_2_migration.md)
- [V0.2 运行手册](user/v0_2_operator_runbook.md)
- [门禁报告 Schema](reports/gate_report_schema.md)

截至 2026-08-22，BTC 5m/15m 结构线为 `STRUCTURAL_STOP`，奖励线为未验证研究假设；仓库中没有已验证盈利策略，新订单入口保持无条件关闭，默认 live 状态是 `LIVE_BLOCKED`。

## V0.2 开发规格

- [V0.2 Research-to-Live Profit Trading System SPEC](specs/V0_2_PROFIT_TRADING_SYSTEM_SPEC.md) — 从机会研究到受控实盘的实现合同、晋级门、交易台和验收标准
- [V0.2 新会话 Goal 提示词](specs/V0_2_GOAL_PROMPT.md) — 在新的 Codex 会话中按 Work Package 持续实现并验证

这两份文件描述的是 V0.2 已实现的软件合同和验收标准；仓库里的软件表面已经以 source-checkout 预览形式交付，但在 Canary 前置条件、明确真钱授权和风险预算全部满足前，现有新订单入口仍须保持关闭。

## 历史材料

`research/` 和 `deploy/` 中保留了冻结实验、失败证据与历史部署资产，用于复算和审计。它们不是当前操作手册。若历史文档与本页、根 README 或停手证明冲突，以停手证明和最新投资决策为准。

本项目仅供研究，不构成投资或法律建议。所在地准入、平台条款和数据许可需要使用者自行确认。
