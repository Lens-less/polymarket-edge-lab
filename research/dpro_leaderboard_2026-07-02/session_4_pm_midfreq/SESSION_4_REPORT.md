# Session 4 Report - Polymarket Mid-Frequency Selective Accounts

Snapshot time: 2026-07-02T10:16:22Z

Scope: public Polymarket data APIs plus local CSV samples only. No credentials, browser sessions, wallet files, private endpoints, account connections, or order actions were used.

Bottom line: all analyzed accounts remain research candidates only. Every candidate is marked `not_trade_recommended=true`; conclusion is `不建议交易` until an independent robustness harness validates event selection, sizing, entry band, exit rules, fee/slippage sensitivity, and drawdown behavior.

## Inputs And Method

- Local samples read: `/tmp/dpro_pm_top20_summary.csv` and `/tmp/dpro_leaderboard_sample.csv`.
- Fresh public endpoints used: monthly Polymarket leaderboard top 50, `user-stats`, `closed-positions`, `positions`, and `activity`.
- Candidate screen: tens to low-thousands of trades, meaningful monthly volume, multiple profitable closed markets/eventSlugs, largest closed win not dominating positive closed realized PnL, and no obvious high-frequency/MM profile.
- Session 1 outputs were not required for this pass; candidates were selected independently from the local samples plus fresh public API reads.
- Concentration metric: primary concentration is `largest closed win / sum positive closed realized` from returned closed-position rows. `largestWin / month PnL` is also reported, but month PnL can be distorted by open losses and realized offsets.
- Limitation: the public closed-position endpoint returned 50 rows per account in these calls even when `limit=500` was requested. Treat positive distribution metrics as top-returned-row evidence, not a complete lifetime ledger.
- Limitation: `activity` rows are recent public activity/fill-style rows and can exceed the aggregate `user-stats` trade count; `user-stats` remains the primary trade-count field.

## Candidate Summary

| Decision | User | Address | Rank | Trades | Month PnL | Volume | PnL/Vol | Largest Closed / Positive Sum | Positive Events | Current Value / Initial | Repro | Risk |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| include_C_watchlist_high_open_risk | 0xd4aa6f8e91cfea29b66a48ebff52814 | `0x709e8dcb133555794decc598e07f2c923b8366f5` | 5 | 252 | $529,667 | $3,142,247 | 16.9% | 6.7% | 47 | $1,338,133 / $2,551,243 | 2 | 5 |
| include_C_watchlist_high_open_risk | Jsram | `0x83720820a8aa6c3f20ad71850e7a1a17d16c5223` | 6 | 145 | $520,452 | $2,889,936 | 18.0% | 13.3% | 46 | $661,854 / $2,375,820 | 2 | 5 |
| include_C_watchlist_high_drawdown_risk | CandleHammerDrums | `0x7c1ee865a785de4c00ee90ed86a38489fb8bbab3` | 8 | 146 | $395,371 | $2,072,962 | 19.1% | 5.2% | 37 | $17,081 / $3,934,063 | 2 | 5 |
| primary_C_validation_candidate | (blank) | `0x5e9458202b5817a72cf81105ec8a30e6f3705ba1` | 17 | 131 | $246,867 | $1,227,815 | 20.1% | 3.2% | 49 | $0 / $0 | 3 | 3 |
| secondary_C_validation_candidate | RISK-IS-NEVER-OK | `0x28059b3fb7af7ed76d83dc9121bbb72ec7cf76ed` | 23 | 488 | $187,275 | $435,883 | 43.0% | 9.4% | 45 | $500 / $70,714 | 3 | 3 |

## Account Deep Dives

### 0xd4aa6f8e91cfea29b66a48ebff52814 - `0x709e8dcb133555794decc598e07f2c923b8366f5`

