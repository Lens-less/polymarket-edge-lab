# Edge Discovery Lab 数据字典与 Provenance 合同

> 合同版本：`edge-data-contract.v1`  
> 制定日期：2026-07-24  
> 适用范围：公开、只读的 Polymarket / 天气 / 标的价格研究数据  
> 安全边界：不记录私钥、API secret、passphrase、签名原文或用户 WebSocket 认证包；不提交订单或链上交易。

## 1. 数据分层

| 层 | 目录 | 可变性 | 用途 |
|---|---|---|---|
| Raw | `data/edge_lab/raw/<source>/` | 批次 finalize 后不可覆盖 | 保存服务器原始响应或原始 WebSocket payload 及接收元数据 |
| Checkpoint | `data/edge_lab/checkpoints/` | 可原子更新 | recorder 重启提示；不是研究证据 |
| Normalized | `data/edge_lab/derived/normalized/` | 由 manifest/version 唯一决定 | Decimal 化、主键映射、时间标准化、规则解析 |
| Features | `data/edge_lab/derived/features/` | 由代码/参数版本决定 | forecast vintage、约束图、队列、奖励和 lead-lag 特征 |
| Experiments | `research/edge_discovery_2026-07-24/` | 追加或新文件 | 实验注册表、结果、成交明细、报告 |

Raw 批次不得就地修正。解析错误、schema drift 或上游脏数据通过新的 normalized 版本处理，并保留到 raw `record_id` 的反向引用。

## 2. Raw 记录信封

每行是 canonical JSON，必含：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | string | 本地信封/源 payload 解析契约版本 |
| `source` | string | 见第 4 节的稳定 source 名 |
| `received_at` | UTC ISO-8601 | 本机收到完整响应/消息的墙钟时间 |
| `event_at` | UTC ISO-8601 或 null | 上游声明的事件时间；不得用 `received_at` 伪造 |
| `sequence` | integer 或 null | 仅在上游真实提供 sequence 时填写；CLOB market WS 当前通常为 null |
| `payload` | object | 未丢字段的原始响应/消息；未知字段原样保留 |
| `record_id` | SHA-256 hex | 整个 canonical 信封的稳定校验值 |
| `schema_fingerprint` | SHA-256 hex | payload 字段结构/类型指纹 |

Recorder 另在 payload 的本地 capture 元数据中保存：

- `connection_id` / `session_id`
- `monotonic_ns`
- HTTP URL、status、RTT、响应 headers 的安全子集
- WebSocket event type、book hash、token/condition/event ID
- `is_resnapshot`、`reconnect_count`、disconnect/error 分类
- 原始文本或其无损 JSON 表示

上游没有 sequence 时，`sequence_gap_count=0` 不代表没有漏数；断线窗口和重连后的 REST resnapshot 必须单独报告。

## 3. 批次 Manifest

每个 finalized `*.jsonl` 对应一个 `*.manifest.json`，至少包含：

- `manifest_version`、`schema_version`、`source`、`batch_id`
- raw 相对路径、finalize 时间
- attempted、written、duplicate 记录数与重复率
- received/event 时间范围及时间倒退
- sequence gap/regression（若该源有 sequence）
- schema fingerprints、drift/change 次数
- SHA-256、字节数、行数
- recorder 版本、代码 tree hash、配置快照 hash（由运行器补充）
- 启动/停止原因、断线次数、resnapshot 成功/失败

多个批次组成实验输入时，`data_manifest_hash` 是按路径排序后的所有批次 manifest canonical JSON 的 SHA-256；实验不得只引用一个易变目录名。

## 4. Source 名与 Provenance

| `source` | 权威入口 | 主键 | 关键时间/完整性说明 |
|---|---|---|---|
| `gamma_events` | `gamma-api.polymarket.com/events[/keyset]` | `event.id` | 保存 rules/description/source/updatedAt 和完整 markets；数组可能是 JSON 字符串 |
| `gamma_markets` | `gamma-api.polymarket.com/markets[/keyset]` | `conditionId` | keyset markets limit 保守为 100；cursor 不解析 |
| `clob_market_info` | `clob.polymarket.com/clob-markets/{condition}` | condition ID | 保存 compact `mos/mts/r/fd/ao` 原文及抓取时点 |
| `clob_book_rest` | `clob.polymarket.com/book` 或公开 `/books` | token ID + received_at | 完整聚合 L2；snapshot 不是逐单队列 |
| `clob_market_ws` | CLOB market WebSocket | connection + token + event/hash/time | `book`、`price_change`、`last_trade_price`、`best_bid_ask`、`tick_size_change`、`new_market`、`market_resolved` |
| `clob_price_history` | 官方 `/prices-history` | token + point time | 仅 L1 price history；不是 L2 或 maker fill |
| `reward_config` | `/rewards/markets/current` | condition + asset + fetch time | 理论配置；不等于 scoring/payout |
| `rtds_crypto` | RTDS `crypto_prices*` | symbol/source/event time | 同存 source timestamp、received_at、carry-forward 标记 |
| `rtds_equity` | RTDS `equity_prices` | symbol/source/event time | 同上 |
| `weather_station` | 市场指定结算源；机场元数据用 AviationWeather | station + effective time | 结算站必须来自市场规则，不按城市中心替代 |
| `weather_forecast_run` | Open-Meteo Single Runs / Previous Runs | model + initialization + location | 保存原始 run、模型、初始化时间、估计可用时间、lead；禁止用事后 stitched 值冒充 vintage |
| `weather_resolution` | Gamma + 市场指定 source + 最终 CTF/UMA | event + condition | Gamma/WS 是索引证据；最终 payout 是资金证据 |
| `chain_negrisk_metadata` | 官方 adapter/CTF 只读调用 | adapter + market ID + block | question count、fee bips、question/index mapping 必须固定 block |

