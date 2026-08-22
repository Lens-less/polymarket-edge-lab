> **已被取代（2026-08-22）：结构线已 STRUCTURAL STOP。** 本文件保留为历史实施合同，不再授权 WP-E2/S0/S1/E0/E1/L。当前权威结论见 [STRUCTURAL_LINE_STOP.md](STRUCTURAL_LINE_STOP.md)。

# BTC 5m/15m 实盘完成规格 v0.9.1

Status: **strategy live NO-GO**; **execution probe NO-GO**.
Date: 2026-08-22
Revision: v0.9.1 adopts the three execution risks in [OPINION_ON_LIVE_COMPLETION_SPEC.md](../../v08_review_2026-08-22/OPINION_ON_LIVE_COMPLETION_SPEC.md).
Authority: 本文件是当前仓库从“纸面就绪代码”走到“允许实盘”的实施合同。
它不放宽 [PREREGISTRATION.json](PREREGISTRATION.json)、[STRATEGY_SPEC.md](STRATEGY_SPEC.md)
或 [CLAUDE_FINAL_ASSESSMENT.md](CLAUDE_FINAL_ASSESSMENT.md) 的停手条件。
它也不把 v0.6 的 +87.52 USDC、公开盘口 touch、或 Gate 0 上界写成已实现利润。

同日复核笔记：
- [GO_LIVE_READINESS_SPEC.md](../../v08_review_2026-08-22/GO_LIVE_READINESS_SPEC.md)
- [OPINION_ON_LIVE_COMPLETION_SPEC.md](../../v08_review_2026-08-22/OPINION_ON_LIVE_COMPLETION_SPEC.md)（已采信，三点风险写入 v0.9.1）
若冲突，以本文件与仍冻结的 v0.8 `PREREGISTRATION.json` 数字门槛为准。
v0.8 的 41 不能改写成“新主机有多少算多少”。v0.9.1 的默认路径是：认定 41 棵树不可恢复，冻结 v0.8 为未完成，另开 v0.9 并在看到任何新结果之前锁死新的 exact count。

本规格回答三件事：

1. 现在到底还缺什么，才能诚实地说“可以实盘”；
2. 每一项工程/策略缺口改哪里、怎么验收、失败后怎么停；
3. 如果结构线被正式判负，实盘路径如何改挂到有真实补贴的奖励市场，而不是偷偷放松当前线。

---

## 0. 一句话合同

**现在不能实盘。** 不是因为还差一个开关，而是因为：

- 结构线的可交易形态（双 maker）还没有通过预注册的经济门；
- 41 个已部署采集树与 200 个中性双 maker shadow 到期样本都不在本 checkout；默认认定 41 棵树不可恢复，不再把“先找回旧树”当作主路径；
- 生产级双 maker `ProbeVenue` 不存在，现有探针仍是 maker+FOK，而 maker+FOK 的成本恒等式为 `1 + b + hedge_fee`，`b ≥ 0` 时恒 > 1；
- `assert_new_orders_disabled()` 必须保持无条件拒绝，直到 Track C 的 provenance-bound 适配器真正拥有放行权。

用户目标是“接下来即将实盘，解决全部工程问题和策略问题”。本规格接受这个目标，但把路径写成 **gated live**：先用修好的 Gate 0 / 双 maker shadow 把结构线正式判完；只有 PASS 才允许最小实盘探针；若 STOP，则结构线永久关闭，另开奖励线预注册。任何跳过 WP-S0/S1/E0 直接下单的改动，都视为违反本规格。

---

## 1. 当前真实状态（2026-08-22 代码复核）

### 1.1 已经完成、不要重做

| 项 | 证据 | 含义 |
|---|---|---|
| A.1 价格带过滤 | `PairPricingPolicy.structural_only=True`，默认与 Gate 0 策略都跳过 `[0.05,0.95]` / 0.03 价差 | 不再把唯一有机会的极端区滤掉 |
| A.2 全窗口扫描 | `build_btc_twap_executable_upper_bound.py` 扫描 `ttc=300..0`，按 expiry 取最优 | 不再只在 tau=240 取样 |
| A.3 / A.3b 三口径 | builder 枚举 `taker_taker` / `maker_taker` / `maker_maker`；readiness 调用点传 `execution_mode` | Gate 0 决策口径是双 maker |
| A.4 `b(t)` | builder 落盘 implicit split diagnostic | 诊断已有代码路径 |
| A.5 回归 | `tests/test_btc_twap_pair_pricing.py` 含双 maker 单侧盘口用例 | 已锁行为 |
| A.6 单侧盘口 | `select_healthy_pair_books(..., structural_only)` 按执行方向校验 bid 或 ask | 不再强制两侧齐全 |
| SDK 迁移 | `polymarket-client==0.6.0`；legacy `py-clob-client` 已从依赖与运行时移除 | Track 0.1 的安装项已完成 |
| 通用客户端 | `src/client.py` / `src/auth.py` / `src/trading.py` 已接到官方 SDK | 这是底座，不是策略适配器 |
| 旧做市机器人删除 | `src/strategy/`、`src/tui/`、`run_mm.py` 已删除；WP-H0 卫生提交绑定本规格 | 不要重建 |
| 纸面双 maker | `btc_twap_structural_shadow.py` + report builder + rolling journal | 可跑，但缺真实 200 expiry 数据 |
| 容量门代码 | `evaluate_capture_capacity()` 与 v0.8 watcher 配置 | 代码在，主机证据不在 |

