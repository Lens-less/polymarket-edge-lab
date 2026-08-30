# Project context

Polymarket Edge Lab is a public-data research and execution-replay repository. The current source-release surface is `v0.2.0`: it is an offline GitHub source preview with local editable install and the `polymm` CLI, but it is not a runnable live-trading bot or an official Polymarket project.

Current authority order:

1. `README.md` — current v0.2.0 source-release surface, editable install, and `polymm` CLI quick start
2. `STRATEGY_DECISION_2026-08-22.md` — current investment decision
3. `research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md` — BTC 5m/15m structural stop
4. `research/edge_discovery_2026-07-24/REPRODUCTION.md` — reproducibility contract
5. `CLAUDE.md` — repository-specific agent orientation

The old `SmartMarketMaker`, `run_mm.py`, `run_tui.py`, `src/strategy/`, and `src/tui/` were removed. Do not recreate them from historical documentation. New orders remain unconditionally blocked, and no strategy in this repository is validated profitable. `v0.1.x` references in legacy docs are historical semantics, not the current supported surface. `uv build` is for release validation only, not an instruction to publish a standalone wheel.
