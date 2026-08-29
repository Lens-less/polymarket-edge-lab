# BTC 5m/15m 结构线停手证明（2026-08-22）

Status: **STRUCTURAL STOP**。这条线关闭，不再采集、不再写适配器、不再做 12 USDC 探针。
Date: 2026-08-22
Decision owner: 本仓库独立复核，不是转述 [PROJECT_REVIEW_2026-08-22.md](../../PROJECT_REVIEW_2026-08-22.md)。

用户要求：先判断这条策略有没有真实盈利可能；有则修工程，无则放弃。
结论是 **无**。继续给它做主机、Gate 0、200-expiry shadow 或 venue 适配器，都是沉没成本。

---

## 0. 一句话

同到期 BTC 5m/15m 结构对的公允价值是 `1 + b`，其中 `b >= 0` 由结算规则强制。
市场把这个结构定价到一个 tick 以内的无套利边界上；三种执行口径都没有可交易的正 floor。
即便按比实测更好的乐观上界，容量也到不了本仓库自己写的 14 日 x $20/天商业门槛。

这不是工程没做完。这是策略层死亡。

---

## 1. 结算恒等式（不依赖样本）

同一个 common close 上，5m 与 15m 共用一个官方 60 秒 TWAP 终值 `T`，各自比自己的开盘 TWAP。

| 行权价 | 结构对 | 可行状态收益 |
| --- | --- | --- |
| `K5 > K15` | 买 5m Down + 买 15m Up | 同向付 1，分裂付 2 |
| `K5 < K15` | 买 5m Up + 买 15m Down | 同向付 1，分裂付 2 |
| `K5 = K15` | 两个交叉对都只有同向 | 只付 1 |

因此结构对：

    最坏收益 = 1
    公允价值 = 1 + b    （b = 市场隐含分裂概率 >= 0）

`b >= 0` 不是经验规律。不可能的分裂状态被行权价排序直接删掉。

设两腿中价之和为 `1 + b`，买卖价差各约 1 分钱，taker 费为 `0.07 * p * (1-p)`：

| 执行形态 | 每对成本 | 正 floor 条件 |
| --- | --- | --- |
| 双 taker | `1 + b + 价差 + 两腿 taker 费` | 需要 `b < 0` |
| 一腿挂单 + 一腿 FOK | `1 + b + 对冲腿 taker 费` | 需要 `b < 0` |
| 双腿挂单 | `1 + b - 半价差之和` | 需要 `b` 小于两腿半价差之和 |

maker+FOK 的半价差精确抵消。这是 v0.8 曾经选过、现已排除的 live 形态。
双 maker 是唯一算术上可能翻正的形态，条件是 `b` 小于两腿价差之和（通常约 0.01-0.02）。
临近到期 `b -> 0` 时，价差和盘口也一起塌缩：输家腿没有买盘，赢家腿没有卖盘。
剩下可执行的组合是吃残渣 ask + 挂在已定赢家 bid，成本落到 `1.000` 再加一点 taker 费。

---

## 2. 独立复算，不是转述

原始观测：[tools/observation_2026-08-18T1042Z.jsonl](tools/observation_2026-08-18T1042Z.jsonl)
（2026-08-18 10:42-11:37 UTC，公开 Gamma / CLOB / RTDS，无凭证、无下单）

复算口径比原脚本更宽：缺一侧盘口的样本不算作丢弃，而是按仍可执行的腿组合计价。

    twap seconds covered: 3077
    book samples: 1017

| 到期 UTC | 行权价 | 结算 | 最便宜可执行组合 | 双 maker | 双侧盘口时的 b |
| --- | --- | --- | --- | --- | --- |
| 11:00 | K5-K15 = +12.74 | **分裂发生** | 1.24000 | 1.24000 | min 0.250 / 中位 0.500；b<0.02 为 0 |
| 11:15 | K5-K15 = +85.91 | 未分裂 | **1.00007**（吃 5m Down 0.001 + 挂 15m Up 0.999） | 1.00100 | min 0.062 / 中位 0.110；b<0.02 为 0 |
| 11:30 | K5-K15 = +44.88 | 未分裂 | 1.00900 | 1.00900 | min 0.034 / 中位 0.360；b<0.02 为 0 |

三个有完整行权价与终值的到期，三种执行口径，**正 floor = 0**。
摸到边界的只有 11:15 最后 25 秒：结果已定，5m Down 没有买盘、15m Up 没有卖盘，成本恰好 1.00007。

原评估里写的「1,017 样本正 floor 0 次」在缺边样本补回之后仍然成立。
更好的一次不是正期望，是无套利边界被摸到之后扣费为负。

