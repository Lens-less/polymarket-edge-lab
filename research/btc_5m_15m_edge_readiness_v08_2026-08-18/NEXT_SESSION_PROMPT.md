> **过期（2026-08-22）：不要再执行 WP-E2。** 结构线已 STRUCTURAL STOP，见 [STRUCTURAL_LINE_STOP.md](STRUCTURAL_LINE_STOP.md)。下面的提示词是历史草稿，会把新会话重新送去换采集主机，那是错误方向。
# 新会话提示词：poly-mm 按 LIVE_COMPLETION_SPEC v0.9.1 执行 WP-E2

把下面从「开始复制」到「结束复制」的整段粘贴到新 Codex 会话。不要写下单适配器，不要解除硬门，不要跑 Gate 0 经济判决，不要把 README 改成可以实盘。

---

开始复制

你在仓库 `C:/Users/28340/Desktop/poly-mm` 工作。这是 Polymarket BTC 5m/15m 结构对研究仓库。当前策略实盘 NO-GO，探针 NO-GO。

当前 HEAD：`eb83600`（`Keep the capture_failed contract; retain recorder-leg error codes`）。
WP-H0 已完成（`dd10709`）。WP-E2.0 已完成（`a9d5f45` + `eb83600`）。本会话只做 **WP-E2 采集容量与告警**。

权威实施合同：

- `research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md`（v0.9.1）
- 数字门槛仍冻结在 `research/btc_5m_15m_edge_readiness_v08_2026-08-18/PREREGISTRATION.json`，不得放宽
- 失败分类：`research/btc_5m_15m_edge_readiness_v08_2026-08-18/CAPTURE_FAILURE_TAXONOMY.md`
- 部署资产：`deploy/aws/edge_readiness_v08/README.md`、`deploy/aws/watch/README.md`
- 服务合同：`research/btc_5m_15m_edge_readiness_v08_2026-08-18/SERVICE_CONFIG.json`

先完整阅读 LIVE_COMPLETION_SPEC 的 WP-E2 节、taxonomy 全文（含 §9.1）、edge_readiness_v08 README 和 watch README，再动手。冲突时以 LIVE_COMPLETION_SPEC.md 与 PREREGISTRATION.json 为准。

## 本会话只做一件事

WP-E2：用本机已配置的 AWS CLI v2 整改采集主机，让容量门能诚实变绿。

用户已明确授权：控制面是这台 Windows 机器上的 AWS CLI v2，目标是它已经能连上的那台 AWS 服务器。不要再问一次“能不能改 AWS”。先用只读命令确认身份、区域、实例，再改。

本会话明确不做：

- 不实现 `ProbeVenue` / 双 maker adapter / 任何下单路径
- 不修改 `assert_new_orders_disabled()`
- 不把 `DRY_RUN=false` 或八项布尔审计当成放行
- 不启用任何 order / probe systemd unit
- 不开始 WP-S0 经济判决，不跑 `build_btc_twap_executable_upper_bound.py` 做 PASS/STOP
- 不把新主机事后数出来的清洁到期数填进 `--expected-clean-attempts`
- 不花费主时间找回 41 棵旧采集树；默认不可恢复，v0.8 冻结为未完成
- 不把 v0.6 +87.52、公开 touch、Gate 0 上界写成利润
- 不重建 `src/strategy/` 或 TUI
- 不把真实 SNS ARN 提交进 git

## 硬约束

- 金额全 Decimal
- 普通研究命令零凭证、零认证端点、零订单；主机上的 v0.8 单元同样 public-only / paper-only / new_orders_disabled
- README / CLAUDE.md 必须继续写「不建议实盘」
- 提交信息遵循仓库 Lore protocol
- 不提交 `.omx/`、会话草稿、大原始数据、真实 ARN、凭证
- Windows 是控制面；发布验证与采集运行在 Amazon Linux。不要在 Windows checkout 上伪造容量证据

## 已冻结的分类结论（不要重做 WP-E2.0）

主证据：`research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/DEPLOYMENT_REPORT.md`。

