# V0.2 Research-to-Live Profit Trading System SPEC

状态：`ACCEPTED TARGET CONTRACT / V0.2.0 PARTIAL PREVIEW CONFORMANCE`
目标软件版本：`0.2.x`
创建日期：`2026-08-30`
适用仓库：`poly-mm`

## 0. 一句话合同

V0.2 必须把当前研究仓库升级为一套单账户、单 venue、利润导向的 Polymarket 交易系统：持续发现候选机会，用统一的现实成本模型验证，在 Shadow 和小额实盘中检验，并把通过门槛的机会转化为可控订单和可归因的真实净 PnL。

V0.2 的成功不以页面数量、模块数量、成交数量或回测曲线为准；唯一业务真相是：系统能否用可复算证据说明某个策略在真实成交、真实费用和真实风险下到底赚不赚钱。

本 SPEC 已被接受为 V0.2 的实现合同；它定义范围、接口和验收标准，并附带 v0.2.0 的 preview conformance notes，但不表示当前 live readiness 已经成立，也不把任何未验证结果包装成盈利结论。

### 0.1 v0.2.0 preview conformance

`v0.2.0` 交付模块化类型、账本、执行、门禁、报告、固定 Track A/B 验收场景和本地 trade-desk 预览，并通过离线软件测试。它没有交付实时市场/配置驱动的 production scanner、replay、shadow、打包前端启动器或可验证外部 provenance 的 Canary 解锁路径。因此本次发布是本合同的部分预览实现，不声称完成第 18 节全部 DoD，也不声称 `LIVE_CANARY_READY`、真实成交或盈利。

## 1. 权威性与当前边界

本 SPEC 是 V0.2 新开发轨的权威实现合同，但不是立即解除现有 live 硬门的授权，也不代表当前盈利性已被验证。后续 `0.2.x` 迭代可以在本合同下继续补齐 production scanner / live desk 等未完成项，但这些都不是 `v0.2.0` 的完成声明。

在 V0.2 的 `LIVE_CANARY` 前置条件全部实现并通过之前：

- `src.edge_lab.compatibility.assert_new_orders_disabled()` 必须继续保护所有 legacy live 入口。
- 当前 BTC 5m/15m `STRUCTURAL_STOP` 结论保持有效，不得因本 SPEC 自动恢复。
- 历史 rewards、结构线和部署材料只作为输入证据，不得视作已验证盈利策略。
- 实现 live-support 代码不等于授权当前 Codex 会话提交真钱订单。
- 真实下单必须由用户在目标环境明确启用，并满足本 SPEC 的 Canary Gate。

当历史文档与本 SPEC 的 V0.2 产品目标冲突时：

- 对 `v0.1.x` 当前支持面，以现有 README、`docs/index.md`、停手证明和硬门为准。
- 对 V0.2 的待实现目标、范围和验收，以本 SPEC 为准。
- 在 V0.2 正式发布前，README 不得声称已经支持实盘。

## 2. 产品目标

### 2.1 业务目标

V0.2 必须形成以下闭环：

```text
发现机会 -> 现实成本评估 -> Replay -> Shadow -> 小额实盘
         -> 真实 PnL 归因 -> 放量 / 降额 / 停止 -> 反馈研究
```

系统必须回答五个问题：

1. 现在有哪些可能具有正净期望的机会？
2. 扣除费用、滑点、成交不确定性、逆向选择和库存成本后还剩多少优势？
3. 这项优势能否在真实盘口和真实成交中实现？
4. 当前应投入多少风险预算？
5. 策略应该继续、放量、降额还是停止？

### 2.2 V0.2 首发目标

V0.2.0 必须交付：

- 一个统一机会扫描和排名入口。
- 两条优先策略研究轨：结构/完整集合套利与 Maker 做市收益。
- Replay、Paper、Shadow、Semi-Auto Live 共用的策略决策逻辑。
- 真实订单、成交、仓位和 PnL 归因。
- 一个利润导向的单页交易台。
- 小额、限额、人工确认的 `LIVE_CANARY` 能力。
- 失败后停止新单、撤单、对账和恢复的最小安全闭环。

