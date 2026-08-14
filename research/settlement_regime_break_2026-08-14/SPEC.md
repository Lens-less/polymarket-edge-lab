# SPEC：结算体制切换（2026-08-14）后的采集解耦、v0.6 前瞻轨道与告警改造

- 状态：deployed-and-monitoring（WP1–WP6 已上线；24h 容量回填待完成）
- 日期：2026-08-14
- 事件：Polymarket 将 `btc-updown-5m-*` 市场结算源由 Chainlink TWAP 30s 切换为
  60s，v0.4 / v0.5 冻结校验按设计 fail-closed，两条 paper 轨道自
  `2026-08-14T00:12Z`（首次观测时间）起停止产出证据，且原始数据同步停采。
- 本 spec 覆盖六个工作包（WP1–WP6），整合了外部评审（Codex）意见并按本仓库
  代码与主机实况做了修正。

---

## 0. 事实基线（全部已核实，非推断）

### 0.1 时间线

| 时间 (UTC) | 事实 | 证据 |
|---|---|---|
| 08-13 23:51:38 | 最后一个成功 capture root 开启（cycle `1786665600`，00:00Z 到期）；capture-config 中 5m 仍绑 30s 源 | 主机 `runs/1786665600/20260813T235138...` |
| 08-14 ~00:12 | 首次 discovery 拒绝（**first-observed mismatch，不是平台切换的精确时间**；市场可能提前批量创建） | 服务 status / 上次探测 |
| 08-14 06:45 | 实测未来所有 5m 盘 `resolutionSource` = `.../btc-usd-twap-60s-streams`；15m 同为 60s；`acceptingOrders=true` | Gamma API 逐 slug 查询 |
| 08-14 06:45 | v0.5 报告冻结在 156（v0.4 为 380），两服务 `phase=error_wait`、`healthy=false`（`service_phase_unhealthy`）、心跳正常、guards 全绿 | `service/status.json`、`monitor/health-latest.json` |
| 08-14 15:42:34 | 主机仍在“**process 活着但 progress 停摆**”状态：四个 systemd 单元均 `active`，但 v0.5 / v0.4 都是 `phase=error_wait`；v0.5 `completed_report_count=156`、`capture_cycles=39`、shadow `+9.465867`，v0.4 `completed_report_count=380`、`capture_cycles=95`、shadow `-79.384309` | 最新 SSM 读数 |
| 08-14 15:42:34 | t3.small 主机 `MemAvailable=859MB`，v0.4 / v0.5 进程 RSS 分别约 `337MB` / `341MB`；过去 6 小时 `CPUCreditBalance` 最低约 `226.39`（09:42Z），15:37Z 升至约 `362.85` | 最新 SSM + CloudWatch |
| 08-14 18:08:36 | v0.4 最终状态、health、unit 状态与哈希封存完成；服务和 health timer 停止并禁用，历史数据未删除 | `v04_final_state/ARCHIVE_MANIFEST.json` |
| 08-14 18:49:43–44 | rawcap 与 v0.6 各自开启首个 prospective capture；缺口按 v0.6 首次 capture 启动时刻闭合 | rawcap / v0.6 status、`GAP_CLOSURE.json` |
| 08-14 19:06:04 | rawcap 首个周期完整收口：61 manifests、230,517 records、0 integrity failure、60s/60s eligible、0 quarantine | rawcap `capture-summary.json` |
| 08-14 19:21–19:24 | v0.6 首周期四个 τ 报告全部生成并通过 v2 验证；四个 raw shadow 均可用，canonical 均 `no_trade`；qualified/OOS 以 `past_only_calibration_insufficient` fail-closed | v0.6 reports / health / validation summary |
| 08-14 19:37:48 | watcher 的 v0.5 权限噪声修正上线：冻结的 0600 状态文件现在报告为 `telemetry_unavailable`，不再误报 stale heartbeat；capture stalled 与 regime mismatch 继续保留 | watcher alert snapshot / journal |

已错过 00:15Z–06:45Z 共 27 个周期（每轨道约 108 份 τ 报告），缺口仍以每
15 分钟 4 份的速度扩大。

### 0.1.1 恢复审计边界

