# 策略投资决策（2026-08-22）

## 决策

本项目当前作出以下 `NO-GO` 决策：

1. **BTC 5m/15m 结构线永久停止。** 不再采集、不再回测、不再为它开发实盘适配器。
2. **选择性流动性奖励（rewards）线不批准为实盘盈利策略。** 它可以作为研究假设保留，但不进入“全面修工程后上线”的阶段。
3. **保持新订单硬门关闭。** 当前没有证据支持解除 `src/edge_lab/compatibility.py` 的新订单保护，也没有合规、可验证的执行网络。

这里的判断标准不是“某一天盈利的概率是否大于零”，而是能否对扣除所有成本后的期望收益建立可信正下界。前者对几乎任何交易策略都成立，不能作为投入工程和资本的理由；后者目前不成立。

## 一、结构线为什么应当放弃

同到期 BTC 5m/15m 结构对的结算价值为：

```text
fair_value = 1 + b,  b >= 0
```

因此：

- 双 taker 与 maker+FOK 要求 `b < 0` 才能产生正 floor，和结算恒等式冲突；
- 双 maker 只有在 `b < 两腿可捕获价差之和` 时可能翻正；
- 1,017 个真实盘口样本中正 floor 为 0，`b < 0.02` 也为 0；
- 三个具有完整官方 TWAP 终值的到期、三种执行口径均未出现正 floor；
- 即使使用项目给出的极乐观容量上界，收益也只有约 29 pUSD/天，而实测最好边际仍为 0。

这不是工程问题，也不是再增加样本就能合理期待翻转的问题。完整证据见：

- `research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md`
- `research/btc_5m_15m_edge_readiness_v08_2026-08-18/CLAUDE_FINAL_ASSESSMENT.md`
- `research/btc_5m_15m_edge_readiness_v08_2026-08-18/PREREGISTRATION.json`

## 二、rewards 线的真实盈利方程

rewards 做市的日收益应写成：

```text
E[PnL]
  = E[confirmed reward payout]
  + E[paired fills] * paired spread PnL
  - E[one-leg fills] * adverse-selection loss
  - taker hedge fees
  - split/merge、失败重试、运行与资金成本
```

当前缺失的不是某个小参数，而是决定符号的四个随机变量：

- 账户实际 scoring 份额；
- confirmed payout；
- 双腿与单腿成交率；
- 单腿损失的尾部分布。

公开 L2 盘口没有 maker identity、订单年龄、账户评分和最终 payout，因此这些量不能从静态快照可靠识别。

## 三、现有证据不能推出正期望

### 1. 2026-07-24 旧证据

- 25 个候选中 22 个仅为 `promising_not_validated`；
- `actual_fill_count = 0`；
- scoring epoch = 0；
- payout epoch = 0；
- `recognized_reward_pnl = 0`；
- `recognized_trading_pnl = 0`；
- `recognized_total_pnl = 0`。

### 2. 2026-08-22 诊断性重扫（不作为硬证据）

本次又用项目自身的只读奖励实验器运行了 25 候选和 100 候选两组诊断。输出写在系统临时目录，没有纳入版本化研究归档；同时，官方奖励分页会动态变化。因此，本报告不把本次页数、唯一 condition 数、配置行日率总和或候选分类数量当作可复核的决策证据。

诊断仍暴露了一个有用事实：运行器会从公开快照生成 `promising_not_validated`、`reward_independent` 等乐观标签，但真实成交、真实 scoring、真实 payout 与 recognized PnL 仍全部为 0。这个结果只用于检查分类语义，不能用于估算收益。

`reward_independent` 不是实证结论。当前代码用固定输入“每天 1 次 paired fill + 1 次 adverse one-leg fill”计算该标签；真实的两类成交率并未被观测。把 reward 设为 0 后的条件现金流为正，只说明该手工场景为正，不说明 `E[PnL] > 0`。

静态奖励分配代理会在零可见合格竞争、短时市场日率折算和静态份额假设下产生极端结果，因此不能当作 payout 预测。实验器自身也正确输出：

```text
profit_claim = none
expected_reward = null
recognized_total_pnl = 0
```

### 3. 原审查中的三项关键误导

#### “12 USDC 真金探针”

版本化的 2026-07-24 奖励 universe 中，最小正奖励尺寸为 20 shares；其他市场还要求 30、40、50、100、200、250、300、500 或 1,000 shares。对最小尺寸为 20 的近互补双边报价，名义资本通常接近 20 pUSD；20–25 pUSD 只是包含少量操作缓冲的保守预算建议，不是协议硬门槛。12 pUSD 只可能在部分低价单边报价上通过尺寸检查，不能代表完整策略。

