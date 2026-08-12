# Session 6 Report: Hyperliquid High-Turnover Multi-Asset

Date: 2026-07-02

Scope: public data only. I did not read `.env`, wallet files, browser sessions, API keys, or credentials, and did not connect to or operate any real trading account.

## Inputs Read

- Local samples:
  - `/tmp/dpro_hl_top10_summary.csv`
  - `/tmp/dpro_leaderboard_sample.csv`
- Public d.Pro Hyperliquid leaderboard:
  - `https://api.d.pro/api/v1/leaderboard?page=1&limit=100&sort=pnl_month&order=desc`
- Hyperliquid public info API:
  - `POST https://api.hyperliquid.xyz/info` with `type=clearinghouseState`
  - `POST https://api.hyperliquid.xyz/info` with `type=userFills`

Session 1 output was not present, so I selected likely type-E accounts independently using: many fills, many coins, high monthly volume, diversified positions/fills, meaningful fees, and frequent switching.

Important limitation: `userFills` returned the latest 2,000 public fills per account in this collection. Recent fill turnover and concentration are therefore a recent-window observation, not full-month turnover. Monthly PnL/ROI/volume come from d.Pro.

## Candidate Summary

| Decision | Rank | Account | Monthly volume | Fills | Coins | Recent turnover | Fee impact | Concentration | Score |
|---|---:|---|---:|---:|---:|---:|---:|---|---:|
| Primary | 8 | `0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00` | $9.923B | 2,000 | 95 | $3.104M | -0.21 bps | top fill coin BTC 38.84%, top3 59.42% | 2/5 reproducibility, 5/5 risk |
| Primary | 81 | `0x7717a7a245d9f950e586822b8c9b46863ed7bd7e` | $1.237B | 2,000 | 133 | $0.508M | +0.65 bps | top fill coin BTC 8.54%, top3 18.44% | 3/5 reproducibility, 4/5 risk |
| Secondary | 4 | `0xfc667adba8d4837586078f4fdcdc29804337ca06` | $7.451B | 2,000 | 37 | $12.507M | +0.01 bps | top fill coin xyz:SP500 46.28%, top3 70.70% | 2/5 reproducibility, 5/5 risk |
| Secondary | 3 | `0x17c3c8fdbcb7d1b240ce08965e09b1fc91cba868` | $0.502B | 2,000 | 15 | $3.036M | +0.19 bps | top fill coin xyz:SPCX 54.99%, top3 85.26% | 2/5 reproducibility, 4/5 risk |
| Watchlist only | 2 | BobbyBigSize `0x7fdafde5cfb5465924316eced2d3715494c517d1` | $1.531B | 2,000 | 9 | $3.487M | -0.10 bps | top fill coin BTC 45.05%, top3 89.22% | 2/5 reproducibility, 5/5 risk |

All rows remain `not_trade_recommended=true`: 不建议交易.

## Account Notes

### `0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00`

Evidence:
- d.Pro rank 8, monthly PnL about $10.31M, monthly volume about $9.92B.
- Latest 2,000 fills covered 95 coins in 0.256 hours, about 7,816 fills/hour.
- Coin-switch rate was 80.44%, consistent with active multi-asset rotation.
- Current state had 82 positions and about $96.12M position notional; top current position was ETH at 35.10%.
- Recent fill notional was not single-asset only: BTC was 38.84%, top three coins were 59.42%.

Inference:
- This is a strong type-E candidate. The visible behavior looks like automated cross-asset liquidity provision, relative-value trading, or fast inventory rotation.
- The exact edge is not observable from public fills. Replication would require latency, maker/taker placement logic, inventory limits, and adverse-selection controls.

Decision: include in primary candidate pool, but 不建议交易 until robustness validation passes.

### `0x7717a7a245d9f950e586822b8c9b46863ed7bd7e`

