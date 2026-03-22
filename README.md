# Polymarket 做市商机器人

一个面向 [Polymarket](https://polymarket.com) 预测市场的生产级自动化做市系统。

[![Tests](https://img.shields.io/badge/tests-339%20passing-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 概述

本机器人在 Polymarket 预测市场提供双向流动性，通过买卖价差（Spread）赚取收益，并可获得流动性奖励。机器人具有自适应策略、全面风险管理及市场情报功能。

### 什么是做市商？

做市商（Market Maker）是金融市场的重要角色，同时提供买入和卖出报价（订单），为市场提供流动性。在 Polymarket 的 YES/NO 预测市场中，做市商通过挂单赚取价差收益。

### 核心功能

- **自适应做市** - 根据波动率、库存和订单流动态调整价差
- **Alpha 信号生成** - 套利检测、流量分析、竞争者检测、市场状态检测
- **风险管理** - 凯利公式（Kelly Criterion）仓位管理、反向选择检测、相关性追踪、熔断机制
- **实时数据** - WebSocket 集成 + 自动 REST 备用方案
- **安全功能** - 断连自动取消、僵尸订单清理、余额监控
- **终端 UI 仪表盘** - 实时监控的富终端界面
- **回测功能** - 历史数据回放用于策略验证
- **遥测** - 交易日志和延迟监控

---

## 快速开始

### 前置要求

- Python 3.11+
- Polymarket 账户（实盘交易需要）

### 安装步骤

```bash
# 克隆仓库
git clone <仓库URL>
cd polymarket-mm-bot

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 配置

```bash
cp .env.example .env
# 使用文本编辑器编辑 .env 文件
```

### 运行

```bash
# 模拟交易（默认）
python run_mm.py

# 带终端仪表盘
python run_tui.py

# 实盘交易（需要凭证）
DRY_RUN=false python run_mm.py
```

---

## 项目架构

```
src/
├── alpha/                    # Alpha 信号生成
│   ├── arbitrage.py          # YES/NO 平价套利检测
│   ├── competitors.py       # 竞争者检测和响应
│   ├── events.py            # 市场事件追踪（结算、交易量飙升）
│   ├── flow_signals.py      # 订单流分析和不平衡检测
│   ├── pair_tracker.py      # 代币对管理
│   ├── regime.py            # 流动性状态检测
│   └── time_patterns.py     # 时间模式分析
│
├── backtest/                 # 历史回测
│   ├── data.py               # 历史数据管理
│   └── engine.py             # 回测执行引擎
│
├── feed/                     # 市场数据基础设施
│   ├── data_store.py         # 本地订单簿存储
│   ├── feed.py               # MarketFeed 主类
│   ├── fill_feed.py         # 成交通知处理
│   ├── mock.py               # 测试用模拟数据
│   ├── rest_poller.py        # REST API 备用轮询
│   ├── trades_poller.py      # 交易历史轮询
│   └── websocket_conn.py     # WebSocket 连接管理
│
├── risk/                     # 风险管理系统
│   ├── adverse_selection.py  # 有毒流（反向选择）检测
│   ├── correlation.py        # 跨市场相关性追踪
│   ├── dynamic_limits.py     # 自适应仓位限制
│   ├── kelly.py              # 凯利公式仓位计算
│   ├── manager.py            # 中心化 RiskManager 类
│   └── market_pnl.py         # 单市场盈亏追踪
│
├── strategy/                 # 交易策略
│   ├── allocator.py          # 跨市场资金分配
│   ├── book_analyzer.py      # 订单簿分析和不平衡检测
│   ├── inventory.py          # 库存管理和偏斜
│   ├── maker_checker.py       # 确保仅做市商订单
│   ├── market_maker.py       # SmartMarketMaker 主类
│   ├── market_scorer.py      # 市场选择评分
│   ├── parity.py             # YES/NO 平价验证
│   ├── partial_fill_handler.py # 部分成交处理
│   ├── pool.py               # 多市场池管理
│   ├── queue_optimizer.py    # 队列位置优化
│   ├── runner.py             # CLI 入口
│   ├── timing.py             # 自适应循环 timing
│   └── volatility.py         # 波动率追踪和价差调整
│
├── telemetry/                # 可观测性
│   ├── latency.py            # 延迟监控和告警
│   └── trade_logger.py       # JSON 交易日志
│
├── tui/                      # 终端 UI
│   ├── collector.py          # 组件状态收集
│   ├── renderer.py           # Rich 控制台渲染
│   ├── runner.py             # TUI 运行器集成
│   └── state.py              # 机器人状态数据类
│
├── auth.py                   # 钱包和凭证管理
├── client.py                 # CLOB API 客户端包装
├── config.py                 # 配置（约 80 个环境变量）
├── markets.py                # Gamma API 市场发现
├── models.py                 # 核心数据模型
├── orders.py                 # 订单查询（挂单、仓位、交易）
├── pricing.py                # 订单簿获取和定价
├── rate_limiter.py           # API 速率限制
├── simulator.py              # DRY_RUN 订单模拟
├── trading.py                # 订单下单和取消
├── utils.py                  # 日志和工具
└── websocket_client.py       # 传统 WebSocket 客户端

tests/                        # 339 个测试覆盖所有模块
run_mm.py                     # 主入口
run_tui.py                    # TUI 入口
```

---

## 配置参考

### 交易模式

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DRY_RUN` | `true` | 模拟交易模式（不下真实订单） |
| `RISK_ENFORCE` | `false`（模拟）/ `true`（实盘） | 强制执行风险限制或仅记录 |

### 做市参数

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MM_SPREAD` | `0.04` | 基础价差（4 美分） |
| `MM_SIZE` | `10` | 订单数量（合约数） |
| `MM_REQUOTE_THRESHOLD` | `0.03` | 中点移动 3 美分时重新报价 |
| `MM_POSITION_LIMIT` | `50` | 跳过侧边前的最大仓位 |
| `SPREAD_MIN` | `0.02` | 最小价差（2 美分） |
| `SPREAD_MAX` | `0.10` | 最大价差（10 美分） |
| `INVENTORY_SKEW_MAX` | `0.02` | 最大库存偏斜（2 美分） |

### 风险管理

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RISK_MAX_DAILY_LOSS` | `50` | 每日亏损限制（美元） |
| `RISK_MAX_POSITION` | `100` | 单币种最大仓位 |
| `RISK_MAX_TOTAL_EXPOSURE` | `500` | 总暴露仓位限制（美元） |
| `RISK_ERROR_COOLDOWN` | `60` | 错误后暂停秒数 |
| `KELLY_FRACTION` | `0.25` | 凯利公式比例 |
| `ADVERSE_TOXIC_THRESHOLD` | `0.4` | 有毒流阈值 |

### 市场选择

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MARKET_MIN_VOLUME` | `10000` | 最小 24 小时交易量（美元） |
| `MARKET_MIN_SPREAD` | `0.02` | 最小价差（过紧=竞争激烈） |
| `MARKET_MAX_SPREAD` | `0.15` | 最大价差（过宽=流动性差） |
| `MARKET_MIN_HOURS_TO_RESOLUTION` | `12` | 距离结算最少小时数 |
| `MARKET_MIN_PRICE` / `MAX_PRICE` | `0.05` / `0.95` | 避免极端价格 |

### Alpha 信号

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ARB_MIN_PROFIT_BPS` | `20` | 最小套利收益（基点） |
| `FLOW_WINDOW_SECONDS` | `60` | 订单流分析窗口 |
| `FLOW_IMBALANCE_THRESHOLD` | `0.15` | 触发价差加宽的流不平衡度 |
| `REGIME_WINDOW_SIZE` | `50` | 状态检测的快照数 |

完整配置参考见 [.env.example](.env.example)。

---

## 安全功能

机器人包含生产级安全功能：

1. **启动清理** - 取消上次会话遗留的孤儿订单
2. **断连自动取消** - WebSocket 断连时取消所有订单
3. **僵尸订单清理** - 移除超过 5 分钟的订单
4. **余额监控** - 下单前检查余额，余额下降 20% 以上时告警
5. **熔断机制** - 错误过多或亏损超限时自动停止交易

---

## 交易模式详解

### DRY_RUN 模式（模拟交易）

- 使用 `OrderSimulator` 进行订单撮合
- 不调用真实 API 下单
- 风险限制默认仅记录不强制
- 适合策略测试和数据收集

### LIVE 模式（实盘交易）

- 通过认证的 CLOB 客户端真实下单
- 强制执行风险限制
- 监控仓位和余额
- 需要有效的 API 凭证

**实盘交易所需凭证：**
```bash
POLY_PRIVATE_KEY=0x...    # 以太坊私钥
POLY_API_KEY=...           # Polymarket API 密钥
POLY_API_SECRET=...        # API 密钥密文
POLY_PASSPHRASE=...        # API 密钥口令
```

---

## API 集成

### Polymarket API

| API | 地址 | 用途 |
|-----|------|------|
| Gamma | `gamma-api.polymarket.com` | 市场发现、元数据 |
| CLOB | `clob.polymarket.com` | 订单簿、交易 |
| WebSocket | `ws-subscriptions-clob.polymarket.com` | 实时更新 |

### 速率限制

- 下单：40/秒 持续，240/秒 突发
- 取消订单：40/秒 持续，240/秒 突发
- 订单簿查询：200/10秒
- 市场查询：125/10秒

### 费率

- **做市商费率：0%** - 保留全部价差
- **吃单费率：0%**
- 符合条件的市场可获得流动性奖励

---

## 策略详解

### 报价计算

1. 从订单簿获取当前中点价格
2. 应用波动率乘数到基础价差
3. 应用库存偏斜（仓位增加时加宽价差）
4. 应用订单流调整（激进流时加宽价差）
5. 检查对应代币的平价（YES + NO 应约等于 1.00）

### 库存管理

- 追踪每个代币的仓位和 VWAP（成交量加权平均价格）
- 向库存方向偏斜报价
- 仓位接近限值时减少数量
- 仓位变化时计算已实现盈亏

### 自适应 Timing

| 模式 | 间隔 | 触发条件 |
|------|------|----------|
| 正常 | 2.0 秒 | 默认状态 |
| 快速 | 0.1 秒 | 高波动或近期有活动 |
| 休眠 | 5.0 秒 | 长时间无活动（>60 秒） |

---

## 开发

### 运行测试

```bash
# 所有测试
pytest tests/ -v

# 特定测试
pytest tests/test_phase1.py -v      # 环境/连接
pytest tests/test_smart_mm.py -v    # 做市商核心
pytest tests/test_safety.py -v       # 安全功能
pytest tests/test_arbitrage.py -v   # 套利检测
```

### 代码风格

- 金额使用 Decimal（非 float）
- 数据传输使用 dataclass
- 全程类型提示
- 全面的文档字符串

---

## 文档

详细指南见 `docs/` 目录：

- [用户文档](docs/user/) - 快速开始、配置、安全、FAQ
- [开发者文档](docs/developer/) - 架构、搭建、贡献
- [路线图](docs/roadmap.md) - 项目计划和未来功能

---

## 免责声明

**本软件仅供教育和研究目的。交易预测市场存在重大亏损风险。**

请务必：
- 从 DRY_RUN 模式开始
- 初始使用小额仓位
- 实盘交易时主动监控
- 切勿使用个人钱包密钥（创建专用交易钱包）

---

## 许可证

MIT 许可证 - 参见 [LICENSE](LICENSE)

## 相关链接

- [Polymarket](https://polymarket.com)
- [Polymarket API 文档](https://docs.polymarket.com/)
- [py-clob-client](https://github.com/Polymarket/py-clob-client)