v08 评估 §10 里写的 A.3b / A.6 / A.4“还没做”，**已被后续提交超前完成**。本规格不再把它们列为 P0 代码任务。

### 1.2 仍然阻塞实盘的事实

| ID | 问题 | 级别 | 为什么不能靠改一行代码解决 |
|---|---|---|---|
| S0 | 41 棵部署采集树不在本 checkout，正式 Gate 0 报告无法生成 | P0 策略 | 缺证据。count ≠ 41 只能 `RERUN_REQUIRED`，不能改期望值 |
| S1 | 没有 ≥200 个 unique common expiry 的中性双 maker shadow | P0 策略 | 纸面验证未完成；现有 5 expiry / 1,017 盘口只是指示，不是停手证明也不是放行证明 |
| S2 | 实测最好 floor = 0.00000，扣费后 −0.00007；乐观上界约 $29/天，商业门槛是 14 个 UTC 日每天 ≥ $20 | P0 策略 | 算术 + 市场定价。双 maker 需要 `b < 价差之和`，临近到期价差也塌缩 |
| E0 | 生产双 maker venue 不存在；`ProbeVenue` 只有 Protocol；现有 harness 仍是 1 maker + FOK | P0 工程 | 即使用官方 SDK 能下单，当前探针形态在经济上无资格 |
| E1 | `compatibility.py` 八项里 `repository_v2_adapter_implemented` 被硬编码 `False`；认证三项必须来自授权探针；本机 geoblock 曾超时 | P0 工程 | 布尔映射不能放行；硬门必须保持 |
| E2 | 采集失败率曾 93.3%；t3.small CPU credit ≈0；磁盘距软停 148 MiB；rawcap 5.3 GB/天 | P0 运维 | 统计与探针晋级全部停 |
| E3 | SNS topic 订阅数为 0；CLOB WS 约 30s 断连 | P0 运维 | 没人收到告警；实盘必须断线即撤单 |
| H0 | 通用做市删除与文档/安全测试同步已提交 `dd10709` | P1 卫生 | 不要重建 `src/strategy/` |
| H1 | Windows 全量测试有环境失败；Linux/Python 3.12 才是发布验证 | P1 | `fcntl`/POSIX durability 在 Windows 被有意跳过 |

### 1.3 冻结的负证据

以下内容 **永远不能** 被重新解释为盈利或实盘资格：

- v0.6 shadow +87.52 USDC：未校准 raw-model 纸面 PnL；单 expiry 48.5%；Brier 输给市场；t=1.46
- maker+FOK / 双 taker 的任何正数：不是 live-eligible 形态
- 奖励理论分、公开成交带 touch、静态快照套利候选
- Gate 0 正上界：只是乐观 hindsight，不能计入 locked-OOS

---

## 2. 目标与非目标

### 目标

把仓库从当前 NO-GO 推进到下面三种终态之一，且必须留下机器可读证据：

1. **Structural STOP**：Gate 0 双 maker 聚合 PnL ≤ 0 或 < $0.5/expiry，或中性 shadow 净 PnL ≤ 0。结构线关闭，禁止继续调参。
2. **Structural PROBE**：Gate 0 PASS 且 200-expiry 中性 shadow 全部门过，才允许 12 USDC 隔离探针。
3. **Structural LIVE**：探针后再满足 200 个清洁到期、200 次可解释成交、bootstrap 95% 下界 > 0、集中度 ≤ 20%、14 个 UTC 日每天 ≥ $20、占用本金 ≤ $2000。
4. **Pivot PRE-REG**：若 1 发生，另开奖励市场预注册；在新预注册完成前不得把奖励当结构线的后备自动交易。

### 非目标

- 不恢复 `SmartMarketMaker` / TUI / 全站选市场挂价差。
- 不把 `DRY_RUN=false` 当成实盘解锁。
- 不把八项 compatibility 布尔数组当成放行权。
- 不提高 MC 路径数、不逆向工程 Chainlink、不为凑 100 笔放松采集纪律。
- 不在 Windows checkout 上伪造 41/200 证据。
- 不把奖励收入算进 `rewards=0` 仍须为正的结构门槛。

---

## 3. 决策树（唯一允许的实盘顺序）

```
WP-H0 卫生提交
    │
    ▼
WP-E2.0 历史失败分类（采购新主机之前）
    │ 若主因是脚本 bug 而不是容量 → 先修代码，再买机器
    │ 若修完仍把 40% 失败当成策略问题 → 违规
    ▼
WP-E2 主机容量 + SNS + compact capture
    │ 失败率≥5% / 磁盘<10GiB / 投影>1GiB/天 / 内存<2GiB / CPU credit 耗尽
    │ → STOP 全部统计与探针
    ▼
WP-S0 冻结 v0.8，开 v0.9，锁死 exact count 后再采再跑 Gate 0
    │ 禁止把事后数出来的清洁到期数填进 expected count
    │ maker/maker 聚合≤0 或均值<$0.5/expiry → Structural STOP → WP-P0
    ▼
WP-S1 锁定 inclusion policy，连续采集到 ≥200 unique clean expiry
    │ 中性队列 + passive_wait 净 PnL≤0
    │ 或去最好 expiry / 最好方向 / 任一 20-expiry 窗 ≤0
    │ 或单 expiry 集中度>20%
    │ → Structural STOP → WP-P0
    ▼
WP-E0 双 maker ProbeVenue + 探针改写 + 故障演练
    │ geoblock.blocked≠false / pUSD 对账失败 / 签名或终态失败
    │ → STOP，不解除硬门
    ▼
WP-E1 最小实盘探针（12 USDC / 10 USDC 名义 / 单对 / 人工二次确认）
    │ 单日聚合亏损超预注册上限 → 永久 kill
    ▼
WP-L  策略实盘门槛（14×$20/天，资本≤$2000，L2 八项全过）
    │ 未过 → 保持探针或停
    ▼
LIVE
```