### 2.3 非目标

V0.2.0 不做：

- 多 venue 路由。
- 多账户或多租户。
- Combos、Perps 或跨链交易。
- 企业级权限、审批流或分布式微服务。
- 通用插件系统或“支持所有策略”的抽象框架。
- 自动提高风险限额。
- 没有盈利证据的全自动交易。
- 仅为展示而存在的大屏、复杂运维台或无决策用途的图表。
- 将回测收益、未实现浮盈或未确认奖励展示为已实现利润。

## 3. 成功指标

### 3.1 北极星指标

北极星指标是：

```text
realized_net_pnl_after_all_costs
```

其定义为：

```text
realized_net_pnl =
    realized_trading_pnl
  - taker_fees
  - builder_fees
  - realized_slippage
  - unwind_and_hedge_cost
  - operational_onchain_cost
  + confirmed_maker_rebates
  + confirmed_liquidity_rewards
```

要求：

- 未实现 PnL 单独展示，不进入策略放量判定。
- 预计 rewards 不得计入 realized PnL。
- rewards/rebates 必须在确认归属后计入，并与 trading PnL 分栏。
- 所有金额使用 `Decimal` 或十进制字符串，禁止用二进制浮点驱动交易决策。

### 3.2 必须展示的策略指标

- `realized_net_pnl`
- `trading_net_pnl`
- `incentive_pnl`
- `return_on_allocated_capital`
- `profit_factor`
- `max_drawdown`
- `expected_net_edge`
- `realized_net_edge`
- `edge_realization_ratio`
- `fill_rate`
- `maker_fill_rate`
- `adverse_selection_bps`
- `capital_utilization`
- `unmatched_leg_exposure_seconds`
- `unresolved_reconciliation_count`

### 3.3 禁止的成功替代指标

下列指标可以观察，但不能单独作为“盈利策略”或“实盘成功”的证明：

- 回测毛收益。
- 胜率。
- 交易数量。
- 报价数量。
- Paper PnL。
- Shadow 理论 PnL。
- 盘口理论套利空间。
- 尚未确认的 rewards。
- 单笔异常大收益。

## 4. 统一经济模型

### 4.1 预期净优势

每个候选机会必须计算：

```text
expected_net_edge =
    expected_gross_edge
  + expected_confirmed_incentive
  - expected_taker_fee
  - expected_builder_fee
  - expected_slippage
  - expected_adverse_selection
  - expected_inventory_cost
  - expected_unwind_cost
  - uncertainty_buffer
```

交易准入使用保守值而不是点估计：

```text
tradable_edge = lower_confidence_bound(expected_net_edge)
```

只有同时满足以下条件才生成可执行计划：

- `tradable_edge > strategy.min_net_edge`
- 机会在 TTL 内。
- 预计容量大于最小可交易量。
- 风险调整后收益为正。
- 数据、市场、账户和执行状态可用。

### 4.2 机会记录

每个 `OpportunityCandidate` 至少包含：

```text
opportunity_id
strategy_id
detected_at
expires_at
market_ids
token_ids
recommended_legs
expected_gross_edge
expected_fees
expected_slippage
expected_adverse_selection
expected_inventory_cost
expected_incentive
expected_net_edge
tradable_edge
max_loss
estimated_capacity
confidence
evidence_refs
rejection_reasons
```

每个字段都必须能从输入快照和版本化策略配置复算。

## 5. 策略组合

### 5.1 Track A：结构与完整集合套利

目标：发现同一事件结果集合或逻辑相关市场之间的可成交定价不一致。

候选类型：

- YES/NO 完整集合在扣除所有成本后的价格偏差。
- 多结果市场的完整集合偏差。
- 逻辑包含、互斥、条件关系造成的跨市场不一致。
- 可立即或在严格时限内平掉的组合腿机会。

必须验证：

