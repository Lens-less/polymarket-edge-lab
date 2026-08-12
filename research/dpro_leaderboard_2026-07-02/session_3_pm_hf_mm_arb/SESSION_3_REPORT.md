# Session 3 Report: Polymarket HF / MM / Arbitrage Deep Dive

Date: 2026-07-02
Fetch timestamp UTC: 2026-07-02T10:16:33Z

## Scope And Safety

- Scope: Polymarket high-frequency / market-making / arbitrage only.
- Inputs used: `/tmp/dpro_pm_top20_summary.csv`, `/tmp/dpro_leaderboard_sample.csv`, fresh public Polymarket leaderboard, public Polymarket user stats, public positions, public closed positions, public activity, and public CLOB order books for sampled open tokens.
- Not used: `.env`, wallet files, browser sessions, private APIs, API keys, credentials, or live order endpoints.
- No orders were placed, canceled, or simulated against a real account.
- Recommendation gate: every account remains `not_trade_recommended=true`; conclusion is `不建议交易` until offline robustness validation passes.

## Public Endpoints Queried

- Monthly leaderboard: `https://data-api.polymarket.com/v1/leaderboard?timePeriod=month&orderBy=PNL&category=overall&limit=50&offset=0`
- User stats: `https://data-api.polymarket.com/v1/user-stats?proxyAddress=<proxyWallet>`
- Current positions: `https://data-api.polymarket.com/positions?user=<proxyWallet>&limit=500&offset=0&sortBy=CURRENT&sortDirection=DESC&sizeThreshold=0.1`
- Closed positions: `https://data-api.polymarket.com/closed-positions?user=<proxyWallet>&limit=500&offset=0&sortBy=realizedpnl&sortDirection=DESC`
- Activity: `https://data-api.polymarket.com/activity?user=<proxyWallet>&excludeDepositsWithdrawals=false&limit=500&offset=0`
- CLOB book: `https://clob.polymarket.com/book?token_id=<asset>`

## Candidate Selection

Session 1 output files were not present when this session started, so candidates were selected independently. The heuristic favored very high trade count, high monthly volume, positive but low PnL/volume, many current positions, broad current event coverage, and negative-risk or multi-position event exposure.

Primary type-B candidates retained:
- RN1 `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`: 83,577 trades, $12,221,594 monthly volume, PnL/volume 1.63%, 500 open-position sample rows across 443 eventSlugs.
- swisstony `0x204f72f35326db932158cba6adff0b9a1da95e14`: 132,633 trades, $33,816,934 monthly volume, PnL/volume 1.82%, 500 open-position sample rows across 109 eventSlugs.
- mooseborzoi `0x84cfffc3f16dcc353094de30d4a45226eccd2f63`: 8,672 trades, $13,108,939 monthly volume, PnL/volume 4.97%, 285 open-position sample rows across 69 eventSlugs.

Watchlist candidates retained for cross-session validation:
- AnonymousUsername `0x9703676286b93c2eca71ca96e8757104519a69c2`: 3,866 trades, $3,154,138 volume, PnL/volume 3.70%, decision `retain_watchlist_type_B_candidate`.
- 0x32b484581fc5606dE9C1e43AF4636b6Be9BC8B21-1774274303653 `0x32b484581fc5606de9c1e43af4636b6be9bc8b21`: 3,127 trades, $4,449,039 volume, PnL/volume 5.01%, decision `retain_watchlist_type_B_candidate`.
- GoalLineGhost `0x0346afae2603313d2bbee96b628536c8cbe352a5`: 11,322 trades, $9,673,091 volume, PnL/volume 7.21%, decision `retain_watchlist_type_B_candidate`.
- curie `0xd1ed12197b7dc22dede923727e9b714024dbd7cb`: 17,813 trades, $1,727,716 volume, PnL/volume 6.95%, decision `retain_watchlist_type_B_candidate`.
- purplegatto `0x032eb1bc893940263ad0b01889f262fc232f2a9e`: 4,996 trades, $1,803,246 volume, PnL/volume 12.94%, decision `watchlist_not_primary_type_B_due_higher_pnl_per_volume_or_weaker_evidence`.