**平行禁止**：WP-E0/E1 不得与 WP-S0/S1 抢跑。没有 PASS 的 Gate 0 报告和中性 shadow 报告，禁止创建可下单的 systemd unit，禁止把 `assert_new_orders_disabled()` 改成“八项全 true 就放行”。

---

## 4. 工作包

每个工作包都是垂直切片：有输入、改动面、命令、验收、失败动作。实施时一次只做一个包。

### WP-H0 仓库卫生（先做，不触及实盘）

**目的**：让后续证据绑定到干净、可复核的 HEAD。

**改动**

- 审查并提交已删除的通用做市代码与文档/安全测试同步（当前约 50 files, +149/−12,248；以提交时 `git diff --shortstat` 为准）。
- 提交信息遵循 Lore protocol，intent 是“避免把无关做市机器人误认为策略入口”，不是“准备实盘”。
- 不提交 `.omx/`、`task_plan.md`、`findings.md`、`progress.md`、本地规划草稿。
- `research/v06_review_2026-08-15/` 与未跟踪的部署报告：只提交有 SHA 绑定、不重复已有结论的文件；大原始数据继续留在 `/data/`。
- 更新过时 `docs/index.md` / `docs/roadmap.md`：删除“v1.0 核心做市已实现”的虚假路线图，改为指向本规格与 v08 评估。

**验收**

- `git status` 在策略/安全相关路径干净。
- `pytest -q -k "btc_twap or compatibility or sdk"` 在当前环境通过。
- README / CLAUDE.md 仍写 **不建议实盘**，并链接本文件。

**失败动作**：有争议的安全测试删除先不提交，开清单，不带着脏树做 WP-E0。

---

### WP-E2.0 历史失败分类（采购或扩容之前，强制）

**目的**：避免把 93.3% 失败率只当成“机器太小”，修完容量后仍卡在 40% 却误判成策略问题。

已有证据（`research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/DEPLOYMENT_REPORT.md`）：

- 源采集失败率 `14/15 = 93.3%`。
- 最近检查的失败都是两条 `RecorderWorkerError` 腿，manifest/raw 完整性数组是干净的。
- 摘要故意消毒了底层 recorder error code，**当时报告明确说没有唯一被证明的软件根因**。
- 高度相关的主机风险：`t3.small` CPU credit 近 0（约 `0.000068`）、利用率钉在 T3 baseline ~20%、credit 模式 `standard`、磁盘距 15 GiB 软停约 148 MiB。
- 同期修复把它复现为：共享持久化回压 + CPU credit 耗尽；V0.7 每次刷新还重读重哈希全部 rejected source tree，成本随失败数增长。停 rawcap、提前压缩、给 rejected-tree 做 cache 后，隔离验收周期能跑出 `capture_error = null`。
- 因此当前最强假设是 **容量/回压相关**，但因为 error code 被消毒，不能跳过分类直接买机器。

**必须先做的一小时诊断**

1. 收集已有 capture-summary / attempt receipt / DEPLOYMENT_REPORT，按 `error_type` + `error_code` 计数。
2. 至少分成四桶，禁止把无法分类的失败丢进“容量”：
   - `capacity_backpressure`：CPU credit 耗尽、磁盘软停、共享盘回压、被消毒但仍与主机饱和同时出现的 recorder 腿失败；
   - `window_miss`：开盘后才开始采、错过 K15→close、future-opening 调度错误；
   - `protocol_or_parser`：WS/REST 响应格式、schema、clock anomaly、非容量的 recorder crash；
   - `expected_evidence_gap`：规则/边界缺口等应记 dirty 但不该算脚本 bug。
3. 写出 `research/btc_5m_15m_edge_readiness_v08_2026-08-18/CAPTURE_FAILURE_TAXONOMY.md`（或 v0.9 目录等价物），包含计数、代表 receipt 路径、以及“容量修复是否对准主因”的是/否。
4. 主因是 `protocol_or_parser` 或 `window_miss`：先修代码/调度，再买机器。
5. 主因是 `capacity_backpressure`：才进入 WP-E2 主机整改。新主机上必须停止消毒 error code，capture-summary 保留 `RecorderWorkerError.error_type` 与 `error_code`。

**验收**

- 分类覆盖全部已检失败样本，未知桶 < 10%。
- 明确写出主因桶，以及为什么换实例能或不能把失败率从 93.3% 打到 < 5%。
- 未完成分类之前，禁止把 WP-E2 标成 in_progress 的采购完成。

**失败动作**：如果新主机把失败率只降到例如 40%，这是采集问题，不是 Gate 0 经济 STOP。回头修主因，不准进入 WP-S0 的 PASS/STOP 判决。

### WP-E2 采集容量与告警（结构线和奖励线的共同前置）

**目的**：让任何统计结论的分母可信。失败率 ≥ 5% 时，后面所有数字无效。WP-E2.0 未完成不得开始本包的采购。

**主机**

- 停用或迁走已经 CPU-credit 耗尽的 `t3.small` 上的旧 recorder 堆叠。
- 新实例最低：非抢占、非耗尽型 credit（或无限 credit 的非 burstable），可用内存 ≥ 2 GiB，`/var/lib/poly-mm-v08` 可用磁盘 ≥ 10 GiB，日增量投影 ≤ 1 GiB。
- 禁止在同一饱和盘上再挂 rawcap 全量帧。rawcap 保持 maintenance-only。