- Leaderboard metrics: rank 5, month PnL $529,667, volume $3,142,247, PnL/volume 16.9%.
- Trade count: 252 public `user-stats` trades; largestWin $613,282, or 115.8% of month PnL.
- Largest-win concentration: largest returned positive closed win $613,282; sum returned positive closed realized $9,102,607; largest/sum 6.7%; top-3/sum 18.8%.
- Profitable market distribution: 50 returned positive closed rows across 47 eventSlugs. Top profitable titles: Will France win on 2026-06-26? [Yes, $613,282] | Will Egypt win on 2026-06-21? [Yes, $575,067] | Will Belgium win on 2026-06-15? [No, $526,502] | Will Morocco win on 2026-06-19? [Yes, $525,956] | Spread: Morocco (-1.5) [Morocco, $472,852].
- Candidate event-selection hypothesis: Selective soccer match-market account; repeated use of winner/draw, spread, O/U, BTTS, and team-total style markets across many fixtures.
- Entry-band hypothesis: Profitable closed-position avgPrice interquartile band 0.395-0.587 with median 0.496; recent BUY price band 0.470-0.510 where available. Treat this as descriptive, not a rule.
- Exit/risk hypothesis: Closed winners are mostly realized at settlement/redemption in public rows; current positions show 21 rows, currentValue $1,338,133 vs initialValue $2,551,243 and cashPnl $-1,213,110. Exit/risk rule must cap unresolved exposure and stale redeemable/loss rows.
- Evidence: Fresh PM leaderboard rank 5, PnL $529,667, volume $3,142,247, trades 252, largestWin $613,282. Public closed-position DESC call returned 50 rows; 50 positive rows across 47 eventSlugs, sum positive realized $9,102,607, top title sample: Will France win on 2026-06-26? [Yes, $613,282] | Will Egypt win on 2026-06-21? [Yes, $575,067] | Will Belgium win on 2026-06-15? [No, $526,502]. Current-position call returned 21 rows; recent activity call returned 500 rows / 475 TRADE rows.
- Inference: Type-C fit is inferred from trade count, distributed profitable eventSlugs, and non-single-winner concentration; API samples do not reveal full order book context, private model inputs, or whether losses outside the returned windows are complete.
- Scores: reproducibility 2/5, risk 5/5.
- Candidate pool decision: `include_C_watchlist_high_open_risk`.
- Trading conclusion: `not_trade_recommended=true`; `不建议交易` until robustness validation passes.

### Jsram - `0x83720820a8aa6c3f20ad71850e7a1a17d16c5223`

- Leaderboard metrics: rank 6, month PnL $520,452, volume $2,889,936, PnL/volume 18.0%.
- Trade count: 145 public `user-stats` trades; largestWin $474,540, or 91.2% of month PnL.
- Largest-win concentration: largest returned positive closed win $474,540; sum returned positive closed realized $3,573,234; largest/sum 13.3%; top-3/sum 28.9%.
- Profitable market distribution: 50 returned positive closed rows across 46 eventSlugs. Top profitable titles: Belgium vs. Senegal: Both Teams to Score [Yes, $474,540] | Liverpool FC vs. Tottenham Hotspur FC: O/U 3.5 [Under, $305,742] | Spread: Manchester City FC (-1.5) [Real Madrid CF, $250,769] | Spread: Liverpool FC (-1.5) [Tottenham Hotspur FC, $245,563] | Will Arsenal FC win on 2026-03-14? [Yes, $147,950].
- Candidate event-selection hypothesis: Selective soccer match-market account; repeated use of winner/draw, spread, O/U, BTTS, and team-total style markets across many fixtures.
- Entry-band hypothesis: Profitable closed-position avgPrice interquartile band 0.516-0.659 with median 0.581; recent BUY price band 0.430-0.480 where available. Treat this as descriptive, not a rule.
- Exit/risk hypothesis: Closed winners are mostly realized at settlement/redemption in public rows; current positions show 57 rows, currentValue $661,854 vs initialValue $2,375,820 and cashPnl $-1,713,966. Exit/risk rule must cap unresolved exposure and stale redeemable/loss rows.
- Evidence: Fresh PM leaderboard rank 6, PnL $520,452, volume $2,889,936, trades 145, largestWin $474,540. Public closed-position DESC call returned 50 rows; 50 positive rows across 46 eventSlugs, sum positive realized $3,573,234, top title sample: Belgium vs. Senegal: Both Teams to Score [Yes, $474,540] | Liverpool FC vs. Tottenham Hotspur FC: O/U 3.5 [Under, $305,742] | Spread: Manchester City FC (-1.5) [Real Madrid CF, $250,769]. Current-position call returned 57 rows; recent activity call returned 500 rows / 489 TRADE rows.
- Inference: Type-C fit is inferred from trade count, distributed profitable eventSlugs, and non-single-winner concentration; API samples do not reveal full order book context, private model inputs, or whether losses outside the returned windows are complete.
- Scores: reproducibility 2/5, risk 5/5.
- Candidate pool decision: `include_C_watchlist_high_open_risk`.
- Trading conclusion: `not_trade_recommended=true`; `不建议交易` until robustness validation passes.

### CandleHammerDrums - `0x7c1ee865a785de4c00ee90ed86a38489fb8bbab3`

