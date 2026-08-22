# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本项目中工作时提供指导。

## 项目概述

Polymarket BTC 5m/15m 结构对（structural pair）edge 就绪性研究项目。核心假设：
同一个 60 秒 TWAP 结算的 5m/15m 市场对，`floor 对 = 买 5m Down + 买 15m Up`
（或反过来）在 `b ≥ 0`（市场隐含分裂概率）时，成本恒等于 `1 + b`；如果能以
低于 `1 + b` 的价格双腿挂单成交，就有正 floor。真正的策略与证据体系在
`src/edge_lab/`（尤其是 `btc_twap_relative_value*.py`、
`btc_twap_relative_value_readiness.py`）和 `research/btc_5m_15m_*` 下。

截至 2026-08-22：**不建议实盘交易。** 最新评估见
`research/btc_5m_15m_edge_readiness_v08_2026-08-18/CLAUDE_FINAL_ASSESSMENT.md`
与实施合同 `LIVE_COMPLETION_SPEC.md`。结构线按当前设计不可盈利；实盘通道
当前物理上不存在——1,017 个真实盘口样本里正 floor 出现 0 次，且
`compatibility.py` 的新订单硬门无条件抛异常。在改变这个结论之前不要假设
"能实盘"或"已验证有效"。

`src/` 下还保留一层通用的 Polymarket API 基础设施（认证、订单查询/下单、
风控、市场数据、DRY_RUN 模拟器）——它不是任何特定策略的专属代码，是未来
任何执行适配器（包括 BTC 结构对的 double-maker probe）都会复用的底座。

**曾经存在一个通用的单边做市机器人**（`src/strategy/` 的 `SmartMarketMaker`，
入口 `run_mm.py`/`run_tui.py`）——它与 BTC 结构对策略无关，只是从 Polymarket
全站自动选一个"打分最高"的市场去挂价差单。这套代码已被彻底删除
（连同 `src/tui/`、`Dockerfile`、若干专属测试），因为它不是这个项目的策略，
容易被误认为"策略入口"而跑错方向。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行全部测试
pytest tests/ -v

# 运行 BTC 结构对相关测试
pytest tests/ -v -k "btc_twap"

# 只读、无凭证地采集当前 BTC 5m/15m 盘口，验证结构 floor 是否存在
cd research/btc_5m_15m_edge_readiness_v08_2026-08-18/tools
python live_structural_probe.py <运行分钟数> <采样间隔秒> <输出.jsonl>
python analyze_structural_floor.py <输出.jsonl>

# 通用回测引擎（与具体策略无关）
python scripts/run_backtest.py --mock
```

## 项目架构

```
src/
├── edge_lab/           # BTC 5m/15m 结构对策略与就绪性证据体系（真正的策略在这里）
│   ├── btc_twap_relative_value*.py       # 结构对定价、执行、影子回放
│   ├── btc_twap_relative_value_readiness.py  # Gate 0 / 就绪性判定
│   ├── btc_twap_pair_pricing.py          # 双腿盘口选择与报价
│   ├── btc_twap_execution_probe.py       # 实盘探针 harness（无凭证、无适配器）
│   ├── compatibility.py                  # 新订单硬门（无条件拒绝，见下）
│   └── ...
├── feed/               # 市场数据：`MarketFeed` 的 WebSocket + REST 备用方案
├── risk/               # 风险管理：`RiskManager` 仓位限制、熔断机制
├── backtest/           # 通用历史回测引擎，与具体策略解耦
├── telemetry/          # 交易日志
├── auth.py, client.py  # 官方 polymarket-client 0.6.0 封装（认证/公开客户端）
├── orders.py, trading.py  # 订单查询/下单，DRY_RUN 与 LIVE 分流
└── simulator.py        # DRY_RUN 无风险模拟成交
```

不要在 `src/` 下重建一个通用做市策略模块；BTC 结构对是本项目唯一的策略。

## 实盘安全边界

- `src/edge_lab/compatibility.py` 的 `assert_new_orders_disabled()` **无条件抛
  异常**，是新订单的硬门。不要移除、削弱或绕过它。
- 8 项实盘就绪检查（`compatibility_audit()`）里，只有 `legacy_v1_dependency_absent`
  与 `current_sdk_installed` 不需要认证证据就能为 true；其余 6 项（V2 适配器、
  pUSD 对账、签名路径、成交终态对账、geoblock 两项）需要授权的认证探针才能
  转 true，不能自证据。
- 任何解除硬门的改动都必须先完成
  `research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md`
  里 WP-S0/S1/E0 的全部前置条件。`IMPLEMENTATION_AND_RUNBOOK.md` 只记录已实现表面，不能单独授权下单。

## 关键模式

- **Decimal 类型** - 所有金额使用 Decimal（非 float）
- **Dataclass** - 数据传输对象
- **类型提示** - 全程使用
- **polymarket-client 0.6.0** - 官方统一 Polymarket API SDK
- **DRY_RUN 模式** - 使用 `OrderSimulator` 进行无风险模拟交易
- **预注册 + 停手条件** - edge_lab 的就绪性判定不接受自报证据；停手条件写在
  `PREREGISTRATION.json` / `CLAUDE_FINAL_ASSESSMENT.md` §8 里，不能事后放宽

## 配置

通过 `.env` 配置，参考 `.env.example`。关键配置：

- `DRY_RUN=true` - 模拟交易（默认）；`DRY_RUN=false` 仍会被上面的硬门拦截
- `RISK_MAX_POSITION` - 单币种最大仓位

## 测试模式

```bash
# 所有测试
pytest tests/ -v

