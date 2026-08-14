# AWS Linux paper v0.6 deployment report

v0.6、独立 rawcap 与进展 watcher 于 `2026-08-14T18:49Z` 起在
`eu-west-1` 实例 `i-045bb69f9cba2dadd` 运行；首个完整 v0.6 周期于
`2026-08-14T19:24:33Z` 通过验收。live trading 始终关闭。

## 不可变身份

- Evidence track：`btc-paper-v06-20260814`，prospective-only after
  `2026-08-14T18:15:00Z`，迁移测试来源为 `btc-paper-v05-20260813`。
- 冻结 implementation revision：
  `5dcc3bd7e09794c578d6572181be81388fdb6661`。
- rawcap / v0.6 runtime release：
  `9a97a10831248a9a6c949fc239eedee45176c6a8`。
- watcher 最终 release：
  `df0e7daac9e659810025ead16a2a9fb361be658b`。
- Frozen strategy SHA-256：
  `ac39ca354c106e11a0bc7e6fd4bbf2ee954e8044aafdbde9f3f2183c69020d1a`。
- Settlement registry SHA-256：
  `692fba2af2aed369b6ebf05197974150e4def3ebd266197e06555955aa71bd0d`。
- 预注册引用的 base gap manifest 保持冻结 SHA-256
  `2a49798d53b730273eac9614be4205e06947130b29cfbad9f318d0c81c89696f`；恢复闭合
  另记于 `GAP_CLOSURE.json`，没有原地修改冻结 manifest。
- 三个安装树的 revision markers 与上述值逐字匹配；v0.6 主机上的 strategy
  和 registry 哈希与预注册一致。
- 部署包经私有、AES-256 server-side encrypted S3 桶做一次性内容寻址传输。
  主机验收哈希后，5 个临时对象和整个传输桶均已删除；它们不是研究证据。

## 服务状态

| 单元 | 验收状态 | 说明 |
|---|---|---|
| v0.4 service / health timer | inactive / disabled | 最终状态已封存；历史文件未删 |
| v0.5 service / health timer | active / enabled | 冻结轨道未改；当前 60s/60s 盘下按设计 `error_wait` |
| rawcap service / health / maintenance | active / enabled | 独立落盘、5 分钟健康检查、30 天保留 |
| v0.6 service / health timer | active / enabled | 60s/60s prospective migration track |
| watcher timer | active / enabled | 每分钟检查 progress；SNS 发布通道已配置 |

v0.5 在 `19:28Z` 仍为 156 份报告、21 笔 shadow trade、净
`+9.46586700 USDC`，新鲜心跳且五个 paper guards 安全；其不健康仅来自冻结
30s/60s universe 与当前 60s/60s 市场不匹配。

## rawcap 首周期验收

- 第一份完成的 capture 从 `18:49:43.428716Z` 运行至
  `19:06:04.043383Z`，包含 61 manifests、230,517 records、2 个目标市场和
  4 个资产。
- `checksum_mismatches`、`invalid_manifests`、`manifest_without_raw`、
  `raw_without_manifest`、`orphan_partials` 全为空，`capture_error=null`。
- 逐市场规范化身份为 `chainlink_twap_60s_5m_and_60s_15m.v1`，
  `qualification_status=eligible`、`destination=v0.6`、`quarantine_count=0`。
- 验收时已完成 2 个 capture，并继续采集下一周期；未知 regime 的测试覆盖证明
  其只进入 quarantine，不会停止原始落盘。

## v0.6 首周期验收

- Cycle `1786734900` 从 `18:49:44.097586Z` 开始，capture summary 在
  `19:21:00.785358Z` 生成：88 manifests、25,284 records、2 个 recorder
  legs、零 recorder failure、零 integrity failure。
- τ60 / 120 / 180 / 240 四份报告全部为
  `btc-5m-15m-relative-value-pilot-report.v2`，且
  `verified_report_v2=true`、`raw_shadow_model.available=true`。
