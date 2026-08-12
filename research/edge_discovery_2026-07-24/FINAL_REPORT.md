# Polymarket Edge Discovery Lab 最终报告

报告日期：2026-07-24  
研究目录：`research/edge_discovery_2026-07-24/`

## 直接结论

**有没有经过严格认定的回测盈利？没有。**

**没有找到经过验证的盈利策略。**

`validated_profitable` 策略数量为 **0**。唯一进入
`promising_not_validated` 的方向是选择性奖励做市；它没有真实订单
scoring、奖励到账或实际成交，不能称为回测盈利。其余规定方向为
`insufficient_data`，旧对称做市基准为 `rejected`。

**不建议实盘交易。** 新订单硬保护保持开启；本项目没有读取交易凭证、
提交订单、签署链上交易、split、merge、redeem 或转移资产。

## 严格分类

| 策略方向 | 最终分类 | 核心原因 |
|---|---|---|
| validated_profitable | 无 | 没有策略同时满足真实数据、严格样本外、悲观成交、rewards=0、95% CI 下界大于 0、至少 100 个独立已结算事件和 100 次可解释成交 |
| 选择性奖励做市 | `promising_not_validated` | 8,964 个唯一奖励市场中评估 25 个；22 个仅在假设成交的条件情景中通过筛选，真实 scoring、payout、actual fill 均为 0 |
| 天气概率优势 | `insufficient_data` | 市场概率在样本外优于天气模型；正式策略在 validation/test 都选择 abstain，0 个候选、0 次成交 |
| 跨市场逻辑约束 | `insufficient_data` | 589 个逻辑候选全部因盘口 Neg-Risk 属性不匹配而 fail-closed，0 accepted |
| Standard Neg-Risk | `insufficient_data` | 590 个候选全部缺少可验证 index mapping / adapter provenance；另有深度和 conversion 阻断，0 accepted |
| Augmented Neg-Risk | `insufficient_data` | 88 个候选全部因 placeholder/规则来源/盘口/同步证据不完整而 fail-closed，0 accepted |
| 标的价格延迟 | `insufficient_data` | 只有一个未结算市场、8.63 分钟目标重叠；缺 clock probe、decision-vintage price-to-beat 和 settlement，0 fill |
| 旧对称做市 | `rejected` | 证据仅为 L0 合成路径或 L1 touch/snapshot；旧模型存在强制成交、队列缺失和裸空头，不符合当前成交认定 |

## 样本、区间和成交

“无 split”不是把快照当成测试集，而是明确表示该实验没有可用于盈利认定的
泄漏安全 train/validation/test 切分。

| 策略 | 样本内 / validation / 样本外 | 市场与事件 | 成交 | 数据等级 |
|---|---|---|---:|---|
| 旧对称做市 | 无合格 split；历史 L0/L1 证据审计 | 8 组旧证据；其中 3 组 L0、5 组 L1 | actual/explainable `0`；另有 499 次合成 fill，仅属 L0 | mixed L0-L1 |
| 天气概率优势 | train `2026-06-16..07-07`：58 events / 22 dates；validation `07-08..07-14`：21 / 7；test `07-15..07-21`：21 / 7 | 2,000 discovered；100 eligible；1,100 outcome rows；test 21 events / 231 outcomes / 9 cities | actual/explainable `0`；正式 validation/test 候选均 `0` | L1 |
| 跨市场逻辑约束 | 无 split；live snapshot `2026-07-24T11:12:49.435Z` | event `741266`；11 markets / 22 books；589 logic candidates | accepted `0`，fills `0` | L1 |
| Standard Neg-Risk | 无 split；同一 live snapshot | event `741266`；11 markets / 22 books；590 candidates | accepted `0`，fills `0` | L1 |
| Augmented Neg-Risk | 无 split；live snapshot `2026-07-24T11:13:29.175Z` | event `32224`；9 expected markets / 4 acquired books；88 candidates | accepted `0`，fills `0` | L1 |
| 选择性奖励做市 | 无 split；public snapshot `2026-07-24T11:26:36.637Z..11:27:10.996Z` | 8,966 rows；8,964 unique conditions；8,954 pUSD-scope；25 evaluated | actual/explainable `0`；scoring epochs `0`；payout epochs `0` | L1 |
| 标的价格延迟 | 无训练或验证 split；描述区间 `2026-07-24T10:27:00.096Z..10:35:39.276Z` | 1 market；519 source observations；7,860 quote observations；0 settled markets | actual/simulated `0` | L2 capture，非回测 |

