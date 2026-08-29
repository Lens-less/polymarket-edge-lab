# Polymarket 流动性奖励策略盈利性核查

查询日期：2026-08-22

结论先行：

- 对“当前执行网络/当前环境”来说，这个策略不能继续做成实盘工程，原因不是单一的收益模型，而是官方地理检查已经阻止新仓；这不等同于推断用户本人的法定所在地。
- 对“抽象的、合规可执行环境中的 Polymarket 做市/奖励捕捉策略”来说，规则允许出现正现金流场景，但现有证据没有证明正期望：它必须命中奖励市场、满足最小挂单量与最大价差、长期维持可评分 resting liquidity，并且拿到足够高的市场份额。
- 12 USDC 最多只可能用于部分低价市场的单边资格探针，既不是普适预算，也不足以证明长期盈利性。

## 1) 官方规则：奖励、评分、费用、库存约束

Polymarket 官方文档明确写了，流动性奖励是给“resting limit orders”的，奖励按日以 pUSD 发放，且最低可支付金额是 $1；每个激励市场都会定义最小合格尺寸、最大合格价差和奖励分配。[Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards)

奖励评分是按分钟采样、按市场内相对份额分配的：页面给出 10,080 个样本/epoch 的公式，最终奖励取决于你的 `Q_epoch` 在同市场所有做市者中的相对占比，而不是“只要挂单就自动拿固定收益”。[Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards)

