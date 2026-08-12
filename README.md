# Polymarket Edge Discovery Lab

这是一个面向 Polymarket 的公开数据研究与成交感知回放工程。主线不再是“对称挂单后调 spread”，而是独立研究天气概率、跨市场逻辑约束、标准/augmented Neg-Risk、选择性奖励做市和标的价格延迟。

> **截至 2026-07-24：没有找到经过严格认定的回测盈利策略，不建议实盘交易。**
>
> 当前可认证成交为 0，实际 reward payout 为 0。所有新实盘订单仍由
> `assert_new_orders_disabled()` 硬阻断，研究命令不会读取钱包私钥或交易凭证。

最终证据入口：

- [最终报告](research/edge_discovery_2026-07-24/FINAL_REPORT.md)
- [机器可读结果](research/edge_discovery_2026-07-24/BACKTEST_RESULTS.json)
- [指标说明](research/edge_discovery_2026-07-24/METRICS.md)
- [数据清单](research/edge_discovery_2026-07-24/capture_freeze_entity_clock_strict_v3/DATA_MANIFEST.json)
- [实验注册表](research/edge_discovery_2026-07-24/EXPERIMENT_REGISTRY.jsonl)
- [复现说明](research/edge_discovery_2026-07-24/REPRODUCTION.md)
- [实盘就绪审计](research/edge_discovery_2026-07-24/LIVE_READINESS.md)
- [当前官方一手资料](research/edge_discovery_2026-07-24/PRIMARY_SOURCES.md)

## 证据等级

| 等级 | 含义 | 能否称为“已回测盈利” |
|---|---|---|
| L0 | 合成数据和工程诊断 | 否 |
| L1 | 公共快照、历史价格或 shadow/touch | 否 |
| L2 | 自采前向 L2/WS/RTDS 上的 queue-bounded 模拟 | 只有同时通过全部盈利门槛才可以 |
| L3 | 另行授权的真实小额 order/scoring 探针 | 需要用户明确授权 |
| L4 | 真实低风险交易表现 | 需要用户明确授权 |

理论奖励、公开盘口 touch、静态快照套利候选和零延迟假设场景都不是成交，也不会写入已实现 PnL。当前 [TRADES.csv](research/edge_discovery_2026-07-24/TRADES.csv) 只收录满足证据契约的样本外成交；没有合格成交时仅保留表头。

## 已实现的研究基础设施

- 不可变原始数据、批次 manifest、SHA-256、checkpoint 与质量审计
- Gamma/CLOB HTTP、CLOB market WebSocket 和 RTDS 前向 recorder
- `Decimal`/最小单位账本、逐档 FAK/FOK、部分成交与当前 fee curve
- maker 队列上下界、撤单/下单延迟、tick/min-order、stale book
- split/merge、resolution/dispute/invalid、资金占用与单腿风险
- 六类逻辑关系的约束图与标准/augmented Neg-Risk 筛选
- 天气规则/站点/时区/温标/档位解析、forecast vintage 与 whole-event 切分
- reward theory、order scoring 与 payout 三套独立账本
- RTDS/CLOB receive-time lead-lag 描述实验
- 失败实验也写入 experiment registry；最终 bundle 对缺失证据使用 `null + reason`

核心实现位于 [src/edge_lab](src/edge_lab)，薄 CLI 位于 [scripts](scripts)。

## 安装

需要 Python 3.10+。推荐使用 `uv`：

```bash
uv sync --python 3.12 --extra dev
source .venv/bin/activate
```

也可以使用 pip 依赖清单：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

公开研究命令不需要 `.env`。旧程序如需工程回归，应保持 `DRY_RUN=true`；把它改为 `false` 也不会解除新订单硬保护。

## 命令与安全分类

### 纯离线、只读

```bash
# 检查旧 SDK/V2 兼容性和新订单 guard
./.venv/bin/python scripts/run_edge_lab.py audit

# 校验 recorder 配置和 guard，不访问网络
./.venv/bin/python scripts/run_edge_capture.py \
  --config research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json \
  --validate-only

# 校验动态短周期 collector 的 finalized 输入、代理策略和下单 guard
./.venv/bin/python scripts/run_dynamic_short_crypto_capture.py \
  --discovery-dir data/edge_discovery_2026-07-24/raw/clob_market_ws \
  --data-root data/edge_dynamic_short_crypto_2026-07-24 \
  --proxy http://127.0.0.1:7897 \
  --validate-only

# 默认不加载 remediation；仅在显式协调后传入一个 exact 0444 plan
# --lifecycle-remediation-plan lifecycle-remediation-plan-<plan_id>.json

# 从不可变 recorder 数据生成 receive-time 延迟描述报告
./.venv/bin/python scripts/run_latency_capture_experiment.py \
  --data-root data/edge_discovery_2026-07-24 \
  --validate-only

# 只读取固定证据，重建最终 bundle
./.venv/bin/python scripts/build_edge_discovery_bundle.py \
  --research-dir research/edge_discovery_2026-07-24 \
  --output-dir research/edge_discovery_2026-07-24 \
  --report-date 2026-07-24
```

