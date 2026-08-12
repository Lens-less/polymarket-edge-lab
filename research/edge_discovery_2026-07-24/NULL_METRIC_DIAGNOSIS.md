# 严格指标 `null` 根因诊断

诊断日期：2026-07-24  
范围：`BACKTEST_RESULTS.json` 中全部 7 个 canonical 策略  
安全边界：只读公开数据；没有读取交易凭证、提交订单或执行链上写入。

## 结论

`null` 不等于“数据请求失败”，也不等于“收益为 0”。本轮真实状态是：

```text
公开行情、规则或实验输入已经取得
→ 但没有形成合格的策略决定 + 可解释成交 + 结算账本
→ actual / explainable / authenticated fills 全部为 0
→ PnL、收益率、回撤、Sharpe、CI 等没有可识别的样本
→ 严格指标必须为 null
```

`fees=0` 和 `rewards=0` 的语义不同：它们表示本次只读、零订单运行中确认没有
发生订单费用和奖励到账。把净 PnL 也写成 `0`，会错误表达“已经完成了一场
收益恰好为零的回测”。

## 数据并非没有取得

截至 `2026-07-24T13:13:43Z`，持续 recorder 已取得：

- 3,988 个 finalized batches；
- 192,687 条 finalized records；
- 7 个 recorder sessions，覆盖 `10:06:13Z..13:13:40Z`；
- 2 个静态 events、12 个 condition markets、24 个 token books；
- 324 条 `last_trade_price` 事件；
- 1,111 条全局 `new_market` 通知。

数据完整性审计没有发现 checksum mismatch、invalid manifest 或 raw/manifest
配对错误。新 session 的六路 HTTP/WS 来源均持续写入。因此不能把当前的
`null` 归因成统一的“接口没拿到数据”。

后续动态建设进一步排除了“完全没取到数据”的解释：截至
`2026-07-24T15:08:03Z`，严格短周期 registry 已累计 126 个 BTC/ETH
5m/15m 目标；新 sidecar run 已写入 122 个 Gamma 身份验证、3 个
`gamma_poll_deferred` 和 1 个可重试公共 HTTP 失败。未激活市场会继续轮询，
不会再被永久误拒。主 recorder 的 CLOB `GET /time` 探针也已在自然轮换后的
新 session 中逐 snapshot 出现。

这些新增数据仍没有改变 canonical 指标。动态 collector、精确 Chainlink
open/close join、immutable ghost decision、公开 taker trade、queue-bounded
replay、settlement ledger 和 strict promoter 的实现与 fixture/fault tests
现在均已完成。重启审计还修复了一个真实的 state-application 缺口，并把
累计 registry journal 改成增量 v2；修复后的全量结果为
`1079 passed, 11 skipped, 1 deselected`。最新 finalized-only 报告
`1ea87575fb680905beee7799b2b719945c936ba09addc225bd2b4ba772a07cc2`
仍只有 236 个 registered、0 个 mature，状态为
`waiting_for_mature_targets`。因此仍是“原始证据继续增长，但成熟完整执行
样本为 0”，而不是“网络请求全部失败”；恢复正确和代码通过也都不能冒充
真实 tracer 完成。

## 四类 `null`

| 类别 | 含义 | 当前实例 |
|---|---|---|
| 原始数据或标签不足 | 目标字段确实尚未采到 | 延迟实验缺 clock probe、price-to-beat、settlement；Augmented 只形成 4/9 books |
| 执行证据不足 | 有行情，但没有可解释成交和结算现金流 | 所有策略均无 queue-bounded settled fill ledger |
| 正确的不交易 | 策略或安全门主动拒绝候选 | 天气 validation 为负后 abstain；约束候选全部 fail-closed |
| 指标不可定义 | 零合格成交时没有收益样本 | PnL、return、drawdown、hit rate、CI、capacity 等 |