**采集策略（已写在 SERVICE_CONFIG，必须真正跑起来）**

- 只留配对四 token、30s 全深锚、5s 公开 taker poll、top-of-book 变化、官方 60s RTDS。
- `persist_raw_clob_frames=false`，`persist_reconstructed_full_depth_frames=false`。
- 单腿 recorder 失败 → 整个 cohort dirty，不准插值。
- 连续 RTDS：K15 开盘前开始，贯穿 K5 开盘与 common close；gap > 2s 或 disconnect → dirty。

**告警**

- 创建 SNS topic，至少一个 **Confirmed** 订阅（邮箱或短信）。
- 实例角色：`sns:Publish` 到该 ARN，`cloudwatch:GetMetricStatistics`。
- bootstrap 必须因 `POLYMM_SNS_TOPIC_ARN` 缺失或订阅仍 `PendingConfirmation` 而失败。
- 把 `deploy/aws/watch/watch-config.json` 的 `sns_topic_arn` 写成部署产物，不把真实 ARN 提交进 git；git 中保留空字符串 + 文档。

**CLOB WS**

- 纸面轨道：断连记 dirty，全量 REST 重拍可以，但要计入失败率。
- 实盘轨道（仅 WP-E0 之后）：市场 WS 或用户 WS 断连必须 `cancel_all`，禁止“先重连再决定是否撤单”。
- 当前约 30s 一次断连若在新主机上复现，先当作容量/网络问题修；修不好则探针不可启用。

**命令**

```text
# 目标主机，安装 v0.8 只读单元，不要启用任何 order path
# 见 deploy/aws/edge_readiness_v08/README.md

python scripts/check_btc_twap_continuous_rtds.py
python scripts/check_btc_twap_relative_value_service.py --config SERVICE_CONFIG.json --maximum-heartbeat-age-seconds 90
```

**验收（机器门，缺一不可）**

`evaluate_capture_capacity()` 对绑定 artifact 返回 `passed=true`：

- `capture_failure_count / capture_attempt_count < 0.05`
- `free_disk_bytes >= 10737418240`
- `projected_daily_capture_bytes <= 1073741824`
- `available_memory_bytes >= 2147483648`
- `burstable_cpu_credit_exhausted is false`
- 连续 4 个 common-expiry cohort 完整：官方 RTDS + 四 token L2/全深锚 + 两边公开成交 + fee/rule + 三时钟

**失败动作**：停止所有统计/探针晋级。不准用更短窗口“看起来健康”来覆盖。

---

### WP-S0 正式 Gate 0（结构线第一判决）

**目的**：用预注册的机器判定回答“双 maker 上界有没有钱”。这是采集达标后的本地/主机作业，不是新模型。

**默认决定（v0.9.1）**：认定 41 棵树不可恢复。那台部署机就是 CPU credit 归零、磁盘只剩约 148 MiB 的 `t3.small`，找回旧树不得阻塞 WP-H0 / WP-E2.0 / WP-E2。v0.8 冻结为 `code_complete_runtime_evidence_pending` / Gate 0 未完成。主路径改走 **v0.9 重新计数**。

**如果之后真的找到 41 棵树**：只能当作独立对照数据集。禁止临时改口“就用这些顶替 v0.9”。对照报告不得授权探针。

**v0.9 在看到任何新结果之前必须锁死**

- 新目录，例如 `research/btc_5m_15m_edge_readiness_v09_<date>/`。
- 新 `PREREGISTRATION.json`，数字门槛继承 v0.8：maker/maker、均值 ≥ $0.5/expiry、200 expiry、20% 集中度、14×$20、资本 ≤ $2000、失败率 < 5%。
- `expected_unique_common_expiry_attempts` 必须在第一笔新 capture 之前写成一个 **exact 正整数**。推荐默认：`4` 个完整健康 cohort 作为容量证明之后，再锁一个前瞻 Gate 0 窗口，例如接下来 48 个 unique common expiry（约 12 小时边界 × 清洁率缓冲），或明确写成“满 48 个清洁 unique expiry 才允许经济判决”。
- 禁止用“新主机事后有多少清洁到期”回填 expected count。count 不匹配只能 `RERUN_REQUIRED`。
- v0.8 的 41 仍然不能被改写成别的数字。

**输入**

- 默认：v0.9 主机上、按已锁 expected count 采满的 finalized capture tree。
- 对照（可选，不阻塞）：若找回旧 41 棵树，用 v0.8 命令另出一份不可授权的报告。
- 精确命令（不得加 `--decision-tau-seconds`，不得用预计算 fill-cap 摘要作为权威）：

```text
# v0.8 对照路径（仅当 41 棵树真的找回；不能授权）
python scripts/build_btc_twap_executable_upper_bound.py \
  --manifest MANIFEST \
  --output /var/lib/poly-mm-v08/reports/gate-0-upper-bound-41.json \
  --expected-clean-attempts 41

# v0.9 主路径（N 必须等于预注册里预先锁死的 exact count）
python scripts/build_btc_twap_executable_upper_bound.py \
  --manifest MANIFEST \
  --output /var/lib/poly-mm-v09/reports/gate-0-upper-bound.json \
  --expected-clean-attempts N
```

**规则**