- 所有腿的真实可成交深度。
- 非原子成交导致的单腿暴露。
- FOK/FAK、Maker-first 和 unwind 路径。
- negative-risk 市场的签名与结算差异。
- 资金占用时间和结算不确定性。
- 组合收益是否在悲观成交假设下仍为正。

禁止：

- 使用中间价代替真实可成交价格。
- 把理论组合差价直接当利润。
- 在没有单腿失败处理时提交组合。

首发状态：`P0 / Research + Replay + Shadow + Canary eligible`。

### 5.2 Track B：Maker 做市与奖励

目标：通过 spread、maker rebate 和 liquidity reward 获得扣除逆向选择与库存成本后的净收益。

报价决策：

```text
maker_expected_net_edge =
    expected_spread_capture
  + expected_confirmed_rebate
  + expected_confirmed_liquidity_reward
  - expected_adverse_selection
  - expected_inventory_cost
  - expected_cancel_replace_cost
  - uncertainty_buffer
```

必须支持：

- 双边或单边 post-only 报价。
- GTD 催化剂前自动过期。
- 库存 skew。
- 报价 TTL 与盘口陈旧判断。
- 撤单重挂。
- 订单 scoring 与 rewards 资格观察。
- 实际 fill 后的短周期 markout，用于估计逆向选择。

必须分开归因：

- spread PnL
- adverse-selection PnL
- fees
- maker rebates
- liquidity rewards
- inventory mark-to-market

首发状态：`P0 / Research + Replay + Shadow`。V0.2 可以完成 Track B 的 Canary 软件链路，但策略资格默认保持 `SHADOW_ONLY / LIVE_BLOCKED`。

这里的 Track B 是供新候选使用的能力轨，不恢复 2026-08-22 已冻结的 rewards thesis，也不授权对现有 rewards 线提交真钱订单。只有拥有新 `strategy_id`、新预注册假设和新证据包的 Maker/Reward 候选，才可独立申请 `PROBE_ALLOWED`。

### 5.3 Track C：事件驱动定价

目标：使用外部数据或事件模型发现方向性错误定价。

V0.2.0 仅要求研究接口和 Shadow 支持，不要求 live 自动化。只有独立证据通过后，才可在 `0.2.1+` 申请 Canary。

首发状态：`P1 / Research + Shadow only`。

### 5.4 旧 BTC 5m/15m 结构线

当前 `STRUCTURAL_STOP` 保持不变。任何重启尝试必须形成新的策略 ID、新的预注册假设和新的证据包，不能复用旧策略的 live 资格。

## 6. 运行模式与晋级门

### 6.1 模式

```text
RESEARCH      离线研究，不产生订单
REPLAY        历史因果回放
PAPER         实时或历史仿真成交
SHADOW        真实行情和真实决策，不提交订单
LIVE_CANARY   小额、白名单、人工确认实盘
LIVE_LIMITED  经过实盘证据放量的受限自动交易
KILLED        禁止新单，仅撤单、对账和恢复
```

### 6.2 Research Gate

策略进入 Shadow 前必须满足：

- 假设、特征、入场、退出和停手条件预注册。
- 使用时间切分的样本外验证。
- 无未来数据泄漏。
- 现实费用、滑点、部分成交和容量模型已启用。
- 基准与消融对照存在。
- 在费用和滑点上浮 25%、成交率下降 25% 的压力测试下仍不出现灾难性反转。
- 输出可复算证据包。

### 6.3 Shadow Gate

默认晋级门槛：

- 连续至少 14 个自然日。
- 至少 50 个独立候选机会。
- 至少 20 个按当时盘口判断可成交的机会。
- Shadow 决策全部保存，不能事后挑样本。
- 预计净优势为正。
- 无严重数据完整性或状态漂移问题。
- 策略配置和阈值在观察窗开始前冻结。

策略可以在运行前预注册更严格的门槛；不得在看到结果后放宽。

### 6.4 Canary Gate

只有同时满足以下条件才允许 `LIVE_CANARY`：

