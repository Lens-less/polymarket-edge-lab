# Polymarket Data API `/trades`：`side` 与 `takerOnly` 第一方契约核对

核对日期：2026-07-26

范围：仅使用 Polymarket 官方文档、官方 SDK/合约代码和第一方只读 API。本文不使用社区文章或第三方解释。

## 结论

| 问题 | 结论 | 证据强度 |
|---|---|---|
| `/trades` 返回行的 `side` 是什么？ | 它是该返回行所代表 wallet 对该 outcome token 的交易方向 `BUY`/`SELL`，不是 maker/taker 角色字段。 | 高 |
| `takerOnly=true` 是什么？ | Polymarket 官方 Rust SDK明确写为“只返回 taker trades”，服务端默认值为 `true`。 | 高 |
| `takerOnly=true` 是否必须同时给 `user`/address？ | 不需要。OpenAPI 中 `user` 不是必填项；官方 TS/Python/Rust SDK也把它设为可选，并支持 global/market 范围查询。 | 高 |
| `market + takerOnly=true`、不传 `user` 时，返回行的 `side` 能否视为 taker side？ | 按当前官方接口契约，可以：查询先把结果限定为 taker trades，而每行的 `side` 是该行 trader/wallet 的交易方向。因此这里的 `side` 是 taker 对该 outcome token 的 BUY/SELL 方向。注意，角色依据来自请求中的 `takerOnly=true`，不是 `side` 字段自身。 | 高，但属于接口契约证明，不是逐行链上角色证明 |
| `takerOnly=false` 是否承诺“完整返回所有 maker 和 taker，且每方恰好一行”？ | 当前官方 OpenAPI 没有写出这个完整性、去重或基数承诺。第一方 API 实测会出现额外非 taker 行，但不能把观察升级为长期稳定的完整性契约。 | 证据不足，必须保守 |
| `BUY outcome A @ p` 能否映射成 `SELL complement A' @ (1-p)`？ | 对二元 complete set，存在第一方合约机制依据，可作为**派生的经济等价归一化**；但不能声称原始 Data API 行或原始撮合订单“其实就是”一笔 complementary SELL。 | 机制依据充分；原始事实改写依据不足 |

## 1. Data API 字段与筛选契约

### 1.1 当前 OpenAPI 能直接证明的内容

官方 [`GET /trades` 文档与内嵌 OpenAPI](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets) 给出：

- `takerOnly` 是可选 boolean，服务端默认 `true`；
- `market` 是可选 condition ID 列表；
- `user` 是可选的 User Profile Address，而不是 required 参数；
- 请求和返回中的 `side` 都只有 `BUY`、`SELL` 两个值；
- 返回行同时包含 `proxyWallet`、`asset`、`outcome`、`price` 和 `transactionHash`。

OpenAPI 本身没有给 `takerOnly` 或响应 `side` 写自然语言角色说明。因此，不能只凭参数名推导语义，还需要官方 SDK 的语义注释。

### 1.2 `takerOnly=true` 的官方语义

Polymarket 官方 Rust SDK 对 Data API `/trades` 请求的说明是：

- `user`：“Filter by user address”，类型为 `Option<Address>`；
- `taker_only`：“If true, only return taker trades (default: true)”；
- `side`：“Filter by trade side (BUY or SELL)”。

