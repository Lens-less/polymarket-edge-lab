# Session 1 Report: Data Collection And Account Classification

Generated: 2026-07-02T10:21:01+00:00

## Scope And Safety

- Public leaderboard and public account detail APIs only.
- No `.env`, keys, wallet files, credentials, private account sessions, or order endpoints were read or used.
- These labels are research triage labels, not trading signals.
- Current recommendation for all candidates: **不建议交易** until the Session 7 robustness plan is implemented and passed.

## Data Sources

- Polymarket source: `fresh_api`; rows classified: 50.
- Hyperliquid source: `fresh_api`; rows classified: 100.
- Local PM sample rows available: 20; local HL sample rows available: 10.
- API/detail errors captured: 11. See `fetch_errors.json`.

## Classification Counts

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

## Top Candidates By Type

### A. Polymarket 大额事件押注型

| rank | name | account | pnl_month | volume_month | trade_count | largest_win_to_pnl | closed_count | top_closed_title | classification_reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | muchobliged | 0x095fbca2e0eaf0c9841005135427e1e0117190b2 | 3141057.026554 | 7301624.277016 | 7 | 0.763109 | 6 | England vs. DR Congo: O/U 2.5 | low trade count or concentrated closed wins; high largestWin/PnL; high PnL/volume |
| 9 | Qpkwks | 0x9ee8bbc36d378af72e5f6b8e2ea2eb67c05a89de | 384402.966165 | 1897945.02216 | 36 | 2.942828 | 19 | Will England win on 2026-06-23? | low trade count or concentrated closed wins; high largestWin/PnL; high PnL/volume |

### B. Polymarket 高频 / 做市 / 套利型

| rank | name | account | pnl_month | volume_month | trade_count | pnl_per_volume | current_position_count | classification_reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | GoalLineGhost | 0x0346afae2603313d2bbee96b628536c8cbe352a5 | 697202.085472 | 9673091.133951 | 11319 | 0.07207645 | 100 | very high trade count; broad/current position footprint; low-to-moderate PnL/volume con... |
| 3 | mooseborzoi | 0x84cfffc3f16dcc353094de30d4a45226eccd2f63 | 652085.853679 | 13108939.437532 | 8672 | 0.0497436 | 100 | very high trade count; broad/current position footprint; low-to-moderate PnL/volume con... |
| 4 | swisstony | 0x204f72f35326db932158cba6adff0b9a1da95e14 | 616649.954979 | 33816933.641712 | 132621 | 0.01823495 | 100 | very high trade count; broad/current position footprint; low-to-moderate PnL/volume con... |
| 10 | ndb1 | 0xfea31bc088000ff909be1dfd8d0e3f2c7ef2d227 | 371595.865135 | 1814477.833015 | 1689 | 0.20479493 | 97 | very high trade count; broad/current position footprint; largest win still large; verif... |
| 12 | sleepy-panda | 0xa49becb692927d455924583b5e3e5788246f4c40 | 276254.22588 | 1103934.802879 | 5432 | 0.25024506 | 4 | very high trade count; broad/current position footprint; largest win still large; verif... |

### C. Polymarket 中频精选型

| rank | name | account | pnl_month | volume_month | trade_count | largest_win_to_pnl | unique_event_count_sample | classification_reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8 | CandleHammerDrums | 0x7c1ee865a785de4c00ee90ed86a38489fb8bbab3 | 395370.664559 | 2072962.298134 | 146 | 0.609555 | 74 | mid-range trade count; multiple sampled markets; not entirely single-win dominated |
| 17 |  | 0x5e9458202b5817a72cf81105ec8a30e6f3705ba1 | 246867.181917 | 1227815.351296 | 131 | 0.495093 | 30 | mid-range trade count; multiple sampled markets; not entirely single-win dominated |

### D. Hyperliquid 集中方向仓型

| rank | name | account | pnl_month | volume_month | position_count | top_position_coin | top_position_share | fill_count_recent | classification_reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Penision Fund | 0x0ddf9bae2af4b874b96d287a5ad42eb47138a902 | 16762611.995774 | 438135073.2 | 1 | ETH | 1.0 | 2000 | few active positions; top position dominates current notional; recent fills concentrate... |
| 13 |  | 0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d | 7679671.92161 | 1888437093.0 | 1 | BTC | 1.0 | 2000 | few active positions; top position dominates current notional |
| 19 |  | 0xcf90cfecf74e631feea816d02e757c0c8e895c0e | 7121404.113188 | 96031964.02 | 1 | BTC | 1.0 | 2000 | few active positions; top position dominates current notional; recent fills concentrate... |
| 22 |  | 0x0b8aa35c28b7c6ab18f11dc168f437a8a69fd4f8 | 5199749.298032 | 921073620.75 | 1 | HYPE | 1.0 | 2000 | few active positions; top position dominates current notional |
| 23 |  | 0x0ad9e656d9e6211d0ea1c5462342e1fc94cc4cbf | 5179438.629858 | 233763073.2 | 1 | BTC | 1.0 | 2000 | few active positions; top position dominates current notional |

