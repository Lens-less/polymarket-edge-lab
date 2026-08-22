# 对 LIVE_COMPLETION_SPEC.md（grok 撰写）的复核意见

作者：Claude
日期：2026-08-22（同日第二次复核）
复核对象：`research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md`
方法：不采信文档自身的文字描述，逐条对照当前代码、测试、已安装依赖重新验证。

---

## 结论：采信为权威合同，我今天独立验证过的部分全部属实

我没有把 grok 的说法当成事实，而是自己重新查了一遍最容易出错、
也最不能出错的几个点：

| 我核实的点 | 方法 | 结果 |
|---|---|---|
| 硬门在全绿布尔审计下仍拒绝 | 读 `compatibility.py:49-63` + 跑 `tests/test_polymarket_sdk_migration.py::test_live_order_boundary_cannot_be_released_by_a_boolean_mapping` | **属实**：把 `REQUIRED_LIVE_CHECKS` 全部塞 `True` 传进去，`assert_new_orders_disabled` 依然抛 `LiveExecutionBlocked`，match 到 "no verified production double-maker venue adapter" |
| `PREREGISTRATION.json` 的冻结数字门槛没被这次改动动过 | `git diff --stat` 该文件 | **属实**：不在改动列表里，41 / 200 / $0.5 / $20 / 20% / 14 天 等阈值原样保留 |
| README/CLAUDE.md 没有把"代码修好了"偷换成"可以实盘" | `git diff README.md`、CLAUDE.md 变更摘要 | **属实**：README 仍写"不建议实盘交易"，只是把日期挪到 08-22 并加了指向本规格的链接；CLAUDE.md 新增"`IMPLEMENTATION_AND_RUNBOOK.md` 只记录已实现表面，不能单独授权下单"——这是个好的澄清，堵住了一个真实存在的歧义 |
| Gate 0 builder 的 CLI 参数与两份规格里写的命令一致 | 读 `scripts/build_btc_twap_executable_upper_bound.py` 的 `add_argument` | **属实**：`--manifest` / `--output` / `--expected-clean-attempts`（必填）/ `--decision-tau-seconds`（可选）逐字匹配 |
| E0.2 表里列的 12 个 SDK 方法（`create_limit_order`、`post_orders`、`place_market_order`、`get_balance_allowance`、`setup_trading_approvals`、`wait_for_order_fill_settlement`、`get_order_scoring` 等）真的存在 | 实际 `import polymarket; dir(polymarket.SecureClient)` | **全部属实**，一个没编 |
| WP-H0 引用的"约 50 files, −12,245/+141" | `git diff --shortstat` | **基本属实**：实测 50 files, +149/−12,248，与引用数字仅有几行的自然漂移，不是编的 |

这是我主动检查、不是走流程——这类文档最容易出问题的地方就是"引用了
一个其实不存在的 API"或"悄悄放宽了一个阈值"，两者今天都没有发生。
基于这个验证结果，我认为可以把 `LIVE_COMPLETION_SPEC.md` 当作可信的
权威实施合同来执行，而不只是"另一个 AI 说的话"。

---

## 我完全同意的部分

1. **A.1–A.6 与 Track B 判定不变**：grok 的表格和我昨天独立读代码的结论
   逐项一致（参见 `GO_LIVE_READINESS_SPEC.md` §1）。两次独立复核收敛到
   同一结果，这本身就是一个有用的交叉验证信号。
2. **拒绝把 maker+FOK 升级为可实盘形态，新开双 maker probe 而不是改旧
   harness**：这是对的架构选择。旧 `btc_twap_execution_probe.py` 的
   "两个候选谁 maker 谁 FOK" 语义和双 maker "两腿同时挂单、超时才紧急
   平仓" 语义本质不同，硬塞进同一个类容易在 `all_in ≤ 0.99` 这类断言上
   出现"有时候检查的是错的口径"这种隐蔽 bug。新增
   `btc_twap_double_maker_probe.py`、把旧 harness 明确留作
   "ineligible harness" 单测对象，这个切法是对的。
3. **`LiveReleaseCapability` 设计**：用一个绑定 host id / adapter SHA /
   Gate 0 报告 SHA / shadow 报告 SHA / 各类收据 + 新鲜度校验的对象取代
   "布尔映射喂全绿"，同时明确保留
   `test_live_order_boundary_cannot_be_released_by_a_boolean_mapping`
   继续红/绿锁死——这是把 `compatibility.py:166` 注释里"a reviewed
   Track-C adapter owns a provenance-bound release capability"落到
   了具体可验收的设计上，而不是留一句空话。我认可这个方向。
4. **WP-P0 的防串线设计**：明确禁止把结构线的适配器、探针额度、journal
   直接挪给奖励市场用，必须重新预注册。这堵住了一个常见的作弊路径——
   策略 A 判负后，把同一套"已经能下单"的基础设施包装成策略 B 上线，
   实质上绕开了 A 的停手条件。这条禁止写得对。
5. **§9 对用户诉求的直接回答**："若用户强制要求跳过 S0/S1，本仓库的
   安全合同不允许实施"——这是正确的立场，也是我认为任何一份"实盘方案"
   都必须写清楚的底线，而不是含糊地默认用户的目标优先于预注册规则。

