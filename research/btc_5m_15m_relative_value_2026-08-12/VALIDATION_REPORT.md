# BTC 5m/15m TWAP Relative Value v0.1 验证报告

验证日期：2026-08-12  
运行模式：Paper only，始终禁止真实下单  
冻结规格 SHA-256：`706abcfd725b3411db2e6c74073553721db8d1d201f36e3b62d8a253b10b53b4`

## 结论

**分类：`insufficient_data`。不允许宣称策略已验证盈利，也不建议实盘。**

当前证据不能证明相对价值 edge 存在或不存在。它已经足以否决“当前 v0.1 配置可以直接进入经济回放”的说法：Phase 0 数据完整性未通过，后续概率校准、OOS PnL 和执行收益均不得计分。

合格净 PnL 为 `null`，不是 0。原因是没有任何一笔交易形成完整的 `decision -> explainable fill -> settlement -> ledger` 证据链。

## 冻结后的实测结果

| 指标 | 结果 | 冻结门槛 |
|---|---:|---:|
| 同到期前向采集 | 3 组 / 6 市场 | — |
| 捕获到的官方结算事件 | 4 | 2,000 resolved markets |
| 可由精确开收盘 TWAP 机械复算 | 3 / 6（50%） | >99.5% coverage |
| resolution conflict | 0 | 0 |
| 可用 raw shadow model | 0 | Phase 1 必需 |
| 可解释模拟交易 | 0 | >=500 |
| 可解释 fills | 0 | >=500 |
| OOS / bootstrap / Brier / ECE | 缺失 | 全部必需 |
| 合格净 PnL | `null` | 正且 95% bootstrap 下界 >0 |

三组结果：

1. `1786529700`：2/2 市场可机械复算并捕获结算；Binance 订阅过滤方式没有返回 BTC predictor；决策时 30s/60s TWAP 源时间年龄均约 4 秒。
2. `1786530600`：捕获 2/2 结算，只有 1/2 市场可机械复算；5m 精确开盘 TWAP 缺失；有 300 个连续且因果的 1 秒 predictor 点；TWAP 源时间年龄约 3 秒。
3. `1786531500`：从 5m 开盘前启动并抓到精确开盘值，但到期前后 RTDS 出现约 78 秒缺口，精确收盘值和 `market_resolved` 事件均未捕获。60 秒决策点的 predictor 历史不足；45 秒诊断点有 300 个连续 predictor 点，但 TWAP 源时间年龄仍约 2 秒。

三组在冻结的 `max_chainlink_staleness_ms=1500` 下都被拒绝。第三组 45 秒诊断说明：即使 predictor 完整，当前 RTDS 观测延迟仍单独阻断交易。这个结果只能用于提出下一版预注册，不得在看到结果后放宽当前门槛并回填收益。

## 已验证的工程属性

- 当前市场规则严格绑定同到期的 5m/15m 合约、Chainlink 30s/60s TWAP、Up 的 `>=` 规则、token 顺序、tick、最小订单、费用和 `itode`。
- 决策切片同时要求源时间和本机接收时间不晚于决策，避免延迟消息造成时间泄漏。
- EWMA/bootstrap 只接受严格连续的 1 秒 predictor；不把不规则消息行数误当秒数。
- 5m/15m 用同一批终端 path 定价；独立 isotonic 边际会投影回一个一致的联合分布，期望、亏损概率和 CVaR 使用同一概率体系。
- 仓位按实际逐档深度和 taker fee 反推，最终总成本不能超过冻结的 `25 USDC` pair risk。
- 250ms taker delay、第二腿 750ms 超时、partial fill、失败后的因果 unwind、no-fill 与完整成交状态均有测试。
- 捕获清单的 checksum、raw/manifest 对应关系和 partial 文件审计均为零异常；这只证明落盘完整，不代表网络事件覆盖完整。

## 测试结果

- 策略、报告和聚合器定向测试：`19 passed`。
- 全仓非 network 测试：1,313 个 selected tests，进程退出码 0（包含 2 个既有 skip，另有 1 个 network deselected）。
- 新增策略/报告文件的 Ruff `E/F/I` 检查通过。
- 三组最终 capture 的完整性审计均为 clean。

## 仍未通过的规格阶段

由于 Phase 0 已失败，以下指标没有经济解释力，保持缺失而不是用合成数据填充：

- past-only、按 horizon/regime/model 绑定的真实 isotonic 校准样本；
- chronological walk-forward train/validation/locked-test；
- 500 笔带真实延迟、深度、费用、partial fill、legging 和结算的执行重放；
- Brier、log loss、ECE、bootstrap 下界、集中度、方向暴露和信号单调性；
- hedge-ratio 网格及 signal-exit 的 OOS 比较。

## 后续唯一合理路径

1. 先修复或替换会在关键到期窗口丢失 78 秒数据的 RTDS 捕获链，并增加可审计的 reconnect/backfill；不能把缺失值插值成 Chainlink 边界。
2. 基于独立的新样本测量 TWAP 的 observation-age 分布；如果要修改 1500ms 门槛，必须发布 v0.2 预注册并从新数据开始，不能重算本批样本。
3. 固定决策时刻、模型、费用、参数、执行和晋级门槛，连续前向收集直到 >=2,000 resolved markets 且 coverage >99.5%。
4. 只有 Phase 0/1 通过后，才运行 >=500 笔 conservative-taker replay 和 locked OOS；全部门槛通过前继续保持 `不建议实盘交易`。

## 官方机制依据

- [Polymarket Chainlink TWAP](https://docs.polymarket.com/market-data/chainlink-twap)
- [Polymarket Crypto Fees](https://docs.polymarket.com/trading/fees)
- [Polymarket Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)
- [CLOB Market Info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [RTDS Realtime Data](https://docs.polymarket.com/market-data/realtime-data)

