# 跨市场逻辑约束与 Neg-Risk 只读实验

日期：2026-07-24  
结论口径：仅为同步公开盘口筛选，不是成交、原子执行、结算或实盘盈利证据。

## 本轮结论

三个分析均为 **0 条 accepted snapshot candidate**，必须分别解释：

| 分析 | 候选行 | 通过 | 状态 | 主要阻断原因 |
|---|---:|---:|---|---|
| NYC 温度 11 档：跨市场 exactly-one | 589 | 0 | `insufficient_data` | 589 行均为 `book_neg_risk_mismatch`；不能把真实 `neg_risk=true` 的书伪装成普通跨市场书 |
| NYC 温度 11 档：标准 Neg-Risk | 590 | 0 | `insufficient_data` | 590 行均为 `standard_neg_risk_provenance_unverified` 和 `index_mapping_missing`；32 行同时深度不足，1 个转换行缺输出 |
| 2026 Senate augmented Neg-Risk | 88 | 0 | `rejected/fail-closed` | 88 行均被 augmented、未经代码审查的 claim、空 resolution source、缺 compact market/book 与 stale/skew 门槛阻断 |

标准/逻辑最终快照为
`current-20260724-standard-v6-security-final`：

- standard Neg-Risk 包含 589 个逐档 bundle 行和 1 个转换行；
- adapter deployment/block/fee 与链上 index 没有官方保存来源时，配置值只可
  驱动诊断，不能把 graph 标为 verified；
- conversion 仍使用 `InventoryTransform`，未来获得权威 provenance 后，卖出
  数量必须严格取扣 adapter fee 后的 `amount_out`；
- logic 的 589 行都被真实 book 的 Neg-Risk 标志阻断；只看不具执行资格的
  诊断值，最大值仍为 `-0.20`。

augmented 最终快照为
`current-20260724-augmented-v5-security-final`。它保留动态/占位 outcome、
7 个失败 compact-market GET、空 resolution source、未经代码审查的
constraint claim 和全部失败行，没有从标题补全语义，也没有把 Gamma 数组
顺序当成链上 bit index。

因此，本轮没有验证出盈利策略，也没有形成实盘许可；**不建议实盘交易**。

## 证据位置

- 标准/逻辑配置：`CONSTRAINT_STANDARD_NEGRISK_CONFIG.json`
- augmented 配置：`CONSTRAINT_AUGMENTED_NEGRISK_CONFIG.json`
- 标准/逻辑最终快照：
  `constraint_experiment_runs/current-20260724-standard-v6-security-final/`
- augmented 最终快照：
  `constraint_experiment_runs/current-20260724-augmented-v5-security-final/`
- 标准/逻辑离线复算：
  `constraint_experiment_runs/replay-20260724-standard-v6-security-final/`
- augmented 离线复算：
  `constraint_experiment_runs/replay-20260724-augmented-v5-security-final/`

每个最终 run 保存：

- `candidates.jsonl`：所有逐档候选、腿级深度、腿级费用和失败码；
- `source_manifest.json` 与 `raw/`：精确 GET 正文、时刻、URL、参数和
  body SHA-256；
- `graphs.json`：constraint claim、event market-set pin、实际 event/rules
  raw source refs、graph hash 和图级阻断；
- `summary.json`：aggregate 及逐 analysis 数量、失败统计和耗时；
- `REPRODUCIBILITY.json`：候选/图/manifest/config/summary 哈希，
  runner/core/models/sources/guard/CLI 源码哈希，以及 Python/requests 版本。

标准最终快照保存 45 个 GET 响应；augmented 最终快照保存 23 个 GET
响应。两者均为 `public_get_only=true`、`network_used=true`、
`new_orders_disabled=true`。最终源码哈希中：

- `constraint_experiment.py`：
  `09df82ba3955f59420023aff9dc4b2029ffd849b66bf020dfdbe178a3712236d`