- 扫描 `ttc=300..0`，三口径都要落盘，决策口径只有 `maker_maker`。
- maker 数量上限必须从该 expiry 决策之后的公开成交带由 builder 重算；调用方塞进来的 fill cap 只能当诊断。
- 结构方向必须与 strike 一致；非 floor 方向只做 hindsight 诊断。
- `b(t)` 与 `b<0` 计数必须进报告。
- v0.8 对照：count ≠ 41 只能 `RERUN_REQUIRED`，禁止改 41。
- v0.9 主路径：count ≠ 预锁 N 只能 `RERUN_REQUIRED`，禁止改 N。
- 禁止把新主机事后数出来的清洁到期数填进 `--expected-clean-attempts`。

**判决**

| 结果 | 动作 |
|---|---|
| `decision=PASS` 且 maker/maker 均值 ≥ 0.5 USDC/expiry | 进入 WP-S1 |
| 证据完整且聚合 ≤ 0 或均值 < 0.5 | **Structural STOP** → WP-P0 |
| 证据不完整 | 停，修采集，不算经济失败 |

**预期**：按 2026-08-18 的 5 expiry / 1,017 样本，PASS 的先验很低。本包的价值是把这条线正式关掉或罕见地打开 WP-S1，而不是“再优化一版模型”。

---

### WP-S1 200-expiry 中性双 maker shadow

**目的**：把 Gate 0 的乐观上界变成队列有界的纸面成交。没有本包，探针代码写了也不能启用。

**采集**

- 在 WP-E2 主机上跑满 K15→close 的 compact pair capture + 连续官方 RTDS。
- 用 rolling journal **在 track 开始前**锁定 inclusion：

```text
python scripts/run_btc_twap_locked_shadow_v08.py init --journal-root ... --policy ...
python scripts/run_btc_twap_locked_shadow_v08.py admit ...
python scripts/run_btc_twap_locked_shadow_v08.py decision ...
python scripts/run_btc_twap_locked_shadow_v08.py finalize ...
```

- 第一次 admitted attempt 永久有效；更干净的重采不能替换。
- dirty / no-trade / no-fill / failed 全部留在分母，PnL=0。

**回放**

```text
python scripts/build_btc_twap_structural_shadow_report.py \
  --input STRUCTURAL_INPUT.json \
  --journal-root /var/lib/poly-mm-v08/data/locked-shadow-v08 \
  --preregistration research/btc_5m_15m_edge_readiness_v08_2026-08-18/PREREGISTRATION.json \
  --output-dir /var/lib/poly-mm-v08/reports/structural-shadow
```

- 中性行永久是 `passive_wait` + `QueueScenario.neutral`。
- `taker_hedge` 与 `FAK_unwind` 只是敏感性行，不能事后选来过门。
- builder 必须从 finalized CaptureStore 重推导 book/trade/outcome；调用方漏字段或发明字段 → reject。

**验收**

同时为真才进入 WP-E0：

- unique common expiry ≥ 200
- 中性 realized net PnL > 0
- 去掉最好 expiry > 0
- 去掉最好方向 > 0
- 每个注册的 20-expiry rolling window > 0
- 单 expiry 集中度 ≤ 0.20
- receipt chain verified，所有 admitted cohort 都在分母里

**失败动作**：**Structural STOP** → WP-P0。禁止改 disposition、改队列假设、把 maker+FOK 敏感性行提升为中性行。

---

### WP-E0 双 maker 生产适配器（仅 S0+S1 PASS 后实现）

**目的**：给结构线一条物理上能下单、经济上合法的通道。当前仓库缺的不是 SDK，而是策略形态。

#### E0.1 重写探针，废除 maker+FOK 作为授权形态

文件：

- `src/edge_lab/btc_twap_execution_probe.py`
- 新增 `src/edge_lab/btc_twap_double_maker_probe.py`（推荐，避免把恒 >1 的旧 harness 改成“有时双 maker”）
- 新增 `scripts/run_btc_twap_double_maker_probe.py`
- 测试：`tests/test_btc_twap_execution_probe.py` 的旧用例保留为 **ineligible harness**；新增双 maker 测试集

必须改变的语义：

| 旧 harness | 新双 maker probe |
|---|---|
| 两候选 = 两条腿谁做 maker、谁做 FOK | 一个 plan = 两腿同时 post-only 挂 bid |
| `maker_submissions_max=1` | `maker_submissions_max=2` 且 `simultaneous_open_maker_orders_max=2` |
| 一腿成交后 FOK 对冲 | 中性路径继续等第二腿；超时/断开才允许预注册的紧急 FAK 平仓 |
| `all_in ≤ 0.99` 挂在 maker+FOK 上 | 只挂在双 maker all-in 上 |
| 无生产 adapter | 有 provenance-bound `ProbeVenue` 实现 |

旧 `ExecutionProbeHarness.execute()` 保持可单测，但 `evaluate_execution_probe_readiness` 继续拒绝它：`double_maker_probe_implemented` 必须指向新代码摘要。

#### E0.2 `ProbeVenue` 生产实现

新增 `src/edge_lab/venue_polymarket_v06.py`，只用已安装的 `polymarket-client==0.6.0`。

SDK 已核实的原语：

| 需要的动作 | SDK 方法 |
|---|---|
| 签名双腿限价单 | `SecureClient.create_limit_order(..., post_only=True)` |
| 一次提交两笔 | `SecureClient.post_orders(signed_orders)` |
| 单笔后备 | `place_limit_order(..., post_only=True)` |
| 紧急平仓 | `place_market_order(..., order_type='FAK' 或 'FOK')` |
| 撤单 | `cancel_order` / `cancel_orders` / `cancel_all` |
| 余额授权 | `get_balance_allowance(asset_type='COLLATERAL'|'CONDITIONAL')` + `setup_trading_approvals()` |
| 终态 | `get_order`、`list_account_trades`、`wait_for_order_fill_settlement` |
| 奖励分（探针禁用） | `get_order_scoring` 只读，不得把 scoring 当 PnL |