- 事故后补做的 recovery audit 只允许作为 **context**，不允许改写 canonical
  轨道。当前已固化到 `RECOVERY_AUDIT.{json,md}`：
  - v0.5 事故窗口共 69 个预期 capture cycle，其中 39 个有 canonical
    report/summary，30 个缺失。
  - `tau120` 只有 10 个 cycle 具备双 surface，可做诊断回放。
  - 诊断回放共看了 3 个动作，其中 2 个 dust 形态为正（`+13.626271`、
    `+10.365131` USDC），**但全部不得进入 qualified / calibration / OOS**。
- 缺口的 `first_observed_at=2026-08-14T00:12:00Z` 仅表示首次观测到 mismatch，
  不是平台体制切换的精确时刻。`GAP_MANIFEST.json` 的 hash 已被 v0.6 预注册
  冻结，因此其 `gap_end=null` 与全部原始字节保持不变；运行闭合 addendum
  `GAP_CLOSURE.json` 将 `gap_end` 记录为 `2026-08-14T18:49:44.097586Z`
  （v0.6 首次 prospective capture 启动；此前未观测到 30s 体制回归），并反向
  绑定 base manifest 的冻结 SHA-256。

### 0.2 根因与结构性缺陷（代码定位）

- 冻结体制常量：`src/edge_lab/btc_twap_relative_value.py` L49
  `_SETTLEMENT_REGIME = "chainlink_twap_30s_5m_and_60s_15m.v1"`。
- 绑定校验：同文件 L168–172 按 horizon 要求精确 URL
  （5m→`btc-usd-twap-30s-streams`，15m→`btc-usd-twap-60s-streams`），
  L180–185 不匹配即 `resolution_source_mismatch` fail-closed。**该行为正确，保留。**
- 结构性缺陷：`scripts/run_btc_twap_relative_value_service.py`
  `run_service()` 中 capture root 的创建（L384–386）位于 discovery（L369）
  **之后**。discovery 抛 `RelativeValueRejection` → 直接 `error_wait`
  （L444–451，`retry_seconds=30`），**原始数据零落盘**。
  "策略不可用"由此升级为"证据数据永久缺失"。
- 该缺陷的附带后果不仅是 raw 缺失，也包括 `capture-summary.json` /
  报告计数长期停滞；如果只看 systemd `active`、fresh heartbeat、guards 全绿，
  会误判为“服务还活着”。因此 watcher 必须把 **report progress** 和
  **capture progress** 作为一等信号（见 WP5）。
- 可复用资产：`src/edge_lab/capture_cli.py` 的 `run_forward_capture()`
  不含任何 TWAP 资格校验，`PublicRecorder`/`CaptureStore` 均可独立使用。
  独立采集器不需要从零开发。

### 0.2.1 对 dust / 第二腿假说的代码审计修正

事故前小样本显示 dust 切片较差，但它**不能直接证明**“决策时未检查第二腿深度”
或“超时后一直补第二腿直到到期”：

- `_candidate()` 已对被选中的两条腿分别走完整个 ask book，并且只在两腿都能
  以同一目标数量成交、总成本不超风险预算时生成动作。这已经是 B1 的核心语义。
- `execute_pair_paper()` 已把第二腿限定在冻结的 `max_leg_delay_ms=750` 窗口；
  第二腿未及时或部分成交时，对第一腿超额部分立即执行市场化 unwind。这已经是
  B2 的核心语义。
- 报告中的长 `dust` 持续时间可能只是极小残余头寸一直留到结算，不能等同于
  整条第一腿在 240 秒内裸露。只有
  `residual_unhedged_max_loss_usdc >= 0.01` 的残余才作为“经济上显著”单独审计。

因此本次不把 B1/B2 作为新交易逻辑重复实现，也不把 `dust -25.78` 当作因果结论。
并行候选轨道改为：A1 极端概率 veto、A4 loss-probability veto、A2 市场概率
收缩的 lambda 网格，以及 B3 决策时双腿深度缓冲（1.25x/1.5x/2x）。这些轨道
只记录决策时可得输入并生成非 canonical 计分板，永不改变 v0.6 的动作；若未来
采用其中任何一项，必须另行预注册并 prospective-only 验证。

### 0.3 平台文档事实（docs.polymarket.com/market-data/chainlink-twap）

- RTDS 提供 `crypto_prices_twap_thirty` 与 `crypto_prices_twap_sixty`
  两个 topic，可同时订阅（`btc/usd`）。