## 严格财务指标

这里的 `null` 是有意的：没有合格成交账本时，不能用“0 收益率”伪装成一场
已经发生的回测。`fees=0` 和 `rewards=0` 仅表示本次只读实验没有实际订单费用
或确认奖励，不表示策略交易成本为零。

| 策略 | 初始资金 | 净 PnL | 收益率 | 手续费 | 已确认奖励 | 最大回撤 | 95% 净 PnL CI |
|---|---:|---:|---:|---:|---:|---:|---:|
| 旧对称做市 | `null` | `null` | `null` | `null` | `null` | `null` | `null` |
| 天气概率优势 | `null` | `null` | `null` | `0` | `0` | `null` | `null` |
| 跨市场逻辑约束 | `null` | `null` | `null` | `0` | `0` | `null` | `null` |
| Standard Neg-Risk | `null` | `null` | `null` | `0` | `0` | `null` | `null` |
| Augmented Neg-Risk | `null` | `null` | `null` | `0` | `0` | `null` | `null` |
| 选择性奖励做市 | `null` | `null`；recognized total `0` | `null` | `0` | `0` | `null` | `null` |
| 标的价格延迟 | `null` | `null` | `null` | `0` | `0` | `null` | `null` |

旧策略另有一个**排除在严格指标之外**的 L0 合成诊断：

- 初始资金 `1000`
- 合成 reconciled PnL `-32.56958870063799670`
- 合成 return `-0.0325695887006379967`
- 合成 max drawdown `0.0472630935224204686`
- 合成 fills `499`
- 裸空头事件 `330`

这组负结果用于证明旧引擎不具备盈利证据，不是可发布的真实回测。

所有策略的最赚钱单一市场贡献比例均为 `null`：没有正的、已实现且可归因的
event-level PnL。因而也不存在“最大贡献市场”或由少数机会贡献的真实利润。
容量、周转率、Sharpe、Sortino、hit rate、one-leg CVaR、forecast alpha、
maker spread 和 adverse selection 的严格值同样为 `null`，具体缺失原因逐项
记录在 [BACKTEST_RESULTS.json](BACKTEST_RESULTS.json) 和
[METRICS.md](METRICS.md)。

## 天气概率实验

最终天气证据来自 100 个合格且已结算的事件，其中严格 test split 为 21 个
events、7 个独立 local-date groups。开放尾档赢家被排除，所以结论只适用于
“非尾档赢家”范围，不能外推到全部天气市场。

样本外概率质量：

| 模型 | Test Brier | Test Log Loss |
|---|---:|---:|
| 市场隐含概率 | `0.7828763704424307` | `1.7031907165033758` |
| 天气 forecast | `0.9308816433843976` | `2.5999068729120045` |
| train-only climatology | `0.9358923624851603` | `2.4324721206702945` |
| 无偏正态基线 | `0.9308816433843976` | `2.5999068729120045` |

市场在 Brier 和 Log Loss 上都胜过天气模型。validation 上所有阈值目标均不为
正；诊断最优阈值 `0.06` 的等日期加权 shadow 均值为
`-0.009587619047619048`，95% cluster bootstrap CI 为
`[-0.06274810714285714, 0.07946757142857143]`，且参数邻域不稳定。

正式策略因此为 `abstain/no_trade`。只作诊断的 taker counterfactual 为：

- validation：17 events / 7 groups，shadow sum `-0.20134`
- test：16 events / 7 groups，shadow sum `-0.06722`
- maker counterfactual：`null`，因为 L1 没有队列或 fill evidence

这些数字是 L1 shadow，不是交易 PnL。rewards 假设固定为 `0`，仍没有正优势。

## 逻辑约束与 Neg-Risk

最终 Standard/logic live run：

