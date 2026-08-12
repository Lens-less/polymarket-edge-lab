# Session 7: Strategy Hypothesis Pool And Offline Validation Plan

Generated: 2026-07-02T10:26:44+00:00

## Final Status

- Sessions 1-6 completed and wrote structured outputs.
- This report compiles hypotheses only; it is not a trading recommendation.
- Current portfolio-level conclusion: **不建议交易** until offline validation passes all robustness gates.

## Inputs

| input | rows |
| --- | --- |
| Session 1 classification | 150 |
| Session 2 PM large event | 6 |
| Session 3 PM HF/MM/arb | 8 |
| Session 4 PM mid-frequency | 5 |
| Session 5 HL directional | 6 |
| Session 6 HL high turnover | 5 |

## Session 1 Classification Snapshot

| classification | count |
| --- | --- |
| A_pm_large_event_bet | 2 |
| B_pm_hf_mm_arb | 7 |
| C_pm_midfreq_selective | 2 |
| D_hl_concentrated_directional | 7 |
| E_hl_high_turnover_multi_asset | 19 |
| F_anomaly_excluded | 2 |
| F_anomaly_or_incomplete_excluded | 5 |
| F_anomaly_or_unclear_excluded | 11 |
| F_detail_pending_excluded | 95 |

## Cross-Validation Summary

| status | count |
| --- | --- |
| deep_dive_overrides_detail_pending | 8 |
| downgrade_or_watchlist | 6 |
| label_divergence_review | 2 |
| match | 14 |

Key interpretation:

- Matches between Session 1 and deep dives are strongest for PM B, HL D top cases, and HL E broad-turnover cases.
- Session 4 adds mid-frequency candidates that Session 1 under-labeled or left unclear because Session 1 used first-pass thresholds.
- Session 5/6 deep dives override some `detail_pending` rows because they fetched additional public details independently.
- Watchlist/downgrade rows are not primary validation candidates.

## Exclusions And Downgrades

| bucket | decision | reason |
| --- | --- | --- |
| A large-event | Do not use as primary strategy sample. | Session 2 reproducibility score is 1; PnL frequently dominated by few resolved sports events and may reflect informat... |
| F/detail_pending | Exclude until detail is fetched and reconciled. | Session 1 intentionally marked non-enriched top50/top100 rows as detail_pending to avoid overclaiming from leaderboar... |
| HL concentrated directional | Research-only until tail risk is modeled. | Session 5 shows high exposure/account-value ratios, capped fill history, liquidation/funding risk, and large day-PnL ... |
| BobbyBigSize | Downgraded from primary E despite Session 1 E label. | Session 6 found recent fills more dominated by BTC/ETH/HYPE than cleaner breadth candidates. |

## Strategy Candidate Pool

| strategy_id | type | pool_status | reproducibility_score | risk_score | representative_accounts | backtestability |
| --- | --- | --- | --- | --- | --- | --- |
| PM_B_NEG_RISK_SPREAD_INVENTORY | B_pm_hf_mm_arb | primary_offline_validation | 2 | 5 | RN1 0x2005d16a...7875ea; swisstony 0x204f72f3...a95e14; mooseborzoi 0x84cfffc3...cd2f63 | medium; requires reconstructed orderbook and fill model, not just position endpoints. |
| PM_C_SPORTS_SELECTIVE_PRICE_BAND | C_pm_midfreq_selective | secondary_offline_validation | 3 | 4 | anon 0x5e945820...705ba1; RISK-IS-NEVER-OK 0x28059b3f...cf76ed | medium-high if historical prices and external sports priors are available. |
| HL_E_MULTI_ASSET_TURNOVER_ROTATION | E_hl_high_turnover_multi_asset | secondary_offline_validation | 3 | 5 | anon 0xecb63caa...b82b00; anon 0x7717a7a2...d7bd7e | medium; feasible with public market data but exact account-style execution needs fills/orderbook assumptions. |
| HL_D_CONCENTRATED_TREND_RISK | D_hl_concentrated_directional | research_only_high_risk | 2 | 5 | Penision Fund 0x0ddf9bae...38a902; anon 0xcf90cfec...895c0e; anon 0xb83de012...7d6e36 | medium for signal proxy; low for copying observed wallet behavior because intent, hedges, and full history are not pu... |
| PM_A_EVENT_CONCENTRATION_FILTER | A_pm_large_event_bet | excluded_as_primary_strategy_use_as_risk_filter | 1 | 5 | muchobliged 0x095fbca2...7190b2; maz26 0x67542c32...bfe3bc; Qpkwks 0x9ee8bbc3...5a89de; surfandturf 0x9f2fe025...2d2ca8 | low as observed wallet behavior; useful as anti-overfit filter. |

