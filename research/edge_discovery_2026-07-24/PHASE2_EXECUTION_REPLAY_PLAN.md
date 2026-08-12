# Phase 2：从公开 L2 到可晋级 execution replay

版本：2026-07-24  
状态：实施与离线故障验证已完成；真实首批生命周期与首个 tracer 验收中  
边界：只读公开数据和本地离线回放；不读取交易凭证，不下单，不执行链上写入。

## 阶段目标

Phase 1 已证明：继续增加 snapshot/touch 行数不会自动生成可信 PnL。Phase 2
只解决一条主链：

```text
动态完整生命周期采集
→ decision-time ghost decision
→ queue-bounded / non-atomic execution replay
→ settlement ledger
→ strict promoter
→ 非空但明确标注为 counterfactual 的 L2 指标
```

第一目标不是“尽快出现正 PnL”，而是让正、负、零和 no-trade 都能由同一套
不可绕过的证据链复算。

## 已完成的 P0 基础修复

1. 修复 `websockets` 在 `connection_made` 前断连时访问未初始化
   `recv_messages` 的 cleanup race；回归测试和在线新 session 均通过。
2. HTTP snapshot 改为逐 resource 失败隔离：成功响应保留，失败项结构化、
   脱敏并 fail-closed，不再整批丢失。
3. live data manifest 容忍已识别的 mutable partial 在审计时正常轮转消失；
   finalized inputs 消失仍立即失败。
4. 新增官方 CLOB `GET /time` 公共时钟探针，保存 request start、server time、
   response end 和原始响应哈希。
5. 新增严格短周期市场公告解析器：只接受 BTC/ETH、5m/15m、`Up/Down`、
   两个唯一 token、condition 映射和精确 Chainlink 规则完全一致的目标；
   announcement `record_id`、规则和 token 身份全部内容寻址。
6. CLOB recorder 已增加两个目标组安全 seam：可显式关闭重复 RTDS 连接，
   并在首个连接上先完成 full-book resnapshot；snapshot watermark 之前的
   WS frame 保留为 quarantine，之后的增量才可进入 replay。

在 `2026-07-24T13:28:57Z` 冻结的 finalized 文件清单中，严格解析器扫描
3,376 个 CLOB WS 文件、165,719 条记录和 1,213 条 `new_market`，接受 74
个唯一目标：BTC 5m 28、ETH 5m 28、BTC 15m 9、ETH 15m 9。74 个目标的
canonical record ID、condition、outcome/token 顺序、event slug 和精确
Chainlink URL 均为 0 conflict；两个 live partial 明确未纳入。

一个独立的 6 秒公开只读 smoke capture 已验证首连接 gate：只建立 CLOB
连接，不建立重复 RTDS；先写入 immutable full-book resnapshot，watermark
之前的一条 book 进入 quarantine，之后 price changes 才进入 data；13 条
记录、2 个 manifests 的 checksum/pairing 审计均无错误，新订单保护仍开启。

官方 `GET /time` 当前返回整数秒，只适合检测粗粒度时钟漂移，不能单独证明
`p95 <= 50ms`。延迟实验还需：

