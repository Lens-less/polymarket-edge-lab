# BTC 5m/15m Relative-Value v0.3 前向纸面部署报告

生成时间：2026-08-12 13:40 UTC（Asia/Shanghai 21:40）

## 当前结论

策略已经实际运行过模型、因果盘口、顺序双腿成交、费用、深度、部分成交、尾差和结算链；首个完整前向市场对不再是单纯的数据门槛 `no_trade`。

首个完整市场对（共同到期 `1786541400`）在冻结的四个决策点中产生两笔 development-shadow 纸面交易，税费后合计净 PnL 为 **+9.140645 USDC**。这是积极的可执行性信号，但仅来自一个共同到期事件、两个高度相关决策，不能据此认定策略已有稳定优势。当前正式分类仍为 `insufficient_data`，`qualified_net_pnl` 仍为 `null`。

## 首个完整周期结果

| 决策点 | 原始模型 | 纸面动作 | 实际成交/状态 | taker fee | 结算净 PnL |
|---|---:|---|---|---:|---:|
| tau=240s | predictor 不足 | 无 | 无 | 0 | null |
| tau=180s | q5=0.0014, q15=0.0925 | Long 5m Up + Long 15m Down | 7 fills；`failed_unhedged`，尾差 0.000012 股低于最小平仓量 | 0.81535 | +4.083313 |
| tau=120s | q5=0, q15=0.00595 | Long 5m Up + Long 15m Down | 4 fills；`complete` | 0.72787 | +5.057332 |
| tau=60s | q5=0.00005, q15=0.00005 | no_trade | 决策点四 token 因果盘口面不完整 | 0 | null |

两笔交易合计部署现金 50.341970 USDC、费用 1.54322 USDC、净 PnL 9.140645 USDC；简单的当轮净收益/部署现金为 18.16%。该比率来自同一结算事件上的重叠风险，不是独立样本收益率，也不是年化收益率。

## 执行真实性

- 决策只使用 source time 与经 SNTP 保守校正后的 receipt time 均不晚于决策点的数据。
- 第二腿数量在第一腿实际成交可见后确定，并重新承受自己的 250ms taker delay。
- 若第二腿失败，unwind 也重新承受自身 250ms delay；不使用失败可知前的盘口。
- 逐档走真实捕获深度，按实际成交量计 fee；USDC 六位基础单位不能表达的 share dust 向下舍弃。
- 结算由精确 Chainlink 30s/60s opening/closing TWAP 和冻结 token/condition 映射交叉验证。
- development shadow 与 qualified/OOS 证据完全隔离；正 PnL 不写入 `qualified_net_pnl`。

## 常驻服务与监控

- LaunchAgent：`com.lens.polymarket-btc-twap-relative-value-paper`
- 当前进程：PID 65348（报告时健康，正在采集共同到期 `1786542300`）
- 状态：`data/btc_5m_15m_relative_value_paper_v03_2026-08-12/service/status.json`
- 冻结参数：`PREREGISTRATION.json`
- 汇总：`VALIDATION_SUMMARY.json`
- 定时监控：Codex task `btc-5m-15m`，每 30 分钟读取健康、时钟、成交、结算、PnL、错误与磁盘；只读诊断，不自动重启或交易。

报告时健康检查：PID、状态 hash、心跳、纸面安全门和磁盘均通过；空闲磁盘约 37.4 GB。两轮证据中 CLOB websocket 发生 23 次 error/reconnect，服务均保留并报告，未把断流区间伪装成可交易数据。

## 验证状态与缺口

- 完整回归：1330 passed、11 skipped、1 deselected；随后新增真实精度回归与相关执行测试也通过。
- 当前汇总：2 capture cycles、8 decision reports、4 officially resolved markets、3 raw shadow models、2 settled shadow trades、11 fills。
- 完整决策覆盖率目前 0.5；tau=240 与 tau=60 的缺口如上，不能填零。
- 仍缺 chronological walk-forward calibration/OOS、至少 2000 个 resolved markets、500 笔模拟交易和 500 个 explainable fills，以及 Brier/ECE/bootstrap/集中度等冻结晋级门槛。
- 当前可行性判断：**工程执行链可行，首轮经济结果为正且值得持续采样；统计优势尚未验证，不建议实盘。**

## 安全边界

全程 `paper_only=true`、`new_orders_disabled=true`、`orders_submitted=0`、`authenticated_endpoints_used=0`。未使用凭证、签名、认证端点、链写入或真实订单。
