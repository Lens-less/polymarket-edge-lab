# Polymarket MM Bot - Developer Context

**Deprecated (2026-08-21).** This document described `SmartMarketMaker`
(`src/strategy/`), a generic single-outcome market maker unrelated to this
project's actual strategy. That code, its TUI, and its entry points
(`run_mm.py`, `run_tui.py`) have been deleted — it auto-selected markets from
across all of Polymarket and had nothing to do with BTC 5m/15m structural
pairs.

The real strategy and its architecture live in `src/edge_lab/` (see
`btc_twap_relative_value*.py` and `btc_twap_relative_value_readiness.py`) and
`research/btc_5m_15m_*`. Start with:

- `CLAUDE.md` — current project orientation
- `research/btc_5m_15m_edge_readiness_v08_2026-08-18/CLAUDE_FINAL_ASSESSMENT.md` — latest strategy verdict
- `research/btc_5m_15m_edge_readiness_v08_2026-08-18/IMPLEMENTATION_AND_RUNBOOK.md` — implemented surfaces and deployment order
- `research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md` — live-path implementation contract
