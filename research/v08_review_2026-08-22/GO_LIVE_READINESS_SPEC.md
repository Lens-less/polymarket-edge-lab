# BTC 5m/15m 结构对：实盘就绪 spec（2026-08-22 复核版）

> 权威实施合同已并入 `research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md`。本文保留为同日复核笔记；冲突时以 LIVE_COMPLETION_SPEC.md 与 PREREGISTRATION.json 为准。

作者：Claude（本次会话独立复核，非转述）
基准文档：`research/btc_5m_15m_edge_readiness_v08_2026-08-18/`
（`CLAUDE_FINAL_ASSESSMENT.md`、`IMPLEMENTATION_AND_RUNBOOK.md`、`PREREGISTRATION.json`、`STRATEGY_SPEC.md`）

本文档目的：用户要求"即将实盘，给出完整的工程+策略修改方案"。在写方案前，
本次会话直接读取了当前代码（不是转述 8-18 那份评估的文字），发现**代码层面
的进度比 v08 主文叙述的更靠前**。所以本文分两部分：先纠正现状认知，再给出
唯一能合法导向实盘的执行路径。

**不在本文范围内的事**：本文不会、也不应该让你"现在就能下单"。
`compatibility.py` 的硬门是故意设计成任何自动化流程都无法单方面解除的
（见 §5）。本文给的是一条诚实的、按预注册规则执行的路径，包括这条路径
大概率在第一步就把结构线关掉的可能性。

---

## 0. 结论先说

1. **好消息**：v08 §10 记录的三个残留缺陷（A.3b 未传 execution_mode、A.6
   仍要求双侧盘口、A.4 缺 b(t) 诊断）**现在全部已经修好**，而且 Track B
   （双腿挂单 shadow 成交、预注册滚动锁定、离线证据重建）的全部代码也已经
   写完。这不是我猜的，是我今天直接读代码验证的（见 §1 逐项对照）。
   相关测试 `pytest tests/ -k btc_twap`：**479 passed, 1 skipped**。
2. **坏消息（也是真正的瓶颈）**：这些代码**从未在真实数据上跑出过一次
   判定**。`research/btc_5m_15m_edge_readiness_v08_2026-08-18/PREREGISTRATION.json`
   里 `current_decision` 仍然是：
   ```
   "strategy_live": "NO_GO",
   "execution_probe": "NO_GO",
   "reason": "the full-window three-mode Gate 0 report and 200-expiry
              neutral double-maker shadow evidence are not available;
              pUSD, signature, terminal reconciliation, double-maker venue,
              and geoblock checks remain closed"
   ```
   本 checkout 里没有部署主机、没有 41 棵采集树、没有 200 个到期的成交带。
   `IMPLEMENTATION_AND_RUNBOOK.md` 的 "Known evidence gaps" 原话：
   *"No host deployment or four complete new cohorts were available in
   this local Windows session"*。
3. 所以当前"工程问题"已经**不是写代码**，而是：把 Gate 0 和 Track B 的
   代码真正跑起来，得到一个真实数字。这是 1-3 天的部署+运维工作，不是
   新的策略开发。
4. 按 v08 已有的实测（5 个到期、1,017 个盘口样本，正 floor 出现 0 次，
   最好一次成本恰好 = 1.00000）和 §1 的成本恒等式，我对 Track A 重跑
   结果的预期仍然是 **大概率 NO_GO**。这不是我为了拖延给的免责声明，
   是把恒等式和已有实测摆在一起的诚实推断。**请把这条线的"实盘"预期
   设为"验证后大概率体面关闭"，而不是"马上能上"。**

---

## 1. 代码状态更正表（本次实测核实）

v08 附记（`CLAUDE_FINAL_ASSESSMENT.md` §10）里说 A.3b/A.4/A.6 还没做完。
下表是 2026-08-22 直接读代码后的真实状态：