- 14/15 = 93.3% 的主因桶是 `capacity_backpressure`
- 5/14 被逐条看过：双腿 `RecorderWorkerError`，manifest/raw integrity 干净，同时 CPU credit ≈0、磁盘距 15 GiB 软停约 148 MiB
- 9/14 是同一心跳里的继承分类，不是独立根因证明
- `window_miss` / `protocol_or_parser` 不是这 14 次的主因；future-opening 调度已修，但是窗口外
- 因此允许做 WP-E2 主机整改。若新主机失败率只降到约 40%，停在采集层，不准进入 WP-S0

§9.1 对「停止消毒」的精确含义，必须遵守：

1. **保留** `src/edge_lab/network_safety.py` 的 `safe_error_details()`。这是隐私脱敏，不去掉 message/URL/query 的保护。
2. **保留** 顶层 `capture_error.error_code = capture_failed`。V0.7 的 `_validated_source_capture_error()` 冻结合约就是这个；不要把它放宽成 `connection_failure` / `tls_failure` / `request_timeout`。
3. **真正要停的是丢腿码**：新主机的 capture-summary 必须保留 `recorder_leg_failures[].error_type` 与 `error_code`（例如 `persistence_sink_timeout`）。`_validated_recorder_leg_failures()` 已经接受任意非空 code。`_recorder_failure_details()` 对 `RecorderWorkerError` 已经转发这两项。若部署后仍看不到腿码，查的是写入/落盘路径，不是去改顶层 `capture_failed` 校验器。

## 已知主机事实

旧采集堆叠所在实例（以只读 AWS 核验为准，不要盲改）：

- Region: `eu-west-1`
- Instance: `i-045bb69f9cba2dadd`
- 类型：`t3.small`，credit mode `standard`，曾经 CPUCreditBalance ≈ 0
- OS：Amazon Linux 2023
- 旧路径：`/opt/poly-mm-v06`、`/opt/poly-mm-v07`、`/var/lib/poly-mm-v06`、`/var/lib/poly-mm-v07`
- rawcap 应保持 maintenance-only，禁止在同一饱和盘上再挂全量帧

WP-E2 最低规格（LIVE_COMPLETION_SPEC）：

- 停用或迁走已经 CPU-credit 耗尽的 `t3.small` 上的旧 recorder 堆叠
- 新实例：非抢占、非耗尽型 credit（或无限 credit 的非 burstable），可用内存 ≥ 2 GiB，`/var/lib/poly-mm-v08` 可用磁盘 ≥ 10 GiB，日增量投影 ≤ 1 GiB
- compact pair capture：只留配对四 token、30s 全深锚、5s 公开 taker poll、top-of-book 变化、官方 60s RTDS
- `persist_raw_clob_frames=false`，`persist_reconstructed_full_depth_frames=false`
- 单腿 recorder 失败 → 整个 cohort dirty
- 连续 RTDS：K15 开盘前开始，贯穿 K5 开盘与 common close；gap > 2s 或 disconnect → dirty
- SNS topic 至少一个 **Confirmed** 订阅；bootstrap 必须因 `POLYMM_SNS_TOPIC_ARN` 缺失或订阅仍 `PendingConfirmation` 而失败
- `deploy/aws/watch/watch-config.json` 的 `sns_topic_arn` 在 git 里保持空字符串；真实 ARN 只写部署产物

不要在旧饱和 `t3.small` 上再叠 v0.8 单元。先停旧 recorder 或换实例，再装 v0.8。不要把 CPU credits 改成 `unlimited` 当作“新主机”；那不是规格里的非 burstable 路径，除非用户另行批准成本。

## 建议执行顺序

1. 只读核验控制面：`aws sts get-caller-identity`、默认 region、能否看到 `i-045bb69f9cba2dadd`。把账号/ARN 打码后写入本会话报告，不要提交密钥。
2. 只读核验实例：instance type、credit specification、CPUCreditBalance、磁盘、现有 systemd 单元、`/var/lib/poly-mm-*` 用量。若实例已不在或 region 不对，停下来说明，不要猜另一台机器。
3. 做容量整改，二选一，优先能留下只读采集、且不再用耗尽型 credit 的方案：
   - 换成非 burstable / 非抢占实例，迁走 v0.8 运行根；或
   - 在用户明确接受成本后，停旧 recorder 堆叠并给现有机足够磁盘/内存，但仍须满足 credit 耗尽不允许。