实现约束：

- 适配器 **不** 读取环境变量里的私钥来把 compatibility 三项变绿。认证证据必须由独立、计量、人工授权的 probe 脚本写出 artifact。
- `post_orders` 不是原子成交：一腿接受一腿拒绝必须立即 `cancel_all` 并 kill。
- 禁止 GTC 非 post-only。禁止 marketable limit 冒充 maker。
- Decimal 全路径；禁止 float 价格。
- 用户成交流：不要用 `src/feed/fill_feed.py` 的 `Bearer POLY_API_KEY` 旧通道，除非用真实账户 payload 证明它仍是官方 user WS。默认应基于官方 SDK 的账户 trade/notification + 文档化的 user WS，并在 `scripts/probe_account_trade_semantics.py` 的人工只读捕获之后锁定字段语义。
- 断线、拒绝、ambiguous ack、restart 时未对账的 in-flight 单 → persistent kill switch。
- 250 ms taker delay（`itode=true`）只进入紧急 FOK/FAK 重报价，不进入中性双 maker 路径。

#### E0.3 把八项检查变成 provenance，而不是改硬门捷径

保持 `assert_new_orders_disabled()` **无条件抛异常**，直到本包的放行对象存在。

允许的最终形态（只能在 WP-S0/S1 PASS 且本包验收后的单独 commit 里做）：

- 新增 `LiveReleaseCapability`：绑定 host id、adapter 代码 SHA、probe plan hash、Gate 0 报告 SHA、shadow 报告 SHA、geoblock 收据、pUSD 收据、签名演练收据、终态演练收据、fault-drill 收据。
- `assert_new_orders_disabled()` 改为：没有这个 capability 对象就抛；有则校验新鲜度（建议 ≤ 5 分钟）与 SHA 全匹配。
- 明文布尔 `audit["checks"]` 仍然 **不能** 放行。现有测试 `test_live_order_boundary_cannot_be_released_by_a_boolean_mapping` 必须继续红/绿锁死这一点。
- `repository_v2_adapter_implemented` 只有在 adapter 文件 + 测试摘要写入 `btc-twap-probe-adapter-evidence.v1` 且 `double_maker_probe_implemented=true` 时才能变 true。

需要的证据 artifact schema（已在 readiness 中预留）：

- `btc-twap-authenticated-read-evidence.v1`
- `btc-twap-fill-stream-evidence.v1`
- `btc-twap-fault-drills-evidence.v1`
- `btc-twap-probe-adapter-evidence.v1`
- `btc-twap-capture-capacity-evidence.v1`
- `btc-twap-service-health-evidence.v1`

#### E0.4 故障演练清单（全部要收据）

`REQUIRED_FAULT_DRILLS` 已冻结：

1. `partial_fill`
2. `disconnect`
3. `reject`
4. `cancel_race`
5. `duplicate_and_late_fill`
6. `fok_depth_shortfall`（仅紧急路径；中性路径不应走到 FOK）
7. `emergency_unwind`
8. `persistent_kill_switch`

另外必须加双 maker 特有演练（写入同一 artifact 的扩展字段，不删除上面八项）：

9. `one_leg_accept_one_leg_reject`
10. `one_leg_fill_other_resting`
11. `user_ws_disconnect_cancels_both`

**验收**

- 无凭证单元测试覆盖计划构造、双提交、单腿失败撤单、kill switch、二次确认过期。
- 适配器测试对 SDK 方法签名锁合同（延续 `tests/test_polymarket_sdk_migration.py`）。
- readiness：`evaluate_execution_probe_readiness` 在缺少任一 artifact 时 `eligible=false`。
- 目标主机 `compatibility_audit()`：`legacy_v1_dependency_absent` 与 `current_sdk_installed` 为 true；其余六项只有在对应收据存在时为 true。
- `geoblock_request_ok` 且 `blocked is false`。本机超时不能用代理假造成功；必须在实盘主机上证明。
- **没有** 任何 systemd unit 默认启用探针。

**失败动作**：硬门保持。禁止用 `src/trading.py:place_order` 的通用 post-only 单腿路径“先实盘看看”。

---

### WP-E1 最小实盘探针（人工门）

**目的**：验证执行与对账，不是赚钱。最大账户损失 12 USDC。

**硬限制（已预注册，不得放宽）**

- 隔离余额 ≤ 12 USDC
- 累计买单名义额 ≤ 10 USDC
- 同时只有 1 个 common-expiry pair
- 同时最多 2 张 post-only maker
- 紧急 FAK 平仓最多 1 次
- 计划 hash 绑定的损失确认 + 5 分钟内二次确认；nonce/短语不明文落盘
- 用户必须显式接受 “I ACCEPT ACCOUNT MAX LOSS 12 USDC”

**运行时**

- 只在 WP-E0 capability 有效时，由人工启动 `scripts/run_btc_twap_double_maker_probe.py`。
- 先跑 4 个完整 cohort 的只读发现，确认实时能生成与 strike 一致的双 maker plan，再允许一次提交。
- 每个 UTC 日按 expiry 聚合亏损超过预注册上限 → 永久 kill，需要新的预注册才能再开。

**验收**