---

## 三处我会加强或调整的地方

这些不是反对意见，是基于历史数据看到的具体风险，值得在开始执行前
明确下来，而不是留到执行中间才发现。

### 1. WP-E2 对"采集失败率"的诊断深度不够

历史实测失败率是 93.3%（14/15），而门槛要求 < 5%——差了近 19 倍，不是
"差一点"。规格里 WP-E2 给的修复手段基本是"换更大的机器 + 关掉 rawcap
全量帧 + 确认 SNS"，这些都对，但**没有回答 93.3% 失败到底是什么原因
造成的**：是 CPU credit 耗尽导致轮询错过窗口？是 WS 断连后 REST 补拍
本身有 bug？还是采集脚本在某类响应格式下直接崩溃？如果不知道根因，
"换个更大的实例"有可能只是把失败率从 93% 降到 40%，仍然过不了 5% 的
门槛，然后错误地把这归因为"策略问题"而不是"采集脚本有一个具体 bug"。

**建议**：在正式启动 WP-E2 的主机采购/部署之前，先花一小时把已有的
v0.7 失败记录（`DEPLOYMENT_REPORT.md` 一类的产物，或历史日志）按错误
类型分类计数，确认修复动作真的对准了主要失败原因，而不是只对准了
"能想到的几个容量指标"。

### 2. "恢复原 41 棵树" vs "冻结为 v0.9 重新计数"，应该现在就决定，
   不要留到执行中间

规格 §WP-S0 正确地给出了两条路（继续找回 41 棵树 / 冻结 v0.8 未完成、
开 v0.9 新计数），但没有给出默认选项。给出背景判断：v0.8 的 41 棵树
所在的部署主机本身就是 §1.2 记录的、CPU credit 耗尽、磁盘只剩 148 MiB
的那台 t3.small——大概率已经被重建或数据已经被覆盖。继续花时间去对象
存储/快照里找这批树，收益不确定，且会占用 WP-H0/E2 本该优先的时间。

**建议**：现在就假设 41 棵树不可恢复，直接按 v0.9 的路径走——在看到
任何新数据之前，把新的 `expected_unique_common_expiry_attempts`
写进新的 preregistration 文件，而不是先花时间尝试恢复。如果之后真的
找到了旧树，作为一个独立的、事后对照用的数据集处理，不要临时改口
说"就用这些"。

### 3. "S1 只需要 2+ 天完美运行"的日历假设偏乐观

规格 §8 的日历把 200-expiry 采集估成"2+ 天"，依据是 15 分钟边界每天
96 个到期。但这个数字隐含"清洁率接近 100%"的假设，而 WP-E2 门槛本身
只要求失败率 < 5%（即清洁率 > 95%）——即使 WP-E2 达标，200 个到期
也需要 200/0.95 ≈ 211 个尝试、约 2.2 天*连续无中断*运行；一旦中间出现
任何单次容量退化（历史上发生过 CPU credit 归零），窗口会显著拉长。
这不影响判定逻辑本身，只是提醒：把"2+ 天"当成下限而不是预期值去
安排后续工作，避免因为日历乐观而在第 3-4 天产生"是不是策略又不行了"
的错误怀疑。

---

## 关于我自己那份 `GO_LIVE_READINESS_SPEC.md`

它已经被改成"复核笔记，冲突时以 LIVE_COMPLETION_SPEC.md 为准"，这个
处理是对的——我不需要改回去或提出异议。两份文档在结论上没有冲突：
都认为当前 NO-GO、都认为 A.1–A.6 已完成、都认为下一步是先跑 Gate 0
而不是先写下单适配器、都认为按现有实测 Gate 0 大概率会判负。同日两次
独立复核收敛到同一结论，这本身是一个值得记录的交叉验证，不是重复劳动。

---

## 给用户的直接建议

1. **认可 `LIVE_COMPLETION_SPEC.md` 为唯一权威实施合同**，按它的 WP-H0
   → WP-E2 → WP-S0 顺序执行，不要因为"想尽快实盘"而调整顺序。
2. **先做上面第 1 条**（诊断历史 93.3% 失败率的根因）再采购/部署新主机，
   否则有较大概率在 WP-E2 上重复踩坑而不自知。
3. **现在就把"41 棵树不可恢复"设为默认假设**，直接准备 v0.9 的
   `expected_unique_common_expiry_attempts` 重新计数方案，不要另开一条
   "先去找旧数据"的支线。
4. 下一步如果要从"写文档"转到"动手"，起点是 WP-H0（把当前 50 个文件、
   −12,248/+149 的删除与安全测试改动审查后提交），然后是 WP-E2 的主机
   容量诊断——这与 grok 的建议一致，我没有不同意见。
5. 心理预期继续按两份文档共同的判断设置：**WP-S0 大概率在第 1 天就把
   结构线判负**。这不是执行失败，是这套预注册机制原本设计要做的事。

---

## 吸收状态

2026-08-22 晚些时候，上述三点已被写入 `research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md` v0.9.1：WP-E2.0 失败分类、默认冻结 v0.8 并改走 v0.9 exact count、S1 日历改为 ≥4–7 天下限。下一会话提示词见同目录 `NEXT_SESSION_PROMPT.md`。