## Cross-Account Findings

- The strongest type-B evidence is turnover and breadth, not confirmed maker status. Public activity does not identify whether fills were maker or taker, and public order books do not expose account-owned resting orders.
- `RN1` and `swisstony` are the clearest high-turnover candidates: both combine low PnL/volume near 1.6-1.8%, very high trade counts, many open positions, and recent rapid trading across many markets.
- Negative-risk exposure is common in the best candidates. `swisstony`, `mooseborzoi`, `AnonymousUsername`, `GoalLineGhost`, `curie`, and `purplegatto` all show substantial `negativeRisk=true` open positions; this supports an arbitrage/negative-risk hypothesis but does not prove risk-free execution.
- CLOB book checks on top open tokens often show tight public spreads. That supports the existence of spread-capture opportunities in the markets sampled, but it is not account-specific evidence that the wallet is market-making.
- Largest-win concentration remains a risk. Some accounts that look high-frequency by trade count still have large closed wins relative to monthly PnL, so leaderboard PnL may be partly directional or event-tail driven.

## Account Deep Dives

### RN1 `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`

- Leaderboard metrics: rank 22, monthly PnL $199,175, monthly volume $12,221,594, PnL/volume 1.63%.
- Trade count and largest win: 83,577 trades; largest win $242,697; largest win/month PnL 1.22x.
- Current positions: 500 sample rows, value $281,250, cost $521,896, cash PnL $-240,646; 96 negative-risk rows; 22 mergeable rows.
- Market breadth: open 443 eventSlugs/478 titles; recent activity 20 eventSlugs/23 titles; closed sample 47 eventSlugs.
- Recent activity sample: 500 trades over 115.9 minutes, 4.314 trades/min, median inter-trade 5s, buy/sell 500/0, median trade size $12.
- Spread/MM/arbitrage evidence: 83,577 lifetime trades; monthly volume $12,221,594; PnL/volume 1.63%; 500 open positions across 443 eventSlugs (96 negativeRisk, 22 mergeable); recent public activity sample: 500 trades across 20 eventSlugs in 115.9 minutes; median inter-trade 5.0s; max 31/minute; CLOB sample on top open tokens: median spread 0.0100; 3/3 <=2c, 3/3 <=5c; 43 open eventSlugs have multiple positions; examples: mlb-bos-nyy-2026-06-06: 7 positions (New York Yankees, Under, Over, New York Yankees, Under) | fifwc-prt-hrv-2026-07-02-more-markets: 5 positions (No, Portugal, Over, Under, Over) | fifwc-arg-cvi-2026-07-03-more-markets: 4 positions (Argentina, Argentina, Argentina, Under).
- Evidence vs inference: Evidence is public stats/positions/activity/books. Inference is: high-frequency or automated execution is plausible from total trades and recent cadence; low PnL per volume fits spread capture, rebate, or turnover-heavy edge more than one-off directional betting; large inventory breadth fits market-making or broad event-arbitrage inventory management; negative-risk and multi-position event exposure support a negative-risk/arbitrage hypothesis.
- Scores: reproducibility 2/5, risk 5/5.
- Candidate pool decision: `retain_primary_type_B_candidate`.
- Recommendation: `not_trade_recommended=true`; conclusion `不建议交易`.

### swisstony `0x204f72f35326db932158cba6adff0b9a1da95e14`