- `constraints.py`：
  `4e881c5df880283a9eebec7f74cbd4d3370b59f1fd88d17350bf41df979f3b10`
- `models.py`：
  `dc88dea62f12307bb97904c4f9b89c933596ac66bf25cb2022033b369be6e511`
- `sources.py`：
  `36d413a517ee95c06a274ccdf5bedd6d6eff4ae5bc2af080a9dc38ff1c959966`
- `network_safety.py`：
  `f22f783b784b3bc27ca6f206f17f914194b3fe21ae72f2ed7d3f9b9cacaa5946`
- `compatibility.py`：
  `8945c207603d176cc0d21de2448e63bf315c6626ded32b2727efe47ec7ec06a3`
- `scripts/run_constraint_experiment.py`：
  `cd8c76342f8ee594d94b7729112886787d99b21ba906b15ea8304fa42d07bde2`

两组离线 replay 均为 `network_used=false`，四个核心文件与各自原 run
逐字节一致。源 run 的 `REPRODUCIBILITY.json` 由调用者在 replay 前
以外部 SHA-256 固定：

- standard：`47b5566d6db84e64920df89fda29fef11ba3940a66957e838654b36fe479b759`
- augmented：`a5af1ab7a757d45f55ae6f218f049461b967d57fdfed51658fd9917e1ee8d42a`

标准/逻辑 replay 的 `candidates.jsonl`、`graphs.json`、
`source_manifest.json` 和 `config.snapshot.json` SHA-256 分别为：

- `f5ec6551da598a4f16fddbf44c19b96f63d571549209992c50f8a6cfa24c24f6`
- `049043db7ad93d2abaa0a3e79acf58eb4e6b6d43f27db244829faa1c332ffc7c`
- `5664b3c2f66509b2c844328fb016b0b22d925a034a1cd200d8943dfb092f4ccf`
- `08c93a15e606c01e3553838b2ecfb8f4fa3e14213cd6471a26a409135be17ca2`

augmented replay 对应 SHA-256 分别为：

- `e90e3418ab47ceb784f841e00d80fd238da7c61a961b40c65167a9ce03e023c8`
- `cd2da7a862b5d19de94a8561eb6f646e63f20e6a53e20f69cbbabd3daf9601a7`
- `134ad61608f41c5e293f61e66d917c30482ef475a7118178aa4be4c5abe57c6b`
- `05b0defa1883418ea22f8ea5813400c008148b1b68063a4dd5a00ece06362a45`

## 获取窗口与旧证据口径

standard v6 的 22 本书采用隔离 session、共享 `8 req/s` 调度的并发 GET
fanout，证据写入仍在主线程串行完成：

- request 启动窗口 `2,625.037 ms`；
- response/evidence receipt window `2,605.813 ms`；
- CLOB server timestamp window `7,574 ms`；
- 最大单请求 RTT `751.019 ms`；
- future server timestamp 数量为 `0`。

本次 standard v6 没有触发 stale/skew；这只说明该保存快照的
server-update window 低于固定 `30,000 ms` 门槛，不改变 provenance 与
index 门槛导致的 0 accepted 结论。

augmented v5 仅成功形成 4 本 book；request window `378.505 ms`、
receipt window `398.5069 ms`、server timestamp window `6,252 ms`、
最大 RTT `513.9069 ms`。其余 7 个 compact-market GET 的失败响应和
失败码已完整保留；缺失不能被当成零费用、零深度或有效映射。

旧 v3 口径同时更正并保留：22 个 response receipt window 为
`2,545.205 ms`，其中实际 11 腿 YES bundle 的 evidence receipt window
为 `2,395 ms`；共有 2 本书的 server timestamp 晚于本地 receipt，
最大 `115 ms`，其中用于 YES bundle 的一本为 `44 ms`。runner 从未把
server timestamp clamp 到 receipt，也未伪造 clock offset。

以下旧目录全部保留但标为 superseded：

