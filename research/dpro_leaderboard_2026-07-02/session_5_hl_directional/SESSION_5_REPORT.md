# Session 5 Report: Hyperliquid Concentrated Directional Accounts

- Generated: 2026-07-02T10:20:19Z UTC / 2026-07-02T18:20:19+08:00 Asia-Shanghai
- Scope: public d.Pro leaderboard API, public Hyperliquid `clearinghouseState` and `userFills`, plus local CSV samples only.
- Safety: no `.env`, wallet files, browser sessions, API keys, private account endpoints, or order endpoints were read or used.
- Recommendation gate: every row remains `not_trade_recommended=true`; conclusion is `不建议交易` until offline robustness validation passes.

## Inputs Read

- Local `/tmp/dpro_hl_top10_summary.csv`: 10 rows.
- Local `/tmp/dpro_leaderboard_sample.csv`: 150 rows.
- Session 1 output: not present at initial selection time; full artifacts appeared during this run and were cross-checked after the deep-dive fetch.
- Public d.Pro endpoint: top 100 sorted by `pnl_month` descending.
- Public Hyperliquid endpoint: `clearinghouseState` and `userFills` for selected public leaderboard addresses.

## Session 1 Cross-Check

Session 1 later classified `Penision Fund` and `0xcf90cfecf74e631feea816d02e757c0c8e895c0e` as `D_hl_concentrated_directional`, matching this session's two cleanest examples. It classified `0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36` as high-turnover multi-asset, so this report keeps it only as a current HYPE-dominant watchlist account. It had deferred or excluded some accounts that this session fetched directly, including `0x4f7634c03ec4e87e14725c84913ade523c6fad5a`, `0xf02d16a272a842f8bac1d9a9e773aba1933454c6`, and `lmlmlm`; those remain deep-dive directional candidates because refreshed public position data shows dominant current exposure.

## Selection Logic

I prioritized accounts with few or clearly dominant current positions, large current notional, positive monthly leaderboard PnL/ROI, and current exposure that looks directional rather than broad high-turnover. User fills are treated as incomplete when the public endpoint returns the 2,000-fill cap.

## Analyzed Accounts

| Rank | Name | Address | Month PnL / ROI | Current Book | Margin/Lev Proxy | Fills Returned | Decision |
|---:|---|---|---:|---|---|---:|---|
| 1 | Penision Fund | `0x0ddf9b...38a902` | $16,762,612 / 50.8% | 1 pos, $82,285,000, top1 100.0%; ETH short 100.0% | notional/account 3.86x; margin/account 128.7% | 2000 capped, 1 coins | include_priority_type_D_directional_watchlist |
| 19 | (blank) | `0xcf90cf...895c0e` | $7,121,404 / 85.0% | 1 pos, $46,680,366, top1 100.0%; BTC short 100.0% | notional/account 3.11x; margin/account 100.0% | 2000 capped, 2 coins | include_priority_type_D_directional_watchlist |
| 5 | (blank) | `0xb83de0...7d6e36` | $12,445,828 / 58.9% | 12 pos, $56,953,222, top1 97.9%; HYPE short 97.9%, FARTCOIN short 1.7%, XPL short 0.1% | notional/account 6.04x; margin/account 119.8% | 2000 capped, 3 coins | include_watchlist_current_HYPE_dominant_but_position_count_and_fill_breadth_need_Session6_crosscheck |
| 63 | (blank) | `0x4f7634...6fad5a` | $2,159,978 / 30.9% | 2 pos, $15,780,539, top1 87.6%; HYPE short 87.6%, NEAR short 12.4% | notional/account 11.59x; margin/account 115.9% | 2000 capped, 1 coins | include_priority_type_D_directional_watchlist |
| 62 | (blank) | `0xf02d16...3454c6` | $2,160,627 / 74.8% | 1 pos, $12,876,524, top1 100.0%; HYPE short 100.0% | notional/account 9.52x; margin/account 95.2% | 2000 capped, 2 coins | include_priority_type_D_directional_watchlist |
| 33 | lmlmlm | `0xb798ae...ec4fbf` | $3,370,430 / 29.0% | 3 pos, $12,468,769, top1 98.0%; BTC short 98.0%, ETH short 1.2%, SOL short 0.8% | notional/account 3.09x; margin/account 100.1% | 2000 capped, 4 coins | include_priority_type_D_directional_watchlist |

