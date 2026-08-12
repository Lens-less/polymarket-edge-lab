# Polymarket 官方机制与一手接口核查

> 查询日期：2026-07-24（Asia/Shanghai）  
> 资料边界：仅使用 Polymarket 官方文档、官方 API、官方 GitHub 仓库与官方列出的 Polygon 合约；没有使用博客、论坛、聚合站或第三方 SDK。  
> 实盘边界：本轮没有连接真实账户、没有读取密钥、没有签名、没有下单，也没有发送链上交易。  
> 结论边界：本文确认的是协议与接口机制，不构成盈利证明；任何策略在完成真实成交、费用、排队与故障注入验证前，均不建议实盘交易。

## 1. 状态标记与结论摘要

- **[已确认]**：由官方文档、官方代码或 2026-07-24 的官方公开接口响应直接支持。
- **[变化/不确定]**：官方资料之间有漂移，或属于可由平台动态调整的参数。
- **[需真实探针]**：只有带认证的请求、真实 WebSocket 会话、链上调用或最小真实交易才能确认；本文不把它们写成已验证事实。

截至查询日，最重要的变化如下：

1. **[已确认] CLOB V2 已是生产版本。** 官方迁移页记载其于 2026-04-28 上线；2026-07-24 对 [`GET /version`](https://clob.polymarket.com/version) 的只读探针返回 `{"version":2}`。V1 SDK、V1 订单和旧签名结构不兼容，迁移时旧挂单已清除。[官方迁移说明](https://docs.polymarket.com/v2-migration)
2. **[已确认] 2026-07-24 起 FAK/FOK 成功提交进入异步提交链路。** `POST /order` 和 `POST /orders` 的成功响应不再保证内联交易哈希，而返回 `tradeIDs`；自建 REST 客户端必须轮询交易状态，不能把 HTTP 成功当作链上成交完成。WebSocket 成交通知不受该项改动影响。[Predictions changelog](https://docs.polymarket.com/changelog/predictions)
3. **[已确认] 费用和返佣已按市场、按类别动态化。** 不应把某个全局费率或返佣比例硬编码进回测；Gamma `feeSchedule` 与 CLOB 市场信息中的 `fd` 才是当前市场约束。[费用说明](https://docs.polymarket.com/trading/fees) · [Maker Rebates](https://docs.polymarket.com/programs/maker-rebates)
4. **[已确认] 当前抵押品是 6 位小数的 pUSD。** 标准 CTF、Neg-Risk CTF、onramp/offramp 与适配器均有新的官方地址；2026-07-17 后旧 v1 Neg-Risk adapter 已不再由 relayer 代赎回。[pUSD](https://docs.polymarket.com/concepts/pusd) · [官方合约地址](https://docs.polymarket.com/resources/contracts) · [Predictions changelog](https://docs.polymarket.com/changelog/predictions)
5. **[变化/不确定] 多处官方文档、旧报告与实况已发生漂移。** 主要包括 CLOB 市场最小挂单年龄的 `oas`/`r.moas` 结构差异、分辨率页面与合约页面的 UMA 地址标注冲突、order-scoring 示例漏传必填 `order_id`，以及奖励文档的“每日发放”与 10,080 个分钟样本之间的周期冲突。
6. **[需真实探针] 公共数据不能证明 maker 身份、队列位置、撤单竞争或账户实际返佣。** 市场 WebSocket 提供聚合深度而非逐单队列；认证后的用户流、成交记录、奖励百分比和链上结果必须共同对账。

### 1.1 已明确过时、不得沿用的旧结论

以下结论在旧报告、历史页面或缓存中仍可能出现，但截至 2026-07-24 已不可作为当前实现依据：

| 旧结论 | 当前官方事实 |
|---|---|
| Sports taker fee 为 0.03、maker rebate 为 25% | 2026-07-10 changelog 已改为 **0.05 / 15%** |
| 所有奖励订单只需存活 3.5 秒 | 3.5 秒来自特定 March Madness 活动；现网 `r.moas` 至少见 4 秒和 30 秒，必须逐市场读取 |
| Neg-Risk 继续使用 v1 adapter `0xd91…5296` | 当前 pUSD adapter 为 `0xadA2…eAab`；旧 adapter 的 relayer 赎回宽限已于 2026-07-17 结束 |
| FAK/FOK 成功响应总会内联交易哈希 | 2026-07-24 起异步提交，成功响应以 `tradeIDs` 为主，需轮询终态 |
| RTDS 可稳定提供 CLOB orderbook/user fills | 当前 RTDS 稳定文档范围是价格源与评论；CLOB 数据应使用 CLOB market/user WebSocket |
| Gamma 市场 keyset 最大 limit 为 500 | 2026-05-14 已降为 **100**；event keyset 仍可到 500 |
| 旧 V1 order typed data、旧 `clob-client` 仍可用于生产 | 生产 `/version` 为 2，V1 不兼容；新项目应使用统一 `@polymarket/client` / `polymarket-client` |

来源：[Predictions changelog](https://docs.polymarket.com/changelog/predictions)、[General changelog](https://docs.polymarket.com/changelog)、[SDK migration](https://docs.polymarket.com/getting-started/migrate-from-previous-sdks)；查询日期：2026-07-24。

## 2. 官方来源与只读探针记录

### 2.1 官方服务面

**[已确认]** 官方 API 总览列出的主要服务为：

| 服务 | 官方入口 | 本项目用途 |
|---|---|---|
| Gamma | [`https://gamma-api.polymarket.com`](https://gamma-api.polymarket.com) | 事件、市场、规则、分类、费用计划、Neg-Risk 元数据 |
| CLOB REST | [`https://clob.polymarket.com`](https://clob.polymarket.com) | 可交易约束、订单簿、订单、成交、奖励与认证 |
| Data API | [`https://data-api.polymarket.com`](https://data-api.polymarket.com) | 公开持仓、活动、账户维度数据 |
| CLOB WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/{market,user}` | 聚合盘口、成交、私有订单/成交事件 |
| RTDS | `wss://ws-live-data.polymarket.com` | 加密货币、股票价格源和评论流 |
| Relayer V2 | [`https://relayer-v2.polymarket.com`](https://relayer-v2.polymarket.com) | Deposit Wallet / Proxy / Safe 的受支持链上操作 |

来源：[API introduction](https://docs.polymarket.com/getting-started/api)；查询日期：2026-07-24。

### 2.2 本轮公开探针

| 探针 | 2026-07-24 结果 | 判定 |
|---|---|---|
| [`GET /version`](https://clob.polymarket.com/version) | `{"version":2}` | **[已确认]** 当前生产入口为 CLOB V2 |
| [`GET /time`](https://clob.polymarket.com/time) | HTTP 200，返回交易所 Unix 时间 | **[已确认]** 可用于签名前的服务器时钟校准 |
| [`GET /rewards/markets/current?limit=3`](https://clob.polymarket.com/rewards/markets/current?limit=3) | 实际响应 `limit=500`、`count=500`、`next_cursor="NTAw"` | **[变化/不确定]** 服务端会忽略或钳制小 limit，分页代码必须以响应为准 |
| [`GET Gamma /markets?limit=1&active=true&closed=false`](https://gamma-api.polymarket.com/markets?limit=1&active=true&closed=false) | 返回 `conditionId`、JSON 字符串形式的 `clobTokenIds`/`outcomes`、`feeSchedule`、`negRisk` 等 | **[已确认]** Gamma 某些数组字段需要二次 JSON 解析 |
| [`GET /clob-markets/{condition_id}`](https://clob.polymarket.com/clob-markets/0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be) | 返回紧凑字段 `c,t,mos,mts,mbf,tbf,ao,fd`，最小挂单年龄出现在 `r.moas` | **[变化/不确定]** 与文档顶层 `oas` 示例不一致 |
| CLOB market/user 与 RTDS HTTP Upgrade | 独立 Upgrade 探针均收到 `101 Switching Protocols`；本机原生客户端路径受系统 SOCKS 代理与缺少 `python-socks` 依赖限制 | **[已确认/需真实探针]** 端点可握手；本地失败不代表官方端点故障，消息级、重连和认证语义仍未验证 |

公开探针仅用于模式与连通性校验；响应中的具体市场、奖励池和数值都是时点数据，不应固化。

## 3. CLOB V2：交易、签名结构与订单生命周期

### 3.1 V2 合约与签名域

**[已确认]**

Polymarket 使用混合式 CLOB：订单在链下创建、签名、排序与撮合，operator 再把匹配结果提交 Polygon；V2 Exchange 合约验证签名并原子结算 pUSD 与 outcome tokens。[Prices & Orderbook](https://docs.polymarket.com/concepts/prices-orderbook) · [Order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)

- CTF Exchange V2：`0xE111180000d2663C0091e4f400237545B87B996B`
- Neg-Risk CTF Exchange V2：`0xe2222d279d744050d28e00520010520000310F59`
- EIP-712 订单域：
  - `name = "Polymarket CTF Exchange"`
  - `version = "2"`
  - `chainId = 137`
  - `verifyingContract` 必须按普通市场或 Neg-Risk 市场选择对应交易所地址

官方迁移页与官方 V2 合约代码相互印证：

- [CLOB V2 migration guide](https://docs.polymarket.com/v2-migration)
- [`Hashing.sol`，固定到官方仓库提交 `ccc0596`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Hashing.sol)
- [官方合约地址表](https://docs.polymarket.com/resources/contracts)

V2 被签名的 `Order` 结构为：

```text
salt
maker
signer
tokenId
makerAmount
takerAmount
side
signatureType
timestamp
metadata
builder
```

来源：[`Structs.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/libraries/Structs.sol)；查询日期：2026-07-24。

这意味着旧 V1 的 `taker`、`expiration`、`nonce`、`feeRateBps` 不属于 V2 的被签名结构。`expiration` 仍可能作为 GTD 的 REST 订单参数存在，但不能继续用 V1 的 typed-data 结构签名。费用由撮合时的市场配置决定，客户端应查询市场而不是签入静态费率。

**[已确认]** `side` 为 `0=BUY`、`1=SELL`；V2 order `timestamp` 是毫秒，而 L1/L2 认证时间戳是 Unix 秒。三个时间概念不可混用。

### 3.2 签名类型

**[已确认]** 官方合约枚举为：

| 值 | 合约枚举 | 当前 SDK 语义 | 校验方式 |
|---:|---|---|---|
| 0 | `EOA` | EOA | `signer == maker`，恢复 ECDSA 签名 |
| 1 | `POLY_PROXY` | Legacy Proxy Wallet | 从 proxy 关系验证所有者 |
| 2 | `POLY_GNOSIS_SAFE` | Legacy Safe | 从 Safe 关系验证所有者 |
| 3 | `POLY_1271` | Deposit Wallet | `signer == maker` 合约并通过 EIP-1271 |

来源：[`Structs.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/libraries/Structs.sol)、[`Signatures.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Signatures.sol)、[Wallets & Authentication](https://docs.polymarket.com/trading/wallets-auth)。

合约还支持已预批准哈希的空签名路径；这是链上预授权机制，不应被离线回测器误当作普通无签名订单。

对 type 3 还要区分两个身份层：L1/L2 的 `POLY_ADDRESS` 是认证 EOA，而订单的 `maker` 与 `signer` 都是 Deposit Wallet。当前统一 SDK 使用 Deposit Wallet 的 EIP-1271/ERC-7739 typed-data 包装完成校验，不能把它当作普通 EOA 签名。[官方统一 SDK `exchange.ts`](https://github.com/Polymarket/ts-sdk/blob/323cde59e004f38a9eff91cfd34806c91d6f4218/packages/client/src/exchange.ts#L118-L164)

### 3.3 订单类型与撮合语义

**[已确认]**

- `GTC`：持续有效，直至成交或取消。
- `GTD`：到期前有效。官方下单文档要求用户给出的时间至少在未来 3 分钟，平台会将实际到期时间设为所给时间前 1 分钟。
- `FOK`：无法立即全部成交则取消。
- `FAK`：立即成交可成交部分，剩余取消。
- `postOnly=true`：如果订单会立即跨价成交则拒绝。
- 选定市场可启用 taker 延迟；通用说明给出 250ms，部分体育市场可配置为秒级。在延迟窗口内不能取消该订单。
- 订单状态与成交状态是两层状态机。用户 trade 流还可出现 `MATCHED_NOT_BROADCASTED`；广播后可经历 `MATCHED → MINED → CONFIRMED`，也可能进入 `RETRYING`，并以 `FAILED` 终止。
- 一次链上撮合中的腿是原子结算；但 REST 批量提交的多张订单逐张成功或失败，不是组合篮子的原子事务。
- 官方批量下单上限为 15 张；每张订单必须独立检查结果。
- V2 合约的链上 `MatchType` 为 `COMPLEMENTARY`（BUY/SELL 直接交换）、`MINT`（两边 BUY，生成 complete set）和 `MERGE`（两边 SELL，合并 complete set）。

来源：[Order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)、[Place orders](https://docs.polymarket.com/trading/place-orders)；查询日期：2026-07-24。

### 3.4 2026-07-24 异步提交改动

**[已确认]** Predictions changelog 于 2026-07-17 公告、并在 2026-07-24 04:00 UTC 启用：

- 成功的 FAK/FOK `POST /order` 和 `/orders` 不再保证返回内联 `transactionHashes`；
- 响应返回 `tradeIDs`；
- 自建 REST 客户端必须轮询这些 trade，取得链上哈希或终态 `FAILED`；
- 用户 WebSocket 的 fill 事件不受此改动影响。

因此实现上必须把“请求已接收”“CLOB 已匹配”“交易已上链”“已确认”分开记录。任何只按 HTTP 200 计成交的回测/影子交易都将高估执行质量。

来源：[Predictions changelog](https://docs.polymarket.com/changelog/predictions)；查询日期：2026-07-24。

### 3.5 SDK 选择

**[已确认]** 迁移页使用 `@polymarket/clob-client-v2` 和 `py-clob-client-v2` 展示 V2 迁移；但当前官方迁移文档与 V2 客户端 README 已建议新项目使用统一 SDK：

- [`@polymarket/client` / Polymarket TypeScript SDK](https://github.com/Polymarket/ts-sdk)
- [`polymarket-client` / Polymarket Python SDK](https://github.com/Polymarket/py-sdk)
- [旧的官方 CLOB V2 TypeScript 客户端](https://github.com/Polymarket/clob-client-v2)

**[变化/不确定]** 现存实现若仍依赖单独 CLOB V2 客户端，不代表立刻失效；但新代码应优先按统一 SDK 的当前接口设计，并固定依赖版本和提交，避免 README/包版本继续漂移。

统一 SDK 源码中已经出现 `ExchangeOrderProtocolVersion.V3` 枚举，但这不能证明标准生产 CLOB 已切到 V3；2026-07-24 的官方 [`/version`](https://clob.polymarket.com/version) 仍明确返回 2，因此订单签名继续按 V2。

## 4. 认证、请求签名与钱包

### 4.1 L1：创建或派生 API 凭证

**[已确认]** L1 认证使用钱包 EIP-712 签名：

```text
domain:
  name: ClobAuthDomain
  version: "1"
  chainId: 137

message:
  address: <wallet address>
  timestamp: <unix seconds>
  nonce: <uint256>
  message: "This message attests that I control the given wallet"
```

请求头为：

- `POLY_ADDRESS`
- `POLY_SIGNATURE`
- `POLY_TIMESTAMP`
- `POLY_NONCE`

创建与派生分别使用 `POST /auth/api-key`、`GET /auth/derive-api-key`。返回值包含 `apiKey`、Base64 secret 和 passphrase。

来源：[API authentication](https://docs.polymarket.com/getting-started/api)；查询日期：2026-07-24。

### 4.2 L2：私有 REST 请求

**[已确认]** L2 HMAC 消息由以下原始内容拼接：

```text
timestamp + UPPERCASE_HTTP_METHOD + request_path + exact_serialized_body
```

使用 API secret Base64 解码后的字节做 HMAC-SHA256，并输出 URL-safe、保留 padding 的 Base64。请求头为：

- `POLY_ADDRESS`
- `POLY_SIGNATURE`
- `POLY_TIMESTAMP`
- `POLY_API_KEY`
- `POLY_PASSPHRASE`

下单需要两层不同的证明：

1. L2 HMAC 证明这次 API 请求有权限；
2. EIP-712 order signature 证明订单本身由对应资金钱包授权。

实现时必须对签名使用的 pathname 和 body 保留字节级一致性；当前官方客户端不把 query parameters 拼入 HMAC 消息。签名后再重排 JSON、改变空白或 pathname 都会导致 HMAC 不一致。

### 4.3 钱包世代与派生漂移

**[已确认]**

- 2026-05-04 后部署的账户默认使用 Deposit Wallet；旧账户可能是 Proxy 或 Safe。
- Deposit Wallet 对应签名类型 3 / EIP-1271。
- 2026-06-29 前的 Deposit Wallet 使用 UUPS；升级后使用 Beacon Proxy，确定性地址派生方式已变化。

来源：[Wallets & Authentication](https://docs.polymarket.com/trading/wallets-auth)；查询日期：2026-07-24。

**[变化/不确定]** 不应仅凭 EOA 地址在本地推导资金钱包，也不能假设所有账户都是同一钱包世代；应从官方 SDK/账户接口取得实际 maker/funder/wallet type。

### 4.4 仍需认证探针的项目

以下项目在没有真实账户和密钥时不能确认：

- **[需真实探针]** 服务器对时钟偏差的实际容忍窗口、nonce 重放行为和派生 API key 的幂等边界；
- **[需真实探针]** L2 body 序列化、空 body、查询参数顺序在生产客户端中的精确兼容性；
- **[需真实探针]** Deposit Wallet、Proxy 与 Safe 的真实 maker/funder 映射；
- **[需真实探针]** API key 权限撤销、轮换、限流和错误码；
- **[需真实探针]** 用户 WebSocket 初始化时提交的 secret/passphrase 是否存在额外短期令牌替代方案。

安全底线：API secret、passphrase、私钥、L1/L2 签名原文和用户 WebSocket 初始认证包不得进入日志、回测产物或异常上报。

## 5. pUSD、合约地址与费用

### 5.1 pUSD

**[已确认]**

- pUSD 是 Polygon 上的标准 ERC-20，6 位小数；
- pUSD 由 USDC 支持，可转账；
- `CollateralOnramp.wrap(asset,to,amount)` 将支持资产包装为 pUSD，调用前需给 onramp 授权；
- `CollateralOfframp.unwrap(...)` 用于解除包装；
- 官方当前没有让 pUSD 在外部交易所上市的计划。

来源：[pUSD concept](https://docs.polymarket.com/concepts/pusd)；查询日期：2026-07-24。

### 5.2 官方合约地址

下表来自官方声明的“single source of truth”合约页；网络均为 Polygon，`chainId=137`。

| 合约 | 地址 |
|---|---|
| CTF Exchange V2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| Neg-Risk CTF Exchange V2 | `0xe2222d279d744050d28e00520010520000310F59` |
| Conditional Tokens Framework | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| pUSD proxy | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| Collateral Onramp | `0x93070a847efEf7F70739046A929D47a521F5B8ee` |
| Collateral Offramp | `0x2957922Eb93258b93368531d39fAcCA3B4dC5854` |
| Standard CTF Collateral Adapter | `0xAdA100Db00Ca00073811820692005400218FcE1f` |
| Current Neg-Risk CTF Collateral Adapter | `0xadA2005600Dec949baf300f4C6120000bDB6eAab` |
| Deprecated v1 Neg-Risk Adapter | `0xd91E80cF2E7be2e162C6513CEcE5f7799d1e5296` |
| UMA Adapter | `0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74` |
| UMA Optimistic Oracle | `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` |

来源：[Contract addresses](https://docs.polymarket.com/resources/contracts)；查询日期：2026-07-24。

**[已确认]** changelog 说明旧 v1 Neg-Risk adapter 的 relayer 代赎回宽限期已在 2026-07-17 结束；新操作应使用当前 pUSD adapter，历史仓位则必须先识别其 adapter 世代。[Predictions changelog](https://docs.polymarket.com/changelog/predictions)

### 5.3 交易费

**[已确认]** 官方费用公式为：

```text
fee = C × feeRate × p × (1 - p)
```

其中 `C` 是成交 shares、`p` 是成交价格。费用关于 `p=0.5` 对称，在中间价格最高；maker 不付交易费，taker 支付。官方 SDK 支持市场级指数的一般形式：

```text
fee = C × r × [p × (1 - p)]^e
```

当前公开样本的 `e=1`，但实现仍必须保存 `r/e/to`，不能硬编码指数。

Taker BUY 从 pUSD 抵押品中额外扣除 fee；Taker SELL 从应收 pUSD proceeds 中扣 fee，两者都不是从 outcome shares 中扣除。来源：[`Trading.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol)。

官方文字还规定费/返佣保留 5 位小数，最小非零值为 `0.00001`，小于该值则归零。**[需真实探针]** 官方没有充分解释恰好在舍入边界附近的 half-way 规则；不能只用普通 `ROUND_HALF_UP` 猜测。

查询日的官方类别表为：

| 类别 | `feeRate` | Maker rebate pool 占所收 taker fees |
|---|---:|---:|
| Crypto | 0.07 | 20% |
| Sports | 0.05 | 15% |
| Finance / Politics / Mentions / Tech | 0.04 | 25% |
| Economics / Culture / Weather / Other | 0.05 | 25% |
| Geopolitics | 0 | 无 |

来源：[Trading fees](https://docs.polymarket.com/trading/fees)、[Maker Rebates](https://docs.polymarket.com/programs/maker-rebates)、[Predictions changelog](https://docs.polymarket.com/changelog/predictions)；查询日期：2026-07-24。

**[变化/不确定]**

- 平台可修改类别、费率和返佣池比例；2026-07-10 的 changelog 已记录 Sports `feeRate` 从 0.03 调到 0.05、maker rebate 从 25% 调到 15%。
- 每个市场的 `feesEnabled` 与 Gamma `feeSchedule {exponent,rate,takerOnly,rebateRate}` 才是实际依据。
- CLOB `/clob-markets/{condition_id}` 还返回紧凑 `fd {r,e,to}`。回测导入时应保存原始快照、查询时间和来源，不能只保存计算后的单一费率。
- `mbf`/`tbf` 仍可能出现在 CLOB 紧凑市场对象中；不要仅凭字段名把其直接当作最终费用公式，先与 `fd`、Gamma `feeSchedule` 和实际成交费用对账。
- 同一 token 的 `/fee-rate?token_id=...` 可能返回 `base_fee=1000`，而 CLOB market 同时返回 `fd.r=0.05,e=1,to=true`；官方 V2 SDK使用 `fd`。不得把 `base_fee=1000` 直接解释成 10% 线性费率。
- Maker Rebate 当前正文称以 pUSD 发放，但旧响应字段仍可能叫 `rebated_fees_usdc`，部分费用文字也残留 USDC 名称；资产会计应按当前抵押品/奖励的 `asset_address` 判断，不能按遗留字段名猜测。
- 官方 V2 仓库 README 中的部分 adapter 地址可能落后于当前 Contracts 页与统一 SDK environment；运行时地址以官方 Contracts 页和当前 SDK 配置交叉验证。

## 6. Maker rebates、Liquidity Rewards 与订单评分

### 6.1 Maker rebates

**[已确认]**

- 只有先增加 resting liquidity、随后被成交的 maker 订单才产生返佣资格；
- 每笔 maker fill 先计算其 `fee_equivalent = C × feeRate × p × (1-p)`；
- 某 maker 在某市场当天的份额为：

```text
maker rebate =
  your_fee_equivalent / total_market_fee_equivalent × daily_rebate_pool
```

- 返佣每日以 pUSD 直接发放到钱包，最低累计支付门槛为 1 pUSD；
- 返佣比例是“从该类市场收取的 taker fees 中拨入池子的比例”，不是直接乘在 maker 成交名义金额上；
- 返佣规则由平台酌情调整，不能被视为保证收益。

来源：[Maker Rebates](https://docs.polymarket.com/programs/maker-rebates)；查询日期：2026-07-24。

### 6.2 Liquidity Rewards

**[已确认]**

每个市场独立配置：

- 最小订单数量；
- 最大合格价差；
- 每日奖励额度。

单边订单的基础分数为：

```text
S(v, s) = ((v - s) / v)^2 × b
```

其中 `v` 是该市场最大合格 spread、`s` 是订单到奖励参考价的 spread、`b` 是合格订单规模。越接近参考价，分数按平方函数增加。

对二元市场，系统将本市场 bid 与互补市场 ask 组合成 `Q_one`，并将本市场 ask 与互补市场 bid 组合成 `Q_two`。官方给定单边折扣常数 `c=3`：

```text
Q = max(min(Q_one, Q_two), max(Q_one / c, Q_two / c))
```

- 若中间价在 `[0.10, 0.90]`，单边流动性可以按折扣计分；
- 若中间价小于 0.10 或大于 0.90，则必须双边报价才计分；
- 每分钟随机采样；公式写一个 epoch 共 10,080 次；
- 每次采样先在 maker 间归一化，epoch 汇总后再次归一化并乘以市场奖励池。

来源：[Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards)；查询日期：2026-07-24。

**[变化/不确定]**

- 10,080 个一分钟样本实际等于 7 天，但同页又称每日 UTC 00:00 发放、最低支付 1 美元等值；官方没有解释这是滚动窗口、周 epoch 还是仅公式示意。实现不得预设窗口或结算周期，必须用真实 payout 对账。
- 历史 changelog 中的 3.5 秒持久在线要求属于特定 March Madness 活动，不是所有市场当前的全局常数。2026-07-24 现网 `r.moas` 至少见到 4 秒和 30 秒，应逐市场读取。
- 文档未完整公开 size-cutoff-adjusted midpoint 的构造和体育比赛进行中倍率 `b` 的数值时间表。

### 6.3 奖励配置与评分接口

**[已确认]**

- 公开奖励配置：`GET /rewards/markets/current`
- 文档声明一页 500 条，游标分页；`LTE=` 表示最后一页。
- 返回字段包括 `condition_id`、`rewards_max_spread`、`rewards_min_size`、`rewards_config`、`native_daily_rate`、`total_daily_rate`。
- 认证后的实时账户奖励份额：`GET /rewards/user/percentages`
- 认证后的订单合格性：`GET /order-scoring?order_id=...`，响应核心为 `{"scoring": true|false}`。
- 公开 maker 日返佣结果：`GET /rebates/current?date=&maker_address=`
- 认证后的用户收益：`GET /rewards/user`、`/rewards/user/total`、`/rewards/user/markets`
- 当前官方 Python SDK 另实现批量 `POST /orders-scoring`，返回 `{order_id: boolean}`，但 API Reference 尚未单列该路由。

CLOB V2 紧凑市场结构的官方类型还定义：

- `fd.r / fd.e / fd.to`：fee rate、fee exponent、taker-only；
- `r.mi / r.ma / r.e`：最小奖励订单尺寸、最大合格价差、奖励是否开启；
- `r.smoa / r.moas`：是否跳过最小挂单年龄、最小挂单年龄秒数。

来源：[Current active rewards configurations](https://docs.polymarket.com/api-reference/rewards/get-current-active-rewards-configurations)、[Order scoring status](https://docs.polymarket.com/api-reference/trade/get-order-scoring-status)、[Current rebated fees](https://docs.polymarket.com/api-reference/rebates/get-current-rebated-fees-for-a-maker)、[官方 Python SDK rewards actions](https://github.com/Polymarket/py-sdk/blob/fe24c401b35370499d8e46e604e77d1293511ad6/src/polymarket/_internal/actions/rewards.py#L51-L288)、[官方 CLOB V2 类型](https://github.com/Polymarket/clob-client-v2/blob/f3e1a05f868a1fd0c34ef85dfc45c6ce78f5bb69/src/types/clob.ts#L293-L320)；查询日期：2026-07-24。

**[变化/不确定]**

- 2026-07-24 的公开探针显示即使传 `limit=3`，服务仍返回 `limit=500`；客户端要按响应游标推进。
- order-scoring 页面把 `order_id` 列为必填查询参数，但部分代码示例没有传它；应以接口 schema 和真实认证探针为准。
- 无鉴权访问单笔/批量 scoring 返回 401，印证其需要 L2 凭证；而 `/rebates/current` 在无结果时实测返回 JSON `null`，不同于文档数组 schema。
- 奖励配置现网出现 pUSD 与 USDC.e 两种 `asset_address`，还可能带 `sponsored_daily_rate`、`native_daily_rate`、`total_daily_rate`；必须按资产地址和 asset rate 分账，不能盲目相加。
- “当前配置满足条件”不等于最终收到固定奖励；最终份额取决于同市场其他 maker、随机采样和日终归一化。

**[需真实探针]** 订单评分、账户实时份额、最低在线时长、奖励发放到账与 maker fill 归属均需使用测试账户在真实订单上对账。

### 6.4 奖励必须维护三套账

**[已确认/工程约束]** 三个概念不能混为一个“奖励收入”字段：

1. **理论账**：公开市场配置与官方公式给出的条件性分数/上界；
2. **Scoring 账**：L2 认证后，对自己某张当前挂单返回的即时 `true/false` 和账户百分比；
3. **Payout 账**：日终 API 结果、奖励资产地址与钱包实际到账。

公开 orderbook 和公开奖励配置没有账户 maker identity，也不能提供某账户的 order-scoring 或最终 payout。链上 `OrderFilled` 可复核成交的 maker、数量和 fee，但不能重建未成交挂单的分钟采样、队列位置、`Q_normal/Q_final` 或后台奖励归一化。因此公开历史数据最多支持理论情景，不能把理论分数直接计入已实现 PnL。

链上成交事件依据：[`ITrading.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/interfaces/ITrading.sol#L18-L40)、[`Events.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Events.sol#L45-L85)。

## 7. WebSocket 与 RTDS

### 7.1 CLOB Market WebSocket

**[已确认]**

- 地址：`wss://ws-subscriptions-clob.polymarket.com/ws/market`
- 初始订阅：

```json
{"assets_ids":["<token_id>"],"type":"market"}
```

- 动态订阅/取消：

```json
{"operation":"subscribe","assets_ids":["<token_id>"]}
{"operation":"unsubscribe","assets_ids":["<token_id>"]}
```

- raw wire 使用 `event_type` 和 snake_case，不是统一 SDK 的 `{topic,type,payload}` 归一化对象；核心事件为 `book`、`price_change`、`last_trade_price`、`tick_size_change`。
- `best_bid_ask`、`new_market`、`market_resolved` 需要启用 `custom_feature_enabled: true`。
- 客户端每 10 秒发送文本帧 `PING`，服务端返回 `PONG`。

来源：[Market WebSocket](https://docs.polymarket.com/api-reference/wss/market)；查询日期：2026-07-24。

**[已确认]** `book` 是按价格聚合的深度，包含时间戳和 hash，但官方 payload 没有公开逐单 queue identity，也没有文档化的全局单调 sequence/replay cursor。因此公共 market 流不能单独证明：

- 自己排在同价位的第几位；
- 哪次撤单来自哪张第三方订单；
- 断线期间是否漏掉了增量；
- 历史成交究竟是 maker 还是 taker。

**[需真实探针]** 生产消费者必须验证断线重连、重复消息、乱序和 hash 变化；稳妥实现应在重连后用 REST `/book` 重新取快照，再恢复增量并检测异常。这是基于官方 payload 缺少 sequence/replay 的工程推论，不是官方保证。

**[变化/不确定]** 2025-05-28 changelog 曾记录 `initial_dump` 可选且默认 `true`，并取消 100-token 上限；当前主示例已不再展示 `initial_dump`。手写客户端应通过现网探针确认该参数是否仍受支持。

### 7.2 CLOB User WebSocket

**[已确认]**

- 地址：`wss://ws-subscriptions-clob.polymarket.com/ws/user`
- 初始包包含 `type:"user"`、condition ID 形式的 `markets` 列表和 `auth {apiKey,secret,passphrase}`；这与 market channel 使用 outcome token IDs 不同；
- 可动态订阅/取消 condition IDs；
- raw wire 同样以 `event_type:"order"|"trade"` 和 snake_case 表示；trade 可带状态、`transaction_hash`、`maker_orders`、`trader_side` 等；
- 同样每 10 秒发送文本 `PING`。

来源：[User WebSocket](https://docs.polymarket.com/api-reference/wss/user)；查询日期：2026-07-24。

由于初始认证包含原始 API secret/passphrase，只能通过 TLS 连接，且必须在日志、指标、错误上报和抓包中做严格脱敏。

**[已确认]** 官方用户实时更新页明确说明断线期间的事件不会回放，WebSocket 不能替代 authoritative reads。重连后必须先拉 open orders 与 recent trades 做恢复，再接新流。[Realtime order updates](https://docs.polymarket.com/trading/realtime-order-updates)

### 7.3 RTDS

**[已确认]**

- 地址：`wss://ws-live-data.polymarket.com`
- 每 5 秒发送文本 `PING`；
- 订阅格式为：

```json
{
  "action": "subscribe",
  "subscriptions": [
    {"topic": "<topic>", "type": "<type>", "filters": "<optional>"}
  ]
}
```

- 当前 raw topics 包括 `crypto_prices`、`crypto_prices_chainlink`、`equity_prices` 和 `comments`；
- 价格 topics 无需认证；
- RTDS 不是 CLOB 聚合订单簿或用户成交的替代品。

来源：[Real-Time Data Socket](https://docs.polymarket.com/market-data/realtime-data)、[官方 real-time-data-client](https://github.com/Polymarket/real-time-data-client)；查询日期：2026-07-24。

**[变化/不确定]**

- 2026-01-16 changelog 明确移除了 legacy CLOB references 与 `clob_auth`；当前 RTDS 页面后来又增加 equity feeds。旧 `activity/trades`、RTDS CLOB orderbook 和 `clob_user` 不应作为稳定能力。
- 旧官方客户端模型仍有 `gamma_auth`，但当前合并文档不再说明任何受保护 topic 的认证条件；只能视为 legacy/待探针，不应作为实现前提。
- 旧官方 client 发送小写 `ping` 且采用无退避重连，当前文档明确要求大写文本 `PING`。应以当前文档为准，自行实现指数退避、jitter 和重订阅。

## 8. Split、Merge、Redeem 与 Neg-Risk

### 8.1 标准二元市场

**[已确认]**

- Outcome token 是 CTF ERC-1155 仓位；
- `1 pUSD` 可以 split 为完整的 `1 YES + 1 NO`；
- 等量 `YES + NO` 可以 merge 回 `1 pUSD`；
- 市场结算后，winning outcome token 可 redeem；
- pUSD 与所有金额均按 6 位小数 base units 传入。

来源：[How positions work](https://docs.polymarket.com/trading/positions/how-positions-work)、[Manage positions](https://docs.polymarket.com/trading/positions/manage)；查询日期：2026-07-24。

当前 pUSD adapter 的主要调用形态：

```text
splitPosition(collateralToken, parentCollectionId, conditionId, [1,2], amount)
mergePositions(collateralToken, parentCollectionId, conditionId, [1,2], amount)
redeemPositions(collateralToken, parentCollectionId, conditionId, [1,2])
```

`mergePositions` 需要两侧等量仓位；`redeemPositions` 没有到期截止，按钱包可赎回余额执行。Deposit Wallet 等合约钱包应走官方 relayer，并等待 `STATE_CONFIRMED`，不能只按交易已提交记账。

官方适配器代码显示，标准 adapter 会在 pUSD 与旧 CTF 使用的抵押品之间 unwrap/wrap：

- [`CtfCollateralAdapter.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/adapters/CtfCollateralAdapter.sol)

### 8.2 标准 Neg-Risk

**[已确认]** Neg-Risk 事件假定所有 outcome 中恰有一个为真。持有某 outcome 的 `1 NO`，可以转换为其余 outcome 各 `1 YES`，扣除该 event 的转换费。该机制使多结果事件中的互补仓位可以在链上重组。

来源：[Negative Risk](https://docs.polymarket.com/concepts/negative-risk)；查询日期：2026-07-24。

当前 pUSD adapter 调用底层 Neg-Risk adapter：

```text
convertPositions(marketId, indexSet, amount)
```

官方代码读取 event 的 `questionCount` 和 `feeBips`，输出金额为：

```text
amountOut = amount - amount × feeBips / 10_000
```

其中 bitmask 选择作为输入的 NO outcome，补集成为输出 YES outcome；转换后收到的旧抵押品再包装为 pUSD。

来源：[`NegRiskCtfCollateralAdapter.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/adapters/NegRiskCtfCollateralAdapter.sol)；查询日期：2026-07-24。

**[需真实探针]** Gamma 中 `negRiskFeeBips` 可能为空；官方适配器实际从链上 `getFeeBips` 读取。回测和执行前必须查询当前 event 的链上 fee，不能把 `null` 当作 0。

**[已确认/工程约束]** `indexSet` 是链上 condition/question 顺序对应的 bitmask。Gamma 的 `outcomes`、market 数组顺序或 UI 展示顺序都不是可安全假定的链上映射。执行 `convertPositions` 前必须用 event 的链上 question count、question/condition 标识和 adapter 映射验证每一 bit 对应的 outcome；顺序不一致应拒绝执行。

### 8.3 Augmented Neg-Risk

**[已确认]**

- 标准 Neg-Risk 在创建时已知完整 outcome 集合；
- Augmented Neg-Risk 可以先创建命名 outcome、未激活的占位 outcome 和显式 `Other`；
- 占位 outcome 可通过官方链上 bulletin board 后续澄清；
- 只有已命名并激活的 outcome 应进入可交易集合；
- `Other` 的定义会随新 outcome 澄清而收窄。

来源：[Negative Risk](https://docs.polymarket.com/concepts/negative-risk)；查询日期：2026-07-24。

2026-07-24 的 Gamma 公开响应可见：

- event 级 `negRisk=true`、`enableNegRisk=true`；
- augmented event 额外出现 `negRiskAugmented=true`；
- `Other` 市场可出现 `negRiskOther=true`；
- 未命名/占位市场可处于 inactive。

**[变化/不确定]** Gamma 的公开 schema 文档尚未完整列出 `negRiskAugmented`，但实时官方响应已有该字段。解析器应保留未知字段，并同时检查 event 和 market 的 `active/closed/acceptingOrders`，不能只按 outcome 文本筛选。

标准与 augmented Neg-Risk 都使用 Neg-Risk exchange/adapter；augmented 是事件元数据和 outcome 生命周期的区别，不是另一套订单签名类型。

### 8.4 原子性边界

**[已确认]**

- 单次链上 split、merge、redeem、convert 是一个交易的原子操作；
- 但从订单簿逐腿买入多个 outcome 并不因此原子；
- 批量 REST 下单也不提供组合篮子原子性。

因此任何“总成本小于 1”的跨 outcome 策略，都必须模拟逐腿成交、腿间价格漂移、失败重试、gas、转换费、pUSD 余额和链上确认延迟。只按最终盘口快照假定所有腿同时成交，会系统性高估套利。

## 9. Gamma、CLOB 市场映射与结算

### 9.1 Gamma 事件与市场

**[已确认]**

- 事件列表：[`GET /events`](https://gamma-api.polymarket.com/events)
- 市场列表：[`GET /markets`](https://gamma-api.polymarket.com/markets)
- Keyset 事件：[`GET /events/keyset`](https://gamma-api.polymarket.com/events/keyset)，当前最大 `limit=500`
- Keyset 市场：[`GET /markets/keyset`](https://gamma-api.polymarket.com/markets/keyset)，当前最大 `limit=100`
- 可按 id/slug 取得单个 event/market；
- market 常用字段包括 `conditionId`、`questionID`、`clobTokenIds`、`outcomes`、`active`、`closed`、`acceptingOrders`、`feeSchedule`、`negRisk`、`resolvedBy` 和 UMA resolution status；
- `clobTokenIds`、`outcomes`、`outcomePrices` 等字段在部分实时响应中是“JSON 编码后的字符串”，需要兼容字符串与原生数组两种形态。

来源：[API introduction](https://docs.polymarket.com/getting-started/api)、[Predictions changelog](https://docs.polymarket.com/changelog/predictions)；查询日期：2026-07-24。

**[已确认/变化历史]**

- 官方于 2026-04-10 增加 keyset endpoints，并保留 offset endpoints，但表示 offset 将来会弃用；
- changelog 于 2026-05-14 把 `/markets/keyset` 最大 limit 从旧值降到 100；旧报告或缓存中的 500 已过时；
- `/events/keyset` 当前仍允许最大 500；
- 实现应使用 opaque `next_cursor`，把它原样作为下次 `after_cursor`；不要从 cursor 推导 offset。keyset endpoint 会拒绝 offset。

### 9.2 Gamma ↔ CLOB 标识映射

**[已确认]**

建议以以下链路建立不可歧义的市场主键：

```text
Gamma event
  └─ Gamma market.conditionId
       ├─ Gamma market.questionID
       ├─ Gamma market.clobTokenIds[]
       ├─ CLOB /clob-markets/{condition_id}
       └─ CLOB /markets-by-token/{token_id}
```

- [`GET /clob-markets/{condition_id}`](https://docs.polymarket.com/api-reference/markets/get-clob-market-info) 返回 token、最小订单量 `mos`、tick `mts`、maker/taker base fee 字段、是否接受订单及 `fd` 等紧凑约束；
- [`GET /markets-by-token/{token_id}`](https://docs.polymarket.com/api-reference/markets/get-market-by-token) 可由 token 反查 condition 和市场标识；
- REST [`GET /book?token_id=...`](https://clob.polymarket.com/book) 与 market WebSocket 使用的是 token ID，不是 Gamma market id。

**[变化/不确定]** 文档将最小挂单年龄示为顶层 `oas`；2026-07-24 实时响应却出现 `r.moas`。生产解析器应同时支持，并把未识别紧凑字段原样保存。

2026-07-24 的现网紧凑结构还直接观察到 `r.mi/r.ma/r.e/r.moas` 与 `fd.r/fd.e/fd.to`。这些是市场级参数，不能从类别默认值推导；`r.moas` 样本至少出现 4 秒和 30 秒。

### 9.3 Resolution 与最终结算

**[已确认]** 官方 resolution 流程使用 UMA Optimistic Oracle：

1. 市场规则中的 resolution source 和 edge cases 定义判定标准；
2. 任何人可提出结果，通常需约 750 pUSD bond；
3. 一般有 2 小时 challenge window；
4. 被争议时可进入再次提议/争议，最终由 UMA DVM 裁决；
5. 争议结算常需约 4–6 天；
6. 罕见 `Unknown/50-50` 结果会让两侧 token 各按 0.50 结算；
7. 结算后停止交易，winning token 通过 adapter/CTF 赎回。

来源：[Market resolution](https://docs.polymarket.com/concepts/resolution)；查询日期：2026-07-24。

市场标题不是结算规则。数据与执行系统至少要保存：

- 完整 question、description、rules、resolution source、end date；
- `conditionId`、`questionID`、`resolvedBy`；
- Gamma `closed` 和 UMA 状态；
- market WebSocket `market_resolved` 的 winning asset/outcome；
- CTF `ConditionResolution` 事件、payout numerators；
- adapter/relayer 的最终 confirmed 交易。

Gamma 与 WebSocket 是及时索引层，链上 CTF payout 与确认交易才是资金结算证据。

### 9.4 跨 API 结算传播窗口

**[已确认]** 2026-07-24 的官方公开接口只读探针观察到一个刚结算约两分钟的 15 分钟市场：

- Gamma 已显示 `closed=true`、`acceptingOrders=false`、UMA 状态 resolved 和终盘 `[0,1]`；
- 同一 condition 的 CLOB market 仍短暂显示 `closed=false`、`accepting_orders=true`、token `winner=false`；
- 更早结算的市场随后已在两端收敛。

这证明 Gamma 与 CLOB 之间存在真实传播窗口。实现必须使用幂等状态机和延迟重试：`market_resolved` WebSocket 可以触发检查，但不能单独作为最终账本。至少应等待 CLOB `closed=true && accepting_orders=false`，并让 `tokens[].winner` / `is_50_50_outcome` 与 Gamma 终态相容；最终可赎回性仍以 Polygon CTF/UMA 链上状态为准。

### 9.5 UMA 地址文档冲突

**[变化/不确定]** 官方资料内部存在需明确保留的冲突：

- [Contract addresses](https://docs.polymarket.com/resources/contracts) 把 `0x6A9D...F74` 标为 UMA Adapter，把 `0xCB18...130` 标为 UMA Optimistic Oracle；
- [Market resolution](https://docs.polymarket.com/concepts/resolution) 的版本列表又把若干地址标作不同版本的 `UmaCtfAdapter`，其中包括上述 oracle 地址。

在官方修正文档前，不能只凭静态页面硬编码 adapter。应同时使用：

1. 当前市场 Gamma `resolvedBy`；
2. 官方合约地址页；
3. 对应地址的链上 bytecode/ABI 和真实事件；
4. 该市场 questionID/conditionId 的链上记录。

本轮公开 Gamma 样本的 `resolvedBy` 为 `0x6A9D...F74`，这只是样本验证，不足以代表全部历史市场。

## 10. 可直接进入实现的约束

### 10.1 已确认，可编码

- `chainId=137`、CLOB V2 order domain 与当前交易所地址；
- V2 order typed-data 字段及签名类型 0–3；
- L1/L2 认证结构；
- pUSD 6 位小数与当前 adapter 地址；
- Gamma condition/token 映射；
- 市场级费用与奖励配置必须按快照保存；
- FAK/FOK 的 `tradeIDs → trade status → tx hash/FAILED` 异步状态机；
- CLOB market/user WS 与 RTDS 必须分开消费；
- standard/Neg-Risk 的 split/merge/redeem/convert 路由；
- 链上确认才是最终资金状态。

### 10.2 必须做兼容解析

- Gamma 数组字段：原生数组或 JSON 字符串；
- keyset limit：markets 最大 100、events 最大 500，并服从实际响应；
- CLOB 最小挂单年龄：兼容 `oas` 与 `r.moas`；
- 未知紧凑字段与新增 Gamma flags：原样保存；
- REST 下单响应：兼容 `tradeIDs` 与历史 `transactionHashes/transactionsHashes` 表达，但以 trade 状态为准；
- Deposit Wallet 地址与钱包世代：从官方数据/SDK解析，不本地硬推；
- UMA adapter：按市场与链上事实选择。

### 10.3 上实盘前必须完成的真实探针

| 探针 | 最低验收条件 |
|---|---|
| L1/L2 认证 | 创建/派生 key、时钟偏差、签名失败码、轮换/撤销均有脱敏日志 |
| V2 下单 | GTC/GTD/FOK/FAK/post-only 各做最小额；记录请求、order ID、trade ID、终态和链上哈希 |
| 异步提交 | HTTP 成功后能轮询到 `CONFIRMED` 或 `FAILED`，重启后可继续恢复 |
| Market WS | 断网、重连、重复、乱序、REST resnapshot 后 hash/盘口可收敛 |
| User WS | 私有订单与 trade 能同 REST 和链上交易逐条对账 |
| Order scoring | 传必填 `order_id`，验证 size/spread/duration 边界和状态变化 |
| Rewards | 实际 maker fill、实时百分比、日终 pUSD 到账与官方公式可复算 |
| Fee | 买/卖、不同价格、最小收费与 5 位小数舍入可复算 |
| Split/Merge/Redeem | standard 与 Neg-Risk 各验证 amount、allowance、gas、relayer `STATE_CONFIRMED` |
| Neg-Risk conversion | 链上 `getFeeBips` 与实际 `amountOut` 一致；多腿失败边界明确 |
| Resolution | Gamma、WS、CTF payout、adapter redeem 与钱包到账形成闭环 |

在这些探针完成前，任何以 maker rebate、liquidity rewards、Neg-Risk 转换或多腿撮合为主要利润来源的策略都只能保留为研究候选。

## 11. 对盈利研究的直接含义

1. **净边际必须逐市场计算。** 使用费率快照、真实 taker/maker 归属、pUSD 舍入、gas、失败重试与奖励的保守下界，不使用“全局零费率”假设。
2. **Maker rebate 是竞争池分成，不是固定返现。** 回测只能在缺少全市场 maker fee-equivalent 时给出上界/区间，不能当作确定 PnL。
3. **Liquidity Rewards 是采样和归一化后的相对分数。** 仅知道市场日奖励额、max spread、min size 仍不足以重建实际收益。
4. **公共盘口不能重建队列。** 若没有真实账户用户流与订单级日志，历史回放不能可靠推断 maker fill probability。
5. **批量下单不消除腿风险。** 组合策略需模拟部分成交、延迟、不可撤窗口与异步链上失败。
6. **Neg-Risk 的会计恒等式不等于可成交套利。** 必须把逐腿执行、转换费、inactive/Other outcome 变化和链上确认纳入。
7. **Resolution 是策略风险的一部分。** 标题相似不代表规则相同，必须对规则、澄清、oracle 状态和链上 payout 做版本化。

## 12. 官方来源索引

以下页面与仓库均于 2026-07-24 查询：

### CLOB、认证与订单

- [CLOB V2 migration](https://docs.polymarket.com/v2-migration)
- [SDK migration](https://docs.polymarket.com/getting-started/migrate-from-previous-sdks)
- [API introduction and authentication](https://docs.polymarket.com/getting-started/api)
- [Wallets & Authentication](https://docs.polymarket.com/trading/wallets-auth)
- [Prices & Orderbook](https://docs.polymarket.com/concepts/prices-orderbook)
- [Place orders](https://docs.polymarket.com/trading/place-orders)
- [Order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)
- [Predictions changelog](https://docs.polymarket.com/changelog/predictions)
- [Official CTF Exchange V2 repository](https://github.com/Polymarket/ctf-exchange-v2)
- [Pinned V2 `Structs.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/libraries/Structs.sol)
- [Pinned V2 `Signatures.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Signatures.sol)
- [Pinned V2 `Hashing.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Hashing.sol)
- [Official TypeScript SDK](https://github.com/Polymarket/ts-sdk)
- [Official Python SDK](https://github.com/Polymarket/py-sdk)

### pUSD、费用、奖励

- [pUSD](https://docs.polymarket.com/concepts/pusd)
- [Official contract addresses](https://docs.polymarket.com/resources/contracts)
- [Trading fees](https://docs.polymarket.com/trading/fees)
- [Market details](https://docs.polymarket.com/market-data/market-details)
- [Maker Rebates](https://docs.polymarket.com/programs/maker-rebates)
- [Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards)
- [Current active rewards configurations](https://docs.polymarket.com/api-reference/rewards/get-current-active-rewards-configurations)
- [Order scoring status](https://docs.polymarket.com/api-reference/trade/get-order-scoring-status)

### 实时流、市场与结算

- [Market WebSocket](https://docs.polymarket.com/api-reference/wss/market)
- [WebSocket overview and dynamic subscription](https://docs.polymarket.com/market-data/websocket/overview)
- [User WebSocket](https://docs.polymarket.com/api-reference/wss/user)
- [Realtime order updates](https://docs.polymarket.com/trading/realtime-order-updates)
- [RTDS](https://docs.polymarket.com/market-data/realtime-data)
- [Official RTDS client](https://github.com/Polymarket/real-time-data-client)
- [CLOB public server time](https://docs.polymarket.com/api-reference/data/get-server-time)
- [CLOB market info by condition ID](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [CLOB market by token ID](https://docs.polymarket.com/api-reference/markets/get-market-by-token)
- [Market resolution](https://docs.polymarket.com/concepts/resolution)

### 仓位与 Neg-Risk

- [How positions work](https://docs.polymarket.com/trading/positions/how-positions-work)
- [Manage positions](https://docs.polymarket.com/trading/positions/manage)
- [Negative Risk](https://docs.polymarket.com/concepts/negative-risk)
- [Pinned `CtfCollateralAdapter.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/adapters/CtfCollateralAdapter.sol)
- [Pinned `NegRiskCtfCollateralAdapter.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/adapters/NegRiskCtfCollateralAdapter.sol)

## 13. 天气概率实验的数据源边界

以下来源均于 2026-07-24 查询。市场规则中的 Weather Underground
页面是具体事件指定的结算来源；预测模型与站点元数据只能用于构造事前
概率，不能替代市场自己的结算规则。

- [Open-Meteo Single Runs API](https://open-meteo.com/en/docs/single-runs-api)：
  支持用 `run` 选择明确的历史模型初始化批次。实验同时保存初始化时间、
  抓取时间、原始响应和校验值，并在决策截止前加入发布延迟与安全延迟；
  未知或事后才可获得的 vintage 一律排除。
- [Open-Meteo Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api)
  与 [Previous Runs API](https://open-meteo.com/en/docs/previous-runs-api)：
  历史预报整合产品和相对当前时间的 previous-run 产品不能自动视为某个
  决策时点真实可见的 exact vintage；严格回测优先使用 Single Runs。
- [AviationWeather Data API](https://aviationweather.gov/data/api/)：
  只读 station-info 用于把规则中的 ICAO 站点映射到坐标。客户端遵守官方
  速率限制并缓存原始响应；坐标映射不改变规则中的站点身份。
- [Polymarket CLOB price history](https://docs.polymarket.com/api-reference/markets/get-prices-history)：
  `GET /prices-history` 提供 token 的时间价格序列，但不包含完整 L2 队列、
  maker 身份或可证明成交。因此它在本项目只属于 L1 决策价格证据，不能
  把价格触及直接算成 maker fill。

天气事件解析还保存 Gamma 事件的完整规则、站点链接、时区、温度单位、
分箱边界和最终 outcome。若规则文本、指定站点、尾部分箱或最终状态无法
无歧义解析，该事件会进入排除清单，而不是靠标题推断。