# BTC 结构对 / Gate 0 相关
pytest tests/ -v -k "btc_twap"

# 带覆盖率
pytest --cov=src tests/
```

```python
# 测试风险管理器
from src.risk import get_risk_manager, reset_risk_manager

def test_something():
    reset_risk_manager()
    risk = get_risk_manager()

# 测试 DRY_RUN 模拟器
from src.simulator import get_simulator, reset_simulator

def test_order_flow():
    reset_simulator()
    sim = get_simulator()
    order = sim.place_order(token_id, OrderSide.BUY, Decimal("0.50"), Decimal("10"))
    assert order is not None
```

## 故障排查

### 无法连接数据源
- 检查 `WS_MARKET_URL` 是否正确
- 验证网络连接
- 检查速率限制（429 错误）

### DRY_RUN 订单未成交
- 模拟器需要价格交叉才能成交
- 检查 `sim.process_fills()` 是否被调用

### 实盘订单被拒绝 / 被硬门拦截
- 这是预期行为，不是 bug——见上面"实盘安全边界"
- 不要为了让订单通过而修改 `compatibility.py`

## 关键文件参考

| 文件 | 用途 |
|------|------|
| `src/config.py` | 所有配置 |
| `src/edge_lab/btc_twap_relative_value_readiness.py` | Gate 0 / 就绪性判定核心 |
| `src/edge_lab/compatibility.py` | 新订单硬门 + 8 项就绪性检查 |
| `src/risk/manager.py` | 中心化风险控制 |
| `src/feed/feed.py` | 市场数据源 |
| `src/simulator.py` | DRY_RUN 订单模拟 |
| `src/trading.py` | 订单下单 |
| `src/orders.py` | 订单查询 |
| `research/btc_5m_15m_edge_readiness_v08_2026-08-18/CLAUDE_FINAL_ASSESSMENT.md` | 最新策略结论 |
| `research/btc_5m_15m_edge_readiness_v08_2026-08-18/IMPLEMENTATION_AND_RUNBOOK.md` | 部署与验证步骤 |
| `research/btc_5m_15m_edge_readiness_v08_2026-08-18/LIVE_COMPLETION_SPEC.md` | 实盘完成规格（当前实施合同） |

## 依赖

```
polymarket-client # 官方统一 Polymarket API SDK
python-dotenv     # 环境变量
websockets        # WebSocket 连接
pandas            # 数据分析
pytest            # 测试
pytest-asyncio    # 异步测试支持
requests          # HTTP 客户端
numpy             # 相关性计算
```

## 其他文档

- [用户文档](docs/user/) - 面向非技术用户（快速开始、配置、安全、FAQ）——部分内容
  仍描述已删除的通用做市机器人，未完全更新，请以本文件和 `research/` 下的最新
  评估为准
- [开发者文档](docs/developer/) - 面向贡献者（架构、搭建、贡献）——同上

---

*最后更新：2026-08-22*
