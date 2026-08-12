# Polymarket MM Bot 彻底重设计与验证结果

日期：2026-07-24  
状态：研究完成，实盘禁用  
结论：**尚未证明存在可稳定盈利的策略，不建议实盘交易。**

## 1. 最终判断

旧项目的“普通单 token 双边做市 + 参数优化”路线应停止。它没有提供可用的盈利证据：

- 所谓 real-data backtest 只读取一次当前市场，再生成正弦/余弦价格路径。
- 示例 backtest 主动在 best ask 买、best bid 卖以强制成交，天然损失 spread。
- dry-run 允许没有 outcome token 库存的 SELL，并把 crossing 当作 maker fill。
- 旧费用模型是固定 2% taker / 1% maker rebate，与当前价格相关费用曲线不符。
- 旧实盘适配器使用已归档的 CLOB V1 SDK；2026-04-28 后不兼容生产 CLOB V2。
- 聚合 L2 不提供 maker 身份、订单年龄或 queue position，无法从公开簿证明真实 fill probability。

重新设计后的主方向不是预测事件，而是：

> **奖励感知、完整集约束的 YES/NO 配对 post-only 做市。**

它有两种库存合法的形态：

1. `BUY YES + BUY NO`：两腿都成交后得到完整集，merge 回 1 pUSD。
2. 先用 1 pUSD split 完整集，再 `SELL YES + SELL NO`：两腿都成交后释放完整集库存。

该方向的机械关系成立，但本次回放显示，单腿逆向选择通常大于一次完整配对毛利。当前唯一仍值得继续观察的子方向是：

> **低流量、低竞争奖励市场中的“近零成交奖励采集”，用完整集库存和严格 flow veto 控制风险。**

它仍是补贴依赖假设，不是结构性套利；必须用自己的真实 order-scoring 和实际奖励支付验证。

## 2. 旧基线的复现

旧 mock backtest，500 个快照：

| 指标 | 结果 |
|---|---:|
| 初始资本 | $1,000 |
| 最终资本 | $952.24 |
| 总收益 | -4.78% |
| 成交数 | 499 |
| Sharpe | -0.2494 |
| 最大回撤 | 21.02% |

`autoresearch_verify.py` 同样没有给出正收益证据。此前的参数搜索只是把负 Sharpe 从一个负值移动到另一个负值，并推动 spread 接近零。

本地 dry-run 日志也不能作为收益证据。其中一个日志有 161 次 BUY 和 161 次 SELL，但 BUY 名义额约 `$24.9`、SELL 名义额约 `$79`；这是无库存 SELL 与失真撮合制造的现金流，不是可实现利润。

## 3. 新系统结构

新增的 `src/edge_lab/` 与旧 live stack 隔离，不读取凭证、不发订单。

### 3.1 Experiment 0：兼容与合规 preflight

`compatibility.py` 检查：

- 是否仍安装旧 V1 SDK。
- 是否安装 CLOB V2 / unified SDK。
- 当前仓库是否已有 V2 adapter。
- pUSD、签名、trade terminal status 是否已验证。
- geoblock 请求是否成功且允许下单；持久化报告不保存 IP/国家/地区。

本机结果：

```text
py-clob-client legacy: 0.34.6
py-clob-client-v2:     未安装
polymarket-client:     未安装
read-only ready:       true
new live orders ready: false
```

旧 `place_order()` 已增加硬性 fail-closed guard。参数本地校验仍可运行，但任何凭证、余额、签名或发单路径之前都会阻止新实盘订单。取消路径保留为遗留状态恢复手段。

### 3.2 奖励市场扫描

`EdgeScanner`：

- 分页读取当前奖励市场。
- 批量读取 Gamma 元数据和两侧 CLOB 深度。
- 使用 Decimal、逐市场 tick、minimum size、fee schedule。
- 对配对 bids、预 split 配对 asks 分别计算：
  - 两腿都成交的完整集毛利。
  - 任一腿先成交后的逐档应急对冲损失。
  - 当前二次奖励 score 的公开 L2 竞争代理。