## Account: Penision Fund (0x0ddf9bae2af4b874b96d287a5ad42eb47138a902)

- Leaderboard metrics: rank 1; account value $28,779,448 on d.Pro vs $21,316,676 in refreshed HL state; month PnL $16,762,612, month ROI 50.8%, month volume $438,135,073; all-time PnL $46,540,015, all-time ROI 155.1%.
- Current concentration: 1 active positions; total notional $82,285,000; top1/top3 share 100.0%/100.0%; uPnL $2,718,349.
- Dominant coins and positions: ETH short 50,000.0000 @ entry 1,700.0600; notional $82,285,000; uPnL $2,718,349; lev 3x liq=2169.169860969 gap=31.8%.
- Margin/leverage proxy: notional/current HL account value 3.86x; margin used/current HL account value 128.7%; withdrawable $0; top liquidation gap proxy 31.8%.
- Fills summary: 2000 fills returned (capped at 2,000), 2026-06-21T10:57:04Z to 2026-06-26T03:46:23Z, 4.70 days; breadth 1 coins; notional $39,926,226; net fees $7,596.20; closed PnL in returned sample $1,851,700; taker/crossed share 58.8%.
- Fill coins/directions: ETH: 2000 fills, $39,926,226, 100.0%; Close Short: 1104, Open Short: 896.
- Directional hypothesis: Single-name ETH short book; leaderboard PnL plausibly comes from concentrated directional exposure rather than diversified market making.
- Drawdown risks: adverse move in dominant ETH short drives most equity risk; top liquidation gap proxy 31.8%; margin used is near or above current account value; withdrawable often zero; d.Pro day PnL already negative at $-3,220,175; fill history sample is capped at 2,000 fills, so turnover/drawdown is incomplete.
- Evidence vs inference: Evidence: public d.Pro month/all-time metrics, current Hyperliquid clearinghouseState positions (ETH), and returned userFills sample (2000 fills). Inference: style label, directional thesis, and repeatability assumption; no private intent, signal, or full historical drawdown is visible.
- Scores: reproducibility 2/5; risk 5/5.
- Candidate pool decision: include_priority_type_D_directional_watchlist; `not_trade_recommended=true`; conclusion: **不建议交易** until robustness validation passes.

## Account: (blank) (0xcf90cfecf74e631feea816d02e757c0c8e895c0e)

- Leaderboard metrics: rank 19; account value $15,501,873 on d.Pro vs $15,014,596 in refreshed HL state; month PnL $7,121,404, month ROI 85.0%, month volume $96,031,964; all-time PnL $4,358,729, all-time ROI 39.1%.
- Current concentration: 1 active positions; total notional $46,680,366; top1/top3 share 100.0%/100.0%; uPnL $5,891,587.
- Dominant coins and positions: BTC short 763.4746 @ entry 68,858.8000; notional $46,680,366; uPnL $5,891,587; lev 15x liq=79810.5046951449 gap=30.5%.
- Margin/leverage proxy: notional/current HL account value 3.11x; margin used/current HL account value 100.0%; withdrawable $0; top liquidation gap proxy 30.5%.
- Fills summary: 2000 fills returned (capped at 2,000), 2026-06-02T13:31:37Z to 2026-06-02T13:39:37Z, 0.01 days; breadth 2 coins; notional $63,593,081; net fees $20,276.94; closed PnL in returned sample $12,996; taker/crossed share 85.2%.
- Fill coins/directions: BTC: 1720 fills, $52,571,953, 82.7%; ETH: 280 fills, $11,021,128, 17.3%; Open Short: 1720, Close Short: 280.
- Directional hypothesis: Single-name BTC short book; leaderboard PnL plausibly comes from concentrated directional exposure rather than diversified market making.
- Drawdown risks: adverse move in dominant BTC short drives most equity risk; top liquidation gap proxy 30.5%; margin used is near or above current account value; withdrawable often zero; d.Pro day PnL already negative at $-1,173,310; fill history sample is capped at 2,000 fills, so turnover/drawdown is incomplete.
- Evidence vs inference: Evidence: public d.Pro month/all-time metrics, current Hyperliquid clearinghouseState positions (BTC), and returned userFills sample (2000 fills). Inference: style label, directional thesis, and repeatability assumption; no private intent, signal, or full historical drawdown is visible.
- Scores: reproducibility 2/5; risk 5/5.
- Candidate pool decision: include_priority_type_D_directional_watchlist; `not_trade_recommended=true`; conclusion: **不建议交易** until robustness validation passes.