- Leaderboard metrics: rank 8, month PnL $395,371, volume $2,072,962, PnL/volume 19.1%.
- Trade count: 146 public `user-stats` trades; largestWin $241,000, or 61.0% of month PnL.
- Largest-win concentration: largest returned positive closed win $241,000; sum returned positive closed realized $4,671,817; largest/sum 5.2%; top-3/sum 14.6%.
- Profitable market distribution: 50 returned positive closed rows across 37 eventSlugs. Top profitable titles: Will Belgium win on 2026-07-01? [No, $241,000] | Will Mexico win on 2026-06-30? [Yes, $235,410] | Will United States win on 2026-06-12? [Yes, $204,000] | Will Belgium win on 2026-06-21? [No, $203,000] | Spread: Brazil (-1.5) [Brazil, $180,000].
- Candidate event-selection hypothesis: Selective soccer match-market account; repeated use of winner/draw, spread, O/U, BTTS, and team-total style markets across many fixtures.
- Entry-band hypothesis: Profitable closed-position avgPrice interquartile band 0.422-0.565 with median 0.490; recent BUY price band 0.430-0.630 where available. Treat this as descriptive, not a rule.
- Exit/risk hypothesis: Closed winners are mostly realized at settlement/redemption in public rows; current positions show 74 rows, currentValue $17,081 vs initialValue $3,934,063 and cashPnl $-3,916,983. Exit/risk rule must cap unresolved exposure and stale redeemable/loss rows.
- Evidence: Fresh PM leaderboard rank 8, PnL $395,371, volume $2,072,962, trades 146, largestWin $241,000. Public closed-position DESC call returned 50 rows; 50 positive rows across 37 eventSlugs, sum positive realized $4,671,817, top title sample: Will Belgium win on 2026-07-01? [No, $241,000] | Will Mexico win on 2026-06-30? [Yes, $235,410] | Will United States win on 2026-06-12? [Yes, $204,000]. Current-position call returned 74 rows; recent activity call returned 500 rows / 479 TRADE rows.
- Inference: Type-C fit is inferred from trade count, distributed profitable eventSlugs, and non-single-winner concentration; API samples do not reveal full order book context, private model inputs, or whether losses outside the returned windows are complete.
- Scores: reproducibility 2/5, risk 5/5.
- Candidate pool decision: `include_C_watchlist_high_drawdown_risk`.
- Trading conclusion: `not_trade_recommended=true`; `不建议交易` until robustness validation passes.

### (blank username) - `0x5e9458202b5817a72cf81105ec8a30e6f3705ba1`

- Leaderboard metrics: rank 17, month PnL $246,867, volume $1,227,815, PnL/volume 20.1%.
- Trade count: 131 public `user-stats` trades; largestWin $122,222, or 49.5% of month PnL.
- Largest-win concentration: largest returned positive closed win $122,222; sum returned positive closed realized $3,768,245; largest/sum 3.2%; top-3/sum 9.3%.
- Profitable market distribution: 50 returned positive closed rows across 49 eventSlugs. Top profitable titles: Chicago Cubs vs. Milwaukee Brewers: O/U 7.5 [Over, $122,222] | New York Yankees vs. Boston Red Sox: O/U 8.5 [Over, $122,222] | Miami Marlins vs. Colorado Rockies [Colorado Rockies, $106,888] | Milwaukee Brewers vs. Cincinnati Reds: O/U 9.5 [Over, $104,081] | Los Angeles Angels vs. Arizona Diamondbacks [Arizona Diamondbacks, $102,281].
- Candidate event-selection hypothesis: Selective MLB moneyline/totals account; public winners are spread across many separate games rather than one tournament market.
- Entry-band hypothesis: Profitable closed-position avgPrice interquartile band 0.492-0.564 with median 0.518; recent BUY price band 0.420-0.570 where available. Treat this as descriptive, not a rule.
- Exit/risk hypothesis: Closed winners are mostly realized at settlement/redemption in public rows; current positions show 0 rows, currentValue $0 vs initialValue $0 and cashPnl $0. Exit/risk rule must cap unresolved exposure and stale redeemable/loss rows.
- Evidence: Fresh PM leaderboard rank 17, PnL $246,867, volume $1,227,815, trades 131, largestWin $122,222. Public closed-position DESC call returned 50 rows; 50 positive rows across 49 eventSlugs, sum positive realized $3,768,245, top title sample: Chicago Cubs vs. Milwaukee Brewers: O/U 7.5 [Over, $122,222] | New York Yankees vs. Boston Red Sox: O/U 8.5 [Over, $122,222] | Miami Marlins vs. Colorado Rockies [Colorado Rockies, $106,888]. Current-position call returned 0 rows; recent activity call returned 500 rows / 492 TRADE rows.
- Inference: Type-C fit is inferred from trade count, distributed profitable eventSlugs, and non-single-winner concentration; API samples do not reveal full order book context, private model inputs, or whether losses outside the returned windows are complete.
- Scores: reproducibility 3/5, risk 3/5.
- Candidate pool decision: `primary_C_validation_candidate`.
- Trading conclusion: `not_trade_recommended=true`; `不建议交易` until robustness validation passes.

