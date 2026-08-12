# Polymarket 可自动化盈利机制：一手资料核查与最小实验设计

日期：2026-07-24  
资料截止：2026-07-24  
范围：仅使用 Polymarket 官方文档、官方 API 当前响应、官方 GitHub 合约/SDK 资料。  
不在范围：第三方策略宣传、排行榜叙事、无法复现的收益截图、未授权实盘。

> 结论边界：本文没有证明任何策略已经盈利。机制成立不等于存在可成交的正期望 edge。当前不建议实盘交易；先完成 CLOB V2 兼容审计、只读录制和影子仿真，再决定是否值得申请极小额受控实盘。

## 1. 一页结论

### 1.1 候选方向排序

| 顺位 | 候选方向 | 机制是否成立 | 当前能否证明盈利 | 处理意见 |
|---|---|---:|---:|---|
| 1 | 激励感知的 YES/NO 配对完整集被动做市 | 是 | 否 | 主实验方向；先影子报价，再用自有订单小样本校准 |
| 2 | 深度感知的二元完整集错价扫描 | 是 | 否 | 最适合先做纯只读、可证伪扫描 |
| 3 | Negative Risk 转换错价扫描 | 是 | 否 | 次级研究；必须把转换费、合约版本和多腿风险纳入 |
| 4 | Holding Reward 持有补贴 | 是 | 否 | 只能作为补贴项，不能覆盖方向性和结算风险 |
| 5 | Taker Rebate | 是 | 否 | 只是交易成本折扣，不是主动刷量策略 |
| 降级 | 普通“围绕 midpoint 双边挂单赚 spread” | 市场机制成立 | 完全未证明 | 没有自有队列、撤单、成交后逆向选择数据前，不应作为主计划 |
| 拒绝 | 复制排行榜账户或用公开成交反推做市 | 公开字段不足 | 否 | 不能辨认完整 maker/taker、队列和撤单过程 |

最值得重新设计的不是“更复杂的预测模型”，而是一个**机械约束明确、补贴单独核算、能被逐步证伪**的系统：

1. 以 YES/NO 完整集恒等式约束报价，避免把主收益假设建立在主观方向判断上。
2. 将 spread、Maker Rebate、Liquidity Reward、Holding Reward 四个分量分开记账。
3. 把单腿成交、排队劣势、取消延迟、成交后价格漂移作为主要成本，而不是边角误差。
4. 只用自有认证订单数据判断真实 maker 成交和奖励；公开账户数据只用于市场筛选。
5. 任何奖励变动、字段缺失、地理限制、V2 签名异常都触发 fail-closed，不继续挂单。

### 1.2 不能再使用的旧前提

- 2026-04-28 起生产环境是 CLOB V2，V1 SDK、V1 签名和旧的 USDC.e 直连假设不再兼容。
- 不能把 Maker Rebate 的 15%/20%/25% 理解为“每笔 maker 成交按成交额返这么多”；它是每日市场级费用池中的分配比例。
- 不能把 Liquidity Reward 的名义 daily rate 当成自己的收入；最终按与其他 maker 的相对得分分配。
- 不能全局硬编码“挂满 3.5 秒就有奖励”。3.5 秒是官方 2026-03 March Madness 公告里的专项规则；当前必须逐市场读取最小订单年龄。
- `POST /orders` 不是多腿原子单。每个返回项可以独立成功或失败。
- 聚合 L2 订单簿不含自己的真实队列位置，也不含其他 maker 的订单年龄和撤单意图。
- “历史 orderbook 已停用”不是截至本报告日期的当前事实；当前官方路由仍返回 2026-07-24 快照，但它未列入当前公开 API 目录/SDK 公共方法，故只能视为不受支持的旁路，不能依赖。

## 2. 先决条件：CLOB V2 迁移是实验 0