- Shadow Gate 通过。
- 用户在目标环境明确启用 live。
- geoblock 允许交易。
- 钱包、余额、allowance、market constraints、签名路径通过。
- user stream、order heartbeat、open-orders/trades reconciliation 可用。
- 单笔、单市场、单策略、日损和总风险预算全部配置。
- kill switch 和重启恢复演练通过。
- 第一次 Canary 计划需要人工确认。

Canary 默认约束：

- 单账户。
- 单策略或单机会。
- 市场白名单。
- 极小风险预算。
- 不允许 UI 临时提高限额。
- 不允许自动放量。

### 6.5 Limited-Live Gate

默认放量门槛：

- 至少连续 30 个自然日的 live 观察窗。
- 至少 100 个独立 fills；低频策略可在运行前预注册等价统计门槛。
- `realized_net_pnl > 0`。
- `profit_factor >= 1.15`。
- `edge_realization_ratio >= 0.70`，并说明异常值处理。
- 最大回撤未突破预注册预算。
- 两个连续评估窗口均为正。
- `unresolved_reconciliation_count == 0`。
- 没有重复下单、失控下单或无法解释的资金差异。

未通过时必须继续 Canary、降额或停止，不得用“工程已完成”代替盈利证据。

### 6.6 策略资格注册表

每个策略必须拥有唯一 `strategy_id` 和持久化的资格记录。资格状态只能按门槛晋级，不能由交易台临时切换：

```text
RESEARCH_ONLY  仅允许 Research、Replay 和 Paper
SHADOW_ONLY    允许 Shadow，禁止生成 venue order
PROBE_ALLOWED  已满足 Canary 软件与证据门槛，但仍需当次人工授权和风险预算
LIVE_ALLOWED   已满足 Limited-Live Gate，只能在冻结限额内运行
STOPPED        禁止新单，只允许撤单、对账和风险收敛
```

每条资格记录至少保存：

- `strategy_id`、策略版本和策略假设摘要。
- 代码提交、配置哈希、数据区间和证据包路径。
- 当前资格、允许模式、市场白名单、风险限额和有效期。
- Research、Shadow、Canary、Limited-Live 各门槛的判定结果。
- 晋级、降级、停止的时间、原因和操作者；历史记录 append-only。

资格检查必须位于 `ExecutionEngine` 和 venue adapter 之前。前端、CLI 参数、环境变量或数据库手工修改均不能单独产生 live 资格；`PROBE_ALLOWED` 也不等于本次运行已经获得真钱授权。

## 7. 产品模块

V0.2 使用模块化单体，不使用微服务。只有真实变化的行为才设置 seam。

### 7.1 `OpportunityEngine`

职责：从标准化市场快照生成、过滤和排名候选机会。

Interface：

```python
scan(snapshot, strategy_configs) -> RankedOpportunities
explain(opportunity_id) -> OpportunityExplanation
```

Implementation 隐藏：特征、成本模型、置信区间、容量、TTL、过滤和排名。

### 7.2 `StrategyRuntime`

职责：让同一策略逻辑跨 Replay、Paper、Shadow 和 Live 运行。

Interface：

```python
evaluate(opportunity, portfolio, mode) -> StrategyDecision
observe(outcome) -> StrategyFeedback
```

约束：

- mode 不能改变信号定义。
- mode 只允许替换成交来源、资金上限和 mutation 权限。
- 策略不能直接接触私钥或 venue client。

### 7.3 `ExecutionEngine`

职责：将批准的交易意图转化为订单，并拥有完整订单生命周期。

Interface：

```python
review(intent, actor, idempotency_key) -> ExecutionPlan
confirm(plan_id, approval) -> ExecutionTicket
intervene(action, actor, idempotency_key) -> InterventionResult
snapshot(scope) -> ExecutionSnapshot
```

必须隐藏：

- tick/min-size/negative-risk 校验。
- 费用、余额、allowance、仓位和风险检查。
- 签名、提交、批量响应和 retry 分类。
- 部分成交、cancel-replace、单腿暴露、ambiguous ack。
- user stream、REST backfill、对账和重启恢复。

