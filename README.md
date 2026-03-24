# Polymarket 做市商机器人

一个面向 [Polymarket](https://polymarket.com) 预测市场的自动化做市系统，支持 `DRY_RUN` 模拟撮合、实盘下单、终端监控、风险控制和历史回测。

[![Tests](https://img.shields.io/badge/tests-363%20passing-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 当前状态

- 最新本地验证日期：2026-03-24
- 全量测试结果：`363 passed, 11 skipped`
- 测试命令：`.venv/bin/python -m pytest tests -q`
- 项目要求：Python `>=3.10`
- 当前主分支远端：`https://github.com/Lens-less/poly-mm.git`

## 这个项目解决什么问题

机器人在 Polymarket 的 YES/NO 市场同时挂双边单，目标是赚取价差并获取流动性奖励。核心逻辑不是“猜方向”，而是：

- 依据订单簿、波动率、库存和订单流动态决定报价
- 在数据不可靠、仓位超限或错误率过高时主动停手
- 在 `DRY_RUN` 中先验证执行与风控，再切换到 `LIVE`

## 关键能力

- 自适应做市：基于波动率、库存和订单簿失衡动态调整 spread
- YES/NO 平价检查：发现 `YES + NO > 1` 时撤单并跳过报价
- 风险控制：每日亏损限制、错误冷却、动态仓位限额、相关性和 toxic flow 检测
- 市场数据韧性：WebSocket 为主，REST fallback 为辅
- Live 状态同步：实盘路径会回收真实 trades 和 open orders，同步库存、PnL 和风控
- 安全机制：启动孤儿订单清理、断连撤单、陈旧订单清理、余额监控
- TUI：终端面板实时显示订单、PnL、库存、feed 状态和风控状态
- 回测：支持历史数据和参数优化

## 快速开始

### 1. 克隆

```bash
git clone https://github.com/Lens-less/poly-mm.git
cd poly-mm
```

### 2. 安装依赖

推荐用 `uv`：

```bash
uv sync --python 3.12 --extra dev
source .venv/bin/activate
```

如果你只想用 `pip`：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

最少先确认这些值：

```bash
DRY_RUN=true
CHAIN_ID=137
WS_MARKET_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
```

如果要实盘，还需要：

```bash
POLY_PRIVATE_KEY=0x...
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_PASSPHRASE=...
```

说明：

- `.env.example` 是模板
- 运行时的权威默认值在 [`src/config.py`](src/config.py)

### 4. 运行

默认先跑模拟：

```bash
python run_mm.py
```

带终端面板：

```bash
python run_tui.py
```

显式实盘：

```bash
DRY_RUN=false python run_mm.py
```

### 5. 测试

```bash
pytest tests/ -v
```

或使用当前验证命令：

```bash
.venv/bin/python -m pytest tests -q
```

## 实盘前你需要知道的事

这部分比“怎么启动”更重要。

- `LIVE` 模式下，`RISK_ENFORCE` 默认开启；风控不再只是记录日志
- 风控现在把 open orders 也算入最坏净敞口，而不只看已成交仓位
- `MarketFeed` 只有在 order book 新鲜时才算健康；仅有 trade/price 更新不够
- WebSocket 断连会立即把 feed 标记为不健康，并触发撤单回调
- 实盘路径会持续同步真实 fills 和 open orders，更新本地订单状态、库存、PnL 和 risk
- `run_tui.py` 的 live 启动会先做配置校验、凭证校验和孤儿订单清理
- 如果你在中国大陆或依赖 VPN，浏览器能访问 Polymarket 不代表做市链路足够稳定；实盘更建议部署在稳定的海外 VPS

默认建议：

1. 先跑一段时间 `DRY_RUN`
2. 再用极小仓位做 live 验证
3. 前 24 小时人工盯盘

## 常用命令

```bash
# CLI 做市
python run_mm.py

# TUI 面板
python run_tui.py

# 指定 token 跑 TUI
python run_tui.py --token <TOKEN_ID> --question "Market question"

# 全量测试
pytest tests/ -v

# 指定测试
pytest tests/test_smart_mm.py -v
pytest tests/test_safety.py -v
pytest tests/test_phase3.py -v
pytest tests/test_phase3_5.py -v

# 回测
python scripts/run_backtest.py
```

## 项目结构

```text
src/
├── alpha/
│   ├── arbitrage.py
│   ├── events.py
│   ├── flow_signals.py
│   └── pair_tracker.py
├── backtest/
│   ├── data.py
│   ├── engine.py
│   ├── fee_model.py
│   └── walk_forward.py
├── feed/
│   ├── data_store.py
│   ├── feed.py
│   ├── fill_feed.py
│   ├── mock.py
│   ├── persistence.py
│   ├── persistent_data_store.py
│   ├── rest_poller.py
│   ├── trades_poller.py
│   └── websocket_conn.py
├── risk/
│   ├── adverse_selection.py
│   ├── correlation.py
│   ├── dynamic_limits.py
│   ├── kelly.py
│   ├── kpi_panel.py
│   ├── manager.py
│   └── market_pnl.py
├── strategy/
│   ├── allocator.py
│   ├── backtest_wrapper.py
│   ├── book_analyzer.py
│   ├── inventory.py
│   ├── maker_checker.py
│   ├── market_maker.py
│   ├── market_scorer.py
│   ├── multi_market_allocator.py
│   ├── multi_param_backtest.py
│   ├── parity.py
│   ├── partial_fill_handler.py
│   ├── pool.py
│   ├── queue_optimizer.py
│   ├── real_data_backtest.py
│   ├── runner.py
│   ├── timing.py
│   └── volatility.py
├── telemetry/
│   ├── latency.py
│   └── trade_logger.py
└── tui/
    ├── collector.py
    ├── renderer.py
    ├── runner.py
    └── state.py
```

主要入口：

- [`run_mm.py`](run_mm.py)：CLI 做市入口
- [`run_tui.py`](run_tui.py)：TUI 入口
- [`src/strategy/market_maker.py`](src/strategy/market_maker.py)：核心策略
- [`src/feed/feed.py`](src/feed/feed.py)：数据入口
- [`src/risk/manager.py`](src/risk/manager.py)：风险入口

## 关键配置

下面列的是最常调的一组。权威来源仍然是 [`src/config.py`](src/config.py)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DRY_RUN` | `true` | 默认模拟交易 |
| `MM_SIZE` | `5` | 单边报价张数 |
| `MM_POSITION_LIMIT` | `50` | 库存管理器的单市场仓位阈值 |
| `MM_REQUOTE_THRESHOLD` | `0.01` | 中点变动达到 1 tick 时重报价 |
| `SPREAD_BASE` | `0.01` | 基础 spread |
| `SPREAD_MIN` / `SPREAD_MAX` | `0.01` / `0.05` | spread 下限和上限 |
| `RISK_MAX_DAILY_LOSS` | `300` | 日亏损上限 |
| `RISK_MAX_POSITION` | `100` | 单 token 风控限额 |
| `RISK_MAX_TOTAL_EXPOSURE` | `500` | 总风险敞口上限 |
| `WS_STALE_DATA_THRESHOLD` | `60.0` | WS 数据陈旧阈值 |

## 文档

- [`docs/index.md`](docs/index.md)
- [`docs/user/getting-started.md`](docs/user/getting-started.md)
- [`docs/user/safety.md`](docs/user/safety.md)
- [`docs/developer/architecture.md`](docs/developer/architecture.md)

## 免责声明

这是高风险交易软件，不保证盈利，也不保证适合任何市场环境。请只用你能承受损失的资金，并在实盘前充分验证。
