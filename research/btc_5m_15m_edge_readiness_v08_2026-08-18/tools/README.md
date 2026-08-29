# 结构 floor 的最小证伪工具

只读公开数据。不使用凭证，不调用认证端点，不下单。

## live_structural_probe.py

同时采集：

- 官方 RTDS `crypto_prices_twap_sixty` / `btc/usd`（`wss://ws-live-data.polymarket.com`），
  用于取共同到期的两个开盘行权价 `K15`、`K5` 和终值 `T`；
- 同到期 5m / 15m 两个市场四个 token 的公开盘口（`clob.polymarket.com/book`）。

```bash
python live_structural_probe.py <运行分钟数> <采样间隔秒> <输出.jsonl>
# 例：python live_structural_probe.py 55 2 run.jsonl
```

## analyze_structural_floor.py

用真实行权价确定 floor 方向，并按三种执行口径算每对成本：

- `taker`：两腿都吃单，含官方动态费 `0.07·p·(1−p)`
- `maker_taker`：一腿挂在买价、另一腿吃单对冲（v0.8 预注册选的形态）
- `maker_maker`：两腿都挂在买价

```bash
python analyze_structural_floor.py run.jsonl
```

## observation_2026-08-18T1042Z.jsonl

2026-08-18 10:42–11:37 UTC 的原始观测快照，覆盖 5 个 common expiry，
其中 11:00、11:15、11:30 三个到期有完整的行权价与结算终值。
`CLAUDE_FINAL_ASSESSMENT.md` 的 §1.3 与 §2.1 全部来自这份数据。