- Leaderboard metrics: rank 4, monthly PnL $616,650, monthly volume $33,816,934, PnL/volume 1.82%.
- Trade count and largest win: 132,633 trades; largest win $1,171,845; largest win/month PnL 1.90x.
- Current positions: 500 sample rows, value $1,677,847, cost $1,676,202, cash PnL $1,646; 161 negative-risk rows; 193 mergeable rows.
- Market breadth: open 109 eventSlugs/428 titles; recent activity 47 eventSlugs/86 titles; closed sample 42 eventSlugs.
- Recent activity sample: 487 trades over 36.15 minutes, 13.472 trades/min, median inter-trade 3.0s, buy/sell 487/0, median trade size $40.
- Spread/MM/arbitrage evidence: 132,633 lifetime trades; monthly volume $33,816,934; PnL/volume 1.82%; 500 open positions across 109 eventSlugs (161 negativeRisk, 193 mergeable); recent public activity sample: 487 trades across 47 eventSlugs in 36.1 minutes; median inter-trade 3.0s; max 27/minute; CLOB sample on top open tokens: median spread 0.0050; 5/5 <=2c, 5/5 <=5c; 59 open eventSlugs have multiple positions; examples: fifwc-prt-hrv-2026-07-02-more-markets: 40 positions (Under, Over, Portugal, Portugal, Over) | fifwc-arg-cvi-2026-07-03-more-markets: 36 positions (Argentina, Argentina, Under, Over, Argentina) | fifwc-esp-aut-2026-07-02-more-markets: 33 positions (Under, Over, Austria, Spain, Spain).
- Evidence vs inference: Evidence is public stats/positions/activity/books. Inference is: high-frequency or automated execution is plausible from total trades and recent cadence; low PnL per volume fits spread capture, rebate, or turnover-heavy edge more than one-off directional betting; large inventory breadth fits market-making or broad event-arbitrage inventory management; negative-risk and multi-position event exposure support a negative-risk/arbitrage hypothesis; largest win exceeds monthly PnL, so realized leaderboard PnL may still be dominated by tail event outcomes.
- Scores: reproducibility 2/5, risk 5/5.
- Candidate pool decision: `retain_primary_type_B_candidate`.
- Recommendation: `not_trade_recommended=true`; conclusion `不建议交易`.

### mooseborzoi `0x84cfffc3f16dcc353094de30d4a45226eccd2f63`

- Leaderboard metrics: rank 3, monthly PnL $652,086, monthly volume $13,108,939, PnL/volume 4.97%.
- Trade count and largest win: 8,672 trades; largest win $880,906; largest win/month PnL 1.35x.
- Current positions: 285 sample rows, value $1,378,155, cost $1,270,994, cash PnL $107,161; 194 negative-risk rows; 0 mergeable rows.
- Market breadth: open 69 eventSlugs/282 titles; recent activity 15 eventSlugs/38 titles; closed sample 48 eventSlugs.
- Recent activity sample: 492 trades over 258.75 minutes, 1.901 trades/min, median inter-trade 2s, buy/sell 492/0, median trade size $15.
- Spread/MM/arbitrage evidence: 8,672 lifetime trades; monthly volume $13,108,939; PnL/volume 4.97%; 285 open positions across 69 eventSlugs (194 negativeRisk, 0 mergeable); recent public activity sample: 492 trades across 15 eventSlugs in 258.8 minutes; median inter-trade 2.0s; max 31/minute; CLOB sample on top open tokens: median spread 0.0010; 5/5 <=2c, 5/5 <=5c; 39 open eventSlugs have multiple positions; examples: mlb-world-series-champion-2026: 26 positions (No, Yes, No, Yes, Yes) | world-cup-golden-boot-winner: 25 positions (Yes, Yes, Yes, Yes, Yes) | mls-cup-winner-2026: 24 positions (No, Yes, Yes, Yes, Yes).
- Evidence vs inference: Evidence is public stats/positions/activity/books. Inference is: high-frequency or automated execution is plausible from total trades and recent cadence; low PnL per volume fits spread capture, rebate, or turnover-heavy edge more than one-off directional betting; large inventory breadth fits market-making or broad event-arbitrage inventory management; negative-risk and multi-position event exposure support a negative-risk/arbitrage hypothesis.
- Scores: reproducibility 2/5, risk 4/5.
- Candidate pool decision: `retain_primary_type_B_candidate`.
- Recommendation: `not_trade_recommended=true`; conclusion `不建议交易`.

