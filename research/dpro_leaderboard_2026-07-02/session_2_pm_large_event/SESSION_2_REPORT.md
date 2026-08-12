# Session 2 Report: Polymarket Large Event Bet Deep Dive

Generated: 2026-07-02T10:16:45+00:00

## Scope And Safety

- Scope: Polymarket large-event / concentrated event-bet accounts only.
- Data used: local `/tmp/dpro_pm_top20_summary.csv`, local `/tmp/dpro_leaderboard_sample.csv` for context, and unauthenticated public Polymarket `data-api.polymarket.com` endpoints.
- Session 1 candidate output was not present at analysis time, so candidates were selected independently from the local Polymarket top-20 sample.
- No `.env`, wallet files, browser sessions, API keys, private endpoints, account connections, or order endpoints were used.
- All accounts remain `not_trade_recommended=true`: 不建议交易 until robustness validation passes.

## Candidate Selection

Selection favored accounts with low public trade count, high monthly PnL/volume, large `largestWin` relative to monthly PnL, realized PnL concentrated in closed/settled markets, and few or low-value current positions. I analyzed six accounts because enough valid type-A candidates existed in the local sample.

| Account | Rank | Month PnL | PnL/Vol | Trades | Largest Win / Month PnL | Current Value | Top-Win Category | Decision |
|---|---:|---:|---:|---:|---:|---:|---|---|
| muchobliged `0x095fbca2...` | 1 | $3,141,057 | 0.430 | 7 | 0.76x | $0 | soccer | include_type_a_representative; reject_for_live_until_validation |
| maz26 `0x67542c32...` | 7 | $417,998 | 0.081 | 126 | 2.99x | $0 | soccer | include_type_a_representative; reject_for_live_until_validation |
| Qpkwks `0x9ee8bbc3...` | 9 | $384,403 | 0.203 | 36 | 2.94x | $12,253 | soccer | include_type_a_watchlist; current_position_count_not_tiny |
| surfandturf `0x9f2fe025...` | 14 | $254,355 | 0.067 | 379 | 3.44x | $0 | combat_sports | include_cross_sport_type_a_case; reject_for_live_until_validation |
| NO-GOD-PLEASE-NO `0x5257aa84...` | 16 | $252,089 | 0.174 | 168 | 1.12x | $0 | soccer | include_type_a_watchlist; lower_priority_than_top_concentration_cases |
| palegrit `0xf5fabdcd...` | 18 | $241,095 | 0.166 | 8 | 0.43x | $157,841 | tennis | include_type_a_watchlist; active_tennis_exposure_requires_followup |

## Account Findings

### muchobliged (0x095fbca2e0eaf0c9841005135427e1e0117190b2)

- Leaderboard metrics: rank 1, month PnL $3,141,057, month volume $7,301,624, PnL/volume 0.430.
- Trade count and largest win: public stats trades 7; largestWin $2,396,970, equal to 0.76x month PnL.
- Current positions: 1 positions from public endpoint, active marked value $0, open-risk positions 0, redeemable current positions 1.
- Winning markets: England vs. DR Congo: O/U 2.5 [Over: $2,396,970] | Belgium vs. Senegal: O/U 2.5 [Over: $777,807] | Germany vs. Paraguay: Team to Advance [Paraguay: $127,098] | Will Mexico win on 2026-06-30? [Yes: $33,608] | United States vs. Bosnia and Herzegovina: Team to Advance [United States: $21,933].
- Concentration evidence: positive closed realized sum $3,374,530; top closed win $2,396,970; top-1 share of positive closed PnL 71.0%; top-3 share 97.8%; top closed win equals 0.76x month PnL.
- Hypothesis: Concentrated soccer match-prop/event-outcome bettor; June/July realized PnL appears dominated by a few settled FIFA/football markets.
- Evidence vs inference: Evidence is the leaderboard/stats/closed-position/current-position snapshot above. Top-win category `soccer` and category mix `{"soccer": 6}` are inferred from titles/slugs; public data cannot prove private information, intent, fill quality, or durable edge.
- Reproducibility score: 1/5. Risk score: 5/5. Candidate decision: include_type_a_representative; reject_for_live_until_validation. Conclusion: 不建议交易.

### maz26 (0x67542c3219b37fd1610aad290676ff91cdbfe3bc)