2026-08-22 当天又读了当前 8:15-8:30 ET 这对市场：

- 两个市场 `clobRewards = null`，这条线没有补贴。
- 5m 开盘前，两腿 maker 成本钉在 1.00 / 0.99；便宜的那条交叉对在 K5 未知时不能当结构对。
- 5m 开盘后 ttc=298..218：5down+15up 成本 1.16-1.29，5up+15down 成本 0.69-0.81。盘口隐含 P(15m Up) 远大于 P(5m Up)，对应 K5 > K15，结构对是贵的那条。便宜的那条是非 floor 方向，不能拿来宣称发现了 edge。

---

## 3. 容量也不是生意

按预注册商业门槛：连续 14 个 UTC 日、按到期聚合净收益 >= $20/天，占用本金 <= $2,000。

乐观上界（比实测最好值更乐观）：

    96 个 common expiry/天 x 150 对 x $0.002 ≈ $29/天

实测最好值是 0.000，扣费后为负。流量够，每对毛利不够。
风险消失的地方，价差也消失。这不是把采集做稳就能翻过来的。

v0.6 纸面 +87.52 USDC、公开 touch、Gate 0 上界、maker+FOK 任何正数，都 **不得** 再解释成盈利证据。

---

## 4. 为什么不再跑正式 Gate 0 / 200-expiry

[LIVE_COMPLETION_SPEC.md](LIVE_COMPLETION_SPEC.md) 曾要求先换主机、采满 exact count、再让机器判 STOP。
那在「还不知道算术是否可能」时是对的。现在已经知道：

1. 恒等式把 maker+FOK / 双 taker 判死；
2. 双 maker 只在 `b` 小于价差之和时可能；
3. 有官方 TWAP 的样本里这个条件一次都没出现；
4. 边界样本的可执行组合在扣费后为负。

再花 3-7 天采 41 或 200 个到期，只能把「0/3 个到期」变成「0/N 个到期」。
它不能把 `b >= 0` 变成 `b < 0`，也不能在价差塌缩时凭空造出 1 分钱的免费队列。

因此 **正式跳过 WP-S0 / WP-S1 / WP-E0 / WP-E1 / WP-L**。
未完成的 WP-E2（换非 burstable 主机、SNS、v0.8 compact capture）也不再为这条线执行。

这不是放宽预注册。这是预注册停手条件第 1 条在更强证据下提前触发：
双 maker 可执行 PnL 的已观测上界 <= 0，且平均远低于 $0.5/expiry。

---

## 5. 明确停止的工程

不要做：

- 为结构线采购或升级采集主机
- 跑 `build_btc_twap_executable_upper_bound.py` 做 PASS/STOP 经济判决
- 200-expiry locked shadow / rolling journal
- 双 maker ProbeVenue、签名路径、12 USDC 结构探针
- 解除或削弱 `assert_new_orders_disabled()`
- 继续调 tau / lambda / veto / Monte Carlo
- 把奖励收入算进这条线

保留：

- 本目录全部研究证据与代码，作为死亡档案
- 新订单硬门
- `validated_profitable = 0` 的证据等级合同

---

## 6. 其它方向不是这条线的修理

| 方向 | 已有分类 | 能否救活结构线 |
| --- | --- | --- |
| 天气概率 | 市场 Brier 优于模型；正式策略 abstain | 否 |
| Neg-Risk / 逻辑约束 | 1,267 候选 fail-closed | 否 |
| 标的延迟 | 单市场、未结算、无成交 | 否 |
| 旧单 token 做市 | rejected | 否 |
| 选择性奖励做市 | promising_not_validated；真实 scoring/payout = 0 | **不是本策略** |

奖励线有真实补贴池，但这是另一条、补贴依赖、尚未验证的策略。
2026-07-24 的回放已经表明：单腿逆向选择中位通常大于一次完整配对毛利。
**本决定不自动转向奖励线，也不批准 12 USDC 奖励探针。**
若以后要开新 track，必须新的预注册；不得复用 v0.8 journal、结构探针限额或本停手证明。

---

## 7. 机器可读决定

    {
      "track_id": "btc_5m_15m_edge_readiness_v08_2026_08_18",
      "decision": "STRUCTURAL_STOP",
      "strategy_live": "NO_GO",
      "execution_probe": "NO_GO",
      "reason": "identity plus official-TWAP samples show no executable positive floor; capacity cannot clear the preregistered commercial bar",
      "do_not_run": ["WP-E2", "WP-S0", "WP-S1", "WP-E0", "WP-E1", "WP-L"],
      "hard_gate": "keep_assert_new_orders_disabled"
    }

当前 `PREREGISTRATION.json` 的 `current_decision` 已改成同一结论。