订单要进入“scoring for rewards”状态，官方 `GET /order-scoring` 要求它满足四件事：在奖励市场里、尺寸达标、价差达标、并且已经存活到要求时长。[Get order scoring status](https://docs.polymarket.com/api-reference/trade/get-order-scoring-status)

maker rebate 不是独立白给的现金流，而是来自 eligible markets 的 taker fee。官方说明是：maker 只有在其 resting liquidity 真的被吃掉时才参与 rebate；rebate 每日发放，最低累计 $1 才支付。[Maker Rebates Program](https://docs.polymarket.com/programs/maker-rebates)

费用本身是 taker-side 公式，maker 不收手续费。官方费用页写得很清楚：`fee = C × feeRate × p × (1 - p)`，并且 fees are applied at match time。[Fees](https://docs.polymarket.com/trading/fees)

市场详情页把奖励字段暴露为 `market.rewards.rewardsMinSize`、`market.rewards.rewardsMaxSpread`、`market.rewards.clobRewards`，也就是说，是否能拿奖励不是“策略自己想象出来”的，而是市场配置硬约束。[Market Details](https://docs.polymarket.com/market-data/market-details)

做市文档还强调了两点：一是要持续报 bid/ask；二是要管理库存，因为补仓、合成 complete set、平仓都会影响可持续 quoting 的能力。多 outcome 事件会用 negative-risk markets，库存操作规则会变。[Market Making](https://docs.polymarket.com/trading/market-making) · [Negative Risk Markets](https://docs.polymarket.com/concepts/negative-risk)

## 2) 可复核配置：奖励池存在，但门槛并不统一

我在 2026-08-22 直接查询了公开 API：

- `https://clob.polymarket.com/rewards/markets/current`
- `https://clob.polymarket.com/clob-markets/{condition_id}`
- `https://clob.polymarket.com/book?token_id={token_id}`

这些是官方公开端点。当天的额外扫描只保存到了系统临时目录，接口又会动态变化，所以本报告不把其页数、市场总数或日率总和写成硬事实。

可复核基线是仓库中 2026-07-24 的版本化归档：18 个原始分页、8,964 个去重 condition（其中 8,954 个进入目标奖励资产范围）。该归档中的完整观察值为：

- `rewards_min_size`：`0, 20, 30, 40, 50, 100, 200, 250, 300, 500, 1000`
- `rewards_max_spread`：`0, 0.2, 2.5, 3.5, 4.5, 5.5, 6.5`
- 最小正尺寸是 20 shares，但这只是该时点 universe 的观察值，不是永久协议常量

证据路径：`research/edge_discovery_2026-07-24/reward_runs/selective-liquidity-rewards-20260724T112636.637029Z/RUN_MANIFEST.json` 与同目录 `REWARD_UNIVERSE.jsonl`。

这说明两件事：

1. 奖励市场是真实存在的，不是空壳。
2. 12 USDC 不是“普适通行证”。它至多能在部分价格较低的市场做单边资格检查；对 20-share 的近互补双边报价，名义资本通常接近 20 pUSD，20–25 pUSD 是保守运营预算而非硬阈值。

一个具体样本里，奖励配置显示 `rewards_min_size=20`、`rewards_max_spread=5.5`，而对应的 order book 显示：

- `min_order_size=5`
- `tick_size=0.01`
- `neg_risk=true`
- `last_trade_price=0.400`

这意味着 12 USDC 在价格约 0.40 时可以买到约 30 股，足以让一张单边订单跨过 20-share 的尺寸门槛，但不代表完整的双边做市组合资本充足，也未必足以跨过 30、50、100 这些更高门槛。这个结论是从官方价格和尺寸字段直接推出来的推断，不是文档原话。

我还抓到了另一类当前奖励市场，`rewards_min_size=50`。对这种市场，12 USDC 只有在入场价非常低时才可能满足尺寸门槛；否则连最基本的合格尺寸都不够。

## 3) 赚钱逻辑：真正有意义的不是 maker rebate，而是 liquidity rewards

maker rebate 只来自 eligible taker-fee pool，并且要求 resting liquidity 真实成交；费率与适用市场可能变化。仓库当前没有真实成交，也没有 confirmed rebate，因此不能把 rebate 当作已验证收益。对小额资格探针，主盈利假设仍只能是 liquidity rewards，而不是先验假定的 rebate。

所以，如果这个策略要赚钱，核心只能是 liquidity rewards，而 liquidity rewards 又是按 `Q_epoch` 的相对份额分配的。换句话说：

- 你必须长期保持在评分范围内；
- 你必须在同市场里拿到足够大的份额；
- 你必须赢过其他 maker，而不是只“存在”。

这也是为什么 12 USDC 只能验证“能不能进入评分”，不能验证“这是不是一个可规模化的盈利策略”。

## 4) 12 USDC 探针能证明什么，不能证明什么

在合规可执行环境中、且目标市场尺寸允许时，小额探针最多能证明：

- 订单能不能下到奖励市场；
- 订单是否满足当前市场的最小尺寸/价差规则；
- 订单是否进入 `order-scoring` 状态。

小额探针不能证明：

- 你能否拿到足够高的市场份额；
- 你能否在多个 epoch 里稳定压过其他 maker；
- 你能否拿到超过 $1 的可支付 rebate 或 rewards；
- 你能否在库存波动、撤单延迟、成交偏斜下仍然正收益。

地理准入应由官方 geoblock 检查先行确认，不能靠试单验证。12 USDC 也不是普适的资格预算；即便某一低价单边订单能进入 scoring，它仍只是资格测试，不是盈利证明。

## 5) 现实阻断：当前执行网络/环境不能直接开新仓

官方地理限制页列出了受限辖区，并规定受限访问是 close-only：不能开新仓，只能关闭既有仓位。[Geographic Restrictions](https://docs.polymarket.com/api-reference/geoblock)

我在当前工作站通过现有网络路径请求 `https://polymarket.com/api/geoblock` 的结果是：

```json
{
  "blocked": true
}
```

禁用系统代理后的直连请求则超时，未能提供 `blocked=false` 的反证。这说明当前工作站及其现有网络路径不能作为实盘执行环境；这里不记录或推断用户的具体所在地。

因此，对于“当前执行环境”而言，这个策略不是一个能继续工程化推进的 live strategy。即使抽象层面存在正收益窗口，也不构成当前这条线路的可执行方案。

## 6) 最终判断

如果问题是“这个策略在 Polymarket 上理论上有没有赚钱可能性？”答案是：存在条件性正现金流场景，但尚未证明正期望；它只可能出现在合规可执行环境、特定市场、满足严格门槛且具备持续竞争优势的情况下。

如果问题是“就当前执行网络/环境，值不值得继续投入工程，把它做成实盘策略？”答案是：不值得。当前这条线应该放弃；这不构成对用户法定所在地的判断。

## 7) 证据局限

- 我只用了一手资料：官方文档、官方公开 API、官方地理限制端点。
- `rewards/markets/current` 是动态分页接口；当天的额外扫描只保存在系统临时目录，因此没有把其中的动态总量用于最终判断。
- 我没有连接真实账户、没有签名、没有下单，因此无法证明个人账户层面的实际 PnL。
- 我没有把“盈利”误当作“能下单”。当前判断里，能否开仓是第一性约束。