### RISK-IS-NEVER-OK - `0x28059b3fb7af7ed76d83dc9121bbb72ec7cf76ed`

- Leaderboard metrics: rank 23, month PnL $187,275, volume $435,883, PnL/volume 43.0%.
- Trade count: 488 public `user-stats` trades; largestWin $149,732, or 80.0% of month PnL.
- Largest-win concentration: largest returned positive closed win $149,732; sum returned positive closed realized $1,589,718; largest/sum 9.4%; top-3/sum 25.6%.
- Profitable market distribution: 50 returned positive closed rows across 45 eventSlugs. Top profitable titles: Germany vs. Paraguay: Team to Advance [Paraguay, $149,732] | Spread: France (-2.5) [France, $145,225] | Spread: Argentina (-1.5) [Argentina, $112,665] | Mexico vs. Korea Republic: Both Teams to Score [No, $107,827] | Will Belgium win on 2026-07-01? [No, $101,300].
- Candidate event-selection hypothesis: Selective soccer match-market account; repeated use of winner/draw, spread, O/U, BTTS, and team-total style markets across many fixtures.
- Entry-band hypothesis: Profitable closed-position avgPrice interquartile band 0.418-0.549 with median 0.492; recent BUY price band 0.370-0.540 where available. Treat this as descriptive, not a rule.
- Exit/risk hypothesis: Closed winners are mostly realized at settlement/redemption in public rows; current positions show 26 rows, currentValue $500 vs initialValue $70,714 and cashPnl $-70,215. Exit/risk rule must cap unresolved exposure and stale redeemable/loss rows.
- Evidence: Fresh PM leaderboard rank 23, PnL $187,275, volume $435,883, trades 488, largestWin $149,732. Public closed-position DESC call returned 50 rows; 50 positive rows across 45 eventSlugs, sum positive realized $1,589,718, top title sample: Germany vs. Paraguay: Team to Advance [Paraguay, $149,732] | Spread: France (-2.5) [France, $145,225] | Spread: Argentina (-1.5) [Argentina, $112,665]. Current-position call returned 26 rows; recent activity call returned 500 rows / 449 TRADE rows.
- Inference: Type-C fit is inferred from trade count, distributed profitable eventSlugs, and non-single-winner concentration; API samples do not reveal full order book context, private model inputs, or whether losses outside the returned windows are complete.
- Scores: reproducibility 3/5, risk 3/5.
- Candidate pool decision: `secondary_C_validation_candidate`.
- Trading conclusion: `not_trade_recommended=true`; `不建议交易` until robustness validation passes.

## Cross-Account Findings

1. The strongest type-C evidence is distributed profitable closed rows: four accounts show 45-49 positive eventSlug counts and CandleHammerDrums shows 37; largest closed win concentration ranges from 3.2% to 13.3% across all five selected accounts.
2. The public data points toward sports-event selection rather than passive market-making: trade counts are 129-488 except one 252-trade account and recent activity is concentrated in event trades/redeems, while PnL/volume is far above a typical spread-capture profile.
3. Reproducibility remains weak without an odds model. The observable pattern is descriptive: pick match markets, enter mostly mid-probability outcome shares, size aggressively, and often hold through resolution. That does not prove edge after data latency, stale lines, fees, market impact, or adverse selection.
4. Open-position/current-position risk is material for the soccer-heavy accounts. Several show high initialValue with much lower currentValue, many negative-risk/redeemable rows, or large unrealized drawdown flags. This makes them unsuitable for direct replication before risk-rule reconstruction.

## Validation Required Before Any Trading

- Build a replay dataset of markets, timestamps, prices, outcomes, and closing/redemption events for each candidate eventSlug.
- Define entry rules from observable odds only; separate pre-match, in-play, and near-resolution entries if timestamps allow.
- Validate on out-of-sample sports/date slices and remove the best 5% of trades plus the best event/day to test concentration fragility.
- Apply capital caps, unresolved-position caps, fee/slippage/market-impact shocks, and stale-line delays.
- Until those checks pass: `不建议交易`.