### 7.4 `PortfolioBook`

职责：提供账户资金、仓位、挂单、PnL 和策略归因的权威读模型。

Interface：

```python
snapshot() -> PortfolioSnapshot
performance(query) -> PerformanceReport
reconcile(venue_snapshot) -> ReconciliationReport
```

真相优先级：

1. venue authoritative open orders/trades/balances
2. authenticated user stream events
3. 本地 append-only execution records
4. 派生 read models

任何不一致必须显式进入 reconciliation，不得静默覆盖。

### 7.5 `TradeDesk`

职责：展示决策和接收操作，不实现策略、风控或 PnL 算法。

前端只消费后端快照和事件；不得在浏览器内重新计算权威交易金额或风险。

## 8. Venue seam

`ExecutionEngine` 后面只设置一个真实外部 seam：

```python
class VenueAdapter(Protocol):
    preflight(...)
    submit(...)
    cancel(...)
    reconcile(...)
    stream_user_events(...)
```

必须提供：

- `ReplayVenueAdapter`
- `PaperVenueAdapter`
- `PolymarketLiveVenueAdapter`

三个 adapter 必须通过同一套合同测试。Live adapter 是唯一允许接触官方 SDK、认证凭据和签名的 implementation。

## 9. 订单生命周期

本地生命周期与 venue 状态分开保存。

```text
DRAFT
  -> REVIEWED
  -> APPROVED
  -> SUBMITTING
  -> LIVE / DELAYED / MATCHED
  -> PARTIALLY_FILLED
  -> FILLED

SUBMITTING
  -> AMBIGUOUS
  -> RECONCILING
  -> LIVE / FILLED / REJECTED / ATTENTION_REQUIRED

LIVE / PARTIALLY_FILLED
  -> CANCEL_REQUESTED
  -> CANCELED / RECONCILING

任意危险状态
  -> KILLED
```

不变量：

- 每次 mutation 必须有 idempotency key。
- 网络超时后禁止盲目重复下单。
- `AMBIGUOUS` 必须先对账。
- 改单是 cancel + replacement child order。
- cancel/kill/reconcile 必须幂等。
- 每个 fill 以 venue fill/trade ID 去重。
- 重启后必须先恢复 open orders、recent trades、positions 和 cash，再恢复策略。
- 未完成提交或撤单在恢复时默认进入 reconcile 或 killed。

## 10. 最小风险规则

风险能力只服务于保护本金和阻止错误订单，不建设独立风险平台。

必须配置：

- `max_order_notional`
- `max_market_exposure`
- `max_event_exposure`
- `max_strategy_exposure`
- `max_total_exposure`
- `max_daily_loss`
- `max_drawdown`
- `max_open_orders`
- `max_unmatched_leg_seconds`
- `max_market_data_age_ms`
- `max_user_stream_gap_seconds`
- `max_order_rate`

必须自动停止新单：

- 资金、仓位或 allowance 不足。
- market constraints 未知或变化。
- geoblock blocked/close-only。
- 行情陈旧。
- user stream 中断且未完成 backfill。
- order heartbeat 失败。
- 存在 unresolved ambiguous submission。
- PnL、回撤或敞口越限。
- 账户与本地账本不一致。
- persistent kill 已触发。

Kill 行为：

1. 持久化 killed 状态。
2. 停止所有新单。
3. 尝试 cancel-all。
4. 回读并验证剩余订单。
5. 在交易台明确展示未撤订单和恢复步骤。
6. 必须人工解除；进程重启不得自动解除。

## 11. 交易台 SPEC

V0.2 使用单页桌面工作区，目标是让用户快速回答“哪里可能赚钱、为什么、准备下什么单、真实赚了多少”。

### 11.1 顶部状态条

- `RESEARCH / PAPER / SHADOW / LIVE_CANARY / LIVE_LIMITED / KILLED`
- 账户可用资金
- 今日、7 日、30 日 realized net PnL
- 当前回撤
- 已用风险预算
- 数据与订单连接状态
- `Cancel All`
- `Kill`