[官方迁移指南](https://docs.polymarket.com/v2-migration) 和 [官方 Changelog](https://docs.polymarket.com/changelog) 明确：

- CLOB V2 于 2026-04-28 上线生产。
- 生产 URL 仍为 `https://clob.polymarket.com`。
- V1 SDK 和 V1-signed orders 不再受支持，没有向后兼容。
- 抵押品从 USDC.e 改为 pUSD；API-only 客户端需要处理包装/解包路径。
- Exchange EIP-712 domain、订单字段、验证合约和 SDK 包均有变化。
- 切换时旧 resting orders 被清空，未迁移。
- 2026-07-24 又上线了异步 commit pipeline；FAK/FOK 成功响应不再保证直接带 `transactionHashes`，应从 `tradeIDs` 继续查询终态。

因此，任何盈亏实验前必须通过以下硬门槛：

1. 项目仅使用当前 V2 SDK/当前签名结构，不能靠“请求没报错”推断兼容。
2. 市场发现、下单、撤单、成交、`tradeIDs` 到链上终态、pUSD 余额和 CTF split/merge 必须端到端对账。
3. 合约地址按角色从当前官方 Contracts/官方 V2 仓库解析；不要混用旧 Neg Risk Adapter、V2 collateral adapter 和 V2 exchange 地址。
4. 所有生产测试均先使用只读调用；需要真实订单时必须另行取得明确授权并设置极小风险限额。

官方 V2 合约仓库也确认了撮合结算类型：

- `COMPLEMENTARY`：买卖同一 outcome，直接转移。
- `MINT`：互补 outcome 的两张 BUY 单，可用抵押品拆分后结算。
- `MERGE`：互补 outcome 的两张 SELL 单，可合并完整集后结算。

来源：[Polymarket/ctf-exchange-v2](https://github.com/Polymarket/ctf-exchange-v2)。

这证明完整集经济关系在撮合层真实存在，但**不证明同一策略发出的两张独立订单会同时成交**。

## 3. 当前费用与激励：能补贴 edge，但不能替代 edge

### 3.1 Taker fee

[官方 Fees](https://docs.polymarket.com/trading/fees) 给出的当前公式为：

```text
fee = C × feeRate × p × (1 - p)
```

其中 `C` 是股数，`p` 是成交价格。Maker 不收费，只有 taker 收费。当前分类参数如下：

| 类别 | Taker fee rate | Maker fee | Maker rebate 池比例 |
|---|---:|---:|---:|
| Crypto | 0.07 | 0 | 20% |
| Sports | 0.05 | 0 | 15% |
| Finance / Politics / Mentions / Tech | 0.04 | 0 | 25% |
| Economics / Culture / Weather / Other | 0.05 | 0 | 25% |
| Geopolitics | 0 | 0 | 无 |

费用关于 0.50 对称，在 0.50 附近美元额最高。费用保留 5 位小数，低于 `0.00001` 的费用会舍入为 0。

实现要求：

- 不按分类字符串硬编码费率；逐市场读取 `feeSchedule` 或 CLOB market info 的 fee curve。
- 每个深度档分别计算费用，不能对整单用 midpoint 粗估。
- 配对做市本身若保持 maker，成交费为 0；但单腿补仓、止损或强平若成为 taker，必须按曲线计费。
- 费用和 tick/最小订单量均可随市场不同，缺字段时拒绝报价。

### 3.2 Maker Rebate

[官方 Maker Rebates Program](https://docs.polymarket.com/programs/maker-rebates) 说明：

- 只有先 resting、后被别人吃掉的 maker liquidity 才有资格。
- 每日以 pUSD 发放；累计低于 1 pUSD 不支付。
- 每笔 maker fill 先计算：

```text
fee_equivalent = C × feeRate × p × (1 - p)
```

- 每日、逐市场分配：

```text
your_rebate =
    your_fee_equivalent
    / total_fee_equivalent_of_all_makers_in_the_market
    × rebate_pool
```

因此，Maker Rebate 是**同市场内竞争后的不确定收入**。公开资料不能提前知道当天其他 maker 的最终 fee-equivalent，也不能把 20% 直接乘自己的成交额。

### 3.3 Liquidity Reward

[官方 Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards) 说明：

- 只有满足逐市场最小股数、最大离中点价差、最小订单年龄的 resting orders 才可能计分。
- 距离越近，单订单得分按二次函数增加。
- 公式把市场 `m` 与互补市场 `m'` 一起考虑：`BUY YES` 与 `SELL NO` 属于同一经济方向，`SELL YES` 与 `BUY NO` 属于另一方向。
- midpoint 在 `[0.10, 0.90]` 时，单边也能以 `1/c` 折扣计分，当前 `c=3`；靠近 0 或 1 时必须双边才计分。
- 每分钟随机抽样，一期 10,080 次；每个样本先和同市场其他 maker 归一化，最终再按相对份额分配。
- 名义 daily rate 是市场奖励上限/配置，不是个体保证收入；最低支付 1 pUSD。

当前配置入口：[GET current active rewards configurations](https://docs.polymarket.com/api-reference/rewards/get-current-active-rewards-configurations)，实时端点为 `https://clob.polymarket.com/rewards/markets/current`。

2026-07-24 08:16:27 UTC 的只读全量分页快照：

- 共有 8,982 个当前配置记录。
- `rewards_min_size` 中位数为 50 股，25 分位约 30 股；这意味着“小资金”也常需为单一配对准备几十股的库存/抵押品。
- `rewards_max_spread` 中位数约 4.5 美分。
- `total_daily_rate` 中位数约 6 pUSD，但分布极偏斜。

这些统计只描述配置供给，**不描述竞争人数、自己的得分份额或净收益**。

#### 3.5 秒规则核验

- [2026-03-17 官方 Changelog](https://docs.polymarket.com/changelog) 对 March Madness 专项写明：订单至少在簿上活跃 3.5 秒才有资格。
- 当前 [CLOB market info schema](https://docs.polymarket.com/api-reference/markets/get-clob-market-info) 将 `oas` 定义为最小订单年龄（秒）。
- 2026-07-24 对当前奖励市场的官方 API 抽查显示，实际响应没有顶层 `oas`，而在 `r.moas` 返回；抽查值至少有 4 秒和 30 秒两种。

结论：3.5 秒不能作为当前全局常量。实现应同时兼容文档字段和实际字段，但二者都缺失或不一致时必须 fail-closed；每次挂单前缓存逐市场值并设置短 TTL。

### 3.4 Holding Reward

[官方 Positions & Tokens](https://docs.polymarket.com/concepts/positions-tokens) 当前写明：

- eligible market 的仓位价值按 4.00% 年化发放 Holding Reward。
- 每小时随机抽样一次，按日发放。
- 比率可由 Polymarket 调整。

这只能作为持仓补贴。4% 年化远低于一笔错误方向仓位可能在几分钟/几天内产生的损失，也不能补偿规则澄清、争议和资本锁定。

### 3.5 Taker Rebate

[官方 Taker Rebate Program](https://docs.polymarket.com/programs/taker-rebates) 于 2026-05-28 上线，以 30 日 weighted volume 分级：

| 30 日 wV | 当前 rebate |
|---:|---:|
| < 2,000 | 0% |
| 2,000 | 3% |
| 20,000 | 8% |
| 200,000 | 18% |
| 1,000,000 | 32% |
| 4,000,000 | 44% |
| 10,000,000 | 50% |

它只对达到等级后的未来 taker 交易生效；maker 不累计 wV，Geopolitics 不累计，参数可变。官方同时明确 wash trading、自我撮合和不真实交易可能被移除奖励。

所以对小中等资金而言，taker rebate 只能降低一个本来就成立的 taker 策略成本。为了升级而主动 churn 会先支付 spread、fee 和逆向选择，且可能违反规则。

## 4. 主候选：配对完整集被动做市

### 4.1 机械收益边界

二元市场中，1 YES + 1 NO 是一个完整集，始终由 1 pUSD 抵押：

```text
1 pUSD -> 1 YES + 1 NO       (split)
1 YES + 1 NO -> 1 pUSD       (merge)
```

来源：[Positions & Tokens](https://docs.polymarket.com/concepts/positions-tokens)、[Inventory Management](https://docs.polymarket.com/market-makers/inventory)。

由此得到两种不依赖事件预测的报价约束。

**配对 BUY：**

```text
gross_pair_edge_buy = 1 - bid_yes - bid_no
```

当自己的两张 post-only BUY 都成交后，得到 1 YES + 1 NO，可 merge 回 1 pUSD。只有两边都成交，`gross_pair_edge_buy` 才被锁定。

**配对 SELL：**

先 split 1 pUSD 准备 YES/NO 库存，然后：

```text
gross_pair_edge_sell = ask_yes + ask_no - 1
```

两张 post-only SELL 都成交后，完整集库存全部释放为成交所得。

真正的净收益必须写成：

```text
net =
    realized_pair_spread
  + realized_maker_rebate
  + realized_liquidity_reward
  + realized_holding_reward
  - single_leg_inventory_loss
  - taker_unwind_fee
  - slippage
  - merge/split/settlement_cost
  - stale_quote_loss
  - failed_or_delayed_settlement_cost
  - capital_lock_cost
```

不能用“报价和小于 1”直接替代 `realized_pair_spread`。

### 4.2 为什么它比普通双边做市更适合重新开始

- 完整集给出清晰的组合价值锚，不必先假设自己能持续预测事件真实概率。
- 配对 BUY 的两张单分别落在奖励公式的两个经济方向，天然比单腿更符合平衡报价目标。
- maker 成交本身当前不收费，同时可能获得 Maker Rebate。
- 组合完成后可 merge，减少持有到结算的规则风险和资本占用。

但它不是套利保证：

- 两张订单是独立订单，不同时成交。
- 信息型 taker 往往只吃掉对自己有利的一腿，留下方向性库存。
- 排队靠后时，可能只在价格即将不利时成交。
- 奖励相对分配可能不足以覆盖逆向选择。
- 为了快速补齐第二腿而跨 spread，会变成 taker 并支付曲线费用。

### 4.3 必须实现成状态机，而不是两个定时下单函数

```text
COLLATERAL_READY
    -> INVENTORY_PREPARED        (可选 split)
    -> BOTH_QUOTES_LIVE
    -> ONE_LEG_FILLED
    -> PAIR_COMPLETE
    -> MERGE_PENDING
    -> FLAT

任何状态
    -> CANCEL_PENDING
    -> RECONCILING
    -> SAFE_STOP
```

核心约束：

1. 每个 market 独立记录 YES/NO 可用库存、预留抵押品、open order、matched size 和 settlement status。
2. 两腿总报价必须满足预设安全垫；改单时重新验证，而不是沿用旧 midpoint。
3. 一腿成交后，第二腿的追价上限由“组合最坏净成本”决定，不能无限追单。
4. 每个 market 设置最大裸露股数、最大单腿持有秒数、最大 taker 解套成本。
5. 只有 `CONFIRMED` 或严格对账后的余额变化才能进入 merge；`MATCHED/MINED/RETRYING` 不能当作最终资金。
6. 每次重连先拉取 open orders、own trades、余额，再恢复状态机。

### 4.4 多腿非原子性

[官方 Place Orders](https://docs.polymarket.com/trading/place-orders) 明确：

- `POST /orders` 一次可提交 1–15 张订单。
- 返回数组中每个订单可独立成功或失败，不能把 batch 当成整体成功/失败。
- FOK 的 all-or-nothing 只针对单张订单，不针对 YES/NO 两腿组合。

因此：

- 配对被动做市需要用真实的单腿风险预算。
- 完整集主动错价也不能把两个 FOK 当作原子 basket。
- “观察到 edge 后两腿同时下单”的回测，若没有腿间延迟和失败模型，会系统性高估收益。

## 5. 只读候选：深度感知完整集与 Negative Risk 扫描

### 5.1 二元完整集

对目标数量 `q`，必须逐档走深度，不用 best quote 或 midpoint：

```text
buy_both_edge(q) =
    q
  - executable_ask_cost_yes(q)
  - taker_fee_yes(q)
  - executable_ask_cost_no(q)
  - taker_fee_no(q)
  - merge_and_execution_buffer(q)
```

```text
sell_both_edge(q) =
    executable_bid_proceeds_yes(q)
  + executable_bid_proceeds_no(q)
  - q
  - taker_fees(q)
  - split_and_execution_buffer(q)
```

只有在保守 buffer 后仍为正，才记录为候选。第一阶段只记录，不发单。

每条机会至少保存：

- 两个 token ID、condition ID、event ID。
- 两边完整深度、book hash、exchange timestamp、客户端接收时间。
- `q=5/10/25/50` 等固定档位的逐档成交成本。
- 逐腿 fee schedule、tick、minimum order size、taker delay。
- 第一次出现、最后一次出现、持续毫秒数。
- 采用 1x/2x/3x 实测网络延迟后的模拟可执行性。

### 5.2 Negative Risk

[官方 Negative Risk 文档](https://docs.polymarket.com/advanced/neg-risk) 说明，在“恰好一个 outcome 胜出”的事件中，一组 NO 可以转换为其余 outcome 的 YES。Augmented Neg Risk 还会有 placeholder 和 `Other`，官方要求 placeholder 未命名前不要交易。

当前 V2 adapter 源码进一步表明转换不能假设零费：

```solidity
uint256 feeBips = adapter.getFeeBips(_marketId);
uint256 amountOut = _amount - (_amount * feeBips / 10_000);
```

来源：[NegRiskCtfCollateralAdapter.sol](https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/adapters/NegRiskCtfCollateralAdapter.sol)。

Negative Risk 扫描的硬条件：

1. 从当前合约读取 `questionCount`、`feeBips`，不得写死。
2. 用 exact `indexSet` 模拟转换输出，不用简单的“所有 YES 价格和应等于 1”替代。
3. 校验事件确为 exactly-one，排除未命名 placeholder，并单独处理 `Other` 定义变化。
4. 把所有 CLOB 腿视为非原子；转换交易原子不等于获取输入资产的多腿原子。
5. 记录 adapter/exchange 角色和地址版本，拒绝旧地址或文档漂移。

该方向的机械关系成立，但合约/事件复杂度、腿数、转换费和延迟都高于二元完整集，故不应先于主实验。

## 6. 市场微结构：公开数据无法证明的变量

### 6.1 订单簿和 WebSocket 只有聚合价格档

[官方 Orderbook](https://docs.polymarket.com/trading/orderbook) 返回：

- `bids[]` / `asks[]` 的 `{price, size}` 聚合档位；
- `timestamp`、`hash`、`tick_size`、`min_order_size`、`neg_risk`；
- 没有公开逐订单 ID、订单年龄或 queue position。

[官方 Real-Time Data](https://docs.polymarket.com/market-data/realtime-data) 提供 `book`、`price_change`、`last_trade_price`、`tick_size_change` 等事件。已文档化 payload 有 timestamp/hash，但未公开 sequence number、重放游标或队列身份。

实现后果：

- 本地 WS 必须配合周期性 REST snapshot，用 hash 发现漂移并重建。
- 不能把某价格档显示 size 当成“自己前方队列量”的精确值；最多是下单瞬间的代理。
- 不能从 L2 变化判断某个 size 消失是成交还是撤单。
- fill probability 必须用自有订单样本校准，不能从聚合书推导成真值。

### 6.2 `/orderbook-history` 当前核验

截至 2026-07-24：

- 当前 [官方 API Overview](https://docs.polymarket.com/api-reference/predictions/overview) 和 [官方 V2 Public Methods](https://docs.polymarket.com/trading/clients/public) 没有列出 orderbook history 方法。
- [官方 Error Codes](https://docs.polymarket.com/resources/error-codes) 仍列出 `GET orderbook-history` 的参数错误。
- 对官方生产 CLOB 的当前只读探针仍返回了当日数据：
  [固定 60 秒窗口、limit=2 的探针](https://clob.polymarket.com/orderbook-history?asset_id=98022490269692409998126496127597032490334070080325855126491859374983463996227&startTs=1784881356000&endTs=1784881416000&limit=2)
  返回 `count=18`，前两条时间戳为 `1784881361066`、`1784881363618`，均含 bids/asks/hash。

所以正确结论是：

> 该旁路当前可返回新数据，但未被当前公开 API 目录/SDK 承诺，历史上也可能中断；不能作为正式回测或生产恢复依赖。

仓库应从现在开始自行录制 WS + REST 校验快照，并把数据完整性作为实验输出。不能用“官方历史簿一定有”来跳过采集。

### 6.3 公开成交历史不能复原其他人的策略

[公开 Data API trades](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets) 提供钱包、outcome、side、size、price、timestamp、tx hash 等，但不提供完整公开订单生命周期、撤单、队列位置或可靠的逐单 maker/taker 角色。

相比之下，[认证 own trades](https://docs.polymarket.com/api-reference/trade/get-trades) 和 [Manage Orders](https://docs.polymarket.com/trading/manage-orders) 才能给自己的：

- order ID、created time、matched size、status；
- `traderSide`、`feeRateBps`、maker orders；
- `MATCHED/MINED/CONFIRMED/RETRYING/FAILED` 进展。

故：

- 公开排行榜/钱包数据可以帮助发现活跃市场。
- 它不能证明对方靠 maker spread、奖励、方向下注或链外对冲赚钱。
- 它也不能为本策略提供可复用的 queue/fill model。

### 6.4 延迟不是小误差

[官方 Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle) 说明：

- 部分 crypto/finance up-down market 的 taker delay 为 250 ms，通过 `itode: true` 标识。
- 配置的 sports market 还有 seconds delay。
- pending delay 内订单不能取消；到期后会重新校验并可能拒绝。
- trade 可能进入 `RETRYING`，并非 match 即终态。

[官方 Geographic Restrictions](https://docs.polymarket.com/api-reference/geoblock) 公开：

- 主服务器在 `eu-west-2`。
- 非限制地区的推荐近端区域是 `eu-west-1`。
- 通过 KYC/KYB 的用户可申请直接 co-location。

这意味着普通小资金客户端面对的不是同等延迟环境。没有本机到 ack/cancel/fill 的分布，就不能把一个持续几十或几百毫秒的纸面 edge 当作可捕获。

## 7. 结算与规则风险

[官方 Resolution](https://docs.polymarket.com/concepts/resolution) 说明：

- 必须按完整 resolution rules 和指定 source 判断，标题不是充分条件。
- 提案后有 2 小时 challenge period。
- 争议结算通常需要 4–6 天。
- 极少数 `Unknown/50-50` 会让 YES/NO 各兑 0.50。
- 规则可能通过链上 bulletin board 增加 clarification。

因此：

- 接近结算的“0.99 看似稳健持有”不是无风险利差。
- Holding Reward 不能覆盖争议天数和尾部规则风险。
- 任何持有到期策略必须保存 resolution source、规则版本、clarification 和 UMA 状态。
- 被动做市应在已知催化剂前使用 GTD/主动撤单，而不是依赖人工反应。

## 8. 地理和市场完整性门槛

[官方 geoblock 文档](https://docs.polymarket.com/api-reference/geoblock) 要求下单前从实际请求 IP 调用：

```text
GET https://polymarket.com/api/geoblock
```

被限制地区的订单会被拒绝，限制名单和 close-only 范围可能变化。实现必须：

1. 启动时、周期性、IP/部署变化后重新检查。
2. `blocked=true`、请求失败或响应不完整时禁止新开仓。
3. 不通过 VPN/代理规避限制。

[官方 Market Integrity](https://integrity.polymarket.com/) 与 Taker Rebate 条款禁止 wash trading、自我撮合、spoofing 等行为。奖励优化只能建立在真实第三方流量和真实风险承担上。

## 9. 仓库的最小实验，不从“策略收益率”开始

### 实验 0：V2 与合规兼容审计

只读验证：

- 当前 SDK/签名/合约/pUSD 版本。
- Gamma condition/token 映射和 CLOB market info。
- geoblock。
- 当前 fee/reward/tick/min-size/order-age/taker-delay 字段。

验收：任一关键字段缺失或角色地址不一致，停止，不进入报价实验。

### 实验 1：连续只读市场与 L2 录制

建议至少保存：

```text
market_config_snapshot.ndjson
l2_events.ndjson
l2_resnapshots.ndjson
complete_set_opportunities.ndjson
negative_risk_opportunities.ndjson
data_quality_events.ndjson
```

每条记录使用 decimal/integer，不用二进制浮点；同时记录 exchange timestamp、receive timestamp、monotonic local time 和 book hash。

必须测试：

- WS 断线、乱序/重复的容错。
- hash 漂移后的 REST 重建。
- tick size change 后旧订单价格失效。
- 奖励配置/fee schedule 热更新。
- 完整集 edge 在 1x/2x/3x p95 网络延迟下还能持续多久。

输出只能叫“观察到的机会”和“模拟可执行机会”，不能叫利润。

### 实验 2：配对影子报价

不发真实订单，只运行完整状态机：

1. 按当前 book 生成 post-only YES/NO 配对价。
2. 验证两腿组合安全垫、最小股数、最大 reward spread 和最小订单年龄。
3. 用后续真实 book/trade events 模拟先后成交，但分别报告：
   - 乐观 queue；
   - 显示档位全在自己前方；
   - 只在逆向价格跳动时成交的压力情形。
4. 每次模拟单腿成交后，计算 1 秒、5 秒、30 秒的 markout 和补齐第二腿成本。
5. Reward 只记录理论 score；没有自己的认证得分/实际支付前，奖励收入设为 0。

关键指标：

- 配对完成率与第二腿完成时间。
- 单腿最大/平均暴露。
- 成交后 1s/5s/30s markout。
- 取消请求到 book 消失的 p50/p95/p99。
- 奖励为 0、rebate 为 0 后的核心净 edge。
- 去掉最佳市场/最佳日后的稳定性。

### 实验 3：极小额受控 maker probe

这一步不是本报告授权的动作。只有用户明确同意、地理检查通过、风险上限确认后才可执行。

目的不是赚钱，而是测公开数据无法提供的变量：

- 真实 post/ack/cancel/fill latency。
- 自己的 maker/taker 身份。
- 价格档代理 queue 与实际 fill 的关系。
- order-scoring 是否为 true 以及开始计分所需时间。
- 实际 Maker Rebate、Liquidity Reward、Holding Reward 支付。
- `MATCHED -> CONFIRMED/FAILED` 的时间和失败率。

建议事先写死：

- 单市场最大抵押品。
- 全局最大裸露股数。
- 单腿最长存活时间。
- 当日最大可接受亏损。
- kill switch 与 cancel-all 后的强制 reconciliation。

## 10. 晋级门槛

在宣称“可盈利计划”前，至少需要：

1. 足够长的跨时段只读数据，而不是单日快照。
2. 自有 maker fills 校准的 fill/queue 模型。
3. 实际奖励和 rebate 对账，不使用配置上限代替收入。
4. 将单腿风险、taker unwind fee、slippage、失败结算和资本占用全部计入。
5. 在以下压力情形仍可接受：
   - 奖励临时归零；
   - rebate 下调或为零；
   - 延迟和 slippage 使用实测 p95 的 2 倍；
   - 删除收益最高的一天和最高的一个市场；
   - 延迟撤单并发生不利选择。
6. 若只有奖励存在时才为正，必须标注为“补贴依赖”，并在配置变化时自动停止，不能称为结构性套利。
7. 只有实盘小样本能证明执行，但小样本不能证明长期正期望；需继续滚动 OOS 验证。

## 11. 需要采集的字段

| 层 | 必需字段 |
|---|---|
| Market | event/condition/question/token IDs，outcomes，negRisk/augmented 标记，规则/source/end time |
| Constraints | tick，minimum order size，accepting orders，seconds delay，`itode` |
| Fees | `feesEnabled`，fee rate/exponent/taker-only，maker rebate 参数 |
| Rewards | min size，max spread，daily allocation，minimum order age，holding reward flag |
| L2 | 每档 price/size，hash，exchange ts，receive ts，REST RTT |
| Own orders | order ID，side，price，size，created/ack/cancel/fill times，matched size，status |
| Own trades | `traderSide`，fee，maker orders，trade ID，transaction hash，MATCHED/MINED/CONFIRMED/FAILED |
| Inventory | pUSD、YES、NO、reserved、mergeable、directional exposure |
| Incentives | order-scoring，maker rebate，liquidity reward，holding reward，支付日期 |
| Neg Risk | market ID，question count，index set，fee bips，adapter/exchange version |
| Compliance | geoblock result、country/region、checked_at、部署出口 IP 变化 |

## 12. 公开资料不能证明的变量

- 自己的精确 queue position 和撮合 tie-break 规则。
- 其他 maker 的逐订单年龄、撤单、改价和保留意图。
- colocated 竞争者的实际延迟优势。
- 自己从发单到 operator 接收、进入 book、撤单生效的完整分布。
- 未成交订单的反事实：如果继续挂着是否会被逆向选择。
- 未来 reward/rebate 配置和未来竞争份额。
- 其他公开钱包的完整 maker/taker、撤单和链外对冲。
- 多腿同时成交概率；官方 batch 不提供 basket atomicity。
- clarification/dispute 的尾部概率。

这些不是可以用“保守常数”永久替代的参数；必须通过自有录制和受控样本持续估计。

## 13. 常见伪 edge 的判定

| 看似盈利 | 为什么会失效 |
|---|---|
| `mid_yes + mid_no < 1` | midpoint 不可成交，深度和两腿延迟未计 |
| `best_ask_yes + best_ask_no < 1` | 数量可能不足；两腿非原子；taker fee 未计 |
| `bid_yes + bid_no < 1` 的 maker 配对 | 只有两腿都成交才锁利；一腿成交会产生方向风险 |
| 名义 reward / 自己挂单量 | 奖励按竞争者相对得分和随机样本分配 |
| Maker rebate rate × 自己成交额 | rebate 逐市场按 fee-equivalent 竞争分池 |
| 高频撤挂“刷积分” | 不满足 minimum order age，不一定被抽样，还增加撤单/逆向选择风险 |
| 为 Taker tier 刷量 | 先损失 spread/fee，且 self-match/wash trading 被禁止 |
| 0.99 持有到结算 + 4% APY | 规则、澄清、争议和 Unknown/50-50 的尾部损失远大于补贴 |
| 从排行榜复制 | 公开字段不含队列、撤单、maker/taker 全貌或链外头寸 |
| Negative Risk 转换天然零费 | 当前 V2 adapter 明确读取 event-specific `feeBips` |
| 依赖 `/orderbook-history` 做长期回测 | 当前可用不等于受支持；API 目录/SDK 未承诺，必须自采 |

## 14. 直接一手来源

以下链接均于 2026-07-24 核对：

1. [CLOB V2 migration](https://docs.polymarket.com/v2-migration)
2. [Predictions Changelog](https://docs.polymarket.com/changelog)
3. [Fees](https://docs.polymarket.com/trading/fees)
4. [Maker Rebates Program](https://docs.polymarket.com/programs/maker-rebates)
5. [Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards)
6. [Taker Rebate Program](https://docs.polymarket.com/programs/taker-rebates)
7. [Current active rewards API](https://docs.polymarket.com/api-reference/rewards/get-current-active-rewards-configurations)
8. [CLOB market info / minimum order age](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
9. [Market Making](https://docs.polymarket.com/trading/market-making)
10. [Inventory Management](https://docs.polymarket.com/market-makers/inventory)
11. [Positions & Tokens](https://docs.polymarket.com/concepts/positions-tokens)
12. [Place Orders / batch semantics](https://docs.polymarket.com/trading/place-orders)
13. [Order Lifecycle / delays and statuses](https://docs.polymarket.com/concepts/order-lifecycle)
14. [Manage Orders / authenticated own data](https://docs.polymarket.com/trading/manage-orders)
15. [Orderbook](https://docs.polymarket.com/trading/orderbook)
16. [Real-Time Data](https://docs.polymarket.com/market-data/realtime-data)
17. [Matching Engine Restarts](https://docs.polymarket.com/trading/matching-engine)
18. [Public Data API trades](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets)
19. [Public user activity](https://docs.polymarket.com/api-reference/core/get-user-activity)
20. [Geographic Restrictions](https://docs.polymarket.com/api-reference/geoblock)
21. [Resolution](https://docs.polymarket.com/concepts/resolution)
22. [Negative Risk](https://docs.polymarket.com/advanced/neg-risk)
23. [Polymarket CTF Exchange V2 source](https://github.com/Polymarket/ctf-exchange-v2)
24. [V2 NegRisk collateral adapter source](https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/adapters/NegRiskCtfCollateralAdapter.sol)
25. [Market Integrity](https://integrity.polymarket.com/)

## 最终判断

当前最合理的重新设计方向是：

> **用完整集约束的 YES/NO 配对 post-only 做市作为主假设，以深度感知完整集/Negative Risk 扫描作为只读发现层，以奖励作为单独且可归零的补贴项；先自采数据证明队列、单腿和逆向选择成本，再决定是否进入极小额受控实盘。**

它比泛化的双边 spread bot 更可证伪，也更适合小中等资金控制方向风险；但一手资料只能证明其机械基础，不能证明当前存在足够可成交 edge。现阶段结论仍是：**不建议实盘交易**。
