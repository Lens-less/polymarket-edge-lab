# BTC 5m/15m TWAP Relative Value 连续纸面验证

部署日期：2026-08-12  
运行模式：公共数据、paper only、真实下单硬关闭  
冻结策略规格 SHA-256：`706abcfd725b3411db2e6c74073553721db8d1d201f36e3b62d8a253b10b53b4`

## 当前结论

连续服务已经使用真实 Polymarket CLOB、Gamma 和 RTDS 数据运行，并完成首个市场轮次。当前分类仍为 `insufficient_data`；`qualified_net_pnl=null`，不能解释为 0，也不允许据此宣称盈利或进入实盘。

首个完整观察到的冻结决策面（到期 `1786536900`，决策时刻 `1786536840000`）显示：

- Binance BTC predictor 有连续且因果的 300 个 1 秒样本；
- 5m Up/Down 两个 token 在信号前和 250ms taker delay 后均有 750ms 窗口内的双边前五档盘口；
- 15m Up/Down 两个 token 在该窗口没有完整双边可执行深度；
- 30s 和 60s Chainlink TWAP 最新可知值的源时间年龄均为 2,000ms，超过冻结的 1,500ms 上限；
- 因此该样本的纸面动作是 `no_trade`，订单数仍为 0。

这是一条真实的拒绝样本，不是空数据，也不是收益样本。

截至 `2026-08-12T12:22Z`，累计严格摘要包含 5 个 pair capture、10 个唯一市场、8 个官方已结算市场和 2 个规则/边界机械可标注市场。5 次纸面决策评估全部为 `no_trade`，可解释模拟交易与 fill 均为 0；由于没有完整执行回放，成本模型仍不算完成，合格 PnL 继续保持 `null`。

## 常驻服务

- LaunchAgent：`com.lens.polymarket-btc-twap-relative-value-paper`
- 配置：`SERVICE_CONFIG.json`
- 运行状态：`data/btc_5m_15m_relative_value_continuous_2026-08-12/service/status.json`
- 标准输出/错误：同一 `service` 目录下的 `stdout.log`、`stderr.log`
- 累计摘要：`VALIDATION_SUMMARY.json`
- 单轮报告：`reports/PILOT_REPORT_*.json`

状态文件每 15 秒原子更新并带 SHA-256。健康检查同时验证：

- LaunchAgent PID 存活；
- 心跳不超过 90 秒；
- 状态哈希未被篡改；
- `paper_only=true`、`public_only=true`、`new_orders_disabled=true`；
- `authenticated_endpoints_used=0`、`orders_submitted=0`；
- 剩余磁盘不低于 12 GiB。

本地只读检查命令：

```bash
.venv/bin/python scripts/check_btc_twap_relative_value_service.py \
  --config research/btc_5m_15m_relative_value_continuous_2026-08-12/SERVICE_CONFIG.json
```

## 数据保留策略

每个同到期 5m/15m pair 使用独立不可变 capture root。落盘保留：

- 目标四个 token 的 100–500ms 级完整 book 事件，bid/ask 各前五档；
- 目标 token 成交与 market resolution；
- BTC predictor、Chainlink 30s/60s TWAP，包含源时间与本机接收时间；
- 每个市场的规则文本 hash、合约 rule hash、token/condition 映射与费用元数据；
- raw/manifest checksum 和重启 checkpoint。

丢弃无关全市场事件、非 BTC RTDS、重复 WebSocket frame 文本，以及可由完整 book 快照替代的高频 price-change/best-bid-ask。下一轮起 recorder 每 10,000 个上游事件 checkpoint 一次，避免小文件放大；每条实际保留记录仍立即 flush。磁盘低于 12 GiB 时不启动新轮次。

## 已安排监控

Codex 已安排任务 `BTC 5m/15m 纸面策略监控`，每小时回到当前任务做只读检查。它报告心跳、当前到期轮次、累计报告与市场数、规则/边界覆盖、no-trade 原因、可解释交易/成交、分类、PnL 和磁盘；异常时只告警，不重启服务、不改历史数据、不启用实盘。

## 验证结果

- 首次全仓测试：`1311 passed, 11 skipped, 1 deselected`；最终在后台实时落盘同时复跑为 `1310 passed, 1 failed, 11 skipped, 1 deselected`，唯一失败是既有动态短周期恢复模块的墙钟微基准，并非本策略路径；
- 该旧微基准隔离复跑 10/10 通过，6 路并发资源竞争时 3/6 失败，确认固定墙钟比值受调度/I/O 竞争影响；没有放宽或删除测试门槛；
- 策略/服务/报告定向测试：`28 passed`；
- 相关文件 Ruff `E/F/I`：通过；
- LaunchAgent SIGTERM 优雅退出：`last exit code = 0`；
- 当前仍需至少 2,000 个 resolved markets、500 笔可解释模拟交易、500 个可解释 fills，以及完整 chronological OOS、成本/延迟/深度/legging replay 和统计门槛。

在这些门槛全部通过前，结论保持：`不建议实盘交易`。