- 未知最小奖励订单年龄、奖励源不一致、关键字段缺失时，生产 preflight 失败。

### 3.3 深度感知完整集扫描

`complete-set-scan` 不依赖奖励市场：

- 对活跃二元市场按 5/10/25/50 份逐档走 YES/NO 深度。
- 每个深度档分别按当前价格相关 taker fee 计费。
- 同时检查 buy-both 后 merge、预 split 后 sell-both。
- 计入每份 0.2¢ 的执行缓冲。
- 明确把两张 FOK 视为非原子。

### 3.4 回放与独立录制

`HistoricalReplay`：

- 使用当前仍返回数据、但未被公开 SDK/API 合同承诺的 `/orderbook-history`。
- 对齐 YES/NO 快照。
- 不把 book touch 当作 fill。
- 公开成交只生成“上界 flow proxy”；保守下界始终是零成交。
- 计算 paired fills、单腿 p90 损失、奖励静态情景和跨腿可配对流量。

`observe`：

- 独立轮询并写入 JSONL。
- 保存接收时间、当前配置、book、quote 和经济计算。
- 可在官方历史旁路中断时继续积累自己的样本。

### 3.5 安全状态机

`PairCycle` 明确表示：

```text
IDLE
  -> BOTH_QUOTES_LIVE
  -> ONE_LEG_FILLED
  -> HEDGE_REQUIRED
  -> RECONCILING
  -> FLAT

BOTH_QUOTES_LIVE
  -> PAIR_COMPLETE
  -> MERGE_PENDING
  -> FLAT

任何异常
  -> CANCEL_PENDING / SAFE_STOP
```

硬约束：

- 配对 asks 没有预先 split 的 YES/NO 库存时禁止启动。
- 两腿不是原子 basket，分别记 matched size。
- 单腿股数、存活秒数、应急 hedge loss 和抵押品都有上限。
- BUY 配对只有 merge 确认后才回到 flat。
- 未通过市场 preflight 时直接 safe-stop。

## 4. 当前公开数据结果

### 4.1 奖励市场即时扫描

最终快照：

| 项目 | 数量 |
|---|---:|
| 奖励配置行 | 2,000 |
| 初筛后 | 63 |
| 活跃、可下单、有 24h 成交且有两侧簿 | 19 |
| 生产 preflight 通过 | 0 |
| 缺少可核验 minimum reward order age | 19 |
| CLOB/Gamma 奖励额度明确不一致 | 7 |
| 主动完整集正 edge | 0 |

所以即时排行榜只能用于研究排序，不能直接生成订单。

### 4.2 活跃市场完整集扫描

Gamma 可访问窗口内：

| 项目 | 数量 |
|---|---:|
| 活跃市场行 | 2,100 |
| 准备扫描的二元市场 | 1,978 |
| 两侧簿可执行评估 | 1,821 |
| 缺失/失效簿而跳过 | 157 |
| 费用、深度、0.2¢/份 buffer 后正 edge | **0** |

即使把执行 buffer 设为 0，在 2,000 行版本中仍然是 0 个正 edge；最接近的市场也为负。因此本快照没有发现“不依赖奖励、直接吃完整集错价”的机会。

### 4.3 五分钟独立观察

新加坡 28°C 市场：

| 项目 | 结果 |
|---|---:|
| 成功快照 | 21 / 21 |
| 奖励额度 | `$15 -> $10`，稍后又变为 `$12` |
| 显示 spread | 1–7¢ |
| 不同配对 quote | 6 组 |
| 单腿应急损失 | `$0.1344–$0.8112` |
| 静态奖励日情景 | `$0.2885–$9.00` |

同一市场在几分钟内出现奖励变化，而 Gamma 还会暂时保留旧值。这证明配置必须热更新、交叉核验和 fail-closed。