- event `741266`
- 11 markets、22 books、2 analyses
- standard：590 candidates / 0 accepted
- logic：589 candidates / 0 accepted
- 逻辑候选全部 `book_neg_risk_mismatch`
- standard 全部缺 index mapping 与已验证 adapter provenance；另有 32 个
  depth failures 和 1 个 conversion-output failure
- 45 个官方公共 GET 响应；证据 receipt window `2,606ms`

最终 Augmented Neg-Risk live run：

- event `32224`
- 9 expected markets、4 acquired books
- 88 candidates / 0 accepted
- 88 个候选全部因 augmented 状态、缺 book/compact market、未审 claim、
  缺失 resolution source 或 stale/unsynced books 而阻断
- 23 个官方公共 GET 响应；证据 receipt window `399ms`

多腿机会按非原子执行处理，逐档检查 depth、当前 fee、tick/min order、
stale/skew、保守 rounding 和单腿退出。缺任一语义、映射、规则或盘口证据即
fail-closed；没有使用 midpoint 制造套利。两组 live 均有零网络 replay，
核心 artifacts 逐字节相同。这里的 1,267 个候选是“被检查并阻断的快照”，
不是交易或盈利机会。

## 选择性奖励做市

最终 v2 运行完整读取 18 页当前奖励配置：

- 8,966 raw rows，8,964 unique conditions
- 8,954 个单一 pUSD reward-asset scope 市场
- 25 个确定性预筛选市场、25 个 compact snapshots、46 个 books
- 22 个 `promising_not_validated`
- 3 个 `insufficient_data`：2 个缺完整手续费表，1 个 NO book 时间戳在未来

对每个可建模候选分别保存：

- theoretical ledger
- order scoring ledger
- confirmed payout ledger
- 100% / 50% / 30% / 0% reward haircut
- 1x / 3x / 10x competition proxy
- midpoint ±1 tick sensitivity
- actual fee schedule、平台五位小数 fee quantum 压力
- paired-fill 和 adverse one-leg 条件情景

22 个 `reward_independent` 标签只说明：**若**假设的一日 paired/adverse fills
发生，则其静态 conditional cashflow 在 reward=0 情景仍为正。公共盘口无法
证明 maker 身份、订单 age、排队位置或成交概率；公共 fill probability 下界
为 0。因此：

- expected reward 为 `null`
- recognized trading/reward/total PnL 均为 `0`
- actual/explainable fills 均为 `0`
- scoring/payout epochs 均为 `0`
- 最大市场贡献、收益率、回撤和 CI 均为 `null`

在没有真实 scoring 和多个独立 payout epochs 前，该方向最高只能是
`promising_not_validated`。没有达到小额实盘探针的离线门槛，因此本次不申请
资金或下单授权。

## 标的价格延迟

最终延迟 artifact 显式绑定 strict-v3 冻结批次中的 659 个相关 raw pins：
2 个 CLOB HTTP、510 个 CLOB WS、145 个 RTDS WS、2 个 rules HTTP。

- market：`Bitcoin Up or Down on July 24?`
- condition：
  `0x60cb891831b1e0d8f6834f35f150ec058d99075d62b75804ac7ce97b660a4dab`
- lifecycle interval：
  `2026-07-24T10:26:59.610Z..10:35:39.327Z`
- target overlap / effective capture：`517,774ms`
- loaded records：`32,868`
- source observations：`519`
- quote observations：`7,860`
- max raw / deduplicated / independent lag samples：`254 / 226 / 146`
- settled markets：`0`

虽然独立 lag 样本峰值 146 超过单序列最低 100，但该实验缺少 clock probes、
decision-vintage price-to-beat、settlement，覆盖短于一小时，并且只有一个
未结算市场。固定 lag 的 receive-time co-movement 只是描述性统计；没有进行
盈利统计推断、成交模拟或回测。

## Rewards=0、利润来源与悲观假设

没有任何策略建立了 rewards=0 后仍为正的**已实现或成交回放 PnL**。

- 天气：reward=0；正式策略 abstain，诊断 taker shadow 为负。
- 逻辑 / Neg-Risk：0 accepted，缺少 fill ledger，reward-zero PnL 为 `null`。
- 奖励做市：22 个 reward-zero 正值仅是“假设成交”的 conditional shadow；
  public fill lower bound 为 0，不能计入 PnL。