`--lifecycle-remediation-plan <path>` 是显式、单次的 append-only cohort
入口；默认不启用。文件必须是 exact canonical JSON、权限精确为 `0444`，
文件名必须为 `lifecycle-remediation-plan-<plan_id>.json`，且 predecessor
必须等于 finalized recovery journal 的当前 chain tip。该参数不会删除或
替换失败 cohort，也不会解除公开数据、零订单和零认证端点约束。

默认 recovery cache 使用
`edge-lab-dynamic-short-crypto-recovery-snapshot.v2` 与
`edge-lab-dynamic-short-crypto-record-index.v2`。index v2 精确包含
`finalized_record_sources`、`finalized_records`、`replay_records` 和
`recovery_run_inventory` 四张表；旧 v1 descriptor/layout 不会被 v2
validator 接受，而是 fail closed 后执行一次 full replay 重建。snapshot
本身不是信任锚：只有其直接后继 run 中唯一、finalized 的
`restart_recovery_decision` command 携带
`edge-lab-dynamic-short-crypto-recovery-anchor.v1` exact proof 时才可复用，
不得跳过 root、不得分叉。service 必须先 emit 并 checkpoint-finalize 该
proof，随后才可 apply recovery 或启动 registry/Gamma/worker/public
network；持久化失败会中止启动。锚定 cache/suffix hit 只深验只读 index
与直接后继 control pair，不重开或重哈希已锚定 prefix raw/manifests。

### 公开网络，只读采集

下面的命令只访问公开端点，不认证、不签名、不下单。需要本地代理时，只接受无认证的 loopback URL。

```bash
# 有界前向 recorder
./.venv/bin/python scripts/run_edge_capture.py \
  --config research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json \
  --duration 3600 \
  --proxy http://127.0.0.1:7897

# 有界运行动态 BTC/ETH 5m/15m 生命周期 collector
./.venv/bin/python scripts/run_dynamic_short_crypto_capture.py \
  --discovery-dir data/edge_discovery_2026-07-24/raw/clob_market_ws \
  --data-root data/edge_dynamic_short_crypto_2026-07-24 \
  --duration 3600 \
  --proxy http://127.0.0.1:7897

# 天气公开历史 acquisition + L1 shadow 实验
./.venv/bin/python scripts/run_weather_experiment.py \
  --start-date 2026-06-16 \
  --end-date 2026-07-23 \
  --max-events 100 \
  --max-candidates 2000 \
  --model ecmwf_ifs025 \
  --decision-lead-hours 24 \
  --release-delay-hours 4 \
  --market-warmup-minutes 30 \
  --output /tmp/polymarket-weather-public \
  --proxy http://127.0.0.1:7897

# 约束/Neg-Risk：validate-only 不联网；实际运行仍仅 GET 公开端点
./.venv/bin/python scripts/run_constraint_experiment.py \
  --config research/edge_discovery_2026-07-24/CONSTRAINT_STANDARD_NEGRISK_CONFIG.json \
  --output-root research/edge_discovery_2026-07-24/constraint_experiment_runs \
  --validate-only

# 奖励市场：validate-only 不联网
./.venv/bin/python scripts/run_reward_experiment.py \
  --max-markets 25 \
  --validate-only
```

### Shadow 与旧合成诊断

天气公开价格回放、reward 静态份额、约束快照筛选和 lead-lag 都是 shadow/theoretical 证据。旧 mock 回测只用于复现错误与测试账本，不是历史回测：

```bash
./.venv/bin/python scripts/run_backtest.py --mock
```

当前没有任何命令可以把这些结果自动升级为真实回测或实盘策略。

## 前向 recorder

recorder 至少记录：

- Gamma events/markets、CLOB market info、规则和奖励配置
- 完整 L2 snapshot、`price_change`、`last_trade_price`
- `best_bid_ask`、`tick_size_change`、新市场和结算事件
- RTDS 标的价格
- 服务端时间、本机接收时间、连接/session、schema/version

原始批次位于 `data/edge_discovery_2026-07-24/raw/`，checkpoint 位于同一数据根目录。研究目录中的 [FORWARD_CAPTURE_AUDIT.md](research/edge_discovery_2026-07-24/FORWARD_CAPTURE_AUDIT.md) 记录服务、恢复和数据完整性证据。服务持续写入并不等于盈利得到验证。