Evidence:
- d.Pro rank 81, monthly PnL about $1.70M, monthly volume about $1.24B.
- Latest 2,000 fills covered 133 coins in 0.228 hours, about 8,782 fills/hour.
- Top fill coin was only 8.54% of recent notional and top three were 18.44%.
- Current state had 168 positions with no dominant current position; top current position was SOL at 4.62%.
- Coin-switch rate was 84.99%, the strongest multi-asset breadth signal in this sample.

Inference:
- This is the cleanest high-turnover multi-asset profile found in the fresh scan.
- The account looks more systematic and diversified than directional, but public data cannot prove whether profits come from market making, rebates, basis, hedging, or short-lived directional signals.

Decision: include in primary candidate pool, but 不建议交易 until robustness validation passes.

### `0xfc667adba8d4837586078f4fdcdc29804337ca06`

Evidence:
- d.Pro rank 4, monthly PnL about $13.17M, monthly volume about $7.45B.
- Latest 2,000 fills covered 37 coins in 1.92 hours, with about $12.51M recent turnover.
- Recent flow concentrated in synthetic/index-like symbols: xyz:SP500 was 46.28%, top three were 70.70%.
- Current state had 6 positions and about $20.26M position notional; top current position was ETH at 31.28%.

Inference:
- This is high-turnover and multi-asset enough to keep, but the recent fills look more like index/sector/macro basket activity than broad multi-asset market making.

Decision: include as secondary candidate, but 不建议交易 until robustness validation passes.

### `0x17c3c8fdbcb7d1b240ce08965e09b1fc91cba868`

Evidence:
- d.Pro rank 3, monthly PnL about $14.81M, monthly volume about $0.50B.
- Latest 2,000 fills covered 15 coins in 16.86 hours, about $3.04M turnover.
- Recent notional was concentrated: xyz:SPCX was 54.99%, top three were 85.26%.
- Current state had 14 positions and about $1.49M position notional.

Inference:
- This is a weaker type-E candidate. It has multi-asset activity, but recent turnover looks concentrated in sector/commodity assets rather than broadly diversified high-turnover execution.

Decision: include as secondary candidate, but 不建议交易 until robustness validation passes.

### BobbyBigSize `0x7fdafde5cfb5465924316eced2d3715494c517d1`

Evidence:
- d.Pro rank 2, monthly PnL about $16.65M, monthly volume about $1.53B.
- Latest 2,000 fills covered 9 coins in 1.53 hours, about $3.49M recent turnover.
- Recent flow was concentrated: BTC 45.05%, BTC/ETH/HYPE 89.22%.
- Current state had 27 positions and about $86.53M position notional; top current position was BTC at 42.27%.

Inference:
- Despite high volume and many current positions, this is not a clean type-E exemplar. It may be a large directional/macro book using automated execution.

Decision: watchlist only, not primary type-E pool. 不建议交易 until robustness validation passes.

## Cross-Account Findings

- The best type-E evidence is not leaderboard PnL alone; it is the combination of 2,000 recent fills, high coin breadth, low top-asset concentration, many current positions, and high coin-switch rate.
- `0x7717...bd7e` is the cleanest diversification profile, while `0xecb6...2b00` has the strongest combination of large leaderboard volume and broad recent activity.
- `0xfc66...ca06` and `0x17c3...a868` are useful but more concentrated by recent notional, especially in index/commodity-like assets.
- BobbyBigSize is too concentrated in recent BTC/ETH/HYPE flow for primary type-E classification, despite whale-scale volume.

## Validation Required Before Any Use

Minimum robustness gates:
- Reconstruct multi-day/month fill history instead of only latest 2,000 fills.
- Separate maker rebates from taker costs and model double fees plus slippage.
- Remove best 5% of fills and best single asset/day/month.
- Test by asset class: majors, alt perps, synthetic equities/indices, commodities, and Hyperliquid-specific symbols.
- Simulate inventory constraints, leverage, funding, liquidation distance, and forced de-risking.
- Require train/validation/test split and parameter perturbation.

Until those pass, every candidate remains: 不建议交易.