- Leaderboard metrics: rank 7, month PnL $417,998, month volume $5,168,917, PnL/volume 0.081.
- Trade count and largest win: public stats trades 126; largestWin $1,248,667, equal to 2.99x month PnL.
- Current positions: 1 positions from public endpoint, active marked value $0, open-risk positions 0, redeemable current positions 1.
- Winning markets: Will Belgium vs. Senegal end in a draw? [Yes: $1,248,667] | Will Canada win on 2026-06-28? [Yes: $1,094,763] | Will United States win on 2026-06-25? [No: $539,388] | Spread: Morocco (-1.5) [Morocco: $309,740] | Will Ecuador vs. Curaçao end in a draw? [Yes: $282,674].
- Concentration evidence: positive closed realized sum $6,798,829; top closed win $1,248,667; top-1 share of positive closed PnL 18.4%; top-3 share 42.4%; top closed win equals 2.99x month PnL.
- Hypothesis: Concentrated soccer/draw bettor with realized PnL dominated by a small set of resolved football markets.
- Evidence vs inference: Evidence is the leaderboard/stats/closed-position/current-position snapshot above. Top-win category `soccer` and category mix `{"soccer": 20}` are inferred from titles/slugs; public data cannot prove private information, intent, fill quality, or durable edge.
- Reproducibility score: 1/5. Risk score: 5/5. Candidate decision: include_type_a_representative; reject_for_live_until_validation. Conclusion: 不建议交易.

### Qpkwks (0x9ee8bbc36d378af72e5f6b8e2ea2eb67c05a89de)

- Leaderboard metrics: rank 9, month PnL $384,403, month volume $1,897,945, PnL/volume 0.203.
- Trade count and largest win: public stats trades 36; largestWin $1,131,232, equal to 2.94x month PnL.
- Current positions: 20 positions from public endpoint, active marked value $12,253, open-risk positions 1, redeemable current positions 19.
- Winning markets: Will England win on 2026-06-23? [No: $1,131,232] | Will Germany win on 2026-06-29? [No: $767,133] | Will Belgium win on 2026-06-21? [No: $671,331] | Will Belgium win on 2026-07-01? [No: $519,140] | Will Portugal win on 2026-06-27? [No: $340,490].
- Concentration evidence: positive closed realized sum $4,175,118; top closed win $1,131,232; top-1 share of positive closed PnL 27.1%; top-3 share 61.5%; top closed win equals 2.94x month PnL.
- Hypothesis: Selective soccer outcome bettor, often contrarian No/draw-style exposures; current marked value is low but position count is not minimal.
- Evidence vs inference: Evidence is the leaderboard/stats/closed-position/current-position snapshot above. Top-win category `soccer` and category mix `{"soccer": 14}` are inferred from titles/slugs; public data cannot prove private information, intent, fill quality, or durable edge.
- Reproducibility score: 1/5. Risk score: 5/5. Candidate decision: include_type_a_watchlist; current_position_count_not_tiny. Conclusion: 不建议交易.

### surfandturf (0x9f2fe025f84839ca81dd8e0338892605702d2ca8)

- Leaderboard metrics: rank 14, month PnL $254,355, month volume $3,818,665, PnL/volume 0.067.
- Trade count and largest win: public stats trades 379; largestWin $873,825, equal to 3.44x month PnL.
- Current positions: 0 positions from public endpoint, active marked value $0, open-risk positions 0, redeemable current positions 0.
- Winning markets: UFC Freedom 250: Justin Gaethje vs. Ilia Topuria (Lightweight, Main Card) [Justin Gaethje: $873,825] | 76ers vs. Celtics [76ers: $794,240] | UFC 328: Sean Strickland vs. Khamzat Chimaev (Middleweight, Main Card) [Sean Strickland: $711,751] | Will United States win on 2026-06-12? [Yes: $667,058] | 76ers vs. Celtics [76ers: $583,778].
- Concentration evidence: positive closed realized sum $16,904,825; top closed win $873,825; top-1 share of positive closed PnL 5.2%; top-3 share 14.1%; top closed win equals 3.44x month PnL.
- Hypothesis: Cross-sport concentrated event bettor; top realized win is a UFC market, suggesting non-replicable event knowledge or high-variance directional selection.
- Evidence vs inference: Evidence is the leaderboard/stats/closed-position/current-position snapshot above. Top-win category `combat_sports` and category mix `{"basketball": 6, "combat_sports": 2, "soccer": 12}` are inferred from titles/slugs; public data cannot prove private information, intent, fill quality, or durable edge.
- Reproducibility score: 1/5. Risk score: 5/5. Candidate decision: include_cross_sport_type_a_case; reject_for_live_until_validation. Conclusion: 不建议交易.

### NO-GOD-PLEASE-NO (0x5257aa84944804bbb0c718814ebebeeafaca3e2a)

