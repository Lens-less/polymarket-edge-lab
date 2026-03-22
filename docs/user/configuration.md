# 配置指南

本指南说明如何根据不同的账户规模和风险承受能力配置机器人。

## 配置概述

机器人通过 `.env` 文件中的环境变量进行配置。每个设置项控制特定的行为。

## 按账户规模的快速配置

### 小额账户（$100 - $500）

适合测试和学习，使用保守设置：

```bash
# 交易设置
DRY_RUN=true
MM_SIZE=5              # 每次挂单数量
MM_SPREAD=0.05         # 买卖价差（5美分）
MM_POSITION_LIMIT=20   # 触发暂停的仓位阈值
RISK_MAX_POSITION=20   # 单币种最大仓位
RISK_MAX_TOTAL_EXPOSURE=100   # 总暴露仓位上限
RISK_MAX_DAILY_LOSS=10        # 每日亏损上限

# 市场筛选
MARKET_MIN_VOLUME=5000        # 最小24小时交易量
MARKET_MIN_HOURS_TO_RESOLUTION=24  # 距离结算最小小时数
```

**配置说明**：较小的仓位可以在您学习过程中限制潜在亏损。

### 中等账户（$500 - $5000）

平衡风险和收益：

```bash
# 交易设置
DRY_RUN=true
MM_SIZE=10
MM_SPREAD=0.04
MM_POSITION_LIMIT=50
RISK_MAX_POSITION=50
RISK_MAX_TOTAL_EXPOSURE=500
RISK_MAX_DAILY_LOSS=25

# 市场筛选
MARKET_MIN_VOLUME=10000
MARKET_MIN_HOURS_TO_RESOLUTION=12
```

### 大额账户（$5000+）

更高容量，但需要仔细监控：

```bash
# 交易设置
DRY_RUN=true
MM_SIZE=25
MM_SPREAD=0.03
MM_POSITION_LIMIT=100
RISK_MAX_POSITION=100
RISK_MAX_TOTAL_EXPOSURE=2000
RISK_MAX_DAILY_LOSS=100

# 市场筛选
MARKET_MIN_VOLUME=25000
MARKET_MIN_HOURS_TO_RESOLUTION=6
```

## 核心参数说明

### 交易参数

| 参数 | 说明 | 推荐范围 |
|------|------|----------|
| MM_SPREAD | 买卖价差（美元） | 0.03 - 0.10 |
| MM_SIZE | 每次挂单数量（合约数） | 5 - 50 |
| MM_POSITION_LIMIT | 暂停挂单前单币种最大仓位 | 20 - 100 |

### 风险管理

| 参数 | 说明 | 推荐范围 |
|------|------|----------|
| RISK_MAX_POSITION | 单币种硬性仓位上限 | 20 - 100 |
| RISK_MAX_TOTAL_EXPOSURE | 整体仓位上限 | 100 - 5000 |
| RISK_MAX_DAILY_LOSS | 触发暂停的每日亏损阈值 | 10 - 100 |

### 市场筛选

| 参数 | 说明 | 推荐范围 |
|------|------|----------|
| MARKET_MIN_VOLUME | 最小24小时交易量 | 5000 - 25000 |
| MARKET_MIN_SPREAD | 避免过度竞争的市场 | 0.02 - 0.05 |
| MARKET_MAX_SPREAD | 避免流动性不足的市场 | 0.10 - 0.20 |

## 常见配置方案

### 激进型（较高收益，较高风险）

```bash
MM_SPREAD=0.03
MM_SIZE=20
KELLY_FRACTION=0.35
RISK_MAX_POSITION=80
```

### 保守型（较低收益，较低风险）

```bash
MM_SPREAD=0.06
MM_SIZE=5
KELLY_FRACTION=0.15
RISK_MAX_POSITION=15
```

## 测试配置更改

1. 在 `.env` 中修改配置
2. 重启机器人
3. 在 DRY_RUN 模式监控 24 小时以上
4. 检查盈亏和仓位情况

## 高级参数（需理解做市原理）

- `KELLY_FRACTION` - 凯利公式（Kelly Criterion）仓位管理参数
- `VOL_MULT_MAX` - 最大波动率乘数
- `ADVERSE_TOXIC_THRESHOLD` - 反向选择（Adverse Selection）检测阈值

---

**相关链接**: [快速开始](user/getting-started.md) | [交易模式](user/trading-modes.md)