- 同一参考价格源的毫秒时钟，例如 Binance 官方
  [`GET /api/v3/time`](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#check-server-time)；
- 本机受控 NTP/系统时钟状态；
- 多次探针的 RTT、offset 区间和异常值政策。

## Workstream A：动态完整生命周期采集

主 recorder 的静态配置仍只跟踪一个 BTC 日市场和一个天气 event，单靠它
永远不能达到 100 个独立已结算市场。动态 sidecar 已补上目标滚动层：在
`2026-07-24T15:08:03Z` 的在线审计点，累计 strict registry 已从历史冻结
快照的 74 个增长到 126 个唯一
`btc/eth-updown-5m/15m-<epoch>` 市场。当前 run 中 122 个目标已通过 Gamma
身份校验，3 个未激活目标进入 `gamma_poll_deferred`，另有 1 个刚公告目标
遇到可重试的公共 HTTP 失败；没有把这些暂态失败提升为永久拒绝。

常驻服务为
`com.lens.polymarket-edge-lab-dynamic-short-crypto-capture`，每次启动写入
唯一 append-only run root。`2026-07-24T15:05:51Z` 的受控 SIGTERM 轮换中，
旧 run 以退出码 0 完成，两个 active partial 均原子封账为 JSONL/manifest；
新 PID 的 lock、LaunchAgent PID 和 run root 一致，stderr 为 0。
所有路径仍保持 `orders_submitted=0`、`authenticated_endpoints_used=0` 和
`new_orders_disabled=true`。

collector 已实现 finalized-input checksum/byte 绑定、累计 registry
确定性重建、schema fail-closed、durable start/stop decision/receipt、
首连接 resnapshot watermark、断线隔离和 close-time CLOB liveness gate。
它现在还会从主 recorder 的 finalized RTDS manifest 提取时间戳精确等于
open/close epoch 的 Chainlink 边界，只用 `full_accuracy_value / 1e18`
构造财务价格，并把 Gamma、token mapping、同连接 PONG bracket 与边界证据
作为一个原子严格结算 gate。Gamma-only 仍绝不晋级。

跨进程恢复也已改为 finalized-only reducer：新进程先重新验证旧 run 的
schema、bytes、lines、SHA-256、canonical record ID 和 checkpoint，再
durable-finalize `restart_recovery_decision`，之后才允许任何公开网络或
worker effect。partial 永不进入恢复状态，unique append-only run root
边界保持不变。这些实现和故障测试通过并不等于真实生命周期已经验收。

现有 CLOB stream 公开广播 `new_market`，市场公告约提前 23.8 小时。
官方 market WebSocket 支持动态
[`subscribe` / `unsubscribe`](https://docs.polymarket.com/market-data/websocket/overview)，
但首版不在一个正在 resync 的 worker 上原地修改 scope：同一时间窗口的
BTC/ETH token 组成独立 target-group CLOB worker，共享单一 RTDS stream。
这样每组拥有独立 connection provenance、resnapshot watermark 和故障边界。

当前实现与剩余顺序：

1. 已完成：从 immutable `new_market` raw records 构建内容寻址的累计 registry；
2. 已完成：严格校验 slug、`Up/Down` outcomes、两个唯一 token、
   condition/market 映射、时间窗口和 Gamma 身份；
3. 已完成：开始前持久化订阅决定和 receipt，再启动独立 target-group worker；
4. 已完成实现：Gamma、CLOB server time、book、decision-vintage fee 与
   主 RTDS Chainlink PTB/close boundary 均按 finalized record ID join；
5. 已完成采集侧：断线后 REST resnapshot，watermark 前数据 quarantine；
6. 已完成实现：持续追踪 Gamma 标签并 fail-closed；只有完整 Chainlink、
   token 与 close-liveness 对账才认定 winner/payout 并取消订阅；
7. 已完成实现：每个目标输出生命周期状态和明确缺失原因；跨进程 continuity
   只从 finalized journal 重建，恢复决定先于 worker effect。

首批 20 个事件的完整生命周期比例必须至少为 80%；否则暂停研究，先修
collector。任何 schema drift、断线无 resnapshot、规则或结算缺失的事件都
进入排除清单，不累计为有效样本。

### Price-to-beat

PTB 必须来自规则指定源，并在首个策略决定之前固定：

- v1 只接收 Chainlink 结算的 BTC/ETH 5m/15m 市场；window open 取 slug
  epoch，Gamma `endDate` 必须等于 open + horizon；
- PTB 只采用 inner Chainlink timestamp 精确等于 window open 的记录；价格
  一律由整数 `full_accuracy_value / 1e18` 构造。展示字段 `value` 必须是
  该精确值本身，或该精确值的 canonical binary64 序列化，不允许任意容差；
- 首个策略决定必须晚于 PTB 的本地 `received_at`；当前样本显示 Chainlink
  到达延迟约 p50 1.43 秒、p95 2.05 秒，因此开头 1–2 秒主动放弃；
- Binance 日/分钟市场留到独立 adapter：使用官方公共
  [`GET /api/v3/klines`](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#klinecandlestick-data)
  获取规则指定的精确 candle close；
- 保存请求时间、目标 candle open/close time、原始响应和 hash；
- 事后补抓只能标记为 retrospective，不能伪装为 decision-time evidence。

真实 RTDS 审计解释了一个关键数据现象：6,277 条 BTC/ETH Chainlink 记录中，
4,014 条展示值与 full-accuracy 整数逐十进制相等，另外 2,263 条全部精确等于
同一 full-accuracy 值的 canonical binary64 文本，除此以外为 0。因而不能把
上游 JSON 数字的最后一位二进制浮点序列化误判成 36% 数据丢失；也不能反过来
用模糊 epsilon 接受任意差异。财务价格始终来自整数 full-accuracy 字段。

结算标签必须和 decision features 分离：收盘后的 `closed/outcomePrices` 只在
评分阶段 join，不能进入信号构建。Gamma one-hot winner、事前 token mapping
和规则指定的 Chainlink open/close 必须一致；任一冲突则整场
`settlement_conflict`。

## Workstream B：ghost decision 与 replay ledger

现有 `MakerReplay`、逐档 taker replay 和 `Ledger` 应直接复用；不新造第二套
执行引擎。新增一个深模块，建议唯一入口为：

```python
verify_and_promote_execution_replay(run_manifest_path) -> PromotionResult
```

它必须自己读取冻结证据、重建 L2、重跑执行和账本；不能相信输入 artifact
自报的 `reconciled=true`、PnL、fill count 或 classification。

### Ghost decision

每个策略决定在真实接收时间写入不可变 CaptureStore，`decision_id` 直接使用
raw envelope 的 `record_id`。最低字段：

- experiment / strategy / config hash；
- event / condition / market / token；
- side、limit price、quantity、submit/cancel latency；
- decision timestamp 和 split=`test`；
- 使用的 L2 record IDs、last book ID、book state hash；
- 当时 visible queue、best bid/ask、tick 和 minimum size。

### Replay fill

纯公共 L2 fill 必须明确：

- `counterfactual=true`
- `actual_fill=false`
- `authenticated_fill=false`
- `evidence_kind=execution_replay_fill`
- decision/order/source public trade IDs；
- market/token/side/price/quantity/fee/time；
- queue before/consumed/after；
- public trade volume、haircut 后可归因量；
- pessimistic queue policy。

Book touch、midpoint、book 内嵌 `last_trade_price` 和撤单量都不能制造 fill。
只有具有 price、size、side 的独立成交事件或公开可验证 `OrderFilled` 才能
消耗队列。同一公开成交量在全部 ghost orders 之间只能消费一次。

### Settlement 与会计

- settlement record 必须属于同一 condition/token；
- fill 必须早于 settlement；
- fee 从相应市场的 decision-time fee evidence 重算；
- 缺费率不能默认为零费；
- 每条 fill 唯一匹配一条 Ledger buy/sell trace；
- 未结算仓位不能进入 realized PnL；
- rewards 未确认时始终为 0；
- cash、position、fee、operation cost、redeem 和 final equity 必须守恒。

## Workstream C：strict promoter

Promoter 应输出内容寻址的 `EXECUTION_REPLAY.json`，至少包含：

- frozen data / capture audit / split / strategy / fee hashes；
- decisions、fills、settlements、ledger entries；
- initial/final cash、positions、fees、operation costs、trace hash；
- `TradeEvidence`；
- actual/authenticated/explainable counts；
- 全部失败原因和不可晋级原因。

关键不变量：

1. 所有 raw IDs 均可用 canonical encoder 重算，且属于绑定的 freeze；
2. decision 不能引用未来 L2；
3. L2 prefix 连续，gap/quarantine 已闭环；
4. visible queue 由 promoter 重算；
5. 只允许固定的 pessimistic policy 晋级；
6. fill/order/cancel/book 的时间和容量约束全部满足；
7. settlement、token、payout 和 ledger trace 一一匹配；
8. 任一 proof 失败则整 run 不可晋级，不能只删除亏损或坏样本；
9. counts、PnL 和 classification 全部由 promoter 生成。

## 第一个 tracer-bullet 验收

先完成一个 binary market 的端到端负责任样例：

1. 一个 immutable ghost decision；
2. 一条独立 public trade；
3. 一条 final settlement；
4. 固定 pessimistic maker BUY replay；
5. 一条 explainable counterfactual fill；
6. 一条可复算 `TradeEvidence`；
7. `TRADES.csv` 出现一条明确标注的 L2 replay row；
8. initial capital、fee、settled PnL、return、turnover 可计算；
9. actual/authenticated fills 仍为 0；
10. classification 仍为 `insufficient_data`，原因是少于 100 events/fills。

这一步只证明证据管线贯通，不证明策略盈利。

## 研究顺序和停止规则

### 1. 短周期 BTC/ETH 延迟

达到 100 个独立已结算 OOS 市场后，固定策略在全费用、可实现延迟和悲观成交
下仍无正信号，则停止该方向。不得继续扫 lag/threshold 直到偶然为正。

### 2. Standard Neg-Risk / logic taker replay

先补 index mapping、adapter deployment/block/fee provenance。覆盖 100 个
独立事件、10,000 个同步快照后仍为 0 accepted，则暂停。只在忽略 conversion
fee、leg skew 或最坏单腿退出时为正，直接拒绝。

### 3. 天气

冻结模型版本后再扩独立日期。若新增 validation 仍不优于市场，保留
abstain，不继续参数搜索。

### 4. 奖励做市

先验证 reward=0 的交易经济性。若 pessimistic public fill 在 100 个事件后
仍为 0，或利润只存在于 optimistic/neutral，不申请 L3。

## 最终统一盈利门槛

只有同时满足以下条件，策略才可从 `insufficient_data` 晋级：

- 至少 100 个独立已结算 OOS 事件；
- 至少 100 次可解释 OOS fills；
- rewards=0 后净 PnL 为正；
- 95% cluster bootstrap 下界大于 0；
- 最大单事件利润贡献不超过 20%；
- 全费用、滑点、延迟、单腿退出和资金成本；
- 参数邻域稳定且无泄漏；
- 完整证据 hash 和 byte-identical replay。

纯 L2 回放达到门槛后仍应称为“可复算 counterfactual backtest”，不是实际
到账。maker 排队校准、真实 scoring 和 payout 最终需要另行授权的 L3
小额探针。本阶段不申请授权，实盘继续关闭。

## 2026-07-24 implementation checkpoint

当前磁盘实现已经形成以下不可绕过的入口：

- `chainlink_ptb.py` 与 `short_crypto_settlement.py`：finalized-only 精确
  open/close boundary、retrospective 标记、严格显示值与原子 settlement；
- `dynamic_short_crypto_recovery.py`：跨 run finalized journal reducer、
  crash/orphan partial 分类、幂等 state hash、generation 隔离，以及旧
  cumulative-v1 / 新 delta-v2 registry journal 的混合重放；
- `dynamic_short_crypto_service.py`：immutable ghost decision、公开
  `Data API /trades?takerOnly=true` 原始字节、悲观 latency/cost 证据、
  full-window close checkpoint、L2 closure，以及 durable recovery decision
  之后对 registry、目标状态、Gamma、Chainlink、deadline、sequence 和
  audit-only liveness 的实际恢复；
- `execution_replay_freeze.py`、`execution_replay_freeze_builder.py` 与
  `execution_replay_assembly.py`：完整 frozen inventory、角色绑定和真实
  candidate 装配；
- `execution_replay_promotion.py`：从 raw evidence 重建 queue、fee、
  settlement 与 ledger，拒绝 self-reported PnL/count/classification；
- `phase2_lifecycle_audit.py`：固定取时间顺序上的首批 20 个成熟目标，
  逐个审计 11 个阶段，坏样本和 partial 不可删除或提高完整率。

公开成交方向只接受官方 Data API 的 taker side。Market WebSocket
`last_trade_price.side` 仍只保留给 fixture invariant test，不能用于生产
晋级。同一 Data API match 使用 condition、asset、taker side、size、price、
timestamp、transaction hash、proxy wallet 与 outcome index 的复合事件键：
重复轮询只消费一次，同一 transaction 内的不同 match 保持独立容量。

恢复与增量 journal 修复后的全量验证结果为
`1079 passed, 11 skipped, 1 deselected`，`compileall` 与
`git diff --check` 同时通过。最新真实 finalized-only 生命周期报告
`1ea87575fb680905beee7799b2b719945c936ba09addc225bd2b4ba772a07cc2`
在 `as_of_ms=1784920354808` 的审计点读到 236 个已登记目标、0 个成熟目标、
12,964 个 manifests 和 665,125 条 records，正确返回
`waiting_for_mature_targets`。因此实现门槛已完成，但真实首批 20 个市场和
首个非空 counterfactual tracer 仍不得宣称完成；Goal 与分类保持 active /
`insufficient_data`。按 finalized registry 的确定性时间排序，第一目标在
北京时间 `2026-07-25T19:25:00+08:00` 才达到 close+1h，第二十个目标在
`2026-07-25T20:00:00+08:00` 达到；在此之前出现 0 mature 是预期外部时间
门槛，不可用 fixture、活动 partial 或事后标签绕过。

2026-07-24T18:37:33Z 的受控部署又验证了“恢复结果真正应用”而不只是
“恢复决定被记录”：新 run
`20260724T183548.092637Z-ddcc99049ebd45c4989afcfed4d9c502`
在任何新 worker action 前先 finalized 一条 recovery decision，重放六个
旧 run 的 14,766 条记录；六个 run 全部为 `clean_completed`，gap、
exclusion 和 worker action 均为 0。离线独立重放得到相同的
`decision_id=b326d64403f51b0584318c23319f3987034cbf2aa52441e279aef8a4dc6e32d5`
和
`state_hash=739280d19ba8cea16d471b9033efc439c58edda270893e042ffb527b756529ef`。
服务现在只对新 finalized discovery 文件构建 delta，再与累计 registry
确定性合并；新 journal 使用
`edge-lab-short-crypto-registry-revision.v2`，不再在每次 revision 重写
整个历史 inputs/targets。旧 v1 仍在验证原始 manifest、schema、canonical
record ID 和累计语义后兼容重放。
