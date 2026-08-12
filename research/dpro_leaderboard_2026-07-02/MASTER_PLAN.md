# d.Pro Polymarket / Hyperliquid Profit-Mode Research Master Plan

Date: 2026-07-02

Scope: public leaderboard, public Polymarket data APIs, public Hyperliquid info APIs, and local sample CSVs only. This research must not connect to real trading accounts, read or print keys, or submit orders.

## Safety Constraints

- Research only: data collection, offline analysis, strategy hypotheses, and validation design.
- Do not read `.env`, wallet files, API keys, browser sessions, or account credentials.
- Do not connect to private account endpoints and do not place, cancel, or simulate live orders against real accounts.
- Do not treat leaderboard PnL as inherently replicable.
- Flag survivorship bias, one-off wins, low-volume artifacts, abnormal ROI bases, and incomplete data.
- Until robustness checks pass, every candidate is marked: `not_trade_recommended=true` with conclusion `不建议交易`.

## Shared Data Inputs

- Local samples:
  - `/tmp/dpro_leaderboard_sample.csv`
  - `/tmp/dpro_pm_top20_summary.csv`
  - `/tmp/dpro_hl_top10_summary.csv`
- Public APIs:
  - Polymarket leaderboard: `https://data-api.polymarket.com/v1/leaderboard?timePeriod=month&orderBy=PNL&category=overall&limit=50&offset=0`
  - Polymarket user stats: `https://data-api.polymarket.com/v1/user-stats?proxyAddress=<proxyWallet>`
  - Polymarket closed positions: `https://data-api.polymarket.com/closed-positions?user=<proxyWallet>&limit=500&offset=0&sortBy=realizedpnl&sortDirection=DESC`
  - Polymarket current positions: `https://data-api.polymarket.com/positions?user=<proxyWallet>&limit=500&offset=0&sortBy=CURRENT&sortDirection=DESC&sizeThreshold=0.1`
  - Polymarket activity: `https://data-api.polymarket.com/activity?user=<proxyWallet>&excludeDepositsWithdrawals=false&limit=500&offset=0`
  - d.Pro Hyperliquid leaderboard: `https://api.d.pro/api/v1/leaderboard?page=1&limit=100&sort=pnl_month&order=desc`
  - Hyperliquid public info POST `https://api.hyperliquid.xyz/info` with `clearinghouseState` and `userFills`.

## Session Map

### Session 1: Data Collection And Account Classification

Owner: main controller.

Output directory: `session_1_classification/`

Inputs:
- Local samples plus fresh Polymarket top 50 and Hyperliquid top 100 public API reads.

Required outputs:
- `pm_accounts_classified.csv`
- `hl_accounts_classified.csv`
- `combined_account_classification.csv`
- `top_candidates_by_type.json`
- `SESSION_1_REPORT.md`

Acceptance criteria:
- Compute PnL, volume, PnL/volume, trade/fill count, largest-win concentration, current position count, settled/closed count where available.
- Classify into A-F:
  - A: Polymarket large event bet
  - B: Polymarket high-frequency / market-making / arbitrage
  - C: Polymarket mid-frequency selective
  - D: Hyperliquid concentrated directional
  - E: Hyperliquid high-turnover multi-asset
  - F: anomaly / excluded sample
- Save candidate lists for downstream sessions.

### Session 2: Polymarket Large Event Bet

Output directory: `session_2_pm_large_event/`

Inputs:
- Fresh public Polymarket leaderboard and per-account public user stats, closed positions, current positions, and activity.
- Session 1 candidate list when available; otherwise independently select likely A accounts using the same criteria.

Required output:
- `SESSION_2_REPORT.md`
- `pm_large_event_accounts.csv`
- Optional raw JSON snapshots under `raw/`.

Acceptance criteria:
- Analyze at least 3 representative accounts if enough valid accounts exist.
- Identify whether PnL is dominated by settled events, largest-win concentration, event category, and possible information/odds/negative-risk explanations.
- Assign evidence, reproducibility score 1-5, risk score 1-5, candidate-pool decision, and exclusion rationale.