- "Subscriptions start with the next update. There is no snapshot,
  history, or replay after a disconnect." → **断采不可无偏重建**。
- "never infer the window from update frequency"；新鲜度应使用
  Chainlink observation timestamp（`payload.timestamp`）。
- Chainlink 未公开该定制 TWAP 的采样边界、加权、舍入与缺失输入处理，
  官方明确要求**不要自行复现**该值。

### 0.4 冻结身份事实

- 预注册冻结的是 `STRATEGY_SPEC.md` 的 sha256 与 `repository_head`
  （`validate_preregistration_runtime_identity`，service L1095–1175）。
- `SERVICE_CONFIG.json`（含 `retry_seconds=30`）**不进入**任何冻结哈希；
  但按部署纪律，v0.5 主机树整体不动（见 §6）。

---

## 1. 对外部评审意见的裁决

| Codex 观点 | 裁决 | 依据 |
|---|---|---|
| fail-closed 发生得太早，采集与资格必须解耦 | **采纳**（本 spec 核心，WP2/WP3） | §0.2 已在代码层证实 |
| 不修改 v0.5 资格规则、不接受 `30s or 60s`、不续接报告编号 | **采纳** | 冻结宇宙完整性 |
| v0.4 停止并封存 | **采纳**（WP1） | 对照价值已死：两轨同停，30s 宇宙未必回归；释放约 200MB 内存 |
| v0.6 独立 60s/60s 轨道重新预注册 | **采纳**（WP4） | 与 v02→v05 的既有轨道模式一致 |
| 路径 A（迁移测试）优先于路径 B（重校准） | **采纳**，默认路径 A | 冻结流程本身支持冷启动（qualified 先 fail-closed 于 `past_only_calibration_insufficient`，随 strictly-prior 数据累积自动解锁） |
| 缺失 6.5h+ 标记为结构性缺失，不伪造回补 | **采纳**（WP6） | RTDS 无 replay（§0.3） |
| 告警看 progress 而非 process | **采纳**（WP5） | 本次 `healthy=false` 已被健康检查正确标记 6.5h，但无人消费该信号 |
| regime router `destination: v0.5` | **修正**：router 只服务新采集栈与 v0.6。v0.5 是自含冻结服务、自行 discovery，任何外部组件**不得**向其 evidence/data 目录写入。30s/60s 盘若回归，v0.5 自动恢复，router 仅打标签并告警 | 服务架构（§0.2） |
| v0.5 检查退避到 5 分钟 | **拒绝**：discovery 探测成本可忽略（每 30s 两个 Gamma GET），改动部署树违反冻结纪律且无收益 | §0.4 |
| （未覆盖）主机资源预算 | **补充**（§5）：t3.small 共 1.9GB 内存，当前可用约 850MB，必须先停 v0.4 再上新组件 | 主机实测 |
| （未覆盖）告警传输通道与凭证边界 | **补充**（WP5）：策略服务零凭证姿态不变；watcher 作为独立单元经实例角色最小权限 `sns:Publish` | 部署安全模型 |

---

## 2. 目标架构

```
        ┌─────────────────────────────────────┐
        │ WP2 polymm-btc-rawcap（独立采集服务）  │ 永不因体制未知而停止
        │  RTDS: crypto_prices + twap_30s+60s  │
        │  CLOB WS: 当期 pair 的 4 个 token     │
        └───────────────┬─────────────────────┘
                        ↓ 逐市场
        ┌─────────────────────────────────────┐
        │ WP3 regime classifier（冻结 registry）│
        └───────┬──────────────┬──────────────┘
                ↓              ↓              ↓
        60s/60s → v0.6    未知组合 →      30s/60s(旧体制) →
        资格层(WP4)       quarantine      标签 + 告警
                ↓
        evidence / paper execution（fail-closed 保留在此层）

  v0.5：保持自含运行、自行 discovery，与上述管线零交互；
        30s 体制回归时自动恢复，无需任何干预。
```

分层原则：

1. **采集层**只对"数据是否完整落盘"负责，遇到未知体制打
   `qualification_status=quarantine, reason=unknown_settlement_regime`，
   继续采集。
2. **路由层**按冻结 registry 逐市场分类（以每个市场自身的
   `resolutionSource` + 规则文本为权威输入，不按日期假定体制）。
3. **资格层**保留 fail-closed：只拒绝进入 evidence / qualified 报告 /
   下单 / 策略统计，**无权阻止原始落盘**。