## Candidate Details

### PM_B_NEG_RISK_SPREAD_INVENTORY

- Type: `B_pm_hf_mm_arb`
- Status: `primary_offline_validation`
- Evidence: High trade counts, high monthly volume, broad current-position coverage, lower PnL/volume, negative-risk and multi-position event evidence in Session 3.
- Representative accounts: RN1 0x2005d16a...7875ea; swisstony 0x204f72f3...a95e14; mooseborzoi 0x84cfffc3...cd2f63
- Applicable market: Polymarket liquid sports/event families with multiple related outcomes or negative-risk structures.
- Entry conditions: Only when orderbook-derived bid/ask or complement baskets imply positive expected spread/arb after fees, slippage, settlement latency, and inventory haircuts; require minimum depth and multiple markets in the same event family.
- Exit conditions: Close inventory when theoretical edge disappears, event correlation breaks, depth vanishes, or unresolved exposure exceeds cap; redeem/merge after settlement where applicable.
- Position rule: Market-neutral or tightly hedged inventory; cap per event family, per outcome, and per unresolved settlement day; no averaging into unhedged event direction.
- Max loss rule: Hard stop by event-family mark-to-market drawdown, stale inventory age, and worst-case settlement loss; disable strategy if realized adverse selection exceeds validation budget.
- Data requirements: Historical CLOB orderbooks, trades, maker/taker flags if available, market metadata, negative-risk grouping, resolution data, fees/rebates, and latency snapshots.
- Backtestability: medium; requires reconstructed orderbook and fill model, not just position endpoints.
- Failure conditions: Edge disappears after doubled fees/slippage, best 5% trades removed, or profits concentrate in one market family/month; maker queue/fill assumptions cannot be validated.
- Current conclusion: **不建议交易**.

### PM_C_SPORTS_SELECTIVE_PRICE_BAND

- Type: `C_pm_midfreq_selective`
- Status: `secondary_offline_validation`
- Evidence: Session 4 found distributed profitable markets, 131-488 trades in selected accounts, largest closed-win concentration 3.2%-13.3%, and descriptive winner avg-price bands around 0.42-0.56 for stronger cases.
- Representative accounts: anon 0x5e945820...705ba1; RISK-IS-NEVER-OK 0x28059b3f...cf76ed
- Applicable market: Polymarket sports moneyline/totals/spread markets with repeated fixtures and enough historical resolution data.
- Entry conditions: Model-implied probability exceeds market price by a validated margin inside a liquidity/price band; avoid single-news markets and low-depth tails.
- Exit conditions: Exit when model edge closes, injury/lineup/news regime invalidates signal, or price moves against position beyond pre-set loss; otherwise settle only when validation shows settlement holding is robust.
- Position rule: Small fixed-fraction sizing by edge and liquidity; cap by league/event/day; no more than a small fraction of bankroll in unresolved correlated fixtures.
- Max loss rule: Per-market max loss, per-day drawdown stop, and per-category drawdown stop; remove strategy if open-risk inventory accumulates like high-drawdown watchlist accounts.
- Data requirements: Resolved market history, trades, prices at entry/exit, event category, external sports odds/injury/lineup feeds, liquidity, and settlement timestamps.
- Backtestability: medium-high if historical prices and external sports priors are available.
- Failure conditions: No edge after out-of-sample validation, category slices fail, or profitability is erased after removing best 5% trades or best single month/market.
- Current conclusion: **不建议交易**.

### HL_E_MULTI_ASSET_TURNOVER_ROTATION