- standard v1、v2、v3-skew30s、v4-provenance、v5-final-provenance；
- augmented v1、v2、v3-provenance、v4-final-provenance；
- `replay-20260724-augmented-v3-provenance`、
  `replay-20260724-standard-v5-final-provenance`、
  `replay-20260724-augmented-v4-final-provenance`。

## 最终安全与 provenance 修复

1. 配置 schema v2 要求 constraint evidence 精确等于 canonical claim hash
   与 pinned event market-set hash；运行时 graph 还绑定实际 event 和每个
   rules body 的 source ref。能成为 verified 的 claim 还必须命中代码内
   `_REVIEWED_CONSTRAINT_CLAIMS` 审查清单；配置自签名、自由文本或 fixture
   adapter/index 值不能制造 accepted 正 edge。
2. `PublicGETSession` 在 dispatch 前限制官方 HTTPS GET、拒绝敏感参数，
   强制 `allow_redirects=false` 并校验最终 URL；证据层再次校验、对敏感
   URL/参数脱敏，`public_get_only` 聚合全部边界门槛。敏感键匹配先折叠
   大小写及分隔符，因此 `apiKey`、`accessToken`、`password` 等嵌套字段
   同样在写盘前被拒绝。
3. run 固定实现/依赖与产物哈希；`--replay-source-run` 从保存的 manifest
   和 raw 正文离线重算。`--replay-source-repro-sha256` 要求调用者提供
   外部信任锚；replay 在读取 raw 前校验源 Repro、本体 5 个 artifact、
   实现 provenance、run/config identity 和 raw body hash，并把完整 lineage
   写入 replay summary/Repro。

最终共享套件：`133 passed`；外部锚篡改、自重哈希篡改、实现漂移、嵌套
camelCase 凭证和 config-only standard provenance 定向复核：`8 passed`。

## 复现命令

当前公开快照：

```bash
./.venv/bin/python scripts/run_constraint_experiment.py \
  --config research/edge_discovery_2026-07-24/CONSTRAINT_STANDARD_NEGRISK_CONFIG.json \
  --output-root research/edge_discovery_2026-07-24/constraint_experiment_runs \
  --run-id <new-unique-run-id> \
  --proxy http://127.0.0.1:7897
```

离线 raw replay：

```bash
./.venv/bin/python scripts/run_constraint_experiment.py \
  --config <source-run>/config.snapshot.json \
  --replay-source-run <source-run> \
  --replay-source-repro-sha256 <externally-trusted-sha256> \
  --output-root research/edge_discovery_2026-07-24/constraint_experiment_runs \
  --run-id <new-unique-run-id>
```

`--proxy` 仅接受无认证 loopback HTTP(S) URL，不写入证据。run ID 只可创建
一次；已存在时直接失败，避免覆盖历史结果。

## 经济正确性边界

- 只调用 Gamma/CLOB 官方公开 GET；不读取 `.env`、API key、钱包或签名。
- 不调用订单、取消、链上 adapter、split/merge/redeem 或资金路径。
- 规则、resolution source、condition/token/event set、constraint claim 和
  实际 raw source ref 逐层绑定；任何缺失或漂移都阻断。
- 公开 Gamma/CLOB 当前不能证明 adapter deployment/block/fee；因此标准
  Neg-Risk 配置 provenance 始终 fail-closed，直到新增权威保存来源。
- augmented Neg-Risk 始终 fail-closed，直到动态 outcome 生命周期、显式
  链上映射和结算规则得到独立验证。
- 每条腿独立使用 compact CLOB fee、tick、min order；Gamma fee 若与
  compact fee 冲突则不定价。
- 逐档吃单、六位份额精度、book hash、RTT、age/skew、depth、fee
  HALF_UP 跳点与 `2x` rounding coverage guard 全部保守处理。
- `accepted_snapshot` 即使未来出现，也只表示单次同步快照数学通过；
  非原子跨书成交、排队、撤单、链上确认、gas 波动和最终 payout 仍需
  独立 forward replay 与结算证据。