- Leaderboard metrics: rank 16, month PnL $252,089, month volume $1,445,052, PnL/volume 0.174.
- Trade count and largest win: public stats trades 168; largestWin $281,214, equal to 1.12x month PnL.
- Current positions: 0 positions from public endpoint, active marked value $0, open-risk positions 0, redeemable current positions 0.
- Winning markets: Will Belgium win on 2026-07-01? [No: $281,214] | Will Chelsea FC win on 2026-05-04? [No: $252,147] | Will Manchester City FC win on 2026-05-19? [No: $151,217] | Germany vs. Paraguay: Team to Advance [Paraguay: $140,601] | Will United States vs. Paraguay end in a draw? [No: $93,296].
- Concentration evidence: positive closed realized sum $2,089,614; top closed win $281,214; top-1 share of positive closed PnL 13.5%; top-3 share 32.8%; top closed win equals 1.12x month PnL.
- Hypothesis: Selective sports outcome bettor with zero current public exposure and a few large resolved wins, but concentration is less extreme than the strongest cases.
- Evidence vs inference: Evidence is the leaderboard/stats/closed-position/current-position snapshot above. Top-win category `soccer` and category mix `{"other": 2, "soccer": 18}` are inferred from titles/slugs; public data cannot prove private information, intent, fill quality, or durable edge.
- Reproducibility score: 1/5. Risk score: 5/5. Candidate decision: include_type_a_watchlist; lower_priority_than_top_concentration_cases. Conclusion: 不建议交易.

### palegrit (0xf5fabdcdc6eb6d9765a228824f16cca9c91f62df)

- Leaderboard metrics: rank 18, month PnL $241,095, month volume $1,455,439, PnL/volume 0.166.
- Trade count and largest win: public stats trades 8; largestWin $103,090, equal to 0.43x month PnL.
- Current positions: 2 positions from public endpoint, active marked value $157,841, open-risk positions 1, redeemable current positions 1.
- Winning markets: Wimbledon ATP: Jenson Brooksby vs Ignacio Buse [Jenson Brooksby: $103,090] | Wimbledon WTA: Solana Sierra vs Coco Gauff [Coco Gauff: $57,585] | Wimbledon WTA: Talia Gibson vs Marie Bouzkova [Marie Bouzkova: $52,083] | Wimbledon ATP: Nicolas Mejia vs Michael Zheng [Michael Zheng: $49,065] | Wimbledon WTA: Jelena Ostapenko vs Antonia Ruzic [Jelena Ostapenko: $42,284].
- Concentration evidence: positive closed realized sum $308,381; top closed win $103,090; top-1 share of positive closed PnL 33.4%; top-3 share 69.0%; top closed win equals 0.43x month PnL.
- Hypothesis: Very low-trade tennis-focused event bettor; edge, if any, is likely event-specific selection rather than market making.
- Evidence vs inference: Evidence is the leaderboard/stats/closed-position/current-position snapshot above. Top-win category `tennis` and category mix `{"tennis": 6}` are inferred from titles/slugs; public data cannot prove private information, intent, fill quality, or durable edge.
- Reproducibility score: 1/5. Risk score: 5/5. Candidate decision: include_type_a_watchlist; active_tennis_exposure_requires_followup. Conclusion: 不建议交易.

## Cross-Account Pattern

- The strongest type-A cases are not market-making profiles: they show low to moderate public trade counts, high PnL/volume, and realized gains dominated by a few resolved sports markets.
- Soccer/football match props and outcomes dominate the selected pool, with additional tennis and UFC/combat-sport examples. This supports a large-event directional-bet hypothesis, not a generic spread-capture hypothesis.
- Concentration is the core finding and the core risk: several accounts have top closed wins larger than or close to their reported monthly PnL, which implies leaderboard performance is sensitive to one or a few settled events and may not be repeatable.
- Current public exposure is usually low by marked value, but the position endpoint can include redeemable/resolved positions; current position count alone is less reliable than active marked value and open-risk count.

## Candidate Pool Decision

- Include these accounts only as research representatives for the Session 7 hypothesis pool: concentrated sports event selection, possible late-information/odds-mispricing/negative-risk variants, and extreme win-concentration stress tests.
- Exclude all from any tradeable candidate pool until offline validation passes train/validation/test splits, rolling windows, double fee/slippage, parameter perturbation, removal of best 5% trades, removal of best single event/month, and category slices.
- Current recommendation: 不建议交易.

## Files

- `pm_large_event_accounts.csv`: structured account-level evidence and decisions.
- `raw/leaderboard_month_pnl_top50.json`: fresh public leaderboard snapshot.
- `raw/*_stats.json`, `raw/*_closed_positions.json`, `raw/*_current_positions.json`, `raw/*_activity_500.json`: public per-account snapshots.
