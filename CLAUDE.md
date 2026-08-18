# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本项目中工作时提供指导。

## 项目概述

Polymarket 做市商机器人 - 一个面向 Polymarket 预测市场的生产级自动化做市系统。在 YES/NO 预测市场提供双向流动性，通过买卖价差（Spread）赚取收益并获取流动性奖励。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 模拟交易模式运行（默认）
python run_mm.py

# 带终端仪表盘运行
python run_tui.py

# 实盘交易模式运行（需要 API 凭证）
DRY_RUN=false python run_mm.py

# 运行所有测试
pytest tests/ -v

# 运行特定测试文件
pytest tests/test_smart_mm.py -v
pytest tests/test_safety.py -v

# 运行回测
python scripts/run_backtest.py
```

## 项目架构

代码库按功能模块组织：

- **src/strategy/** - 核心做市逻辑：`SmartMarketMaker` 类管理主交易循环、报价计算、库存偏斜和订单下单
- **src/feed/** - 市场数据基础设施：通过 `MarketFeed` 的 WebSocket + REST 备用方案获取订单簿和交易数据
- **src/risk/** - 风险管理：`RiskManager` 强制执行仓位限制、凯利公式仓位管理、反向选择检测和熔断机制
- **src/alpha/** - Alpha 信号生成：套利检测、竞争者分析、订单流信号、市场状态检测
- **src/backtest/** - 历史回测引擎用于策略验证
- **src/tui/** - 富终端仪表盘用于实时监控
- **src/telemetry/** - 交易日志和延迟监控

入口点：`run_mm.py`（CLI）、`run_tui.py`（仪表盘）

### 数据流

1. `MarketFeed` 订阅 WebSocket 获取实时订单簿更新
2. `SmartMarketMaker` 使用波动率、库存和流信号计算报价
3. `RiskManager` 验证每个报价是否符合仓位限制和熔断条件
4. 订单通过 `Client`（CLOB API）下单或在 DRY_RUN 模式中模拟
5. 成交由 `PartialFillHandler` 处理并更新库存
6. `TradeLogger` 记录所有活动用于分析

### 目录结构详解

```
src/
├── strategy/          # 核心做市策略
│   ├── market_maker.py    # SmartMarketMaker 主类
│   ├── runner.py          # CLI 入口点
│   ├── inventory.py       # 库存管理
│   ├── volatility.py      # 波动率计算
│   └── ...
├── feed/             # 市场数据
│   ├── feed.py            # MarketFeed 主类
│   ├── websocket_conn.py  # WebSocket 连接
│   └── rest_poller.py     # REST 备用
├── risk/             # 风险管理
│   ├── manager.py         # RiskManager 主类
│   ├── kelly.py          # 凯利公式
│   └── ...
├── alpha/            # Alpha 信号
│   ├── arbitrage.py      # 套利检测
│   ├── flow_signals.py   # 订单流分析
│   └── ...
├── backtest/         # 回测引擎
├── tui/              # 终端 UI
└── telemetry/        # 遥测
```

## 关键模式

- **Decimal 类型** - 所有金额使用 Decimal（非 float）
- **Dataclass** - 数据传输对象
- **类型提示** - 全程使用
- **polymarket-client 0.6.0** - 官方统一 Polymarket API SDK
- **WebSocket + REST 备用** - 数据韧性
- **DRY_RUN 模式** - 使用 `OrderSimulator` 进行无风险模拟交易

## 配置

通过 `.env` 中的约 80 个环境变量配置。关键配置：

- `DRY_RUN=true` - 模拟交易（默认）
- `MM_SPREAD=0.04` - 基础价差（4 美分）
- `MM_SIZE=10` - 订单数量（合约数）
- `RISK_MAX_POSITION=100` - 单币种最大仓位

完整配置参考见 `.env.example`。

### 配置分类

1. **网络配置** - CHAIN_ID, API URLs
2. **认证配置** - POLY_* 凭证
3. **WebSocket 配置** - 重连、心跳设置
4. **交易配置** - DRY_RUN, 仓位限制, 订单大小
5. **做市配置** - 价差、重报价阈值、Timing
6. **市场选择** - 交易量、价差、价格过滤
7. **Alpha 配置** - 套利、流、竞争者、状态设置
8. **风险配置** - 亏损限值、凯利、反向选择

## 安全功能

机器人包含生产级安全功能：

- **启动清理** - 取消上次会话遗留的孤儿订单
- **断连自动取消** - WebSocket 断开时取消所有订单
- **僵尸订单清理** - 5 分钟超时自动清理
- **余额监控** - 带告警的余额监控
- **熔断机制** - 错误过多或亏损超限自动停止

## 测试模式

### 单元测试

```python
# 使用模拟数据测试
from src.feed.mock import MockFeed