### AnonymousUsername `0x9703676286b93c2eca71ca96e8757104519a69c2`

- Leaderboard metrics: rank 39, monthly PnL $116,853, monthly volume $3,154,138, PnL/volume 3.70%.
- Trade count and largest win: 3,866 trades; largest win $160,573; largest win/month PnL 1.37x.
- Current positions: 500 sample rows, value $743,548, cost $1,406,968, cash PnL $-663,420; 312 negative-risk rows; 0 mergeable rows.
- Market breadth: open 225 eventSlugs/480 titles; recent activity 17 eventSlugs/23 titles; closed sample 43 eventSlugs.
- Recent activity sample: 495 trades over 159.55 minutes, 3.102 trades/min, median inter-trade 11.5s, buy/sell 494/1, median trade size $10.
- Spread/MM/arbitrage evidence: 3,866 lifetime trades; monthly volume $3,154,138; PnL/volume 3.70%; 500 open positions across 225 eventSlugs (312 negativeRisk, 0 mergeable); recent public activity sample: 495 trades across 17 eventSlugs in 159.6 minutes; median inter-trade 11.5s; max 46/minute; CLOB sample on top open tokens: median spread 0.0025; 5/5 <=2c, 5/5 <=5c; 46 open eventSlugs have multiple positions; examples: democratic-presidential-nominee-2028: 44 positions (Yes, Yes, Yes, Yes, Yes) | presidential-election-winner-2028: 36 positions (Yes, Yes, Yes, Yes, Yes) | republican-presidential-nominee-2028: 34 positions (Yes, Yes, Yes, Yes, Yes).
- Evidence vs inference: Evidence is public stats/positions/activity/books. Inference is: high-frequency or automated execution is plausible from total trades and recent cadence; low PnL per volume fits spread capture, rebate, or turnover-heavy edge more than one-off directional betting; large inventory breadth fits market-making or broad event-arbitrage inventory management; negative-risk and multi-position event exposure support a negative-risk/arbitrage hypothesis.
- Scores: reproducibility 2/5, risk 4/5.
- Candidate pool decision: `retain_watchlist_type_B_candidate`.
- Recommendation: `not_trade_recommended=true`; conclusion `不建议交易`.

### 0x32b484581fc5606dE9C1e43AF4636b6Be9BC8B21-1774274303653 `0x32b484581fc5606de9c1e43af4636b6be9bc8b21`

- Leaderboard metrics: rank 21, monthly PnL $222,830, monthly volume $4,449,039, PnL/volume 5.01%.
- Trade count and largest win: 3,127 trades; largest win $225,855; largest win/month PnL 1.01x.
- Current positions: 500 sample rows, value $145,223, cost $857,761, cash PnL $-712,538; 36 negative-risk rows; 12 mergeable rows.
- Market breadth: open 482 eventSlugs/489 titles; recent activity 7 eventSlugs/7 titles; closed sample 37 eventSlugs.
- Recent activity sample: 493 trades over 31.283 minutes, 15.759 trades/min, median inter-trade 1.0s, buy/sell 493/0, median trade size $2.
- Spread/MM/arbitrage evidence: 3,127 lifetime trades; monthly volume $4,449,039; PnL/volume 5.01%; 500 open positions across 482 eventSlugs (36 negativeRisk, 12 mergeable); recent public activity sample: 493 trades across 7 eventSlugs in 31.3 minutes; median inter-trade 1.0s; max 68/minute; CLOB sample on top open tokens: median spread 0.0025; 5/5 <=2c, 5/5 <=5c; 16 open eventSlugs have multiple positions; examples: what-price-will-bitcoin-hit-may-11-17: 3 positions (Yes, Yes, Yes) | what-price-will-bitcoin-hit-on-may-2: 3 positions (Yes, Yes, Yes) | atp-nakashi-struff-2026-07-01: 2 positions (Brandon Nakashima, Jan-Lennard Struff).
- Evidence vs inference: Evidence is public stats/positions/activity/books. Inference is: high-frequency or automated execution is plausible from total trades and recent cadence; low PnL per volume fits spread capture, rebate, or turnover-heavy edge more than one-off directional betting; large inventory breadth fits market-making or broad event-arbitrage inventory management.
- Scores: reproducibility 2/5, risk 5/5.
- Candidate pool decision: `retain_watchlist_type_B_candidate`.
- Recommendation: `not_trade_recommended=true`; conclusion `不建议交易`.