### Session 3: Polymarket High-Frequency / MM / Arbitrage

Output directory: `session_3_pm_hf_mm_arb/`

Inputs:
- Fresh public Polymarket data and local samples.
- Session 1 candidate list when available; otherwise independently select likely B accounts.

Required output:
- `SESSION_3_REPORT.md`
- `pm_hf_mm_arb_accounts.csv`
- Optional raw JSON snapshots under `raw/`.

Acceptance criteria:
- Analyze at least 3 representative accounts if enough valid accounts exist.
- Look for high trade count, high volume, low PnL/volume, many current positions, many markets, and spread/negative-risk/microstructure hypotheses.
- Explicitly separate actual evidence from inference.

### Session 4: Polymarket Mid-Frequency Selective

Output directory: `session_4_pm_midfreq/`

Inputs:
- Fresh public Polymarket data and local samples.
- Session 1 candidate list when available; otherwise independently select likely C accounts.

Required output:
- `SESSION_4_REPORT.md`
- `pm_midfreq_accounts.csv`

Acceptance criteria:
- Analyze at least 3 accounts if enough valid accounts exist.
- Identify whether profitability is distributed across multiple markets and can be expressed as event selection, entry band, exit/risk rule.

### Session 5: Hyperliquid Concentrated Directional

Output directory: `session_5_hl_directional/`

Inputs:
- d.Pro Hyperliquid leaderboard top 100, Hyperliquid `clearinghouseState`, and `userFills`.
- Session 1 candidate list when available; otherwise independently select likely D accounts.

Required output:
- `SESSION_5_REPORT.md`
- `hl_directional_accounts.csv`

Acceptance criteria:
- Analyze at least 3 accounts if enough valid accounts exist.
- Measure position concentration, dominant coins, current notional, leverage/margin proxy, fill recency, turnover, and drawdown-risk evidence.

### Session 6: Hyperliquid High-Turnover Multi-Asset

Output directory: `session_6_hl_high_turnover/`

Inputs:
- d.Pro Hyperliquid leaderboard top 100, Hyperliquid `clearinghouseState`, and `userFills`.
- Session 1 candidate list when available; otherwise independently select likely E accounts.

Required output:
- `SESSION_6_REPORT.md`
- `hl_high_turnover_accounts.csv`

Acceptance criteria:
- Analyze at least 3 accounts if enough valid accounts exist.
- Measure fill count, coin breadth, notional turnover, fee impact, concentration by asset, and whether returns appear driven by a few assets.

### Session 7: Strategy Hypothesis Compilation And Offline Validation

Output directory: `session_7_strategy_validation/`

Inputs:
- Completed outputs from Sessions 1-6.

Required output:
- `SESSION_7_STRATEGY_POOL.md`
- `strategy_candidates.csv`
- `offline_validation_plan.md`

Acceptance criteria:
- De-duplicate accounts and hypotheses.
- Cross-check Session 1 labels against deep-dive findings.
- Exclude F/anomalous and low-evidence samples from the candidate pool.
- For each strategy hypothesis define market, entry, exit, sizing, max loss, data needs, backtestability, and failure conditions.
- Validation plan must include train/validation/test, rolling time windows, double fee and slippage, parameter perturbation, remove best 5% trades, remove best single market/month, category slices, and final `不建议交易` unless robustness passes.

## Master Control Sequence

1. Start Sessions 2-6 as independent bounded subagents with disjoint output directories.
2. Run Session 1 locally as the current critical path.
3. Save Session 1 classification tables and candidate lists.
4. Wait for Sessions 2-6 only after Session 1 finishes or when their results are needed.
5. Run Session 7 after Sessions 1-6 are complete.
6. Final report must include:
   - account classification summary
   - representative accounts by type
   - abnormal/excluded samples
   - strategy hypothesis pool
   - offline validation design
   - current recommendation: `不建议交易`