直接来源：[`TradesRequest` 注释与字段定义](https://github.com/Polymarket/rs-clob-client-v2/blob/222143d321eba97d5711a848265eb9aab3bc7ff4/src/data/types/request.rs#L112-L164)。

这把两个正交概念分开了：

1. `takerOnly` 选择交易角色；
2. `side` 选择 outcome-token 的买卖方向。

所以，`side=BUY` 本身不等于“aggressor”，`side=SELL` 本身也不等于“maker”。只有在请求证据明确包含 `takerOnly=true` 时，返回行才按接口契约属于 taker trade。

### 1.3 返回行的 `side` 属于谁

同一官方 Rust SDK 将 Data API `Trade` 定义为 outcome-token 的已执行交易，并把：

- `proxy_wallet` 描述为 “The trader's proxy wallet address”；
- `side` 描述为 “Trade side (BUY or SELL)”；
- `asset` 描述为 outcome token；
- `price` 描述为 execution price per token。

直接来源：[`Trade` 响应类型](https://github.com/Polymarket/rs-clob-client-v2/blob/222143d321eba97d5711a848265eb9aab3bc7ff4/src/data/types/response.rs#L172-L229)。

官方 TS SDK 也把 wire 字段 `proxyWallet` 归一化为 `wallet`，而保留同行的 `side`：[`TradeSchema` 映射](https://github.com/Polymarket/ts-sdk/blob/0760f99f04e879164fafe79d8277395bb200cee9/packages/bindings/src/data/activity.ts#L312-L338)。同一 SDK 对 trade activity 的说明更明确地称 `side` 为 “Direction of the wallet's trade”，并称 token ID 为该 wallet 买入或卖出的 outcome token：[`TradeActivityBase` 与 `ClobTradeActivity`](https://github.com/Polymarket/ts-sdk/blob/0760f99f04e879164fafe79d8277395bb200cee9/packages/bindings/src/data/activity.ts#L46-L99)。

因此，最窄且有第一方依据的解释是：

> Data API `/trades` 每行的 `side` 是该行 `proxyWallet` 对该行 `asset`/`outcome` 的 BUY/SELL 方向；maker/taker 是另一维角色。

### 1.4 `user` 不需要与 `takerOnly` 绑定

除 OpenAPI 未把 `user` 标为 required 外，还有三份官方 SDK 证据：

- Rust：`TradesRequest.user` 是 `Option<Address>`，见上面的 [`TradesRequest`](https://github.com/Polymarket/rs-clob-client-v2/blob/222143d321eba97d5711a848265eb9aab3bc7ff4/src/data/types/request.rs#L143-L164)。
- Python：`list_trades_spec` 的 `user` 默认 `None`，`market` 与 `taker_only` 分别独立传给 `/trades`，且没有“必须提供 user”的校验：[`list_trades_spec`](https://github.com/Polymarket/py-sdk/blob/6a8f73267f3e776c1d2e8abed538dd5f3fbcda00/src/polymarket/_internal/actions/data.py#L359-L399)。
- TypeScript：`takerOnly`、`market`、`user` 都是 optional；`listTrades` 默认接受空请求：[`ListTradesRequestSchema` 与 `listTrades`](https://github.com/Polymarket/ts-sdk/blob/0760f99f04e879164fafe79d8277395bb200cee9/packages/client/src/actions/activity.ts#L40-L157)。官方集成测试还明确测试了无 `user` 的 global trades：[`lists global trades`](https://github.com/Polymarket/ts-sdk/blob/0760f99f04e879164fafe79d8277395bb200cee9/packages/client/tests/integration/activity.test.ts#L27-L36)。

所以 `market=<conditionId>&takerOnly=true` 不需要添加虚构或任意 `user` 才能获得 taker-only 结果。

## 2. 第一方 API 的只读交叉检查

以下是固定 market、固定秒级窗口的第一方 Data API 查询。它们是 2026-07-26 的行为观察，用来检查官方 SDK 解释是否与服务行为一致；它们不替代规范，也不构成未来永不改变的返回基数保证。

### 2.1 普通 BUY/SELL 撮合

固定交易 `0x61d9f4c29fe4bbb40c917e91514f833e24125fffbe7372af78de63f07157b84e`：

- [`takerOnly=true` 查询](https://data-api.polymarket.com/trades?market=0x36ed4680a4ce307b782205cfca5f6031c39c3a69811c5c1cd03b7d0f85c4153c&takerOnly=true&start=1785063091&end=1785063091&limit=100) 中，该交易只有 wallet `0x64ef...bf82` 的 `SELL Yes @ 0.17` 行；
- [`takerOnly=false` 查询](https://data-api.polymarket.com/trades?market=0x36ed4680a4ce307b782205cfca5f6031c39c3a69811c5c1cd03b7d0f85c4153c&takerOnly=false&start=1785063091&end=1785063091&limit=100) 中，同一交易还出现另一 wallet 的 `BUY Yes @ 0.17` 行。

这与“`true` 只保留 taker trade；`side` 是该交易者的方向”一致。它也说明，不能把 `SELL` 或 `BUY` 本身当成 maker/taker 角色标签。

### 2.2 互补 outcome 的双 BUY 撮合

固定交易 `0x01281f639c2a5fe715a1856eb48c726d0a7dca4fc650cbba4c5cbf4571b76932`：

- [`takerOnly=true` 查询](https://data-api.polymarket.com/trades?market=0x7ac8f8be4704e43f8c54895479bbf4984cbda8280c7c2c0172e803bc8d765cc2&takerOnly=true&start=1785063004&end=1785063004&limit=100) 中，该交易是 `BUY Down @ 0.57`、size `10`；
- [`takerOnly=false` 查询](https://data-api.polymarket.com/trades?market=0x7ac8f8be4704e43f8c54895479bbf4984cbda8280c7c2c0172e803bc8d765cc2&takerOnly=false&start=1785063004&end=1785063004&limit=100) 中，同一交易另有 `BUY Up @ 0.43`、size `10`；
- 两个价格相加为 `1.00`。

这与合约的 MINT（both BUY）机制一致，也给出一个重要反例：

> 即使 `BUY Down @ 0.57` 在经济上可以归一化为 `SELL Up @ 0.43`，第一方 API 的原始另一侧记录仍可能是 `BUY Up @ 0.43`，而不是 `SELL Up @ 0.43`。

因此，归一化字段不能覆盖或冒充 observed `side`。

## 3. `BUY A @ p` 与 `SELL A' @ (1-p)` 的机制依据和边界

### 3.1 第一方机制依据

Polymarket 官方 CTF Exchange 文档明确给出：

- `A` 与 `A'` 是互补 outcome token；
- 一份 `A` 加一份 `A'` 可 merge 为一单位 collateral；
- 一单位 collateral 可 split 为一份 `A` 加一份 `A'`，即 `A + A' = C`；
- 互补 token 的订单可以通过 mint/merge 交叉撮合。

直接来源：[`CTF Exchange Overview` 的资产与 complete-set 定义](https://github.com/Polymarket/ctf-exchange/blob/ed5c7708b7be3aa98bf5f0c6602b57cc498e2ef4/docs/Overview.md#L3-L16)。

同一官方文档展示：

- `MINT` 场景可以撮合 `BUY A` 与 `BUY A'`：[`Scenario 2`](https://github.com/Polymarket/ctf-exchange/blob/ed5c7708b7be3aa98bf5f0c6602b57cc498e2ef4/docs/Overview.md#L59-L99)；
- `MERGE` 场景可以撮合 `SELL A` 与 `SELL A'`：[`Scenario 3`](https://github.com/Polymarket/ctf-exchange/blob/ed5c7708b7be3aa98bf5f0c6602b57cc498e2ef4/docs/Overview.md#L102-L142)；
- 对二元互补 token，官方以“`SELL A @ 0.99` 与 `BUY A' @ 0.01`”作为对称经济关系的明确例子：[`Fees` 的对称性说明](https://github.com/Polymarket/ctf-exchange/blob/ed5c7708b7be3aa98bf5f0c6602b57cc498e2ef4/docs/Overview.md#L144-L150)。

当前官方 CTF Exchange v2 也保留相同三类结算机制：`COMPLEMENTARY` 为 BUY vs SELL，`MINT` 为 both BUY，`MERGE` 为 both SELL：[`v2 Order Lifecycle`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/README.md#L90-L98)。

### 3.2 经济等价的推导

对二元 complete set，令一单位 collateral 的价值为 `1`：

1. 直接 `BUY A @ p` 的净资产变化是 `+A - pC`。
2. 先将 `1C` split 为 `A + A'`，再 `SELL A' @ (1-p)`：
   - split 后：`+A + A' - C`；
   - 卖出 `A'` 后：`-A' + (1-p)C`；
   - 合计仍为 `+A - pC`。

因此，`BUY A @ p` 与 `SELL A' @ (1-p)` 有 complete-set 机制支持的经济等价关系。官方对 `SELL A @ 0.99` 与 `BUY A' @ 0.01` 的对称性例子正是同一关系换名后的方向。

### 3.3 不能越过的证据边界

第一方资料**足以**支持：

- 在已经证明是二元互补 token 的前提下，生成一个明确标注为 derived/economic-equivalent 的方向：
  - observed `BUY A @ p`
  - derived `SELL A' @ (1-p)`。

第一方资料**不足以**支持：

- 把 derived complementary SELL 写成原始 Data API observed row；
- 声称原始撮合的另一张订单一定是 SELL；它也可能是合约 MINT 场景中的 complementary BUY；
- 仅凭 `side` 推断 maker/taker；
- 在没有保存 `takerOnly=true` 请求参数/响应来源的情况下，事后把普通 `/trades` 行标成 taker；
- 对非二元、未证明 complement token 身份、negative-risk 多 outcome 或 combo position 无条件应用 `1-p`；
- 忽略 fee、tick rounding、size rounding、split/merge 可用性、库存/抵押品约束后宣称两条路径的真实净现金流完全相同。

## 4. 对 replay 数据契约的最小安全要求

建议把原始事实和派生归一化严格分栏：

```text
observed.proxyWallet
observed.asset
observed.outcome
observed.side
observed.price
observed.size
observed.transactionHash
observed.request.market
observed.request.takerOnly

role = "taker" only if observed.request.takerOnly == true

derived.complementAsset
derived.complementOutcome
derived.side = opposite(observed.side)
derived.price = 1 - observed.price
derived.basis = "binary_complete_set_economic_equivalence"
derived.isObservedOrder = false
```

其中 `derived.*` 只有在 market metadata 已证明二元 outcome 配对、token identity 无歧义且价格/精度规则被冻结后才可生成。若缺少这些证据，应保留原始行并将 complementary mapping 标为 `insufficient_official_evidence`，不能猜测。

## 5. 最终判定

1. **Data API `/trades.side` 不是独立的 aggressor 标志。**它是返回行 wallet/trader 对该 outcome token 的 BUY/SELL 方向。
2. **`takerOnly=true` 才是角色过滤依据。**官方 SDK 明确把它定义为只返回 taker trades，默认值为 `true`。
3. **`user` 不是必要条件。**`market + takerOnly=true` 且无 `user` 是官方接口支持的查询形态；按接口契约，返回行的 `side` 可视为该 taker 对该 token 的方向。
4. **证明强度有限定。**响应没有独立 `traderSide`/`role` 字段，所以这是请求契约级的角色证明；若 replay 需要逐行更强认证，必须保存请求、原始响应、时间和内容哈希，或再与 CLOB/on-chain order evidence 对接。
5. **互补 `1-price` 变换有第一方机制依据，但只能是派生经济等价。**不能用它篡改 observed trade，也不能据此虚构原始 counterparty order 的 side 或 maker/taker 身份。