4. 安装 v0.8 只读单元，不要启用任何 order path：
   - 用户 `polybotv08`
   - checkout `/opt/poly-mm-v08`，绑定本仓库当前 HEAD（先记下 SHA）
   - 运行根 `/var/lib/poly-mm-v08/{data,monitor,reports}`
   - 按 `deploy/aws/edge_readiness_v08/README.md` 先启动 continuous RTDS，再启动 paired capture 与 health timer
   - 配置必须是 SERVICE_CONFIG 里的 compact capture，不是 rawcap 全量帧
5. 告警：创建 SNS topic，至少一个 Confirmed 订阅（邮箱或短信，问用户要地址如果还没有）。实例角色需要 `sns:Publish` 到该 ARN，以及 `cloudwatch:GetMetricStatistics`。真实 ARN 只进 `/etc/polymm-watch.env` 或部署副本，不进 git。
6. 新主机上确认 capture-summary 保留 `recorder_leg_failures` 的 `error_type` / `error_code`。若被折叠或丢失，修写入路径；不要改 `safe_error_details()`，不要放宽 `_validated_source_capture_error()`。
7. 连续跑满 **4 个 common-expiry cohort**：官方 RTDS + 四 token L2/全深锚 + 两边公开成交 + fee/rule + 三时钟。单腿失败整组 dirty，不准插值。
8. 写出 `btc-twap-capture-capacity-evidence.v1`，用 `evaluate_capture_capacity()` 判定。缺一不可：
   - `capture_failure_count / capture_attempt_count < 0.05`
   - `free_disk_bytes >= 10737418240`
   - `projected_daily_capture_bytes <= 1073741824`
   - `available_memory_bytes >= 2147483648`
   - `burstable_cpu_credit_exhausted is false`
9. 目标主机健康检查：

```text
python scripts/check_btc_twap_continuous_rtds.py
python scripts/check_btc_twap_relative_value_service.py --config SERVICE_CONFIG.json --maximum-heartbeat-age-seconds 90
```

本地仍跑：

```text
pytest -q -k "btc_twap or compatibility or sdk"
pytest -q tests/test_polymarket_sdk_migration.py::test_live_order_boundary_cannot_be_released_by_a_boolean_mapping
```

硬门必须继续拒绝全绿布尔审计。

## 验收

只有同时成立，才能把 WP-E2 标成完成：

- 旧耗尽型 recorder 堆叠已停或已迁走，v0.8 不跑在 credit≈0 的 t3.small 上
- 容量 artifact `passed=true`
- 连续 4 个清洁 common-expiry cohort
- SNS 至少 1 个 Confirmed 订阅，bootstrap 对缺失/Pending 失败
- git 中 `sns_topic_arn` 仍是空字符串
- 新 capture-summary 能看到腿级 `error_code`，顶层仍是 `capture_failed`
- 无下单单元、无凭证进 git、README 仍写不建议实盘

失败动作：失败率 ≥ 5%、磁盘 < 10GiB、投影 > 1GiB/天、内存 < 2GiB、CPU credit 耗尽 → STOP 全部统计与探针晋级。不准用更短窗口“看起来健康”覆盖。若失败率只降到例如 40%，这是采集问题，不是 Gate 0 经济 STOP，回头修主因。

## 完成后报告（中文）

1. 改了哪台实例 / 是否换机 / 停了哪些旧单元
2. 容量门五项数字与 `evaluate_capture_capacity()` 结果
3. 4 个 cohort 是否清洁；若否，按 taxonomy 四桶分类新失败，尤其看 `recorder_leg_failures`
4. SNS 是否 Confirmed（不要打印完整 ARN）
5. 是否宣称 WP-E2 完成；下一会话能不能开 v0.9 预注册（仍然不能跑 S0 判决，除非 exact count 已锁且采集已满）
6. 仍然 NO-GO 的事实一句

结束复制