### 4.4 跨市场回放

请求 12 小时，但这些日度天气市场只提供约 3.4–4.0 小时可覆盖历史，已构成样本不足证据。

每次 quote 为 20 份，2-tick 应急 hedge 压力：

| 市场 | 覆盖小时 | 快照 | 配对毛利中位 | 单腿损失中位 / p90 / max | 静态奖励情景 | 公开 flow 压力损失 |
|---|---:|---:|---:|---:|---:|---:|
| Singapore 28°C | 4.00 | 2,431 | $1.00 | $0.605 / $1.382 / $6.330 | $0.927 | $4.803（5 分钟内无配对） |
| Munich 28°C | 3.54 | 1,247 | $1.00 | $1.904 / $2.089 / $10.271 | $1.900 | $2.204 |
| Ankara 25°C | 3.41 | 1,208 | $1.00 | $0.282 / $1.046 / $1.400 | $0.161 | $0（没有触及假设 quote） |
| Hong Kong 32°C | 3.83 | 999 | $1.00 | $0.363 / $0.667 / $1.692 | $0.126 | $0.168 |

说明：

- 静态奖励情景不是支付预测；它使用聚合 L2 竞争代理和当前奖励额度。
- flow 压力损失假设公开交易会打到我们的价，再用 p90 单腿成本解套；真实 queue 未知。
- 公开成交方向本身也只是代理。因此该表适合淘汰，不适合证明利润。
- Ankara 没有报价触及，说明“安静市场奖励采集”值得继续影子观察；不证明它实际得分或能收到超过 `$1` 的最低支付门槛。

### 4.5 参数压力：Singapore 28°C

| 距离占 max reward spread | 配对毛利中位 | 单腿损失 p90 | 静态奖励日情景中位 | 假设 ask 触及事件 |
|---:|---:|---:|---:|---:|
| 25%（更紧） | $0.60 | $1.582 | $2.142 | 13 |
| 50% | $1.00 | $1.382 | $0.994 | 8–9 |
| 75%（更宽） | $1.60 | $1.182 | $0.172 | 5 |

额外压力：

- hedge 从 2 ticks 提高到 5 ticks，单腿损失中位从约 `$0.605` 增至 `$1.223`，p90 从 `$1.382` 增至 `$1.997`。
- 奖励 multiplier 设为 0 时，奖励收入严格为 0；不能靠配置上限掩盖 spread-only 风险。
- 基准路径的 ask 公开流量在 60 秒和 5 分钟内可配对量均为 0；30 分钟内也只有约 2.53 份。

结论：更紧报价提高奖励得分，但减少完整集毛利并增加成交/逆向选择；更宽报价改善 spread 风险，却几乎放弃奖励。没有一个参数点能用公开数据证明净正期望。

## 5. 保留与淘汰

### 保留：低流量奖励采集，研究优先级 1

市场特征：

- 奖励额度足以超过最低支付门槛。
- 聚合竞争低。
- 影子 quote 很少或从不被公开 flow 触及。
- 两侧应急 hedge 深度足够，p90 loss 在预算内。
- 完整集库存使方向风险有明确上限。

当前最像这一类的是 Ankara 25°C，但只有约 3.4 小时数据，不能晋级。

### 保留：配对完整集做市，研究优先级 2

作为状态机和风险容器保留，不将 spread 当作已证明收入。只有自己的 maker fills 能校准 queue、markout 和配对完成率。

### 只读保留：Negative Risk

机械关系成立，但必须读取 event-specific `feeBips`、question count、index set、当前 adapter 地址并排除 placeholder/Other。多腿获取输入资产仍非原子。本轮不使用简化的“所有 YES 价格和”伪造收益。

### 淘汰