- 真实成交或明确的 no-fill 都写入 hash chain receipt。
- 对账：CLOB 终态、conditional balance、pUSD、本地账本四者一致。
- 无凭证泄漏进 git / 报告。

本包 **不能** 把策略状态改成 LIVE。

---

### WP-L 策略实盘门槛

只有 WP-E1 之后，且 `evaluate_strategy_live_readiness_inputs()` 全绿：

- ≥ 200 个清洁、预标记、已结算 common-expiry cohort
- ≥ 200 次可解释结构经济尝试（含 no-trade/no-fill 分母）
- 单侧 95% cluster-bootstrap 均值下界 > 0
- 单 expiry 集中度 ≤ 20%
- 真实费用/滑点/延迟/对账完整
- 连续 14 个 UTC 日净 PnL ≥ 20 USDC/天
- 峰值占用本金 ≤ 2000 USDC
- 服务持续健康
- 预测轨道保持 action-disabled，计数不得混入

L2 八项盈利门槛继续适用（真实数据、严格 OOS、pessimistic fill、`rewards=0` 仍正、bootstrap、样本量、集中度、单腿尾部）。缺一项就只能是 `promising_not_validated`。

---

### WP-P0 结构线关闭后的奖励线预注册

**触发**：WP-S0 或 WP-S1 判负，或用户提前接受结构线死亡。

**禁止**：把当前 BTC 5m/15m 适配器、探针限额或 v08 journal 直接复用到奖励市场。必须新开 track。

**已知事实（2026-07-24 / 2026-08-18）**

- BTC 5m/15m 的 `clobRewards = null`，结构线没有补贴。
- 全站奖励池曾观测单页 500 市场合计约 $7,577/天，头部约 $1,667/天。
- 2026-07-24 结论：单腿逆向选择通常大于一次完整配对毛利；只有“低流量低竞争、近零成交的奖励采集”还值得观察。Ankara 25°C 曾像这一类，但覆盖只有 ~3.4 小时。
- 奖励理论分不是 fill；`rewards=0` 仍须单独过门。若策略依赖补贴，必须在预注册里写成 **subsidy-dependent**，并另设“奖励配置被下调/取消即停”的 kill。

**新预注册最小内容**

1. 市场筛选：`clobRewards` 非空、min_size/max_spread 可执行、竞争代理低、应急 hedge 深度足够。
2. 执行：完整集双边 maker，禁止 naked 单 token MM。
3. 证据：自采 WS+REST ≥ 7 天、多市场多时段；reward=0 / rebate=0 / 2×p95 延迟滑点 / 去掉最好一天 / 去掉最好市场 / 单腿不利。
4. 工程：复用 WP-E2 主机与官方 SDK，但 venue 必须支持 split/merge、order scoring、reward epoch 对账。
5. 在以上 PASS 之前，奖励线同样 NO-GO。

相关旧证据：`research/profit_redesign_2026-07-24/REDESIGN_AND_RESULTS.md`、`research/edge_discovery_2026-07-24/FINAL_REPORT.md`。

---

## 5. 停手条件（写死，不可事后放宽）

任一成立即停当前 track：

1. 正式 Gate 0（v0.8 对照的 41，或 v0.9 预锁的 N）双 maker 聚合 PnL ≤ 0，或平均 < $0.5/expiry。
2. 中性双 maker shadow 净 PnL ≤ 0。
3. 去掉贡献最大的单个 expiry 后净 PnL ≤ 0。
4. 去掉最好方向或任一 20-expiry 窗后净 PnL ≤ 0。
5. 采集失败率不能降到 5% 以下，或容量门任一失败。
6. `geoblock.blocked ≠ false`，或 pUSD / 签名 / 终态不能端到端验证。
7. 实盘阶段单日按 expiry 聚合亏损超过预注册上限。
8. 发现 maker+FOK 被重新标成 live-eligible。
9. 发现硬门被布尔 audit 或 `DRY_RUN=false` 绕过。

---

## 6. 代码与文档改动清单（按包）

| 包 | 主要文件 | 测试 |
|---|---|---|
| H0 | `CLAUDE.md`, `README.md`, `docs/index.md`, `docs/roadmap.md`, 已删除的 `src/strategy/` 提交 | 现有安全回归 |
| E2 | `deploy/aws/edge_readiness_v08/*`, `deploy/aws/watch/*`, `SERVICE_CONFIG.json`（部署副本） | capacity artifact 测试已有 |
| S0 | 无必须改代码；运行 `scripts/build_btc_twap_executable_upper_bound.py` | `tests/test_btc_twap_executable_upper_bound.py` |
| S1 | 无必须改代码；运行 locked journal + shadow builder | `tests/test_btc_twap_structural_shadow.py` 等 |
| E0 | 新增 `venue_polymarket_v06.py`, `btc_twap_double_maker_probe.py`, 双 maker CLI；有限修改 `compatibility.py`（仅 capability 对象） | 新探针测试 + 既有 hard-gate 测试必须仍失败于布尔放行 |
| E1 | 部署文档 + 人工 runbook；**不**加默认 enabled unit | 故障演练收据 |
| L | 只读报表绑定 daily/capital/reconciliation artifact | readiness 测试 |
| P0 | 新目录 `research/reward_mm_<date>/`：PREREGISTRATION.json + STRATEGY_SPEC.md | 新 track 自己的测试，不混入 v08 计数 |

---

## 7. 明确不改的不变量

- `src/edge_lab/compatibility.py` 在 WP-E0 capability 落地前保持无条件拒绝。
- 普通研究命令零凭证、零认证端点、零订单。
- 金额全 `Decimal`。
- 预测/oracle 轨道 action-disabled。
- 盈利八项门槛。
- 证据等级 L0–L4：L2 未过不能称“已回测盈利”。
- 不得把 `src/trading.py` 当成 BTC 结构对的策略入口。