| 项 | v08 (08-18) 说法 | 2026-08-22 实测状态 | 证据 |
|---|---|---|---|
| A.1 结构腿跳过 `[0.05,0.95]` 价格带 | 已完成 | **确认已完成** | `src/edge_lab/btc_twap_pair_pricing.py:279-291`，`structural_only` 分支不再走价格带/价差过滤 |
| A.2 扫描全窗口 tau | 已完成 | **确认已完成** | `PREREGISTRATION.json` `gate_0.tau_override_is_diagnostic_only: true`；builder 按 expiry 取最优点 |
| A.3b 三种执行口径都传 execution_mode | **只做一半，两个调用点仍默认 TAKER_TAKER** | **已修复** | `scripts/build_btc_twap_executable_upper_bound.py:186` `_execution_mode_diagnostics()`，对 `PairExecutionMode` 三个值循环出三档报告，`:815` 输出 `execution_modes` 字典，决策口径固定为 `maker_maker`（与 `PREREGISTRATION.json` 的 `gate_0.decision_execution_mode: "maker_maker"` 一致） |
| A.4 `b(t)` 诊断落盘 | **未做** | **已完成** | 同一函数返回 `split_probability_diagnostics`（`build_btc_twap_executable_upper_bound.py:732-733`）；`PREREGISTRATION.json.gate_0.implicit_split_probability_curve_persisted: true` |
| A.5 回归测试 | 已完成 | **确认已完成** | `tests/test_btc_twap_pair_pricing.py` 存在且通过 |
| A.6 `structural_only` 只校验实际要成交的那一侧 | **未做，仍要求两侧盘口都在** | **已修复** | `src/edge_lab/btc_twap_pair_pricing.py:279-290`：`structural_only` 分支改为按 `_execution_role_orientations(mode)` 逐个方向调用 `_orientation_has_required_sides(...)`，不再对 `best_bid`/`best_ask` 做无条件双侧要求（双侧要求现在只出现在 `structural_only=False` 的旧预测口径分支，`:293-307`） |
| Track B 双腿挂单 shadow 成交 | 未提及具体实现 | **代码已存在，未跑过真数据** | `src/edge_lab/btc_twap_structural_shadow.py`（1,994 行）、`scripts/build_btc_twap_structural_shadow_report.py`（2,244 行）、`src/edge_lab/btc_twap_locked_shadow_v08.py`（885 行，滚动预锁定+收据链）；对应测试 `tests/test_btc_twap_structural_shadow.py`、`tests/test_btc_twap_structural_shadow_report_cli.py` 全过 |
| Track 0.1 官方 SDK 迁移 | 未装 | **已完成** | `IMPLEMENTATION_AND_RUNBOOK.md`: "runtime dependency and generic client calls use official `polymarket-client 0.6.0`" |
| Track C `ProbeVenue` 生产适配器 | 无实现 | **仍然无实现** | `compatibility.py` 里 `repository_v2_adapter_implemented` 被硬编码为 `False`（`compatibility.py:166`），注释明确写"安装 SDK 不能让这项自动转真"；这是 Track C 唯一还没开始的部分 |
| 硬门 `assert_new_orders_disabled` | 无条件抛异常 | **更严格了** | 现在连 `audit` 参数本身都被 `del audit` 丢弃（`compatibility.py:49-63`），连"传一个全绿的审计结果进去"这条路都被显式堵死，注释写"deliberately unable to weaken the stop" |

**结论**：v08 报告里"还需要修的 Gate 0 缺陷"这部分工作已经在 08-18 至
08-21 之间由另一条并行工作线完成并测试通过。不需要为此再规划工程改动。
2026-08-21 的另外 5 个提交（`51ab6a4`…`f2c05cb`）与这条线无关，是对已删除的
通用做市模块 `src/strategy/market_maker.py` 的安全修复，随后该模块被整体
删除（当前 `git status` 里的大量 `D` 就是这次删除，`src/risk/manager.py`
的未提交改动只是移除了专为该模块开的 snapshot 逃生舱，属于无害清理，
我已读过完整 diff 确认）。

---

## 2. 唯一合法能让这条线转正的路径：把 Track A / Track B 真正跑起来

这不是新方案，是 `IMPLEMENTATION_AND_RUNBOOK.md` 里"Deployment order"
已经写好的步骤，本节把它整理成可执行清单，并把每一步的**一票否决阈值**
从 `PREREGISTRATION.json` 里逐字摘出来，放在旁边，避免执行时被悄悄放宽。

### 步骤 1：主机容量必须先达标（否则后面全部数字无效）

`PREREGISTRATION.json.execution_probe_gate.capture_capacity_gate_required`：