当前汇总器没有发现“成功回测值被误写为 `null`”的 bug。相反，
[`final_bundle.py`](../../src/edge_lab/final_bundle.py) 有意将 touch、理论奖励、
聚合盘口和被阻断候选排除在 realized metrics 之外。

## 按策略的直接根因

| 策略 | 已取得的证据 | 为什么仍为 `null` | 单纯继续原配置挂机是否解决 |
|---|---|---|---|
| 天气概率 | 100 个合格已结算事件，严格日期 split | test 只有 7 个独立日期；validation 目标为负且邻域不稳，正式策略 abstain，0 candidate / 0 fill | 否；先改模型并增加独立日期与 decision-time L2 |
| 跨市场逻辑 | 11 markets / 22 books，589 candidates | 全部盘口是 Neg-Risk，与普通逻辑语义不匹配；0 accepted | 否；换非 Neg-Risk 事件或实现已验证语义 |
| Standard Neg-Risk | 22 books，590 candidates | 缺链上 index mapping、正式 adapter deployment/block/fee provenance；0 accepted | 否；需定向补 provenance |
| Augmented Neg-Risk | 88 candidates | 9 个预期市场只取得 4 本 book，另缺 resolution source 和 reviewed claim；0 accepted | 部分；需换完整事件并先实现语义 |
| 奖励做市 | 扫描 8,964 个唯一奖励市场，评估 25 个 | 公共数据没有自己的 maker identity、order age、scoring epoch、payout epoch 或 fill ledger | 否；公开模式最高只能形成理论上下界 |
| 标的价格延迟 | 公开 CLOB/RTDS 前向流、真实 L2；动态 registry/collector 与严格 replay seam 已上线 | 最新 finalized-only 审计有 236 个 registered targets，但 0 个已达到 close+settlement timeout；尚无真实 promoted fill/ledger | 继续采集到首批目标成熟，并用 strict freeze/promoter 验证；不得用 fixture 代替 |
| 旧对称做市 | L0 合成与 L1 snapshot/touch 审计 | 旧 fill 不满足当前成交认定，且合成结果为负、有裸空头 | 否；应退休并用新 ledger 重做 |

逐字段原因和 canonical 计数分别见
[`BACKTEST_RESULTS.json`](BACKTEST_RESULTS.json)、
[`METRICS.md`](METRICS.md) 和
[`FINAL_REPORT.md`](FINAL_REPORT.md)。

## 让 `null` 有资格变成数值的最短路径

必须先打通以下证据链：

```text
decision-time L2 / 规则 / PTB
→ 冻结的 ghost decision
→ 保守队列或逐腿非原子 replay
→ 可核验 public trade 消耗量
→ settlement
→ cash / position / fee / equity ledger
→ 指标 promoter
```

任何一步缺失时，相关财务指标继续为 `null`。完成整条链后，L2
queue-bounded replay 可以生成明确标注的模拟 PnL；它仍不能冒充实际到账
收益。奖励 scoring/payout 和真实 maker 排队校准最终需要另行授权的 L3
小额探针，本阶段不申请。

## 当前优先级

1. 继续运行已上线的动态 BTC/ETH collector，并量化首批 20 个成熟市场的
   完整生命周期率；不足 80% 时先修 collector；
2. 对第一个真实 `strict_settlement_committed` candidate 构建完整
   finalized capture freeze，并连续两次运行 promoter 验证 byte-identical；
3. 检查 immutable ghost decision、复合 public-trade event key、悲观队列、
   fee、operation cost 与 settlement ledger 是否逐笔守恒；
4. 定向补齐 Neg-Risk mapping/adapter provenance，再做全市场结构扫描；
5. 天气只在模型版本冻结后扩样本；奖励快照降为次级持续性研究；
6. 达到 100 个独立已结算 OOS 事件和 100 次可解释成交后，才做盈利晋级。

在此之前，结论保持：没有经过严格验证的盈利策略，**不建议实盘交易**。