### GoalLineGhost `0x0346afae2603313d2bbee96b628536c8cbe352a5`

- Leaderboard metrics: rank 2, monthly PnL $697,202, monthly volume $9,673,091, PnL/volume 7.21%.
- Trade count and largest win: 11,322 trades; largest win $938,350; largest win/month PnL 1.35x.
- Current positions: 500 sample rows, value $499,702, cost $567,614, cash PnL $-67,912; 141 negative-risk rows; 80 mergeable rows.
- Market breadth: open 319 eventSlugs/442 titles; recent activity 16 eventSlugs/28 titles; closed sample 43 eventSlugs.
- Recent activity sample: 500 trades over 71.55 minutes, 6.988 trades/min, median inter-trade 4s, buy/sell 500/0, median trade size $3.
- Spread/MM/arbitrage evidence: 11,322 lifetime trades; monthly volume $9,673,091; PnL/volume 7.21%; 500 open positions across 319 eventSlugs (141 negativeRisk, 80 mergeable); recent public activity sample: 500 trades across 16 eventSlugs in 71.5 minutes; median inter-trade 4.0s; max 30/minute; CLOB sample on top open tokens: median spread 0.0100; 5/5 <=2c, 5/5 <=5c; 64 open eventSlugs have multiple positions; examples: fifwc-esp-aut-2026-07-02-more-markets: 24 positions (Under, Austria, Spain, Under, Under) | fifwc-prt-hrv-2026-07-02-more-markets: 19 positions (Under, Croatia, Over, Under, Over) | fifwc-par-fra-2026-07-04-more-markets: 16 positions (Under, France, Paraguay, Over, Paraguay).
- Evidence vs inference: Evidence is public stats/positions/activity/books. Inference is: high-frequency or automated execution is plausible from total trades and recent cadence; large inventory breadth fits market-making or broad event-arbitrage inventory management; negative-risk and multi-position event exposure support a negative-risk/arbitrage hypothesis.
- Scores: reproducibility 2/5, risk 5/5.
- Candidate pool decision: `retain_watchlist_type_B_candidate`.
- Recommendation: `not_trade_recommended=true`; conclusion `不建议交易`.

### curie `0xd1ed12197b7dc22dede923727e9b714024dbd7cb`

