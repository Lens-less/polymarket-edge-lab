# 架构

**已废弃（2026-08-21）。** 本文档原本描述 `SmartMarketMaker`（`src/strategy/`），
一个与本项目 BTC 5m/15m 结构对策略无关的通用单边做市机器人。该代码、其 TUI
以及入口点（`run_mm.py`、`run_tui.py`）已被删除——它只是从 Polymarket 全站
自动选一个"打分最高"的市场去挂价差单，和 BTC 结构对策略没有关系。

真正的策略与架构在 `src/edge_lab/`（尤其是 `btc_twap_relative_value*.py`、
`btc_twap_relative_value_readiness.py`）与 `research/btc_5m_15m_*` 下。请参考：

- `../../CLAUDE.md` — 当前项目导览
- `../../research/btc_5m_15m_edge_readiness_v08_2026-08-18/CLAUDE_FINAL_ASSESSMENT.md` — 最新策略结论
- `../../research/btc_5m_15m_edge_readiness_v08_2026-08-18/IMPLEMENTATION_AND_RUNBOOK.md` — 已实现的部分与部署顺序
- `../../research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md` — 实盘完成规格