---

## 3. 工作包

### WP1：v0.4 停止并封存

变更：

1. 采集最终状态快照（`status.json`、`health-latest.json`、报告计数 380、
   最后成功 cycle `1786665600`）存入
   `research/settlement_regime_break_2026-08-14/v04_final_state/`。
2. `systemctl stop && systemctl disable` v0.4 服务与健康 timer。
3. `/opt/poly-mm`、`/var/lib/poly-mm` 目录、日志、哈希、报告**全部保留**，
   只读封存，不删除。
4. 在 GAP_MANIFEST（WP6）中记录封存时间与理由。

不变更：v0.4 的任何历史证据文件。

验收：单元 `inactive`+`disabled`；数据目录字节不变（封存前后 du/哈希抽查一致）；
释放内存约 200MB。

### WP2：独立原始采集器 `polymm-btc-rawcap`

新增：

- `scripts/run_btc_regime_agnostic_collector.py`：
  - 市场枚举：按 slug 模式 `btc-updown-{5m,15m}-{epoch}` 对齐未来到期时刻
    （:00/:05/.../:15 网格）逐个 Gamma 查询，**不做任何合约资格校验**；
    查得即采。
  - 采集窗口：对齐 15m 周期，提前量 ≥ TAU240 预测窗左边界 + 与 v0.5 相同的
    `settlement_grace_seconds`，实际近似连续运行。
  - 订阅：RTDS `crypto_prices`、`crypto_prices_twap_thirty`、
    `crypto_prices_twap_sixty`（`btc/usd`，30s 与 60s **同时**采）+
    当期 pair 4 个 token 的 CLOB market WS。复用
    `run_forward_capture`/`PublicRecorder`/`CompactRecorderSink`。
  - 每市场落盘元数据（capture-config 扩展）：`observed_at`、`market_id`、
    `market_slug`、`market_start/end`、`duration_seconds`、
    `resolution_source_raw`、`resolution_source_canonical`（WP3）、
    `rules_text_hash`、`gamma_payload_hash`、`token_ids`、
    `twap_window_seconds`、`regime_classification`、`qualification_status`。
  - guards：与现有服务同款 `paper_only/public_only/new_orders_disabled/
    orders_submitted=0/authenticated_endpoints_used=0` 字段，写入自身
    status.json，纳入健康检查。
- 部署：`deploy/aws/rawcap/`（unit + health timer + healthcheck，克隆
  v05 模板改名）；`/opt/poly-mm-rawcap`（root 只读）+
  `/var/lib/poly-mm-rawcap`（服务账号 `polybotraw`）；沿用
  `ProtectSystem=strict`、IMDS 封禁、空 capability 集。
- 存储治理：jsonl 轮转后 zstd 压缩（独立 timer）；保留策略 30 天；
  健康检查沿用 `minimum_free_disk_bytes=12GiB` 门槛；首个 24h 实测
  写入速率并回填本 spec 的预算表（§5）。

不变更：v0.5 服务及其采集逻辑。

验收：连续 3 个周期在"当前 60s 体制"下正常落盘 raw；人为向 registry 注入
未知体制样例时采集不中断且标记 quarantine；断连重连计数进入健康快照；
内存 RSS ≤ 250MB。

### WP3：结算体制 registry 与分类器

新增 `REGIME_REGISTRY.json`（冻结，sha256 进入 v0.6 预注册）：

```json
{
  "schema_version": "btc-twap-settlement-regime-registry.v1",
  "regimes": {
    "chainlink_twap_30s_5m_and_60s_15m.v1": {
      "5m":  {"provider": "chainlink", "symbol": "btc/usd", "product": "twap",
               "window_seconds": 30, "stream_slug": "btc-usd-twap-30s-streams"},
      "15m": {"provider": "chainlink", "symbol": "btc/usd", "product": "twap",
               "window_seconds": 60, "stream_slug": "btc-usd-twap-60s-streams"},
      "note": "v0.5 宇宙；router 不向 v0.5 写入，仅打标签"
    },
    "chainlink_twap_60s_5m_and_60s_15m.v1": {
      "5m":  {"provider": "chainlink", "symbol": "btc/usd", "product": "twap",
               "window_seconds": 60, "stream_slug": "btc-usd-twap-60s-streams"},
      "15m": {"provider": "chainlink", "symbol": "btc/usd", "product": "twap",
               "window_seconds": 60, "stream_slug": "btc-usd-twap-60s-streams"},
      "destination": "v0.6"
    }
  },
  "unknown_regime": {"destination": "quarantine"}
}
```