### E. Hyperliquid 高周转多资产型

| rank | name | account | pnl_month | volume_month | position_count | fill_count_recent | fill_coin_count | dominant_fill_share | classification_reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | BobbyBigSize | 0x7fdafde5cfb5465924316eced2d3715494c517d1 | 16649303.976956 | 1531033122.5 | 27 | 2000 | 9 | 0.3635 | many recent fills; multi-asset recent activity; multiple active positions; fills not fu... |
| 3 |  | 0x17c3c8fdbcb7d1b240ce08965e09b1fc91cba868 | 14813429.257143 | 501698134.23 | 14 | 2000 | 15 | 0.3745 | many recent fills; multi-asset recent activity; multiple active positions; fills not fu... |
| 4 |  | 0xfc667adba8d4837586078f4fdcdc29804337ca06 | 13166099.828094 | 7451356167.35 | 6 | 2000 | 37 | 0.159 | many recent fills; multi-asset recent activity; multiple active positions; fills not fu... |
| 5 |  | 0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36 | 12445827.745577 | 128248179.68 | 12 | 2000 | 3 | 0.474 | active recent trading; several traded assets |
| 6 |  | 0xf822fa0fd364c573fcdb7009fcf47601bc8be01a | 11598312.531395 | 234411135.94 | 5 | 2000 | 3 | 0.7215 | active recent trading; several traded assets |

### F. 异常 / 剔除样本

| market | rank | name | account | pnl_month | volume_month | classification | classification_reasons |
| --- | --- | --- | --- | --- | --- | --- | --- |
| polymarket | 5 | 0xd4aa6f8e91cfea29b66a48ebff52814 | 0x709e8dcb133555794decc598e07f2c923b8366f5 | 529667.261448 | 3142246.633786 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 6 | Jsram | 0x83720820a8aa6c3f20ad71850e7a1a17d16c5223 | 520452.48078 | 2889935.901201 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 7 | maz26 | 0x67542c3219b37fd1610aad290676ff91cdbfe3bc | 417998.353153 | 5168916.511168 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 11 | skk1ch | 0x349606c1b77f3ba668879cbc9347f15a44cf8fc4 | 318643.953225 | 3745491.692678 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 13 | Kch-Temp | 0x924379a79c64b77ad5816ad362122a5f6228658e | 261335.495052 | 1011131.36863 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 14 | surfandturf | 0x9f2fe025f84839ca81dd8e0338892605702d2ca8 | 254355.278075 | 3818665.145365 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 16 | NO-GOD-PLEASE-NO | 0x5257aa84944804bbb0c718814ebebeeafaca3e2a | 252089.172986 | 1445051.851213 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 18 | palegrit | 0xf5fabdcdc6eb6d9765a228824f16cca9c91f62df | 241095.133826 | 1455439.387245 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 20 | 0xC41D736bDed9ED1acCD6A44235039266219774fD-1777101352681 | 0xc41d736bded9ed1accd6a44235039266219774fd | 227809.398149 | 1463656.615328 | F_anomaly_or_unclear_excluded | does not cleanly meet A/B/C thresholds |
| polymarket | 21 | 0x32b484581fc5606dE9C1e43AF4636b6Be9BC8B21-1774274303653 | 0x32b484581fc5606de9c1e43af4636b6be9bc8b21 | 222829.966947 | 4449039.328714 | F_detail_pending_excluded | top50 leaderboard row read; per-account public detail deferred to deep-dive sessions |

## Evidence Notes

- Polymarket `largest_win_to_pnl > 1` means the account had a single closed win larger than monthly PnL, implying losses or open-position drag offset the win. It is concentration risk, not proof of a stable edge.
- Hyperliquid `fill_count_recent` is from public `userFills` and can be capped at 2000 by the API; capped rows should be interpreted as lower bounds.
- Current-position concentration on Hyperliquid is a snapshot and may not reflect the whole month. Deep-dive sessions must cross-check fills and position changes.
- Classification is a triage result for downstream research. No account enters a live-trading sample pool before robustness validation.

## Next Round Plan

- Session 2 validates A candidates for single-event concentration, event type, negative-risk exposure, and whether the result looks like information advantage or luck.
- Session 3 validates B candidates for market breadth, current inventory, spread capture, negative-risk or cross-market arbitrage evidence.
- Session 4 validates C candidates for distributed wins and whether event-selection / entry-band / exit rules can be stated cleanly.
- Session 5 validates D candidates for directional exposure, leverage, current notional concentration, and drawdown/liquidation risk.
- Session 6 validates E candidates for fill breadth, turnover, fees, and whether returns are driven by few assets.
- Session 7 should compile only high-evidence, non-anomalous hypotheses into offline validation designs. Until those pass: **不建议交易**.