---

## 8. 建议日历（只有证据过门才消耗后面的时间）

| 日 | 包 | 产出 |
|---|---|---|
| 0 | H0 | 干净提交 |
| 0 | E2.0 | 失败分类备忘。未完成不得采购 |
| 0–2 | E2 | 新主机、SNS、4 个清洁 cohort、容量 artifact。若失败率只降到例如 40%，停在采集层 |
| 先于任何新 capture | S0 预注册 | 冻结 v0.8，写入 v0.9 exact count |
| 采集满 N | S0 | `gate-0-upper-bound.json`。按 2026-08-18 样本，大概率 STOP |
| 若 STOP | P0 | 结构线关闭记录 + 奖励预注册草稿。**不要做 E0。** |
| 若 PASS | S1 | 200 个 **清洁** unique expiry。下限不是 2 天：15m 边界每天 96 个，门槛允许失败率 < 5%，故至少 `200 / 0.95 ≈ 211` 次尝试 ≈ 2.2 天连续无中断；历史上出现过容量中途退化，应按 **≥ 4–7 天日历下限** 排期，2.2 天只是理论最短，不是预期 |
| 采集满 | S1 报告 | PASS 才开 E0 |
| +3–5 | E0 | 适配器 + 演练。仍无默认下单 |
| +1 | E1 | 人工 12 USDC 探针 |
| +14 | L | 只有真的每天 ≥ $20 才讨论 LIVE |

按当前市场证据，**最可能在 v0.9 Gate 0 当天把结构线判负**。那也是本规格的成功：用预注册判定停手，而不是带着没有边际的策略去实盘。第 3–4 天还在采 S1 时，不要把日历拉长误读成“策略又不行了”。

---

## 9. 对用户诉求的直接回答

“帮我解决所有工程问题和策略问题，接下来要实盘。”

- **工程问题**可以按 WP-H0 / E2 / E0 / E1 全部修完；其中 E0/E1 在经济门 PASS 之前做，只会造出一条能亏钱的通道。
- **策略问题**的核心不是 bug，是 `成本 = 1 + b + 开销` 且市场把 `b` 定价到一个 tick 以内。这不能靠 SDK、队列模型或主机升级解决。
- 因此完整方案不是“先打通下单再观察”，而是“先让 Gate 0 和 200-expiry shadow 说话，再决定有没有资格碰 WP-E0”。
- 若用户强制要求跳过 S0/S1：本仓库的安全合同不允许实施。需要先改预注册，而这等于承认放弃“已回测”话语。

---

## 10. 验收总表

只有下表全绿，才能把 README 的“不建议实盘”改成“允许 12 USDC 探针”。改成“策略实盘”还要再加 WP-L。

| 门 | 权威产物 | 当前 |
|---|---|---|
| 卫生提交 | 干净 git HEAD | WP-H0 已提交 `dd10709` |
| 失败分类 | `CAPTURE_FAILURE_TAXONOMY.md` | WP-E2.0 已分类；主因 `capacity_backpressure`；WP-E2 主机整改未开始 |
| 容量 | `btc-twap-capture-capacity-evidence.v1` | 无主机证据 |
| v0.8 Gate 0 41 | 对照报告，不可授权 | 默认不可恢复 |
| v0.9 Gate 0 N | 预锁 exact count 的报告 `decision=PASS` | 尚未开 track |
| Shadow 200 | structural-shadow 报告中性行 PASS | 无 |
| 双 maker adapter | `btc-twap-probe-adapter-evidence.v1` | `False` |
| 认证读 | authenticated-read artifact | 未做 |
| 用户成交流 | fill-stream artifact | 未做 |
| 故障演练 | fault-drills artifact 八项+双 maker 三项 | 仅单测 harness |
| geoblock | 目标主机 `blocked=false` | 本机曾超时 |
| pUSD / 签名 / 终态 | 授权探针收据 | 关闭 |
| 人工二次确认 | 5 分钟 nonce，不明文持久化 | 代码有旧 harness，形态错误 |
| 硬门 | capability 对象，不是布尔 | 无条件拒绝（正确） |

---

## 11. 相关文件

- 本规格（实施合同）
- [STRATEGY_SPEC.md](STRATEGY_SPEC.md) — 结算与纸面门
- [IMPLEMENTATION_AND_RUNBOOK.md](IMPLEMENTATION_AND_RUNBOOK.md) — 已实现表面与部署顺序
- [CLAUDE_FINAL_ASSESSMENT.md](CLAUDE_FINAL_ASSESSMENT.md) — 2026-08-18 经济结论
- [PREREGISTRATION.json](PREREGISTRATION.json) — v0.8 冻结的数字门槛（v0.9 可继承，不可放宽）
- [OPINION_ON_LIVE_COMPLETION_SPEC.md](../../v08_review_2026-08-22/OPINION_ON_LIVE_COMPLETION_SPEC.md) — 同日独立复核，三点风险已吸收
- [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md) — 下一会话从 WP-H0 开始的完整提示词
- `src/edge_lab/btc_twap_relative_value_readiness.py` — 机器门
- `src/edge_lab/compatibility.py` — 新订单硬门
- `src/edge_lab/btc_twap_execution_probe.py` — 旧 maker+FOK harness（无资格）
- `src/edge_lab/btc_twap_structural_shadow.py` — 纸面双 maker