动态短周期 collector 从主 recorder 的 finalized `new_market` 批次构建累计
registry，并把每次启动隔离到
`data/edge_dynamic_short_crypto_2026-07-24/runs/<run-id>/`。它会严格验证
BTC/ETH 5m/15m 的 slug、token、condition、时间窗和规则身份；尚未激活的
Gamma 市场只会延后重试，不会被永久误拒。当前 sidecar 仍是 fail-closed
采集层：它只从主 recorder 已 finalized 的 RTDS manifest 提取精确
Chainlink 开/收盘边界，并要求 Gamma、token mapping 和同连接 close-time
PONG 一起通过原子 settlement gate；Gamma-only 永不晋级。崩溃重启只从
旧 run 的 finalized journal/checkpoint 恢复，partial 永不进入状态；恢复
决定必须先 durable-finalize，之后才允许状态应用、网络或 worker effect。
恢复器会重建 registry、目标状态、Gamma/Chainlink、deadline、decision
sequence 和 audit-only liveness；当前 registry journal 已改为只保存新输入
delta 的 v2 格式，避免每次扫描重复写入整个历史快照。旧 v1 与混合历史仍
在 manifest、checksum、schema 和 canonical record ID 全部验证后兼容重放。
这些恢复与性能修复不生成成交，也不改变当前 `insufficient_data` 分类。

真实生命周期审计和 replay 装配使用：

```bash
# --as-of-ms 必须固定，保证报告可复算；重复 --dynamic-run-root 可包含历史 run
./.venv/bin/python scripts/build_phase2_lifecycle_report.py \
  --dynamic-run-root data/edge_dynamic_short_crypto_2026-07-24/runs/<run-id> \
  --main-capture-root data/edge_discovery_2026-07-24 \
  --as-of-ms <unix-epoch-ms> \
  --output-base research/edge_discovery_2026-07-24/phase2_lifecycle_reports

# 省略 --slug 只列出 finalized strict candidates；加 --slug 和 --promote
# 才会在本地离线封存并运行 strict counterfactual promoter。
./.venv/bin/python scripts/build_phase2_execution_freeze.py \
  --dynamic-run-root data/edge_dynamic_short_crypto_2026-07-24/runs/<run-id> \
  --main-capture-root data/edge_discovery_2026-07-24 \
  --request-base research/edge_discovery_2026-07-24
```

生命周期 CLI 输出 schema v3，并明确报告 active cohort、active
eligibility boundary 与 retained predecessor failure 数。固定 `--as-of-ms`
报告只计在 cutoff 前已 finalized 的 manifest/record；cutoff 后新增批次以及
当前 `.partial` 集合不会进入 canonical report body 或改变 `report_id`。
remediation failure 和等待成熟均返回非零状态，只有明确通过才返回 0。

这两个入口都保留新订单硬保护，不使用认证端点。fixture 只能验证不变量，
不能满足真实 tracer 或盈利门槛。

## 盈利门槛

`validated_profitable` 默认必须同时满足：

- 真实数据、严格样本外、无未来泄漏
- pessimistic fill 扣除费用、滑点、延迟、对冲和资金成本后为正
- `rewards=0` 仍为正
- bootstrap 95% 净 PnL 下界大于 0
- 至少 100 个独立已结算事件和 100 次可解释样本外成交
- 最大单一事件贡献不超过 20%
- 邻近参数大部分稳定为正
- 单腿尾部损失可接受，且 PnL 可由逐笔明细完全复算

奖励策略还必须有真实 scoring 和多个独立 payout epoch。未满足时只能归入 `promising_not_validated`、`insufficient_data` 或 `rejected`。

## 测试

常规全量测试默认排除显式标记的网络 canary：

```bash
./.venv/bin/python -m compileall -q src scripts
./.venv/bin/python -m pytest -q
git diff --check
```

需要单独验证公开网络时再运行：

```bash
./.venv/bin/python -m pytest -q -m network tests/test_websocket_canary.py
```

具体的最终测试数量、manifest verifier 和逐实验复现命令写在 [REPRODUCTION.md](research/edge_discovery_2026-07-24/REPRODUCTION.md)，避免 README 中的固定测试徽章随新增测试失真。

## 旧做市程序

`run_mm.py`、`run_tui.py` 和 `src/strategy/` 仍保留用于兼容与工程诊断。旧 `real_data_backtest` 实际是当前快照加合成路径，存在强制穿越价差、缺少队列、空头/翻仓记账错误等问题；其结果已被降为 L0，不能作为盈利依据。

## 免责声明

这是高风险交易研究软件，不保证盈利。当前明确结论是：**不建议实盘交易**。