## Account: (blank) (0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36)

- Leaderboard metrics: rank 5; account value $91,182,253 on d.Pro vs $9,430,637 in refreshed HL state; month PnL $12,445,828, month ROI 58.9%, month volume $128,248,180; all-time PnL $136,502,325, all-time ROI 96.2%.
- Current concentration: 12 active positions; total notional $56,953,222; top1/top3 share 97.9%/99.8%; uPnL $2,393,859.
- Dominant coins and positions: HYPE short 867,488.6900 @ entry 66.2403; notional $55,776,920; uPnL $1,685,804; lev 5x liq=88.2659170171 gap=37.3% | FARTCOIN short 6,803,285.1000 @ entry 0.2474; notional $985,252; uPnL $697,913; lev 10x liq=3.3281455323 gap=2198.1% | XPL short 847,101.0000 @ entry 0.1086; notional $78,728; uPnL $13,271; lev 5x liq=24.7954817587 gap=26579.6% | SOL short 733.9100 @ entry 91.5472; notional $58,605; uPnL $8,583; lev 4x liq=30484.3797280679 gap=38075.6% | BTC long 0.6280 @ entry 71,584.6000; notional $38,398; uPnL $-6,558; lev 5x.
- Margin/leverage proxy: notional/current HL account value 6.04x; margin used/current HL account value 119.8%; withdrawable $0; top liquidation gap proxy 37.3%.
- Fills summary: 2000 fills returned (capped at 2,000), 2026-06-29T18:18:06Z to 2026-07-02T02:08:56Z, 2.33 days; breadth 3 coins; notional $2,256,859; net fees $633.98; closed PnL in returned sample $-492; taker/crossed share 93.6%.
- Fill coins/directions: xyz:GOLD: 948 fills, $1,034,616, 45.8%; @107: 639 fills, $612,246, 27.1%; HYPE: 413 fills, $609,997, 27.0%; Open Short: 1360, Buy: 632, Sell: 7, Close Short: 1.
- Directional hypothesis: One dominant HYPE short with satellite positions; treat as concentrated directional exposure with some execution/hedge noise.
- Drawdown risks: adverse move in dominant HYPE short drives most equity risk; top liquidation gap proxy 37.3%; alt/perp squeeze and funding risk; margin used is near or above current account value; withdrawable often zero; fill history sample is capped at 2,000 fills, so turnover/drawdown is incomplete.
- Evidence vs inference: Evidence: public d.Pro month/all-time metrics, current Hyperliquid clearinghouseState positions (HYPE, FARTCOIN, XPL), and returned userFills sample (2000 fills). Inference: style label, directional thesis, and repeatability assumption; no private intent, signal, or full historical drawdown is visible.
- Scores: reproducibility 1/5; risk 5/5.
- Candidate pool decision: include_watchlist_current_HYPE_dominant_but_position_count_and_fill_breadth_need_Session6_crosscheck; `not_trade_recommended=true`; conclusion: **不建议交易** until robustness validation passes.

## Account: (blank) (0x4f7634c03ec4e87e14725c84913ade523c6fad5a)

