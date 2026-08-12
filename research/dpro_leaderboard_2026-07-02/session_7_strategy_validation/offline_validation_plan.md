# Offline Validation Plan

All validation is offline and public-data-only. No real account connection, key reading, or live order submission is allowed.

## Data Splits

- Use rolling train / validation / test, not one static split.
- Polymarket: train 6 months, validation 1 month, test 1 month where historical data coverage permits; roll forward by 1 month.
- Hyperliquid: train 90 days, validation 30 days, test 30 days; roll forward by 14-30 days depending on data density.
- Keep final untouched test windows by market category and by asset.

## Required Cost And Execution Stress

- Base fees and realistic slippage from historical orderbook depth.
- 2x fee test.
- 2x slippage test.
- 2x fee plus 2x slippage combined test.
- Queue-position / partial-fill haircut for Polymarket maker or spread hypotheses.
- Funding-cost and liquidation-distance stress for Hyperliquid directional and turnover hypotheses.

## Robustness Tests

- Parameter perturbation: entry threshold, holding time, stop, position cap, liquidity filter, and rebalance frequency.
- Remove best 5% trades by realized PnL.
- Remove best single market.
- Remove best single month.
- Market-category slices: soccer, basketball, baseball, tennis, politics/news/other where available.
- Hyperliquid asset slices: BTC/ETH majors, HYPE, top alts, long-tail alts, synthetic equities/commodities where available.
- Regime slices: high/low volatility, high/low funding, trend/mean-reversion regimes, high/low liquidity.
- Start-date and end-date shifts to detect luck from a narrow window.

## Minimum Pass Gates

- Positive validation and test net PnL after base costs.
- Positive or acceptable risk-adjusted return after 2x fee/slippage.
- No single trade, market, asset, or month accounts for most of net PnL.
- Max drawdown and tail loss stay inside pre-declared limits.
- Trade count and turnover are high enough for statistical interpretation.
- Strategy remains valid across at least two independent time windows or categories.

## Failure Rule

If any required robustness gate fails, the conclusion is: **不建议交易**.