- 延迟：没有 price-to-beat、settlement 或 fills，reward-zero PnL 为 `null`。
- 旧做市：没有确认奖励账本，L0 合成 PnL 为负。

因此确认发生额分解为：已确认 reward `0`、已确认 rebate `0`、确认发生的
spread `0`、确认发生的 forecast alpha `0`；这些 `0` 来自零成交/零到账，
不会把严格可估的 spread/alpha 指标从 `null` 改写为 `0`。不存在可报告的最大
利润贡献者。

统一悲观边界包括：

1. public touch、聚合盘口、候选约束和 co-movement 都不是 fill；
2. maker 初始排在可见同价库存之后，只有可解释对手成交耗尽队列后才成交；
3. 多腿执行非原子，计入 partial fill、盘口撤走、对冲超时和单腿退出；
4. 使用当前逐市场 fee、逐档 depth、tick、minimum size、slippage、rounding；
5. 奖励未确认则为 0，未知 operation/cancel/gas/资金占用成本不得默认为收益；
6. 缺规则、mapping、时间、结算或 provenance 时 fail-closed；
7. L0/L1 数字只能作为 diagnostic/shadow，不能升级为“已回测盈利”。

## 数据与实现

canonical 冻结清单为 strict-v3：

- 663 finalized raw/manifest pairs
- 32,872 records；32,210 domain records
- 6 verified checkpoints
- checksum mismatches `0`
- invalid manifests `0`
- unsafe paths `0`
- 6 个 active partials 作为 mutable L0 排除
- internal manifest hash
  `ba5d86527a4bece9046591187f256d6e677596336904a3257209c954a821ecab`

配套 `DATA_QUALITY.json` 的总体状态是 `degraded`，不是 clean：它保留了
跨事件时间排序 advisory、仅含 lifecycle/diagnostic 的批次、schema drift 和
上游 sequence 不可用等 warning。这些 warning 不否定已通过的 checksum 与
manifest 完整性，但会阻止把 L2 输入保真度直接升级成成交或盈利证据。

长期 recorder 由 LaunchAgent
`com.lens.polymarket-edge-lab-capture` 监督。首个一小时进程自然退出后，
`runs` 从 1 变为 2、上一轮 exit code 为 0、PID 从 45088 变为 9770；
新 session 六路数据全部恢复并持续生成 manifest，stderr 为 0。详见
[FORWARD_CAPTURE_AUDIT.md](FORWARD_CAPTURE_AUDIT.md)。

关键模块：

- `src/edge_lab/weather*.py`：天气解析、forecast-vintage、校准和实验
- `src/edge_lab/constraints.py`、`constraint_experiment.py`：语义图和
  Standard/Augmented Neg-Risk fail-closed runner
- `src/edge_lab/rewards.py`、`reward_experiment.py`：scoring、竞争、
  haircut、fee 和单腿风险
- `src/edge_lab/execution.py`：Decimal 逐档成交、queue bounds、费用、
  partial fill、hedge、split/merge/resolution 和会计守恒
- `src/edge_lab/recorder.py`、`capture_runtime.py`、`data_store.py`：
  可恢复前向采集、不可变 batch、checkpoint 和 integrity audit
- `src/edge_lab/latency_capture_experiment.py`：receive-time 因果边界与
  显式冻结 pin replay
- `src/edge_lab/final_bundle.py`：fail-closed 最终证据聚合
- `src/edge_lab/network_safety.py`：最终 Edge Lab 入口的公共网络安全边界

安全声明只覆盖最终 Edge Lab 工作流及其入口；不把未参与最终证据链的 legacy
HTTP 模块泛称为已全面加固。所有最终 HTTP session 禁用 ambient proxy、
`.netrc` 和 redirect，拒绝 auth/cookie/敏感参数，只接受显式无认证 loopback
proxy。`assert_new_orders_disabled()` 在 credentials、签名和网络下单路径前
硬阻断新订单。

## 交付物

- [PRIMARY_SOURCES.md](PRIMARY_SOURCES.md)：2026-07-24 查询的官方一手资料
- [NULL_METRIC_DIAGNOSIS.md](NULL_METRIC_DIAGNOSIS.md)：逐策略 `null` 根因与
  可数值化条件