分类规则：

- 从 Gamma `resolutionSource` 解析出规范化身份元组
  `(provider, symbol, product, window_seconds, stream_slug)`，
  同时保存原始 URL 与规则全文 hash。**比较规范化身份而非原始字符串**，
  防止尾部斜杠/展示格式变化造成无意义拒绝；provider/标的/窗口真实变化时
  仍然 fail-closed。
- 5m 与 15m 的组合整体匹配某个 regime 才路由；任何未收录组合（含未来可能的
  90s、新 URL、字段缺失）→ quarantine + 告警，采集不停止。
- 每市场独立分类；混合批次自动按市场路由。

验收：单元测试覆盖三类路由（已知 30/60、已知 60/60、未知），及 URL 规范化
（尾斜杠、大小写、查询串）等价类。

### WP4：v0.6 前瞻轨道（默认路径 A：迁移测试）

**路径裁决**（需用户确认，默认 A）：

- **路径 A（推荐，最快恢复前瞻证据）**：完整沿用 v0.5 冻结策略参数
  （τ 集、阈值、vol 方法、bootstrap、风险额度等 `frozen_strategy` 全部字段），
  仅将资格宇宙改为 60s/60s。研究问题 = "原冻结模型在新结算体制下是否可迁移"。
  注意：v0.5 的校准本就是
  `isotonic_train_only_per_tau_from_strictly_prior_raw_observations`，
  轨道内自冷启动——shadow 立即出数，qualified/OOS 随本轨道 strictly-prior
  数据累积自动解锁，与 v0.5 上线首日行为一致。
- **路径 B（重校准）**：若要重训 predictor/calibration/阈值/τ 分桶，最初一批
  60s/60s 数据只能作为 calibration/in-sample，**不得**同时计入 v0.6 OOS。
  必须**预先**冻结 `calibration_end_utc` 或 `calibration_sample_count`，
  不得看完表现再定截止点。建议留作后续 v0.7，不阻塞路径 A。

代码变更（main 分支，v0.5 部署树不动）：

1. `btc_twap_relative_value.py`：将 horizon→期望结算源的映射从模块常量
   （L49、L168–172）参数化为冻结 `RegimeSpec`（由 SERVICE_CONFIG 指定
   regime id，启动时对照预注册 `scope.settlement_regime` 与 registry hash
   校验一致，不一致拒绝启动）。精确匹配 fail-closed 语义不变。
2. 校验新鲜度继续使用 Chainlink observation timestamp；不从更新频率推断窗口。
3. 新增/更新单元测试：60s/60s 绑定通过、30s 盘在 v0.6 下被拒、regime id 与
   预注册不一致时启动失败。
4. 每份报告附加 `candidate_hypotheses` 诊断块，只保存候选轨道所需的决策时输入
   与事后标签；`build_btc_regime_candidate_scoreboard.py` 独立汇总 A1/A4/B3
   留存交易的反事实 PnL、A2 lambda 网格的 Brier，以及 B1/B2 既有控制和经济上
   显著残余的审计。该计分板明确标记 `non_canonical=true`，不能改变动作、不能
   进入 v0.6 qualified PnL，也不能用于看完 OOS 后挑参数。

预注册 `PREREGISTRATION.json`（相对 v0.5 的 diff）：

| 字段 | v0.6 取值 |
|---|---|
| `schema_version` | `btc-5m-15m-relative-value-preregistration.v6` |
| `evidence_track_id` | `btc-paper-v06-<冻结日YYYYMMDD>` |
| `scope.settlement_regime` | `chainlink_twap_60s_5m_and_60s_15m` |
| 新增 `scope.settlement_sources` | 5m/15m 的规范化身份元组 + 原始 URL |
| 新增 `regime_registry.sha256` | WP3 registry 冻结哈希 |
| 新增 `pre_v06_gap` | 引用 WP6 GAP_MANIFEST（结构性缺失声明） |
| `frozen_strategy` | 路径 A：与 v0.5 **逐字段相同**，另加一行声明 `"transfer_test_of": "btc-paper-v05-20260813"` |
| `split_policy.pre_v06_data_use` | v0.5 及更早轨道证据永不进入训练/校准/qualified PnL |
| `promotion_gates` | 与 v0.5 完全一致（2000/500/500、coverage 0.995、OOS Brier、ECE≤0.05、bootstrap LB>0、单事件 20% 上限） |
| `prospective_only_after` | 冻结时刻；**只纳入该时刻之后开盘的市场**，注册前已见结果的市场一律排除 |