- Type: `E_hl_high_turnover_multi_asset`
- Status: `secondary_offline_validation`
- Evidence: Session 6 identified high fill count, broad coin breadth, high notional turnover, and lower top-coin concentration in primary accounts; BobbyBigSize was downgraded to watchlist.
- Representative accounts: anon 0xecb63caa...b82b00; anon 0x7717a7a2...d7bd7e
- Applicable market: Hyperliquid perpetuals across liquid crypto and listed synthetic assets.
- Entry conditions: Asset enters validated rotation basket by momentum/mean-reversion/funding-volatility signal; require volume, spread, and funding filters; avoid assets where fees consume expected edge.
- Exit conditions: Exit on signal decay, volatility shock, funding reversal, liquidity drop, or portfolio risk cap breach; rebalance on fixed schedule only if turnover survives fee stress.
- Position rule: Diversified gross and net exposure caps; per-asset notional cap; volatility-scaled sizing; exposure concentration cap based on top coin share.
- Max loss rule: Portfolio drawdown stop, per-asset stop, liquidation-distance floor, and fee-burn stop; no live use if fill model cannot reproduce fee/slippage.
- Data requirements: HL trades/fills, L2 or best bid/ask, funding, mark prices, liquidation/margin state proxy, open interest, fees, and asset metadata.
- Backtestability: medium; feasible with public market data but exact account-style execution needs fills/orderbook assumptions.
- Failure conditions: Net PnL becomes negative under doubled fees/slippage, turnover drops after parameter perturbation, or profits come from one asset/month.
- Current conclusion: **不建议交易**.

### HL_D_CONCENTRATED_TREND_RISK

- Type: `D_hl_concentrated_directional`
- Status: `research_only_high_risk`
- Evidence: Session 5 found single-name ETH/BTC/HYPE shorts with 100% or near-100% current notional concentration, high exposure/account-value ratios, capped 2,000-fill samples, and large day-PnL swings.
- Representative accounts: Penision Fund 0x0ddf9bae...38a902; anon 0xcf90cfec...895c0e; anon 0xb83de012...7d6e36
- Applicable market: Hyperliquid major and high-liquidity alt perpetuals.
- Entry conditions: Only if independent trend/funding/volatility regime model identifies directional edge and liquidation distance remains wide; never copy wallet positions directly.
- Exit conditions: Exit on trend invalidation, funding reversal, volatility expansion against position, liquidation gap compression, or max drawdown hit.
- Position rule: Volatility-scaled directional exposure with strict leverage cap; no single asset above concentration cap; no account-value-sized copy trades.
- Max loss rule: Hard liquidation-distance buffer, daily loss limit, and per-position stop; disable after any stress test that shows tail loss exceeds budget.
- Data requirements: OHLCV, funding, orderbook liquidity, volatility, open interest, liquidation estimates, and full fills for representative wallets if available.
- Backtestability: medium for signal proxy; low for copying observed wallet behavior because intent, hedges, and full history are not public.
- Failure conditions: Edge vanishes in validation, best month/asset removal kills PnL, drawdown/liquidation risk exceeds budget, or funding costs dominate.
- Current conclusion: **不建议交易**.

### PM_A_EVENT_CONCENTRATION_FILTER

- Type: `A_pm_large_event_bet`
- Status: `excluded_as_primary_strategy_use_as_risk_filter`
- Evidence: Session 2 found sports-event PnL dominated by few settled markets, largest-win/month-PnL ratios up to multiple times monthly PnL, and reproducibility score 1 across selected accounts.
- Representative accounts: muchobliged 0x095fbca2...7190b2; maz26 0x67542c32...bfe3bc; Qpkwks 0x9ee8bbc3...5a89de; surfandturf 0x9f2fe025...2d2ca8
- Applicable market: Polymarket event outcomes, mainly sports samples in this run.
- Entry conditions: No direct entry rule accepted from leaderboard evidence; use only as a filter to identify and exclude one-off large-win samples from strategy training.
- Exit conditions: Not applicable as a primary strategy; for research, require settlement and post-event attribution before sample inclusion.
- Position rule: Do not size from copied wallet outcomes; any future event-specific hypothesis must be separately modeled and capped.
- Max loss rule: Exclude from candidate pool unless edge is independently explained and survives removal of the best market/trade.
- Data requirements: Resolved markets, price path, external pre-event odds/news, entry timestamps, and full trade history.
- Backtestability: low as observed wallet behavior; useful as anti-overfit filter.
- Failure conditions: If PnL relies on one match/event or unverifiable information advantage, reject from replicable strategy pool.
- Current conclusion: **不建议交易**.

## Next Execution Plan

1. Build a local immutable dataset cache for Polymarket historical trades/orderbooks/settlements and Hyperliquid OHLCV/funding/orderbook data.
2. Implement backtest harnesses per strategy ID with no live account connectivity.
3. Run rolling train/validation/test and all robustness tests listed in `offline_validation_plan.md`.
4. Promote only strategies that pass all gates; otherwise keep conclusion as **不建议交易**.