未公开 `/orderbook-history` 统一标记 `unsupported_clob_history`，只能作 L1 补充，不能成为唯一盈利证据。

## 5. 标准主键

```text
event_key       = gamma_event_id
market_key      = condition_id
outcome_key     = condition_id + token_id
weather_key     = station_id + local_calendar_date + metric
forecast_key    = provider + model + initialization_time + location + lead
book_key        = token_id + server_event_time/hash + received_at
experiment_key  = strategy + canonical_params + data_manifest_hash
```

Gamma market 数组顺序不得作为 Neg-Risk 链上 question index。链上 index 必须由 adapter 在固定 block 的显式映射证明。

## 6. 金额、价格与数值精度

- pUSD、价格、shares、费用、返佣、PnL、概率、温度边界及订单簿 size 在 normalized/derived 层使用 `Decimal`。
- JSON/CSV 中金融数值写十进制字符串；不得转成 binary float。
- 原始 API 若返回 JSON number，raw 层保留原始文本；解析时通过 `Decimal(str(value))` 或 `json.loads(..., parse_float=Decimal)`。
- 费用逐成交价位计算：`shares × r × [p(1-p)]^e`。Raw fee `< 0.00001` 归零；边界舍入规则在真实探针前保持显式假设。
- maker fee 为零不等于奖励确定；理论、scoring、payout 分账。

## 7. 时间语义

| 字段 | 语义 |
|---|---|
| `received_at` | 本机墙钟，用于端到端延迟；需记录与 CLOB `/time` 的偏移诊断 |
| `monotonic_ns` | 本进程内排序/耗时；不能跨重启比较绝对值 |
| `event_at` | 上游事件时间；缺失时为 null |
| `forecast_initialized_at` | NWP run 的初始化时刻，不等于公众可用时刻 |
| `forecast_available_at` | 初始化时刻加模型发布延迟；模型输入必须早于交易决策 |
| `decision_at` | 策略在回放中可见全部输入的最晚时刻 |
| `activation_at` / `cancel_effective_at` | 提交/撤单延迟后的订单有效边界 |
| `resolved_at` | 分 Gamma、WS、CLOB、链上确认四层记录，不压成一个布尔值 |

训练、验证、测试按整个 event 分组；同一 event 的多个 outcome 不得跨集合。天气本地日以规则指定 station 的时区计算，不以 UTC 日期代替。

## 8. 数据等级

| 等级 | 最低数据证据 | 禁止的表述 |
|---|---|---|
| L0 | 合成/fixture/诊断 | 不得称真实回测 |
| L1 | 公开快照、price history、touch/shadow | 不得称成交或回测盈利 |
| L2 | 本项目真实前向 raw L2/WS/RTDS，manifest 完整，queue-bounded 模拟 | 不得称真实 maker 身份或 payout |
| L3 | 获明确授权的真实小额 order/scoring 探针 | 不得外推为稳定实盘 |
| L4 | 真实低风险多事件交易表现 | 仍需全部统计门槛 |

## 9. 天气字段

Normalized weather event 至少包含：

- `event_id`、`city`、`station_id`、station 坐标
- `local_date`、`timezone`
- `metric`（daily maximum/minimum 等）、`unit`
- 有序且穷尽的 `bins[{lower,upper,lower_inclusive,upper_inclusive,condition_id,yes_token}]`
- resolution source、rounding/precision、revision cutoff、异常/取消规则
- `rules_hash`、parser version、raw record references

Forecast vintage 至少包含 provider/model/run、初始化和可用时间、target station/grid cell、hourly values、local-day aggregate、lead hours、raw record reference。尾档 outcome 只提供删失区间，不伪造成精确温度。

## 10. 约束、奖励与成交字段

Constraint edge 必含语义类型、数学式、rules/source evidence、有效期、graph hash、状态 `proposed|verified|blocked`。Augmented Neg-Risk 的 placeholder 与 `Other` 默认 blocked。

Reward 记录分为：

1. `theoretical_reward`：公开配置与上下界，`recognized_pnl=0`
2. `order_scoring`：自己的认证 order evidence，仍非到账
3. `actual_payout`：日期/condition/asset/maker/到账 evidence，才可 recognized

模拟成交逐笔保存：

- order/decision/source event IDs
- token、side、price、size、queue ahead
- submit/cancel/hedge latency 与 scenario
- gross cash、fee、slippage、reward、net PnL
- inventory/collateral before/after
- partial/residual、single-leg risk、resolution path

每笔 PnL 必须能从 raw 事件、策略决策和参数完整复算。

## 11. 数据质量的 fail-closed 规则

以下任一情况阻断对应策略实验或降级证据：

- 缺 condition/token 映射、tick、min size、fee schedule 或 rules/source
- Gamma/CLOB schema 无法识别且未保留 raw
- 断线后没有 REST resnapshot
- 使用未来 forecast run、事后修订值或同 event 跨 split
- book/trade 时间倒退未处理，或时间偏移不可界定
- augmented Neg-Risk graph 变化、Other/placeholder、链上 index 未验证
- 公共聚合 L2 被当作 maker identity、订单 age 或真实 queue
- 理论 reward 被计入 realized PnL
- 缺失 hedge depth 被过滤，而不是计入不可退出的最坏情形