- [PHASE2_EXECUTION_REPLAY_PLAN.md](PHASE2_EXECUTION_REPLAY_PLAN.md)：
  动态采集、ghost decision、replay ledger 与 promoter 路线图
- [DATA_MANIFEST.json](capture_freeze_entity_clock_strict_v3/DATA_MANIFEST.json)
- [DATA_QUALITY.json](capture_freeze_entity_clock_strict_v3/DATA_QUALITY.json)
- [DATA_DICTIONARY.md](DATA_DICTIONARY.md)
- [BACKTEST_RESULTS.json](BACKTEST_RESULTS.json)
- [TRADES.csv](TRADES.csv)
- [METRICS.md](METRICS.md)
- [DATA_QUALITY.md](DATA_QUALITY.md)
- [LIVE_READINESS.md](LIVE_READINESS.md)
- [EXPERIMENT_REGISTRY.jsonl](EXPERIMENT_REGISTRY.jsonl)
- [REPRODUCTION.md](REPRODUCTION.md)
- [FINAL_BUNDLE_CONFIG.json](FINAL_BUNDLE_CONFIG.json)
- [天气运行](weather_public_run/RUN_MANIFEST.json)
- [约束 / Neg-Risk 结果](CONSTRAINT_EXPERIMENT_RESULTS.md)
- [奖励 v2 运行](reward_runs/selective-liquidity-rewards-20260724T112636.637029Z/RUN_MANIFEST.json)
- [延迟运行](LATENCY_CAPTURE_EXPERIMENT_v1_capture-latency-fddaaadb3cdbf2fc8c93.json)
- [持续采集审计](FORWARD_CAPTURE_AUDIT.md)

## 验证结果

- 最终 bundle 连续构建两次，8 个文件 SHA-256 逐字节一致。
- bundle：7 个 canonical 策略行、每行 18 个必需指标。
- `TRADES.csv`：仅 header，0 qualifying trade rows。
- experiment registry：51 rows；旧 reward、constraint、latency 运行保留并
  标记 superseded。
- weather manifest verifier：通过。
- reward immutable manifest replay：artifact/universe byte-identical，
  `network_requests=0`。
- Standard/Augmented constraint replay：四个核心 artifacts 分别与 live
  逐字节一致。
- latency `--pin-from` replay：全对象逐字节一致，SHA-256
  `54767a449cbd2fba3548999d9ec1a726294a8a97d02a6440abddfe5c669be0cc`。
- 最终 bundle 冻结时的 Edge Lab 综合测试：427 passed。
- 最终 bundle 冻结时的全仓测试：813 passed、11 skipped、1 deselected、
  0 failures。
- Phase 2 动态 collector、持久化与 shutdown 加固后的全仓复验：
  965 passed、11 skipped、1 deselected、0 failures。
- 与离线 bundle 分开执行的公开网络 WebSocket canary：1 passed、0
  failures；它不属于离线盈利证据链。
- `compileall`、数据完整性检查、secret/header scan 和 `git diff --check`
  均通过。

完整离线复现命令和输入哈希见 [REPRODUCTION.md](REPRODUCTION.md)。

## 尚缺的唯一外部证据

当前结论不是“这些方向永远没有 edge”，而是“现有证据不足以通过严格门槛”。
要改变分类，至少仍需：

1. 天气：不少于 100 个独立样本外已结算事件、100 次可解释 queue-bounded
   fills，并在 pessimistic、rewards=0 下 CI 下界大于 0；
2. 约束 / Neg-Risk：经官方规则与 adapter 验证的语义/mapping、同步逐档盘口，
   以及真实或可解释的多腿顺序成交和单腿退出证据；
3. 奖励：获得明确授权后的真实 maker order scoring、多个独立 payout epochs、
   对应成交和完整 payout reconciliation；
4. 延迟：可靠 clock probes、decision-vintage price-to-beat、结算标签、多个
   独立市场和可实现延迟下的成交证据；
5. 所有方向：至少 100 次可解释样本外成交、完整 PnL ledger、费用/滑点/对冲/
   gas/资金成本、最大单事件贡献不超过 20%、参数邻域稳定。

在这些证据到位前，保持 recorder 运行、保持实盘关闭，继续收集只读数据。