```json
{
  "failure_rate_upper_bound_exclusive": "0.05",
  "minimum_free_disk_bytes": 10737418240,
  "maximum_projected_daily_capture_bytes": 1073741824,
  "minimum_available_memory_bytes": 2147483648,
  "burstable_cpu_credit_exhaustion_allowed": false
}
```

即：采集失败率 < 5%、可用磁盘 ≥ 10 GiB、日采集量 ≤ 1 GiB、可用内存
≥ 2 GiB、CPU 突发积分不能耗尽。v08 §4.3 记录的旧实例（t3.small，
CPU credit 跌到 ≈0，磁盘只剩 148 MiB，rawcap 实测 5.3 GB/天超预算 5 倍）
**不满足任何一条**。这是纯运维工作：

- 换到有稳定基线 CPU（非 burstable，或 t3 系列开 unlimited 模式）、
  ≥ 20 GiB 磁盘的实例；
- 按 `IMPLEMENTATION_AND_RUNBOOK.md` 里已经写好的口径，把 rawcap 的全量
  CLOB 帧采集停掉，只保留结构评估需要的 4 个 token book + 官方 TWAP +
  成交带（这条已经在 `SERVICE_CONFIG.json`/`deploy/aws/edge_readiness_v08/`
  的服务单元里体现，不需要重新设计，只需要部署）；
- 确认 SNS topic 有真实订阅（v08 记录订阅数为 0）。

### 步骤 2：部署 v0.8 采集栈，跑通 4 个完整 cohort

用 `deploy/aws/edge_readiness_v08/` 下已有的 systemd 单元部署
`btc_twap_continuous_rtds.py` + 现有 pair capture。验收标准（原文）：

> capture failures <5%, free disk >=10 GiB, retained projection <=1 GiB/day,
> available memory >=2 GiB, and no exhausted burstable CPU credits

以及至少 4 个完整 common-expiry cohort（K15 开盘到 common close 全程
覆盖，四路 token book、双边成交带、费率快照、全部时钟都在）。

### 步骤 3：跑 Gate 0，得到第一个真实判定

```bash
python scripts/build_btc_twap_executable_upper_bound.py \
  --manifest MANIFEST \
  --output /var/lib/poly-mm-v08/reports/gate-0-upper-bound.json \
  --expected-clean-attempts <实际清洁到期数>
```

注意 `IMPLEMENTATION_AND_RUNBOOK.md` 原文特别强调："如果数量不精确等于
预期值，停下来核对差异，而不是改预期数字"——41 这个数字是 v08 时的
历史值，本 checkout 里不存在这批树，**要用新部署产生的真实清洁到期数**，
不要为了凑历史数字而回填。

**一票否决阈值**（`PREREGISTRATION.json.gate_0`）：

```
decision_execution_mode: "maker_maker"
minimum_average_best_total_pnl_per_expiry_usdc: "0.5"
aggregate_upper_bound_not_positive_action: "stop_route"
```

即：双 maker 口径的聚合可执行 PnL 必须 > 0，且平均每个到期 ≥ 0.5 USDC，
否则 `stop_route`——**这条线判负，直接停，不再迭代 veto/tau/lambda**
（`CLAUDE_FINAL_ASSESSMENT.md` §5 原话："不要做的：继续加 veto、调
tau/lambda、提高 MC 路径数……为了凑够 100 笔而放松采集纪律"）。

### 步骤 4：只有步骤 3 是 GO，才进入 200 到期 shadow

用 `btc_twap_locked_shadow_v08.py` 的滚动准入命令（`admit`/`decision`/
`finalize`）持续收集，累计到 `PREREGISTRATION.json.execution_probe_gate.
minimum_neutral_shadow_expiries: 200`，然后：

```bash
python scripts/build_btc_twap_structural_shadow_report.py \
  --input STRUCTURAL_INPUT.json \
  --journal-root /var/lib/poly-mm-v08/data/locked-shadow-v08 \
  --preregistration research/btc_5m_15m_edge_readiness_v08_2026-08-18/PREREGISTRATION.json \
  --output-dir /var/lib/poly-mm-v08/reports/structural-shadow
```

**一票否决阈值**（同一 JSON 的 `execution_probe_gate` 与
`strategy_live_gate`）：

