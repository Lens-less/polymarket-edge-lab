# BTC 5m / 15m 新预测器选型
## 预注册 OOS 中同时打赢两个市场概率基线的可行方案

日期：2026-08-22

## 结论先说

有盈利可能，但前提很苛刻：它必须是“市场锚定的残差型概率模型”，而不是一个独立的 BTC 方向预测器。

如果目标是同时打赢 5m 和 15m 两条市场概率基线，最有希望的不是去预测价格本身，而是去预测“当前可成交市场概率表面”与“真实条件分布表面”之间的残差。Polymarket 的公开文档明确说明：价格本身就是概率，市场有 CLOB、可见 bid/ask、实时数据流、成交流与 250ms taker-delay，而且 taker fee 会直接侵蚀小边际优势。换句话说，能活下来的 alpha 必须很小、很稳定、并且在可执行价格上仍为正。  
来源：Polymarket 的价格/订单簿/实时数据/费用/订单生命周期文档，以及概率预测的标准评价文献。[Polymarket Prices & Orderbook](https://docs.polymarket.com/concepts/prices-orderbook), [Get CLOB market info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info), [Real-Time Data](https://docs.polymarket.com/market-data/realtime-data), [Fees](https://docs.polymarket.com/trading/fees), [Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle), [Probabilistic Forecasting](https://www.annualreviews.org/content/journals/10.1146/annurev-statistics-062713-085831), [Strictly Proper Scoring Rules, Prediction, and Estimation](https://www.tandfonline.com/doi/abs/10.1198/016214506000001437)

## 为什么原思路最终会输

1. 原思路把“同一条未来分布”拆成两个 binary market 再做相对价值，本质上是在争夺很窄的残差空间。5m 和 15m 共享同一 BTC 结算世界，只是窗口与 strike 不同，所以可预测自由度没有想象中那么大。

2. 如果模型不先贴住市场概率，它要在 OOS 中同时打赢两条基线，实际上是在和一个已经吸收了大量信息的 live prior 对抗。Polymarket 官方文档把价格直接定义为概率，且显示价通常是 midpoint，不是可成交价；在做 OOS 时若没有把 bid/ask、fee、delay 一起算进去，纸面 alpha 很容易消失。[Prices & Orderbook](https://docs.polymarket.com/concepts/prices-orderbook), [Get market price](https://docs.polymarket.com/api-reference/market-data/get-market-price), [Fees](https://docs.polymarket.com/trading/fees), [Place Orders](https://docs.polymarket.com/trading/place-orders)

3. 同时预测 5m 和 15m，不等于两个独立问题。它们更像同一个条件分布的两个约束。如果模型没有显式建模单调性、尾部和跳跃，通常只会在一条腿上“看起来赢一点”，另一条腿把它拉回去。概率预测文献一直强调，应该用 proper scoring rules、calibration 和 sharpness 来评估，而不是只看方向命中率。[Probabilistic Forecasting](https://www.annualreviews.org/content/journals/10.1146/annurev-statistics-062713-085831), [Strictly Proper Scoring Rules, Prediction, and Estimation](https://www.tandfonline.com/doi/abs/10.1198/016214506000001437)

4. 这类策略最容易死在执行摩擦上。Polymarket 文档明确写了：市场化订单会受到 taker-delay 影响，费用按价格和成交量收取，且是 taker-side only。你的模型如果只比市场强一点点，手续费和滑点会把这点优势吃掉。[Get CLOB market info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info), [Fees](https://docs.polymarket.com/trading/fees), [Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)

所以，原思路的问题不是“方向错得离谱”，而是它把一个非常窄的统计残差问题，包装成了一个看起来很大的预测问题。这个包装会让人高估可学信号、低估执行成本。

## 我认为真正可行的 6 种方案

下面按“同时打赢 5m 和 15m 基线”的现实概率排序。

| 排名 | 方案 | 目标形式 | 数据入口 | 最可能的 edge 来源 | 主要失败点 | 工程成本 |
|---|---|---|---|---|---|---|
| 1 | 市场锚定残差 surface | 直接预测 `p5, p15` 的残差，而不是绝对概率 | Polymarket bid/ask、spread、depth、TWAP 状态、fee、delay | 市场短时失真、价格锚偏移、执行层校准误差 | 如果市场已很有效，残差会迅速塌缩到 0 | 中 |
| 2 | 联合分布 / 量化 surface | 先预测 BTC 剩余区间分布，再推导 5m/15m 概率 | 交易所 trade/book + Chainlink TWAP state | 尾部、跳跃、波动率结构建模更好 | 分布假设错了就会双腿一起错 | 中高 |
| 3 | 微结构事件模型 | 用 order flow 预测短窗内 TWAP 方向/分布 | Polymarket LOB + BTC spot/perp LOB/aggTrade | order-flow imbalance、spread、depth depletion | 延迟、采样噪声、 regime 切换 | 中 |
| 4 | regime-gated 专家混合 | 先分 regime，再由专门子模型输出概率 | 同上 + 波动/跳跃状态 | 低波动和跳跃状态通常不是同一种动力学 | regime 划分不稳会碎片化样本 | 中 |
| 5 | 跨市场先验融合 | 把 spot/perp/option implied move 当先验 | Binance / Deribit / Polymarket | 外部市场对未来波动和跳跃的约束 | 映射到 Chainlink TWAP 不一定干净 | 中高 |
| 6 | 纯方向分类器 | 直接预测 Up / Down | 任意特征 | 实现最简单 | 最不可能稳定打赢两条基线 | 低 |

### 1) 市场锚定残差 surface

这是我最推荐的版本。

做法不是“预测 BTC 会不会涨”，而是先读取当前可成交市场概率，再预测一个很小的修正量：

- 输入：当前 bid/ask、spread、depth、last trade、taker-delay、fee、距离 strike 的相对位置、剩余时间、30s/60s Chainlink TWAP 状态
- 输出：`Δp5` 和 `Δp15`
- 约束：`p5, p15 ∈ [0,1]`，并尽量保持跨 horizon 的单调性和一致性

最现实的实现是：一个市场-only baseline + 一个残差模型，残差模型只允许修正很小的范围。这样做的好处是，市场本身已经是很强的 prior，模型只需要抓少量系统性偏差，而不是从零学整个世界。

为什么它最像“可盈利”：

- 它天然对齐可成交价格，而不是 midpoint；
- 它最适合做预注册 OOS；
- 它最容易证明自己到底是改进了 calibration，还是只是偶然猜中方向。

风险：

- 一旦市场有效性太高，残差会小到不够付费；
- 如果你在训练里悄悄把市场信号用成了标签泄漏，OOS 会立刻翻车。

### 2) 联合分布 / 量化 surface

这条线更“正统”，也更像真正的概率预测。

先预测 BTC 剩余时间内的条件分布，再从同一分布推导 5m 和 15m 两个 binary 概率。实现上可以用：

- NGBoost / probabilistic boosting；
- 分位数回归；
- mixture density；
- 受约束的 CDF surface。

这条路的优势是，它天生是 joint model，不会把 5m 和 15m 当两条互相打架的独立任务。它更适合做：

- calibration curve；
- Brier / log loss；
- cross-horizon consistency 检查。

它的弱点也很明确：分布假设一旦错了，两个 market 会一起错。对极短窗口来说，fat tails、jump、volatility clustering 都很难完全吃掉。[NGBoost: Natural Gradient Boosting for Probabilistic Prediction](https://arxiv.org/abs/1910.03225), [Probabilistic Forecasting](https://www.annualreviews.org/content/journals/10.1146/annurev-statistics-062713-085831)

### 3) 微结构事件模型

如果真要从“价格为什么会在接下来几分钟偏移”里找信号，这条线是最接地气的。

官方文档已经把你需要的 live 输入都开放了：

- CLOB order book；
- 实时市场流；
- 订单生命周期；
- taker delay；
- market info 里的 tick / fee / order age / delay 参数。[Real-Time Data](https://docs.polymarket.com/market-data/realtime-data), [Real-Time Order Updates](https://docs.polymarket.com/trading/realtime-order-updates), [Get CLOB market info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)

然后再叠加 BTC 交易所的 order flow：

- Binance spot / futures 的 `aggTrade` 和 bookTicker 流；
- Deribit 的订单簿和 ticker；
- 这些官方 API 都能拿到实时深度/成交。[Binance Spot WebSocket Streams](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-streams/~), [Binance Futures Market Streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market), [Deribit public/get_order_book](https://docs.deribit.com/api-reference/market-data/public-get_order_book)

它的 edge 来源通常是：

- order-flow imbalance；
- spread 扩张；
- depth depletion；
- 近端跳跃；
- 市场对 TWAP 窗口状态的反应滞后。

这类模型在小样本、短窗口、强微结构场景里是有前途的，研究也支持 crypto LOB 里存在稳定的工程化特征。[DeepLOB: Deep Convolutional Neural Networks for Limit Order Books](https://arxiv.org/abs/1808.03668), [Explainable Patterns in Cryptocurrency Microstructure](https://arxiv.org/html/2602.00776v1), [Exploring Microstructural Dynamics in Cryptocurrency Limit Order Books](https://arxiv.org/html/2506.05764v2)

但它不适合幻想成超短线 HFT：

- Polymarket 有 taker-delay；
- 你的预测对象是未来 TWAP 概率，不是单笔成交；
- 你很可能没有足够低延迟去吃到传统意义上的微秒级错价。

所以这条路应该被当作“短窗微结构预测”，而不是“抢单机”。

### 4) regime-gated 专家混合

这其实不是独立预测器，而是一个控制层。

先把市场分成几种 regime：

- calm / normal；
- high-vol；
- jump；
- stale / delayed；
- oracle state close to strike。

然后每个 regime 用不同子模型。好处是：

- calm regime 的最优特征和 jump regime 完全不同；
- 你不会让一个大模型平均掉所有局部信号；
- 预注册 OOS 可以按 regime 分层报告。

这条线的价值主要在“减少模型互相污染”，而不是单独创造全新 alpha。它适合作为方案 1 或 2 的壳。

### 5) 跨市场先验融合

如果你能拿到外部市场的实时信息，可以把它当作先验，而不是主模型。

官方文档和 API 说明表明，Binance、Deribit 都有实时订单簿和成交流；Deribit 还覆盖 BTC futures/options 这类更接近隐含分布的市场。[Binance Developer Docs](https://developers.binance.com/en/docs/), [Deribit API Documentation](https://docs.deribit.com/)

适合做的事情：

- 用 spot/perp 的短期波动和 order flow 作为 BTC forward variance prior；
- 用 options IV/skew 提供尾部先验；
- 再让 Polymarket 残差模型去吸收“Polymarket 相对外部市场的偏差”。

这条路的优点是更接近真实的概率 surface；缺点是映射链条长，外部市场和 Chainlink TWAP 之间并不完全同构。

### 6) 纯方向分类器

这个我建议直接放弃，除非你只是想做一个 very cheap baseline。

原因很简单：你不是在预测“下一秒 BTC 是否涨 0.1%”，而是在预测一个有窗口、TWAP、strike、fee、delay、概率定价的 binary market。把它压成一个方向分类问题，会丢掉最关键的信息。

## 我建议的最终架构

如果只选一个，我会选这个：

**市场锚定残差 surface + 联合分布主干 + regime gate + 轻量执行层。**

也就是：

1. 先用市场报价构造一个 executable prior；
2. 再预测 BTC 剩余时间的条件分布；
3. 从分布导出 5m / 15m 两个概率；
4. 用 regime gate 控制不同子模型；
5. 只在残差足够大、且扣费后仍有正 EV 时交易。

这不是一个“聪明的大模型”，而是一个“不会自欺的概率系统”。

## 预注册 OOS 应该怎么做

如果真要判断它是否比基线强，建议把预注册写死成下面这个形态：

1. 预测目标分开记：5m 和 15m 各自的 binary log loss / Brier。
2. Baseline 不能只用 raw midpoint，要用可成交的 bid/ask 基线，再加一个 market-only calibration baseline。
3. 时间切分必须 walk-forward，不能 shuffle。
4. 由于 5m/15m 样本高度重叠，评估要按 expiry/day 做 block 或 cluster 级别统计，不能把每个 tick 当独立样本。
5. 主指标用 proper scoring rules：log loss、Brier、校准曲线。方向命中率只能做次要参考。[Evaluating probability forecasts](https://projecteuclid.org/journals/annals-of-statistics/volume-39/issue-5/Evaluating-probability-forecasts/10.1214/11-AOS902.pdf), [Probabilistic Forecasting](https://www.annualreviews.org/content/journals/10.1146/annurev-statistics-062713-085831)
6. 若要做显著性检验，用依赖数据的 block bootstrap 或类似时间序列方法，不要假设独立同分布。[Bootstraps for Time Series](https://projecteuclid.org/journals/statistical-science/volume-17/issue-1/Bootstraps-for-Time-Series/10.1214/ss/1023798998.pdf)

## 我对“能不能盈利”的判断

我的判断是：

- “有可能”不等于“有足够高的概率”；
- 纯方向预测的胜率很低；
- 市场锚定残差模型有真实机会；
- 真正能不能赚，取决于你能否在 OOS 中持续赢到足以覆盖 fee、delay、spread、滑点和样本依赖性的那一点点边际。

所以，最现实的态度不是“能不能一把做成神级预测器”，而是先问：

> 这个模型有没有能力在严格预注册 OOS 中，持续把 executable loss 压到市场基线以下？

如果答案是否定的，就应该放弃；如果答案是肯定的，接下来才值得把工程问题彻底补平。

## 参考来源

- [Polymarket Prices & Orderbook](https://docs.polymarket.com/concepts/prices-orderbook)
- [Polymarket Prices and Order Books](https://docs.polymarket.com/market-data/prices-order-books)
- [Polymarket Real-Time Data](https://docs.polymarket.com/market-data/realtime-data)
- [Polymarket Real-Time Order Updates](https://docs.polymarket.com/trading/realtime-order-updates)
- [Polymarket Chainlink TWAP Prices](https://docs.polymarket.com/market-data/chainlink-twap)
- [Polymarket Get CLOB market info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [Polymarket Get order book](https://docs.polymarket.com/api-reference/market-data/get-order-book)
- [Polymarket Get market price](https://docs.polymarket.com/api-reference/market-data/get-market-price)
- [Polymarket Fees](https://docs.polymarket.com/trading/fees)
- [Polymarket Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)
- [Polymarket Place Orders](https://docs.polymarket.com/trading/place-orders)
- [Polymarket Overview](https://docs.polymarket.com/api-reference/predictions/overview)
- [Probabilistic Forecasting](https://www.annualreviews.org/content/journals/10.1146/annurev-statistics-062713-085831)
- [Strictly Proper Scoring Rules, Prediction, and Estimation](https://www.tandfonline.com/doi/abs/10.1198/016214506000001437)
- [Evaluating probability forecasts](https://projecteuclid.org/journals/annals-of-statistics/volume-39/issue-5/Evaluating-probability-forecasts/10.1214/11-AOS902.pdf)
- [Bootstraps for Time Series](https://projecteuclid.org/journals/statistical-science/volume-17/issue-1/Bootstraps-for-Time-Series/10.1214/ss/1023798998.pdf)
- [NGBoost: Natural Gradient Boosting for Probabilistic Prediction](https://arxiv.org/abs/1910.03225)
- [DeepLOB: Deep Convolutional Neural Networks for Limit Order Books](https://arxiv.org/abs/1808.03668)
- [The price impact of order book events: market orders, limit orders and cancellations](https://arxiv.org/abs/0904.0900)
- [Explainable Patterns in Cryptocurrency Microstructure](https://arxiv.org/html/2602.00776v1)
- [Exploring Microstructural Dynamics in Cryptocurrency Limit Order Books](https://arxiv.org/html/2506.05764v2)
- [Binance Developer Docs](https://developers.binance.com/en/docs/)
- [Binance Spot WebSocket Streams](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-streams/~)
- [Binance Futures Market Streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)
- [Deribit API Documentation](https://docs.deribit.com/)
- [Deribit public/get_order_book](https://docs.deribit.com/api-reference/market-data/public-get_order_book)
