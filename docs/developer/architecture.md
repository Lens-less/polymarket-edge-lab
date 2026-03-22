# 架构概述

面向想要了解机器人技术设计的开发者。

> **注意**：这是高级概述。详细技术文档请参阅项目根目录的 `PROJECT_CONTEXT.md`。

## 系统概览

```
                          ┌─────────────────────────────────────────────────┐
                          │                 SmartMarketMaker                 │
                          │  - 报价计算（价差、库存偏斜、数量）              │
                          │  - 订单生命周期管理                              │
                          └───────────────────────┬─────────────────────────┘
                                                    │
          ┌─────────────────────────────────────────┼─────────────────────────────────────────┐
          │                                         │                                         │
          ▼                                         ▼                                         ▼
┌─────────────────────┐              ┌─────────────────────┐              ┌─────────────────────┐
│     MarketFeed     │              │     RiskManager     │              │   Alpha Signals    │
│  - WebSocket数据   │              │  - 仓位限制          │              │  - 套利检测        │
│  - REST备用方案    │              │  - 每日盈亏         │              │  - 流量分析        │
│  - 健康状态追踪   │              │  - 熔断开关         │              │  - 竞争者检测      │
└─────────────────────┘              └─────────────────────┘              └─────────────────────┘
```

## 核心组件

| 组件 | 位置 | 用途 |
|------|------|------|
| SmartMarketMaker | `src/strategy/market_maker.py` | 主策略，报价计算 |
| MarketFeed | `src/feed/feed.py` | 实时市场数据 |
| RiskManager | `src/risk/manager.py` | 风险控制 |
| Alpha Signals | `src/alpha/` | 市场情报 |

## 关键文件

- `run_mm.py` - 主入口点
- `run_tui.py` - 终端 UI 入口
- `src/config.py` - 所有配置
- `src/strategy/market_maker.py` - 核心策略
- `src/risk/manager.py` - 风险管理

## 数据流

1. MarketFeed 订阅 WebSocket 获取订单簿更新
2. SmartMarketMaker 根据波动率、库存、流量信号计算报价
3. RiskManager 验证每个报价是否符合仓位限制
4. 通过 Client 下单或在 DRY_RUN 中模拟
5. 成交处理和库存更新
6. TradeLogger 记录所有活动

## 快速参考

### 运行测试
```bash
pytest tests/ -v
```

### 添加新的 Alpha 信号
1. 在 `src/alpha/` 创建信号类
2. 添加配置到 `src/config.py`
3. 集成到 SmartMarketMaker._calculate_quotes()
4. 添加测试

### 修改报价逻辑
所有报价计算在 `SmartMarketMaker._calculate_quotes()` 中

---

*详细技术文档：参见项目根目录的 PROJECT_CONTEXT.md*