- 四个 canonical 动作均为 `no_trade`；完整 order-book surface 可用，但
  strictly-prior isotonic calibration 尚不存在。qualified 与 OOS 全部按预注册
  以 `past_only_calibration_insufficient` fail-closed。
- `paper_only/public_only/new_orders_disabled=true`、`orders_submitted=0`、
  `authenticated_endpoints_used=0`；guard missing/unsafe fields 均为空。
- `qualified_net_pnl=null`：首周期没有经济尝试、fills、残余风险或已实现 PnL，
  `null` 不得解释为零。
- CLOB 首周期累计 24 次断连/重连记录，但双 recorder legs、四 token signal 和
  delayed-execution surface 均完整，`degraded_capture_cycles=0`、
  `recorder_leg_failures=0`、`evidence_healthy=true`。该频率列为 24h 趋势监控项，
  当前不构成证据缺失。

## 并行候选验证

- 每份报告都带 non-canonical candidate metadata；scoreboard 已消费 4 份报告。
- A1 极端概率 veto、A4 loss-probability veto、B3 1.25x/1.5x/2x 双腿深度缓冲
  均已开始并行计分；首周期没有 canonical trade，因此没有可评估的反事实 PnL。
- A2 的 lambda 网格已出 Brier，但 4 个样本来自同一个 expiry cluster 的四个 τ，
  只能证明管线可运行，不能用于选 lambda 或声称校准改善。
- 代码审计字段确认 canonical 路径已经同时具备 B1 的双腿完整深度 walking 与
  B2 的 750ms timeout 后立即 unwind；首周期经济上显著第二腿失败为 0。

## 主机与安全边界

- t3.small 验收时 `MemAvailable≈920MB`；v0.5 / rawcap / v0.6 RSS 约
  `340MB / 121MB / 229MB`，均低于冻结预算。
- 磁盘可用约 `36.3GB`，高于 12GiB fail-closed 门槛；CPU credit 在 19:05Z
  为 `430.86`。
- Chrony 使用 Amazon Time Sync `169.254.169.123`，验收时系统偏差约
  `1.2µs`，Leap status Normal；四个决策的 clock policy 全部通过。
- rawcap 与 v0.6 进程环境未发现 AWS access key/session token、私钥、wallet 或
  proxy 变量。策略与采集服务的零凭证姿态未改变。
- watcher 单独通过实例角色获得唯一豁免：只允许向
  `arn:aws:sns:eu-west-1:998948477566:polymm-paper-alerts` 执行
  `sns:Publish`。topic 当前没有 subscription，需另给 email/webhook 才能收到
  SNS 消息。

## 监控验收

- watcher 对 rawcap 与 v0.6 当前无活跃告警；预期保留 v0.5 的
  `capture_stalled` 与 `regime_mismatch`。
- v0.5 冻结版 status/health 文件为 `0600`。未修改其权限；watcher release
  `df0e7da` 将这种情况准确标记为 `telemetry_unavailable`，不再误报 stale
  heartbeat。外部巡检仍可经 SSM 直接验证 v0.5 的真实心跳与 phase。
- Codex heartbeat automation `btc-v0-6-rawcap` 已启用，每 30 分钟做只读巡检，
  覆盖 unit、progress、regime、quarantine、guards、候选计分板、内存、RSS、
  磁盘和 CloudWatch CPU credits；不会改服务、配置、证据或参数。

## 验证记录与剩余边界

- 相关回归共 240 项通过；新增模块全量 Ruff 通过，触及的 legacy 模块按仓库
  F/E9/I 基线通过；`compileall` 通过。
- 全仓 pytest 的 Windows collection 仍受既有 `fcntl` 与可选
  `py_clob_client` 环境依赖阻塞；部署相关 Linux runtime 已通过主机首周期验收。
- 尚未完成的不是修复项，而是数据窗口：24h 实测写入/压缩率、多个独立 expiry
  clusters 的候选表现、以及 v0.6 自身 strictly-prior calibration 解锁。任何候选
  采纳仍须另行预注册，v0.6 冻结策略不得原地改动。