部署（复刻 v0.5 仪式）：

- `research/btc_5m_15m_relative_value_paper_v06_linux_<date>/`
  四件套（PREREGISTRATION / STRATEGY_SPEC / SERVICE_CONFIG / 部署后补
  DEPLOYMENT_REPORT）；`deploy/aws/paper_v06/` 单元文件。
- `/opt/poly-mm-v06`（root 只读）+ `/var/lib/poly-mm-v06` + 服务账号
  `polybotv06`；git archive 双 marker + 内容寻址 S3 一次性传输 + 即删；
  `--validate-only` 通过后 staged 手动启动；CRLF 零容忍检查沿用。
- 启动对齐：从哈希冻结完成后的**下一个对齐 15m 周期**开始。

验收：`--validate-only` 全绿（`paper_only/public_only/new_orders_disabled`）；
首周期四份 τ 报告 schema v2、`verified_report_v2=true`；qualified/OOS 按预期
以 `past_only_calibration_insufficient` fail-closed；v0.5 服务全程不受影响。

### WP5：进展告警 watcher `polymm-watch`

原则：**监控 progress，不监控 process**。本次事故中 systemd `active`、
fresh heartbeat、paper guards 全程正常，但报告计数与 capture-summary
已经停滞；`OnFailure=` 永远不会触发——不依赖它。

新增 `scripts/watch_paper_tracks.py`（stdlib-only）+
`polymm-watch.service/.timer`（每 60s），读取 v0.5 / v0.6 / rawcap 的
`status.json` 与 `health-latest.json`（只读），维护去重状态机：

| 信号 | 触发条件 | 级别 |
|---|---|---|
| heartbeat staleness | `heartbeat_at` 落后 > 60s | page |
| phase unhealthy | `phase ∈ {error_wait, stopped}` 持续 > 10 min | page |
| report stalled | `completed_report_count` 连续 2 个预期周期（30 min）无增长 | page |
| capture stalled | rawcap / paper 轨道的最新 `capture-summary.json` 时间戳连续 2 个预期周期无增长 | page |
| regime mismatch | 首次发现 `resolution_source_mismatch` 即告警；连续 3 个市场后升级 | warn→page |
| quarantine 增长 | rawcap 新增 quarantine 市场 | warn |
| rejection 单一化 | 连续 N 个市场同一 reason code 拒绝 | warn |
| host memory pressure | `MemAvailable < 300MB` 或单进程 RSS 超过预算 | warn→page |
| CPU credit pressure | `CPUCreditBalance < 100` 或持续下降且 30 分钟内无恢复 | warn |
| disk / staleness | 复用现有健康字段越限 | warn |
| recovery | unhealthy→首个成功周期 | notice |

告警正文必须直接包含：`expected_regime`、`observed_regime`、
`first_seen_at`、`last_success_at`、`consecutive_rejections`、
`affected_market_slugs`，让收件人无需登录主机即可判断。

传输与凭证边界（对零凭证姿态的**唯一**、显式豁免）：

- 新建 SNS topic `polymm-paper-alerts`（email 订阅，地址待定）。
- watcher 单元**不**继承策略服务的 IMDS 封禁，经实例角色取凭证，IAM 仅授
  `sns:Publish` 于该 topic ARN（最小权限）。
- watcher 对所有策略/采集目录只读（`ProtectSystem=strict` + 显式
  `ReadOnlyPaths=`），自身状态目录独立；策略与采集服务保持零凭证不变。
- 去重与降噪：同一告警键首报即时，重复进入每小时 digest；恢复必发 notice。

验收：注入式演练——手动把某 status.json 的镜像副本改为 `error_wait`（在
watcher 测试目录，不碰真实目录）触发告警；恢复演练收到 notice；重复告警
落入 digest 不刷屏。

### WP6：缺口 manifest、恢复审计与证据卫生

新增 `research/settlement_regime_break_2026-08-14/GAP_MANIFEST.json`：