- 中性队列（`passive_wait`）假设下净 PnL 必须 > 0；
- 去掉贡献最大的单个到期后仍 > 0；
- 去掉贡献最大的单一方向后仍 > 0；
- 每个 20-到期滚动窗口都必须 > 0；
- 单到期集中度 ≤ 20%；
- 若要谈"策略可实盘"，还需要连续 14 个 UTC 日、按到期聚合净收益
  ≥ 20 USDC/天，占用本金 ≤ 2,000 USDC。

任一条不满足 → 停，不迭代。

---

## 3. 步骤 1-2 之外、暂时挡在"能不能跑出结论"前面的工程项（P0）

这些是 v08 §4.3 记录、且据 `IMPLEMENTATION_AND_RUNBOOK.md` "Known evidence
gaps" 仍未在真实主机上验证过的项：

1. **主机容量与部署**（见 §2 步骤 1）——目前没有任何已部署主机，这是
   最大的单一阻塞项，且是纯运维工作，无需再设计。
2. **WS 断线处理**：代码层面已经实现了"connection epochs / gaps /
   monotonic timestamps"（`btc_twap_continuous_rtds.py`，见
   `IMPLEMENTATION_AND_RUNBOOK.md` "Implemented surfaces"），但从未在
   真实主机上验证过 CLOB WS 每 30 秒断连一次时的实际行为——这是"待
   在真实部署里验证"，不是"待写代码"。