- 普通单 token naked MM。
- 复制排行榜钱包。
- midpoint / best quote 伪套利。
- 为 taker tier 或积分主动刷量。
- 把 daily reward 或 maker rebate 比例直接当收入。
- 当前快照上的主动完整集套利。

## 6. 晋级门槛

### Gate A：继续只读，至少 7 天

- 自采 WebSocket + REST hash 校验。
- 覆盖多个日度市场、多个城市和不同流量时段。
- 奖励配置变化、minimum order age 和 fee schedule 全部版本化。
- 每个候选必须通过：
  - reward=0；
  - rebate=0；
  - 2x 实测 p95 延迟与滑点；
  - 删除最好一天；
  - 删除最好市场；
  - 单腿不利成交。

### Gate B：V2 迁移

- 选择并锁定官方 CLOB V2 或 unified Python SDK 版本。
- pUSD collateral、signature type、funder/deposit wallet 流程。
- post-only、cancel、own trades、order-scoring。
- `MATCHED -> MINED -> CONFIRMED/FAILED/RETRYING` 对账。
- split/merge 合约地址和余额对账。
- 任何一步缺失都保持 live guard。

### Gate C：极小额 maker probe，需要用户再次明确授权

建议上限：

```text
总抵押品              <= $25
同时市场数            = 1
单腿最大暴露          <= 20 shares
单腿最长存活          <= 5 seconds
单次应急 hedge loss   <= $2
当日最大损失          <= $5
```

Probe 的目的只是测量：

- 真实 post/ack/cancel/fill latency。
- maker/taker 身份。
- 自己的 queue 代理与实际 fill。
- order-scoring 起效时间。
- 实际 Liquidity Reward / Maker Rebate 支付。

它不是获利授权。任何真实订单、签名或资金动作都不在本轮范围内。

## 7. 可复现命令

```bash
# 兼容与 geoblock 的脱敏只读审计
.venv/bin/python scripts/run_edge_lab.py audit \
  --output research/profit_redesign_2026-07-24/compatibility_audit.json

# 当前奖励市场扫描
.venv/bin/python scripts/run_edge_lab.py scan \
  --pages 4 --page-size 500 --max-evaluations 80 \
  --output research/profit_redesign_2026-07-24/reward_scan_final.json

# 活跃二元市场完整集扫描
.venv/bin/python scripts/run_edge_lab.py complete-set-scan \
  --max-markets 2100 \
  --output research/profit_redesign_2026-07-24/complete_set_scan_2100.json

# 指定 condition 回放
.venv/bin/python scripts/run_edge_lab.py replay <CONDITION_ID> \
  --hours 12 --stress-ticks 2 --competition-multiplier 3 \
  --output research/profit_redesign_2026-07-24/replay.json

# 独立录制，不发订单
.venv/bin/python scripts/run_edge_lab.py observe <CONDITION_ID> \
  --minutes 10 --interval-seconds 15 \
  --output research/profit_redesign_2026-07-24/observe.jsonl
```

## 8. 交付物

- `PRIMARY_SOURCES.md`：33 个一手来源的机制与接口核查。
- `REDESIGN_AND_RESULTS.md`：本报告。
- `compatibility_audit.json`：V2/合规脱敏 preflight。
- `reward_scan_final.json`：奖励市场最终扫描。
- `complete_set_scan_2100.json`：活跃市场完整集扫描。
- `singapore_28_observe.jsonl`：五分钟独立采样。
- `*_replay_12h*.json`：跨市场与压力回放。
- `src/edge_lab/`：扫描、经济模型、回放、公开 API 和状态机。
- `tests/test_edge_lab.py`：费用、深度、库存、对冲、回放、live guard 与状态机测试。

## 9. 一句话结论

项目现在有了一条可证伪、库存合法、成本完整的研究路线，但还没有盈利证明。下一步最合理的不是恢复旧 bot，而是连续观察“低流量奖励采集”候选；在 V2、真实得分和小额 maker probe 都通过之前，维持 **不建议实盘交易**。