- Leaderboard metrics: rank 38, monthly PnL $120,136, monthly volume $1,727,716, PnL/volume 6.95%.
- Trade count and largest win: 17,813 trades; largest win $132,457; largest win/month PnL 1.10x.
- Current positions: 500 sample rows, value $627, cost $244,868, cash PnL $-244,241; 69 negative-risk rows; 2 mergeable rows.
- Market breadth: open 414 eventSlugs/453 titles; recent activity 11 eventSlugs/68 titles; closed sample 47 eventSlugs.
- Recent activity sample: 395 trades over 268.65 minutes, 1.47 trades/min, median inter-trade 2.0s, buy/sell 395/0, median trade size $41.
- Spread/MM/arbitrage evidence: 17,813 lifetime trades; monthly volume $1,727,716; PnL/volume 6.95%; 500 open positions across 414 eventSlugs (69 negativeRisk, 2 mergeable); recent public activity sample: 395 trades across 11 eventSlugs in 268.6 minutes; median inter-trade 2.0s; max 43/minute; CLOB sample on top open tokens: median spread 0.0060; 4/4 <=2c, 4/4 <=5c; 63 open eventSlugs have multiple positions; examples: nba-nyk-sas-2026-06-05: 5 positions (Knicks, Spurs, Knicks, Spurs, Knicks) | mlb-min-det-2026-06-10: 4 positions (Detroit Tigers, Detroit Tigers, Under, Detroit Tigers) | fifwc-ecu-kor-2026-06-20-total-corners: 4 positions (Under, Over, Under, Under).
- Evidence vs inference: Evidence is public stats/positions/activity/books. Inference is: high-frequency or automated execution is plausible from total trades and recent cadence; large inventory breadth fits market-making or broad event-arbitrage inventory management; negative-risk and multi-position event exposure support a negative-risk/arbitrage hypothesis.
- Scores: reproducibility 2/5, risk 4/5.
- Candidate pool decision: `retain_watchlist_type_B_candidate`.
- Recommendation: `not_trade_recommended=true`; conclusion `不建议交易`.

### purplegatto `0x032eb1bc893940263ad0b01889f262fc232f2a9e`

- Leaderboard metrics: rank 19, monthly PnL $233,426, monthly volume $1,803,246, PnL/volume 12.94%.
- Trade count and largest win: 4,996 trades; largest win $210,120; largest win/month PnL 0.90x.
- Current positions: 500 sample rows, value $147,490, cost $623,195, cash PnL $-475,705; 196 negative-risk rows; 118 mergeable rows.
- Market breadth: open 263 eventSlugs/427 titles; recent activity 34 eventSlugs/60 titles; closed sample 46 eventSlugs.
- Recent activity sample: 495 trades over 403.65 minutes, 1.226 trades/min, median inter-trade 21.0s, buy/sell 495/0, median trade size $12.
- Spread/MM/arbitrage evidence: 4,996 lifetime trades; monthly volume $1,803,246; PnL/volume 12.94%; 500 open positions across 263 eventSlugs (196 negativeRisk, 118 mergeable); recent public activity sample: 495 trades across 34 eventSlugs in 403.6 minutes; median inter-trade 21.0s; max 12/minute; CLOB sample on top open tokens: median spread 0.0010; 5/5 <=2c, 5/5 <=5c; 84 open eventSlugs have multiple positions; examples: world-cup-winner: 17 positions (Yes, No, No, No, Yes) | world-cup-nation-to-reach-round-of-16: 16 positions (Yes, No, Yes, No, Yes) | 2026-mens-wimbledon-winner: 14 positions (No, No, No, Yes, No).
- Evidence vs inference: Evidence is public stats/positions/activity/books. Inference is: high-frequency or automated execution is plausible from total trades and recent cadence; large inventory breadth fits market-making or broad event-arbitrage inventory management; negative-risk and multi-position event exposure support a negative-risk/arbitrage hypothesis.
- Scores: reproducibility 1/5, risk 4/5.
- Candidate pool decision: `watchlist_not_primary_type_B_due_higher_pnl_per_volume_or_weaker_evidence`.
- Recommendation: `not_trade_recommended=true`; conclusion `不建议交易`.

## Reproducibility Notes

- Reproducibility is scored low because public data lacks maker/taker flags, account-owned order IDs, queue position, cancellation history, fee/rebate treatment, and latency conditions.
- A valid validation path needs historical order books, fills with maker/taker attribution where available, market-resolution data, fees/slippage, inventory constraints, and robustness tests that remove the best markets and best trades.
- Until that validation is done, no account or hypothesis should move to live or paper execution. `不建议交易`.

## Deliverables

- `pm_hf_mm_arb_accounts.csv`
- `SESSION_3_REPORT.md`
- Raw public snapshots under `raw/`