```json
{
  "schema_version": "settlement-regime-gap-manifest.v1",
  "gap_start": "2026-08-14T00:12:00Z",
  "gap_start_semantics": "first_observed_mismatch_not_platform_switch_time",
  "gap_end": "<v0.6_activation_time 或 30s 体制回归时刻，二者先到者>",
  "gap_type": "structural_market_regime_mismatch",
  "affected_tracks": ["btc-paper-v04-20260813", "btc-paper-v05-20260813"],
  "last_good_cycle": 1786665600,
  "reports_reconstructed": false,
  "excluded_from_qualified_evidence": true,
  "evidence": ["status.json 快照", "health history 引用", "Gamma 逐 slug 探测输出"]
}
```

新增 `RECOVERY_AUDIT.json` / `RECOVERY_AUDIT.md`，用于记录：

- canonical 轨道的缺失规模（本次为 v0.5 `69 total / 39 reported / 30 missing`）
- recovery diagnostic 的输入充分性（如 `tau120` 仅 10 个双 surface）
- 诊断动作数量、PnL 结果，以及“不得进入 qualified”的边界

允许回补（仅作上下文，存 `context/` 并标记 `non_evidence=true`）：
Gamma 市场元数据、市场规则全文、最终结算结果、公开价格历史。

**禁止宣称等价回补**：原定采样时刻的实时 order book、原定延迟下的行情、
丢失期间的 RTDS TWAP 更新、当时实际可得的信息集、原始 τ 报告。
依据 §0.3：RTDS 无重放，且 Chainlink TWAP 内部计算规则未公开。

冻结后闭合规则：一旦 `GAP_MANIFEST.json` 的 SHA-256 被预注册引用，禁止再原地
填写 `gap_end`。恢复时间与首周期验收必须写入 `GAP_CLOSURE.json`，并由 closure
文件绑定 base manifest 的路径和 SHA-256；closure 只属于 operational context，
不进入 qualified strategy evidence。

---

## 4. 执行顺序

| 步骤 | 内容 | 前置 |
|---|---|---|
| 1 | WP6 GAP_MANIFEST + RECOVERY_AUDIT 落盘（给事件盖时间戳）+ WP5 最小版 watcher（unhealthy / report stalled / capture stalled → SNS）上线，先解除"盲飞" | 无 |
| 2 | WP1 v0.4 快照并封存，释放内存 | 1 |
| 3 | WP2 rawcap 采集器部署（从此不再丢新体制数据，即使 v0.6 尚未就绪） | 2 |
| 4 | WP3 registry 冻结 + 分类器测试 | 可与 3 并行 |
| 5 | 用户拍板路径 A/B → WP4 预注册冻结 → 部署仪式 → 下一对齐周期启动 | 3, 4 |
| 6 | WP5 完整信号集 + 恢复演练；24h 主动监控（复刻 v0.5 上线流程） | 5 |
| 7 | 首个 24h 后回填 §5 实测数字，DEPLOYMENT_REPORT 归档 | 6 |

执行状态：步骤 1–6 已完成并经首周期验收；步骤 7 的部署报告已先记录首周期
事实，24h 写入速率与长期资源趋势将在定时监控取得足够窗口后追加。全程保持
live trading 关闭；v0.6 晋级仍受预注册冻结门槛约束。

## 5. 资源预算（t3.small，部署前估算）

| 项 | 内存 | 备注 |
|---|---|---|
| 现状可用 | ~850MB | `free -m` 实测（total 1913 / avail 852） |
| 停 v0.4 | +~200MB | WP1 |
| rawcap | −150~250MB | 无 MC 模拟，纯录制 |
| v0.6 | −~200MB | 与 v0.5 同量级 |
| watcher | 忽略 | timer 短进程 |
| 预计余量 | ~400–500MB | 低于 300MB 时告警（纳入 WP5） |

首周期实测（`2026-08-14T19:24:33Z`）：rawcap RSS 约 `121MB`、v0.6 RSS
约 `229MB`、v0.5 RSS 约 `340MB`，`MemAvailable` 约 `920MB`；CPU credit
在 19:05Z 为 `430.86`，磁盘可用 `36.3GB`。均优于预算和告警门槛。首个
完整 rawcap 周期写入量不代表稳定 24h 压缩率，仍不得据此外推 30 天容量。