Live 模式必须使用与其他模式明显不同的视觉状态。

### 11.2 左侧机会列表

每行展示：

- 策略
- 市场
- `tradable_edge`
- 预计净利润
- 最大损失
- 预计容量
- 置信度
- 剩余 TTL
- `REJECTED / WATCH / REVIEW / EXECUTABLE`

默认按风险调整后的预计净利润排序，而不是毛差价排序。

### 11.3 中央市场与解释区

- 市场标题与到期/结算信息
- 盘口 ladder
- 最近成交
- 相关市场或组合腿
- 机会形成原因
- 成本拆解
- 乐观/基准/悲观情景
- 真实可成交深度

### 11.4 右侧执行计划

- 订单腿
- side、price、size、order type、post-only
- 预计费用、滑点、净利润和最坏损失
- 执行顺序与单腿失败处理
- 当前风险预算变化
- `Review` / `Confirm` / `Cancel`

### 11.5 底部工作区

Tabs：

- Orders
- Fills
- Positions
- Strategy PnL
- Expected vs Realized
- Reconciliation

异常必须附着在相关策略、订单或持仓旁边，不建设独立的复杂运维大屏。

## 12. 数据与持久化

V0.2 单机 implementation 使用 SQLite WAL，并保留 append-only execution journal。

最低数据表/记录：

- `market_snapshots`
- `opportunities`
- `strategy_decisions`
- `execution_plans`
- `orders`
- `fills`
- `positions`
- `pnl_attribution`
- `reconciliation_runs`
- `risk_events`
- `strategy_evaluation_windows`

要求：

- 所有事件有单调 sequence。
- 关键记录包含 strategy/config/code version。
- 订单和 fill ID 有唯一约束。
- 写入必须事务化。
- read model 可从 journal 和 authoritative venue snapshot 重建。
- 备份和恢复必须有测试。

## 13. 技术实现约束

### 13.1 推荐栈

- 后端：现有 Python 3.11+ 包。
- 异步 venue 接入：官方 `AsyncSecureClient`，封装在 live adapter 内。
- Web 后端：FastAPI 或等价 ASGI implementation。
- 前端：React + TypeScript + Vite。
- 前端 E2E：Playwright。
- 本地状态：SQLite WAL。

本 SPEC 授权为完成 V0.2 添加上述最小依赖；不得借机引入通用平台、消息总线或分布式基础设施。

### 13.2 建议目录

```text
src/profit_system/
  opportunities/
  strategies/
    arbitrage/
    maker/
    event_driven/
  execution/
  portfolio/
  persistence/
  web/
  cli.py

apps/trade-desk/
  src/

config/v0.2/
  system.json
  strategies/
  risk.json

tests/profit_system/
```

### 13.3 现有代码复用

优先复用：

- `src/edge_lab` 的公共数据、采集、回放、证据与账本思想。
- `src/edge_lab/execution.py` 的现实成交与 Decimal 语义。
- `src/edge_lab/btc_twap_execution_probe.py` 的 plan hash、确认、幂等、对账、收据和 kill 语义。
- `src/client.py`、`src/auth.py`、`src/orders.py`、`src/trading.py` 中经过测试的 SDK、账户读和撤单逻辑。

禁止：

- 直接把 legacy `place_order()` 接到交易台。
- 把现有浅 `OrderSimulator` 当 V0.2 的现实 Paper implementation。
- 把旧 `fill_feed.py` 当正式 user-stream implementation。
- 复制一套新的手续费、tick、余额或 PnL 算法。

## 14. CLI 与运行入口

必须提供一个安装后主命令，例如 `polymm`：

```text
polymm doctor
polymm scan --strategy arbitrage
polymm replay --strategy maker --config ...
polymm shadow --strategy maker --config ...
polymm acceptance
polymm desk
polymm status
```

Live mutation 不提供一个仅靠 `--live` 即可触发的简单开关。Canary 必须读取完整 risk/config、通过 preflight，并要求明确确认。