- Leaderboard metrics: rank 63; account value $4,229,391 on d.Pro vs $1,361,567 in refreshed HL state; month PnL $2,159,978, month ROI 30.9%, month volume $43,832,164; all-time PnL $42,553,170, all-time ROI 142.4%.
- Current concentration: 2 active positions; total notional $15,780,539; top1/top3 share 87.6%/100.0%; uPnL $-750,844.
- Dominant coins and positions: HYPE short 215,000.0000 @ entry 60.6813; notional $13,821,275; uPnL $-774,779; lev 10x liq=78.8877220912 gap=22.7% | NEAR short 1,008,681.9000 @ entry 1.9661; notional $1,959,264; uPnL $23,935; lev 10x liq=5.054962295 gap=160.2%.
- Margin/leverage proxy: notional/current HL account value 11.59x; margin used/current HL account value 115.9%; withdrawable $0; top liquidation gap proxy 22.7%.
- Fills summary: 2000 fills returned (capped at 2,000), 2026-07-01T16:14:27Z to 2026-07-01T18:59:28Z, 0.11 days; breadth 1 coins; notional $999,841; net fees $260.00; closed PnL in returned sample $41,700; taker/crossed share 56.4%.
- Fill coins/directions: NEAR: 2000 fills, $999,841, 100.0%; Close Short: 1127, Open Short: 873.
- Directional hypothesis: Small directional basket led by HYPE short, NEAR short; likely macro/relative trend or conviction basket, not proven from public data.
- Drawdown risks: adverse move in dominant HYPE short drives most equity risk; top liquidation gap proxy 22.7%; alt/perp squeeze and funding risk; margin used is near or above current account value; withdrawable often zero; fill history sample is capped at 2,000 fills, so turnover/drawdown is incomplete.
- Evidence vs inference: Evidence: public d.Pro month/all-time metrics, current Hyperliquid clearinghouseState positions (HYPE, NEAR), and returned userFills sample (2000 fills). Inference: style label, directional thesis, and repeatability assumption; no private intent, signal, or full historical drawdown is visible.
- Scores: reproducibility 2/5; risk 5/5.
- Candidate pool decision: include_priority_type_D_directional_watchlist; `not_trade_recommended=true`; conclusion: **不建议交易** until robustness validation passes.

## Account: (blank) (0xf02d16a272a842f8bac1d9a9e773aba1933454c6)

- Leaderboard metrics: rank 62; account value $19,712,834 on d.Pro vs $1,353,144 in refreshed HL state; month PnL $2,160,627, month ROI 74.8%, month volume $97,638,178; all-time PnL $3,218,477, all-time ROI 17.4%.
- Current concentration: 1 active positions; total notional $12,876,524; top1/top3 share 100.0%/100.0%; uPnL $489,249.
- Dominant coins and positions: HYPE short 200,260.1000 @ entry 66.7420; notional $12,876,524; uPnL $489,249; lev 10x liq=72.0321079363 gap=12.0%.
- Margin/leverage proxy: notional/current HL account value 9.52x; margin used/current HL account value 95.2%; withdrawable $648; top liquidation gap proxy 12.0%.
- Fills summary: 2000 fills returned (capped at 2,000), 2026-06-29T17:58:56Z to 2026-07-02T10:20:32Z, 2.68 days; breadth 2 coins; notional $8,519,283; net fees $1,419.99; closed PnL in returned sample $155,310; taker/crossed share 74.2%.
- Fill coins/directions: @107: 909 fills, $4,485,573, 52.7%; HYPE: 1091 fills, $4,033,710, 47.3%; Close Short: 617, Sell: 602, Open Short: 474, Buy: 307.
- Directional hypothesis: Single-name HYPE short book; leaderboard PnL plausibly comes from concentrated directional exposure rather than diversified market making.
- Drawdown risks: adverse move in dominant HYPE short drives most equity risk; top liquidation gap proxy 12.0%; alt/perp squeeze and funding risk; margin used is near or above current account value; withdrawable often zero; fill history sample is capped at 2,000 fills, so turnover/drawdown is incomplete.
- Evidence vs inference: Evidence: public d.Pro month/all-time metrics, current Hyperliquid clearinghouseState positions (HYPE), and returned userFills sample (2000 fills). Inference: style label, directional thesis, and repeatability assumption; no private intent, signal, or full historical drawdown is visible.
- Scores: reproducibility 2/5; risk 5/5.
- Candidate pool decision: include_priority_type_D_directional_watchlist; `not_trade_recommended=true`; conclusion: **不建议交易** until robustness validation passes.

## Account: lmlmlm (0xb798aef79972ce8f73d47b9ebbcda6bbb7ec4fbf)