磁盘：当前 37GB 空闲；v0.5 活跃采集实测约 58KB/10s，rawcap 双 TWAP 流 +
CLOB 估 0.3–0.7GB/天（未压缩），zstd 后预计 <100MB/天；30 天保留 +
12GiB 最低空闲门槛。是否 S3 离线归档为开放决策。首个 24h 实测后回填。

风险：

- 平台把 5m 改回 30s → v0.5 自动恢复；v0.6 将 30s 盘 quarantine（不在其
  regime 内），两轨并行各采各的，无需干预。
- 再次出现新体制（90s、新 URL）→ quarantine + 告警，raw 不丢——本 spec 的
  核心目标即覆盖此情形。
- 内存吃紧 → 降级顺序：rawcap 先收窄到仅双 TWAP 流（弃 CLOB 深度）。
- 告警疲劳 → 去重 + digest（WP5）。

## 6. 明确禁止事项

1. 不修改 v0.5 部署树、资格规则、`retry_seconds` 或任何运行参数；
   不把校验放宽为 `30s or 60s`。
2. 不把 60s 数据写入 v0.5 evidence 目录；不把 v0.6 报告接在 v0.5 编号后。
3. 不回补/重建缺口期 τ 报告并宣称等价（§0.3、WP6）。
4. 不按日期假定体制；逐市场以其自身 resolutionSource + 规则文本路由。
5. 不从更新频率推断 TWAP 窗口；不自行复现 Chainlink TWAP 值。
6. 注册前已见结果的市场不得进入 v0.6 前瞻证据。
7. 策略与采集服务保持零凭证；唯一豁免是 watcher 的 `sns:Publish`（WP5）。
8. live trading 保持关闭，直至 v0.6 通过预注册冻结的全部晋级门槛。

## 7. 决策状态与剩余开放项

1. **路径 A 已执行**：v0.6 是相对 v0.5 的 prospective migration test；路径 B
   重校准保留给未来 v0.7，当前没有调整冻结策略参数。
2. **告警传输已部署但 SNS 收件端点仍开放**：topic 与最小权限
   `sns:Publish` 已就绪，尚无 email/webhook subscription。作为立即可用的补充，
   Codex task heartbeat 已设为每 30 分钟做一次外部只读巡检。
3. **rawcap 暂不做 S3 离线归档**：本地 30 天保留已启用；待 24h 实测写入率后
   再决定，避免先扩权限与成本面。
4. **v0.4 已封存**：于 `2026-08-14T18:08:36Z` 完成，未删除任何历史证据。

## 8. 首周期部署验收

- rawcap 与 v0.6 均保持 `paper_only/public_only/new_orders_disabled=true`、
  `orders_submitted=0`、`authenticated_endpoints_used=0`；服务进程环境未发现
  AWS access key、session token、wallet/private-key 或 proxy 变量。
- v0.6 cycle `1786734900` 的 τ60/120/180/240 四份报告均为
  `btc-5m-15m-relative-value-pilot-report.v2`，`verified_report_v2=true`，
  `raw_shadow_model.available=true`。四个 canonical 决策均为 `no_trade`，原因是
  qualified 轨道尚无 strictly-prior isotonic calibration；这与预注册相符。
- capture integrity、双 recorder legs、四 token signal/delayed execution book
  surface 均完整；首周期没有经济尝试、fills、残余风险或 PnL，因此
  `qualified_net_pnl=null`，不得解释成零收益。
- 非 canonical 计分板已经并行记录 A1/A4/B3 与 A2 lambda 网格。当前只有一个
  独立 expiry cluster（四个 τ 报告），任何 Brier 排序都只是管线验收，不能用于
  选择或冻结参数。
- watcher 当前唯一活跃的策略告警属于预期的 v0.5 旧体制：capture stalled、
  regime mismatch，以及冻结 0600 文件导致的 telemetry unavailable。rawcap 与
  v0.6 没有活跃告警。外部 heartbeat 会直接通过 SSM 验证 v0.5 真心跳。
- 当前服务 release：rawcap/v0.6 为
  `9a97a10831248a9a6c949fc239eedee45176c6a8`，watcher 为
  `df0e7daac9e659810025ead16a2a9fb361be658b`；三者冻结 implementation revision
  均为 `5dcc3bd7e09794c578d6572181be81388fdb6661`。
