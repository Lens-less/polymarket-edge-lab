# 文档中心

Polymarket Edge Lab 是公开数据研究与成交感知回放项目，不是可实盘运行的做市机器人，也不是 Polymarket 官方项目。软件版本是 `v0.1.0`；研究目录中出现的 `v0.5`–`v0.8` 是实验协议版本，两者不是同一版本轴。

## 从这里开始

- [快速开始](user/getting-started.md) — 安装并完成离线验收
- [复现指南](user/reproduction.md) — 从任意检出目录复算固定证据
- [配置说明](user/configuration.md) — CLI、固定配置和可选环境变量
- [安全边界](user/safety.md) — 网络、写盘、凭证和下单边界
- [证据与执行模式](user/trading-modes.md) — L0–L4 证据等级
- [常见问题](user/faq.md)
- [开发环境](developer/setup.md)
- [架构说明](developer/architecture.md)
- [贡献指南](../CONTRIBUTING.md)

## 当前权威结论

- [最终研究报告](../research/edge_discovery_2026-07-24/FINAL_REPORT.md)
- [复现说明](../research/edge_discovery_2026-07-24/REPRODUCTION.md)
- [实盘就绪审计](../research/edge_discovery_2026-07-24/LIVE_READINESS.md)
- [BTC 结构线停手证明](../research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md)
- [最终投资决策](../STRATEGY_DECISION_2026-08-22.md)
- [数据存储政策](../DATASETS.md)

截至 2026-08-22，BTC 5m/15m 结构线为 `STRUCTURAL_STOP`，奖励线为未验证研究假设；仓库中没有已验证盈利策略，新订单入口保持无条件关闭。

## 历史材料

`research/` 和 `deploy/` 中保留了冻结实验、失败证据与历史部署资产，用于复算和审计。它们不是当前操作手册。若历史文档与本页、根 README 或停手证明冲突，以停手证明和最新投资决策为准。

本项目仅供研究，不构成投资或法律建议。所在地准入、平台条款和数据许可需要使用者自行确认。