而且 20-share 最小尺寸意味着一次跳空单腿成交的损失可能超过 5 pUSD；“5 pUSD 日损上限”只能限制后续动作，不能把已发生的单次跳空损失物理截断在 5。

#### “全站奖励池 7,577 美元/天”

该数字没有随原审查给出可复核的查询快照和聚合口径，不能作为可经营收入。即使对奖励配置行求和，结果也会随时间、去重方式、资产范围和短时市场日率折算显著变化；更重要的是，配置行总和不是本账户的竞争份额、confirmed payout 或策略容量。因此本决策不采用 7,577，也不以任何“全站日率总和”替代账户收益证据。

#### “先上真钱再补工程”

真实 scoring/payout 确实最终需要账户探针，但这不等于应在规则字段、订单年龄、费率来源、断线撤单、风险 fail-closed 和持仓对账尚未闭环时先下单。否则得到的是混杂数据，不能回答盈利问题。

## 四、当前执行环境还存在硬阻断

官方 `https://polymarket.com/api/geoblock` 在当前网络路径返回：

```json
{"blocked": true}
```

禁用系统代理后的直连检查超时，没有取得 `blocked=false` 的证据。因此当前工作站不能作为新订单执行环境。本项目不会设计、建议或实现任何绕过地理限制的路径，也不据此推断用户的具体所在地。

## 五、工程决策

鉴于策略没有通过投资门槛，本轮**不修**以下内容来推动实盘：

- 不解除 `compatibility.py` 的新订单硬门；
- 不为 rewards 编写可下单的 live adapter；
- 不迁移主机来规避 geoblock；
- 不为小额探针先行投入 I1–I12 的全量修复；
- 不把理论 reward、静态 scoring 或 public touch 写入 PnL；
- 不继续为已停止的结构线运行 Gate 0、200-expiry 采集或部署轨道。

基础设施问题 I1–I12 仍是真问题，但在没有批准策略时修复它们只会得到一套更安全的负期望或未验证交易系统，不会创造 edge。

## 六、只有满足以下外部条件才允许重新立项

rewards 线不是当前待实施工作。未来只有同时满足以下条件，才值得重新提交一个独立的 measurement-only 项目：

1. 预定执行环境不借助代理即可从官方端点得到 `blocked=false`，且使用者自行确认符合适用规则；
2. 风险资本足以覆盖目标市场的双边最小尺寸和对冲缓冲；对 20-share 级别配置，20–25 pUSD 仅作为保守预算示例，并接受单次损失可能超过 5 pUSD；
3. reward order age、资产/费率来源、市场状态与双边资本预检全部可解释；
4. 探针至少覆盖 3 个独立 confirmed payout epochs；
5. `confirmed payout + realized trading PnL - fees - one-leg losses - operation cost > 0`；
6. payout 至少覆盖实测 p90 单腿损失，而不是只超过平均损失；
7. 在进入盈利策略评审前，再取得至少 100 次可解释成交、100 个独立已结算事件，且 cluster/bootstrap 95% PnL 下界大于 0。

任一条件不满足，继续保持 `NO-GO`。

## 七、复核命令与结果

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_btc_twap_relative_value_replay.py `
  tests/test_btc_twap_executable_upper_bound.py `
  tests/test_edge_lab_rewards.py `
  tests/test_backtest.py
# 50 passed

.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_polymarket_sdk_migration.py::test_live_order_boundary_cannot_be_released_by_a_boolean_mapping
# 1 passed
```

2026-08-22 的 25 候选与 100 候选重扫均使用：

```powershell
.\.venv\Scripts\python.exe scripts\run_reward_experiment.py `
  --max-markets <25|100> `
  --max-reward-pages 40 `
  --timeout 20 `
  --proxy http://127.0.0.1:7897 `
  --output-dir <系统临时目录>
```

两个诊断运行均报告 `orders_submitted=false`。由于输出只保存在系统临时目录，不能据此固化任何市场总量；代理仅用于公开只读采集，地理准入检查也没有因此被当作放行证据。

## 最终结论

- **结构线：放弃。** 经济学已判负。
- **rewards 线：作为实盘盈利策略放弃；作为未验证研究假设冻结。** 它存在抽象的盈利场景，但没有可投资的正期望证据，当前环境也不可执行。
- **工程：不进入全面修复。** 先有 edge，再为 edge 修执行系统；不能反过来用工程质量替代盈利证据。