## 15. Work Packages

### WP0：合同、基线与项目骨架

交付：

- `src/profit_system` 和前端骨架。
- 统一 Decimal、时间、ID、模式和版本类型。
- `VenueAdapter` 合同与 fake adapters。
- 主 CLI 和配置 schema。
- 当前离线测试基线保持通过。

验收：

- 新模块可导入。
- Replay/Paper/Live adapter 合同测试存在。
- legacy live 硬门仍然生效。

### WP1：统一经济模型与机会扫描

交付：

- `OpportunityCandidate` schema。
- 费用、滑点、逆向选择、库存和奖励模型。
- 机会排名与解释。
- JSON/CLI 输出。

验收：

- 每个机会可从固定输入复算。
- 默认排名使用 `tradable_edge` 和预计净利润。
- 未知成本导致拒绝，而不是默认为零。

### WP2：Track A 与 Track B

交付：

- 套利候选生成、组合腿和失败路径。
- Maker 报价、库存 skew、markout 和奖励归因。
- 时间切分回放与压力测试。
- 策略级报告。

验收：

- Research Gate 自动判定。
- 失败/无 edge 的结果可以明确输出 NO-GO。
- 不得为了通过门槛修改冻结阈值。

### WP3：Paper 与 Shadow

交付：

- 现实 Paper adapter。
- 实时 Shadow 运行器。
- expected vs realized-capable 对照结构。
- 连续运行、恢复和证据包。

验收：

- 同一策略配置跨 Replay/Paper/Shadow。
- Shadow 不允许发单。
- Shadow Gate 可机器判定。

### WP4：实盘执行与账本

交付：

- 正式 Polymarket live adapter。
- GTC/GTD/FAK/FOK/post-only/batch/cancel/replace。
- authenticated user stream 和 order heartbeat。
- authoritative reconciliation。
- positions、cash、fees 和 PnL attribution。
- persistent kill。

验收：

- 全部合同和故障注入测试通过。
- 代码支持 Canary，但默认仍关闭真钱 mutation。
- 没有配置和明确用户确认不能提交新单。

### WP5：利润导向交易台

交付：

- 单页工作区。
- 机会、解释、执行计划、订单、成交、持仓和策略 PnL。
- Paper/Shadow/Live 模式明显区分。
- Cancel All 和 Kill。

验收：

- Playwright 覆盖核心路径。
- 页面不自行计算权威金额。
- live 凭据不出现在浏览器 bundle、storage 或网络返回中。

### WP6：Canary 准备

交付：

- `polymm doctor` live preflight。
- fault drills。
- operator runbook。
- Canary config 模板。
- live probe 结果 schema。

验收：

- Canary Gate 可机器判定。
- 未提供外部授权时，系统以 `LIVE_BLOCKED` 完成，并准确列出剩余外部条件。
- 不以自动真钱下单作为普通 CI 或 Codex goal 的完成条件。

## 16. 测试与验证

### 16.1 单元与性质测试

- Decimal 精度和 rounding。
- tick/min-size/negative-risk。
- 费用、奖励和 PnL 守恒。
- opportunity TTL。
- 成本未知时 fail-closed。
- idempotency。
- 状态机允许/禁止跃迁。
- risk limit 和 kill。

### 16.2 合同测试

- 三个 Venue adapters。
- 官方 SDK 返回 shape 和异常分类。
- market/user WebSocket 事件。
- batch 独立成功/失败。
- cancel 与 cancel-all。
- restart reconciliation。

### 16.3 回放与故障注入

- 部分成交。
- 单腿成交。
- reject。
- submit 后 timeout。
- cancel race。
- duplicate/late fill。
- stale quote/book。
- WS 断连和 backfill。
- HTTP 425/429/503。
- heartbeat 失败。
- 进程在 submitting/cancel-pending 时崩溃。
- SQLite 写入中断和恢复。

### 16.4 前端 E2E