3. **Linux/Python 3.12 校验**：`IMPLEMENTATION_AND_RUNBOOK.md` 明确写
   "Targeted strict type checks and the portable Windows suite pass.
   Linux/Python 3.12 release validation remains required to exercise the
   real `fcntl` lock and POSIX durability/performance paths that are
   intentionally skipped on Windows"。滚动锁定收据链（`btc_twap_locked_
   shadow_v08.py` 的 O_EXCL + fsync）目前只在 Windows 便携测试下验证过
   语义，真正的 POSIX 文件锁路径没有在目标操作系统上跑过。部署到 Linux
   主机时必须重新跑一遍这套测试。
4. **SNS 订阅确认**（见 §2 步骤 1）。

以上都是"部署+验证"工作，预计 1-2 天，不涉及新的策略或架构设计。

---

## 4. Track C：如果 §2 的两道判定都是 GO，实盘还需要什么

**在 §2 出结果之前不要做这部分**——这是 `CLAUDE_FINAL_ASSESSMENT.md`
§5 的第 4 条优先级，且明确写了"1-2 周且大概率白花"。这里只界定范围，
本次不实现：

| # | 工作 | 说明 |
|---|---|---|
| C.1 | `ProbeVenue` 生产适配器 | `btc_twap_execution_probe.py:892` 定义的 Protocol 目前零实现。需要实现 `assert_authenticated_readiness` / `open_user_event_stream` / `submit_post_only_maker` / `quote_fok_hedge` / `submit_fok_hedge` / `quote_fak_unwind` / `submit_fak_unwind` / `cancel_all` / `reconcile_after_cancel`，全部走双腿挂单形态（不是 v0.8 已排除的 maker+FOK） |
| C.2 | pUSD 抵押品余额/授权对账 | 需要真实凭证 + 真实账户，只能由你本人在拥有凭证的机器上完成，不应该在这类会话里处理原始私钥/凭证 |
| C.3 | 签名路径端到端验证 | 1 笔最小单、立即撤单，验证 `polymarket-client 0.6.0` 的签名与官方 V2 CLOB 兼容 |
| C.4 | 目标主机 geoblock 检查 | 本机当前 `polymarket.com/api/geoblock` 连接超时（v08 §4.2），实盘主机必须先证明这项能通且 `blocked=false` |
| C.5 | 断线即撤单、250ms taker 延迟计入 FOK 重报价窗口 | 代码骨架已在 `btc_twap_execution_probe.py` 里（inert harness），需要接上真实 venue 后验证 |
| C.6 | 保留全部安全门 | 12 USDC 隔离余额、10 USDC 单次买单名义额上限、单到期对、单次挂单、单次对冲、单次紧急平仓、二次新鲜确认（5 分钟过期）、持久 kill switch——这些已经实现（`PREREGISTRATION.json.execution_probe_gate`），Track C 不应该改动它们 |

**关于硬门本身**：`compatibility.py:49-63` 的 `assert_new_orders_disabled()`
现在无条件抛异常，且**连传入的 audit 参数都被丢弃**——这是刻意设计成
"没有任何自动化流程能通过传参数让它放行"。要解除它，唯一合法方式是
`repository_v2_adapter_implemented` 这一项由**人工评审通过的 Track-C
适配器**来拥有发布边界（`compatibility.py:164-166` 注释原话："a reviewed
Track-C adapter owns a provenance-bound release capability"），而不是把
`compatibility_audit()` 的 8 项检查喂成全绿就自动解锁。我不会、也不应该
在没有走完 §2 两道判定、没有你本人提供并操作真实凭证的情况下，代为
实现或解除这一层。

---

## 5. 预注册停手条件（逐字，来自 `PREREGISTRATION.json` 与
`CLAUDE_FINAL_ASSESSMENT.md` §8，不放松）

任一条成立就停，且停的动作是"体面关闭这条线"，不是"调参数再试一次"：

1. Gate 0（双 maker 口径）聚合可执行 PnL ≤ 0，或平均 < 0.5 USDC/到期。
2. 中性队列假设下双腿挂单 shadow 净 PnL ≤ 0。
3. 去掉贡献最大的单个到期后净 PnL ≤ 0。
4. 去掉贡献最大的单一方向后净 PnL ≤ 0。
5. 任一个 20-到期滚动窗口净 PnL ≤ 0。
6. 单到期集中度 > 20%。
7. 采集失败率降不到 5% 以下。
8. `geoblock.blocked ≠ false`，或 pUSD/签名路径无法端到端验证。
9. 实盘阶段单日按到期聚合亏损超过预注册上限（12 USDC 账户隔离上限）。

---

## 6. 建议的执行时间线

| 阶段 | 内容 | 预计耗时 | 产出 |
|---|---|---|---|
| 1 | 主机容量整改 + 部署 v0.8 采集栈 + 4 cohort 验收 | 1-2 天，纯运维 | 一台达标主机，`phase=capturing` |
| 2 | 积累清洁到期数据，跑 Gate 0 builder | 视到期节奏，约 30 分钟一个清洁到期，几天 | 第一个真实三口径 + b(t) 报告 |
| **判定点 1** | 双 maker 聚合 PnL 是否 > 0 且 ≥ 0.5 USDC/到期 | — | **GO / 停** |
| 3（若 GO） | 滚动锁定累积到 200 到期，跑 structural shadow report | 约 2 天（96 个到期/天，需要跨越足够多不同 regime） | 3×3 shadow 报告 |
| **判定点 2** | 中性队列净 PnL、去最优到期、去最优方向、滚动窗口、集中度 | — | **GO / 停** |
| 4（若都 GO） | Track C 适配器 + pUSD/签名/geoblock + 最小实盘探针 | 1-2 周，需要你本人操作凭证 | 可能的、有限额的实盘探针 |
| 5（若判定点 1 或 2 是"停"） | 归档结论，评估是否转向奖励市场方向（需重新预注册） | — | 结项报告 |

---

## 7. 我在本文档中不做的事，以及为什么

- **不会**修改或削弱 `compatibility.py` 的硬门，也不会实现能让它自动
  放行的代码路径——项目的 `CLAUDE.md` 与硬门自身的注释都明确这是刻意
  设计。
- **不会**跳过 §2 的两道判定直接开始写 Track C 适配器——那是"1-2 周且
  按现有证据大概率白花"的工作，且需要你亲自操作真实凭证，不适合在没有
  先出判定结果的情况下投入。
- **不会**把"你想实盘"当成"这条策略能盈利"的证据——恒等式
  （成本 = 1 + b + 执行开销，`b ≥ 0` 由结算规则强制）和已有实测
  （0/1,017 次正 floor）都指向大概率关闭，本文档的诚实定位是"如何
  用你自己预注册的规则把这件事一次性问清楚"，不是"如何绕过规则上线"。

如果你希望，我可以接下来：(a) 帮你把 §2 步骤 1-3 需要的部署脚本/主机
配置整理成可以直接在新 EC2 实例上跑的 checklist；或 (b) 先在本地用
`live_structural_probe.py` 再采一轮新的只读盘口样本，看当前市场状态下
b(t) 是否有变化。两者都不涉及凭证或下单。