- Leaderboard metrics: rank 33; account value $8,968,885 on d.Pro vs $4,038,119 in refreshed HL state; month PnL $3,370,430, month ROI 29.0%, month volume $30,755,913; all-time PnL $10,054,218, all-time ROI 149.1%.
- Current concentration: 3 active positions; total notional $12,468,769; top1/top3 share 98.0%/100.0%; uPnL $870,134.
- Dominant coins and positions: BTC short 200.0006 @ entry 65,315.7000; notional $12,224,239; uPnL $838,947; lev 3x liq=79492.459355177 gap=30.1% | ETH short 87.9114 @ entry 1,985.8200; notional $144,597; uPnL $29,980; lev 2x liq=2926.98082615 gap=78.0% | SOL short 1,252.4500 @ entry 80.7543; notional $99,933; uPnL $1,208; lev 2x liq=3949.8575092783 gap=4850.3%.
- Margin/leverage proxy: notional/current HL account value 3.09x; margin used/current HL account value 100.1%; withdrawable $0; top liquidation gap proxy 30.1%.
- Fills summary: 2000 fills returned (capped at 2,000), 2026-04-24T13:07:05Z to 2026-07-02T08:37:35Z, 68.81 days; breadth 4 coins; notional $37,508,725; net fees $8,113.53; closed PnL in returned sample $747,751; taker/crossed share 44.3%.
- Fill coins/directions: BTC: 1065 fills, $25,043,404, 66.8%; @142: 669 fills, $9,232,191, 24.6%; ETH: 242 fills, $3,110,328, 8.3%; SOL: 24 fills, $122,802, 0.3%; Close Short: 780, Sell: 420, Open Short: 332, Buy: 248, Close Long: 119, Open Long: 100, Spot Dust Conversion: 1.
- Directional hypothesis: One dominant BTC short with satellite positions; treat as concentrated directional exposure with some execution/hedge noise.
- Drawdown risks: adverse move in dominant BTC short drives most equity risk; top liquidation gap proxy 30.1%; margin used is near or above current account value; withdrawable often zero; d.Pro day PnL already negative at $-464,260; fill history sample is capped at 2,000 fills, so turnover/drawdown is incomplete.
- Evidence vs inference: Evidence: public d.Pro month/all-time metrics, current Hyperliquid clearinghouseState positions (BTC, ETH, SOL), and returned userFills sample (2000 fills). Inference: style label, directional thesis, and repeatability assumption; no private intent, signal, or full historical drawdown is visible.
- Scores: reproducibility 2/5; risk 5/5.
- Candidate pool decision: include_priority_type_D_directional_watchlist; `not_trade_recommended=true`; conclusion: **不建议交易** until robustness validation passes.

## Accounts Considered But Not Used As Primary Type-D Examples

- `BobbyBigSize` (`0x7fdafde5cfb5465924316eced2d3715494c517d1`) has very high PnL and notional, but local samples show 27 active positions and broad recent fill activity, so it fits Session 6/high-turnover more than a clean concentrated directional archetype.
- `0xfc667adba8d4837586078f4fdcdc29804337ca06` has only 6 active positions, but local/fresh evidence shows very broad recent stock/index/commodity-style fill breadth and very high volume, so it is lower priority for this concentrated-directional session.
- `0x9cc53c5af67fb83a16cc41f61e242bade875ab3d` has a single current BTC short, but month volume is much higher relative to PnL, making it more ambiguous than the selected primary candidates.

## Research Limits And Validation Needs

- d.Pro leaderboard metrics and Hyperliquid state are live snapshots and can drift; d.Pro account value and refreshed Hyperliquid account value may differ by timing or definition.
- Public `userFills` is capped for several accounts, so the returned fill sample is not a full month/all-time history.
- No causal signal, entry rule, stop rule, or full drawdown path is observable from these APIs alone.
- Candidate hypotheses require offline validation before any trading use: train/validation/test split, rolling windows, fee/slippage shocks, parameter perturbation, best-trade/month removal, regime slices, and liquidation/funding stress tests.
- Current recommendation for all analyzed accounts: **不建议交易**.