- 浏览机会与解释。
- Paper/Shadow 计划。
- Live review/confirm 锁。
- 撤单、全撤和 kill。
- 断连/陈旧/blocked 状态。
- expected vs realized 和策略 PnL。
- 刷新或后端重启后状态恢复。

### 16.5 CI

普通 CI 必须运行：

- dependency lock check
- Python compile
- lint/format check
- typecheck
- offline tests
- frontend build
- frontend unit tests
- Playwright fake/paper E2E
- package build

真实 mutation 测试必须显式 opt-in，永远不能在普通 CI 自动运行。

## 17. 停手条件

### 17.1 策略停手

满足任一条件即降额或停止：

- `realized_net_pnl <= 0` 且达到预注册最小样本。
- edge realization 持续低于阈值。
- adverse selection 超过 spread + confirmed incentives。
- 收益由少数异常值主导。
- 容量提升后 edge 明显衰减。
- 最大回撤或日损越限。
- 对账无法解释。

### 17.2 系统停手

满足任一条件即进入 KILLED：

- 重复或失控下单。
- unresolved ambiguous submission。
- 无法确认活动订单状态。
- 账户资金/仓位与本地账不一致。
- user stream 和 backfill 同时不可用。
- persistent storage 无法安全写入。
- geoblock 或账户状态不允许交易。

### 17.3 范围停手

出现以下需求时不得在 V0.2 顺手扩展，应记录为后续版本：

- 第二个 venue。
- 第二套账户/钱包体系。
- 多用户远程部署。
- Combos/Perps。
- 自动提高资金上限。
- 通用策略插件平台。

## 18. Definition of Done

V0.2 软件实现完成必须同时满足：

- WP0-WP6 代码和文档全部交付。
- Research、Replay、Paper、Shadow 和 Live-Canary-ready 路径存在。
- Track A 和 Track B 的固定可复算验收场景至少各有一条完整走通 Research -> Replay -> Paper -> Shadow；真实候选是否通过 Research/Shadow 盈利门取决于证据，`NO-GO` 也是合法结论。
- 同一策略逻辑跨 Replay/Paper/Shadow，Live 只替换 adapter 和权限。
- Semi-Auto 交易台完整走通机会 -> 计划 -> 确认 -> 订单 -> 成交 -> PnL。
- 所有普通 CI 检查通过。
- 无已知高优先级正确性、安全或资金风险缺陷。
- legacy live 硬门没有被一个布尔值或环境变量绕过。
- 无 live 凭据进入浏览器或日志。
- 无法执行真钱 Canary 时，生成完整 `LIVE_BLOCKED` 报告，而不是伪造完成。

V0.2 策略获得 `LIVE_LIMITED` 资格是另一个证据结果，必须满足 Limited-Live Gate；软件完成不等于策略已经盈利。

## 19. 交付文件

最终至少包含：

- V0.2 代码与测试。
- 交易台前端。
- 配置 schema 与示例。
- Research/Shadow/Canary 报告 schema。
- operator runbook。
- live readiness 报告。
- strategy performance 报告。
- 变更日志和迁移说明。

## 20. 参考

仓库内：

- `README.md`
- `docs/index.md`
- `docs/user/trading-modes.md`
- `docs/developer/architecture.md`
- `STRATEGY_DECISION_2026-08-22.md`
- `src/edge_lab/compatibility.py`
- `src/edge_lab/execution.py`
- `src/edge_lab/btc_twap_execution_probe.py`
- `src/client.py`
- `src/auth.py`
- `src/orders.py`
- `src/trading.py`
- `src/risk/manager.py`

Polymarket 官方：

- https://docs.polymarket.com/trading/place-orders
- https://docs.polymarket.com/trading/manage-orders
- https://docs.polymarket.com/trading/realtime-order-updates
- https://docs.polymarket.com/trading/session-keys
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/trading/market-making
- https://docs.polymarket.com/trading/matching-engine
- https://docs.polymarket.com/api-reference/geoblock
- https://docs.polymarket.com/api-reference/rate-limits
- https://github.com/Polymarket/py-sdk