feed = MockFeed()
feed.set_midpoint("token", Decimal("0.50"))
feed.set_health(True)

# 测试风险管理器
from src.risk import get_risk_manager, reset_risk_manager

def test_something():
    reset_risk_manager()  # 新实例
    risk = get_risk_manager()
    # ...
```

### 集成测试

```python
# 使用模拟器测试
from src.simulator import get_simulator, reset_simulator

def test_order_flow():
    reset_simulator()
    sim = get_simulator()

    order = sim.place_order(token_id, OrderSide.BUY, Decimal("0.50"), Decimal("10"))
    assert order is not None
```

### 运行测试

```bash
# 所有测试
pytest tests/ -v

# 特定模块
pytest tests/test_smart_mm.py -v

# 带覆盖率
pytest --cov=src tests/

# 跳过慢速测试
pytest tests/ -v -m "not slow"
```

## 常见工作流程

### 添加新的 Alpha 信号

1. 在 `src/alpha/` 创建信号类：
   ```python
   @dataclass
   class MySignal:
       value: float
       confidence: float
       direction: str

   class MyAnalyzer:
       def __init__(self, window_size: int):
           self._data = deque(maxlen=window_size)

       def record(self, value: float):
           self._data.append(value)

       def get_signal(self) -> MySignal:
           # 分析逻辑
           pass
   ```

2. 添加配置变量到 `src/config.py`
3. 集成到 `SmartMarketMaker._calculate_quotes()`
4. 在 `tests/test_my_signal.py` 添加测试
5. 从 `src/alpha/__init__.py` 导出

### 添加风险检查

1. 添加到 `RiskManager._run_checks()`：
   ```python
   def _check_my_condition(self) -> bool:
       """返回 True 表示风险正常"""
       if some_condition:
           self._log_risk_event("MY_CHECK", {"details": "..."})
           return False
       return True
   ```

2. 添加配置阈值到 `src/config.py`
3. 添加测试

### 修改报价逻辑

1. 所有报价计算在 `SmartMarketMaker._calculate_quotes()`
2. 调整因子相乘基础价差：
   ```python
   effective_spread = (
       SPREAD_BASE
       * vol_multiplier
       * regime_multiplier
       * competitor_multiplier
   )
   ```
3. 偏斜移动中点：
   ```python
   adjusted_mid = mid + inventory_skew + flow_skew
   ```

## 实盘交易检查清单

上线前：

- [ ] 在 DRY_RUN 模式测试通过
- [ ] 验证熔断机制正常（`risk.kill_switch()`）
- [ ] 确认断连自动取消触发
- [ ] 检查僵尸订单清理运行
- [ ] 验证余额监控告警正常
- [ ] 从小 `MM_SIZE` 和 `RISK_MAX_POSITION` 开始
- [ ] 前 24 小时主动监控
- [ ] 准备好手动取消脚本

## 故障排查

### 无法连接数据源
- 检查 `WS_MARKET_URL` 是否正确
- 验证网络连接
- 检查速率限制（429 错误）

### DRY_RUN 订单未成交
- 模拟器需要价格交叉才能成交
- 检查 `sim.process_fills()` 是否被调用

### 实盘订单被拒绝
- 验证凭证是否设置
- 检查余额/授权
- 验证 tick 大小（0.01 增量）
- 检查仓位限制

### 反向选择过高
- 订单流可能知情，加宽价差
- 检查 `ADVERSE_TOXIC_THRESHOLD` 设置
- 考虑退出该市场

## 关键文件参考

| 文件 | 用途 |
|------|------|
| `run_mm.py` | 主入口 |
| `run_tui.py` | TUI 入口 |
| `src/config.py` | 所有配置 |
| `src/strategy/market_maker.py` | 核心策略 |
| `src/strategy/runner.py` | 带市场选择的 CLI 运行器 |
| `src/risk/manager.py` | 中心化风险控制 |
| `src/feed/feed.py` | 市场数据源 |
| `src/simulator.py` | DRY_RUN 订单模拟 |
| `src/trading.py` | 订单下单 |
| `src/orders.py` | 订单查询 |

## 依赖

```
polymarket-client # 官方统一 Polymarket API SDK
python-dotenv     # 环境变量
websockets        # WebSocket 连接
pandas            # 数据分析
pytest            # 测试
pytest-asyncio    # 异步测试支持
rich              # TUI 渲染
requests          # HTTP 客户端
numpy             # 相关性计算
```

## 其他文档

- [用户文档](docs/user/) - 面向非技术用户（快速开始、配置、安全、FAQ）
- [开发者文档](docs/developer/) - 面向贡献者（架构、搭建、贡献）
- [路线图](docs/roadmap.md) - 项目计划和未来功能

---

*最后更新：2026-03-23*
*测试：339 个通过*
