# Polymarket Edge Lab

[![CI](https://github.com/Lens-less/polymarket-edge-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/Lens-less/polymarket-edge-lab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Public-data-only Polymarket research, replay, and evidence-audit lab. The
repository is designed to be reproducible and fail-closed: it does not ship a
supported live-trading path, and new-order entry remains hard-disabled.

面向 Polymarket 的公开数据研究、成交感知回放与证据审计工具箱。项目用于验证交易假设，重点是可复现和防止把理论机会、静态盘口或合成成交误写成真实利润；它不是可直接实盘的交易机器人，也不是 Polymarket 官方项目。

English-first project metadata is used for the repository/package name, while
most long-form research notes and user guides currently remain in Chinese.

> **截至 2026-08-22：BTC 5m/15m 结构线已 `STRUCTURAL_STOP`，仓库中没有经过严格认定的盈利策略，不建议实盘交易。**
>
> 独立复算确认结构对公允价值为 `1 + b`（`b >= 0`）：三个有官方 TWAP 的到期、三种执行口径的正 floor 均为 0。不要再为这条线换主机、跑 Gate 0、采 200 expiry 或写实盘适配器。见[结构线停手证明](research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md)。
>
> 选择性流动性奖励线同样未获准成为实盘策略，只作为未验证研究假设冻结。见[最终投资决策](STRATEGY_DECISION_2026-08-22.md)。

## 快速开始

前置条件：Git、Python 3.11+；持续集成验证 Python 3.11 和 3.12。推荐使用 [uv](https://docs.astral.sh/uv/)。公开研究和下面的验收命令都不需要账户、钱包或 `.env`。

```bash
git clone https://github.com/Lens-less/polymarket-edge-lab.git
cd polymarket-edge-lab
uv sync --locked --python 3.12 --extra dev
uv run python scripts/run_edge_capture.py --config research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json --validate-only
```

最后一条命令完全离线，只校验已提交的采集配置和安全门。成功时输出应包含：

```json
{
  "new_orders_disabled": true,
  "valid": true
}
```

`uv run` 无需激活虚拟环境，在 macOS、Linux 和 Windows 上命令一致。

不用 uv 时，可安装由 `uv.lock` 导出的冻结依赖：

```bash
python -m venv .venv
```

激活环境：

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

然后安装：

```bash
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
python scripts/run_edge_capture.py --config research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json --validate-only
```

## 可以运行什么

### 离线、只读

```bash
# 查看主研究 CLI
uv run python scripts/run_edge_lab.py --help

# 校验 recorder 配置和零下单安全门
uv run python scripts/run_edge_capture.py --config research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json --validate-only

# 运行合成诊断；输出只用于测试账本，不是盈利证据
uv run python scripts/run_backtest.py --mock
```

### 公开网络、只读

```bash
# 检查 SDK 状态和 Polymarket geoblock；会访问公开网络，
# 不读取凭证、不签名、不下单
uv run python scripts/run_edge_lab.py audit

# 奖励市场预检；validate-only 不访问网络
uv run python scripts/run_reward_experiment.py --max-markets 25 --validate-only

# 实际扫描只访问公开端点
uv run python scripts/run_edge_lab.py complete-set-scan --max-markets 100
```

可移植的复现入口见[复现指南](docs/user/reproduction.md)；生成时的完整证据合同保留在[冻结复现记录](research/edge_discovery_2026-07-24/REPRODUCTION.md)。需要代理时，仅使用命令显式支持的无认证 loopback 代理参数；不要把凭证写进 URL、配置或日志。

## 当前实现

- Gamma/CLOB HTTP、CLOB market WebSocket 与 RTDS 前向采集
- 不可变原始数据、manifest、SHA-256、checkpoint 与数据质量审计
- `Decimal`/最小单位账本、逐档 FAK/FOK、部分成交、费用和滑点
- maker 队列上下界、延迟、tick/min-order 与 stale-book 处理
- 标准/augmented Neg-Risk、天气、奖励和 lead-lag 描述实验
- 严格的证据分级；缺失证据使用 `null + reason`，失败实验仍进入注册表
- 无条件关闭的新订单边界：`DRY_RUN=false` 也不能解除

核心实现位于 [`src/edge_lab`](src/edge_lab)，薄命令入口位于 [`scripts`](scripts)。旧的 `run_mm.py`、`run_tui.py`、`src/strategy/` 和 `src/tui/` 已删除；不要按旧文章或历史计划重建这些入口。

## 证据与结论

| 等级 | 含义 | 可否称为“已回测盈利” |
|---|---|---|
| L0 | 合成数据和工程诊断 | 否 |
| L1 | 公共快照、历史价格或 shadow/touch | 否 |
| L2 | 自采前向 L2/WS/RTDS 上的 queue-bounded 模拟 | 仅在全部预注册门槛通过时 |
| L3/L4 | 另行授权的真实探针或低风险实盘 | 需要新的明确授权和证据合同 |

理论奖励、静态快照、公开盘口 touch 和零延迟场景都不是成交。当前权威入口：

- [最终报告](research/edge_discovery_2026-07-24/FINAL_REPORT.md)
- [机器可读结果](research/edge_discovery_2026-07-24/BACKTEST_RESULTS.json)
- [指标说明](research/edge_discovery_2026-07-24/METRICS.md)
- [数据清单](research/edge_discovery_2026-07-24/capture_freeze_entity_clock_strict_v3/DATA_MANIFEST.json)
- [实验注册表](research/edge_discovery_2026-07-24/EXPERIMENT_REGISTRY.jsonl)
- [实盘就绪审计](research/edge_discovery_2026-07-24/LIVE_READINESS.md)
- [结构线停手证明](research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md)
- [最终投资决策](STRATEGY_DECISION_2026-08-22.md)

大体量的本地采集数据不进入 Git；边界和重建方式见 [`DATASETS.md`](DATASETS.md)。

## 开发与验证

```bash
uv sync --locked --python 3.12 --extra dev
uv run python -m compileall -q src scripts
uv run python -m pytest -q
uv build
git diff --check
```

常规测试默认排除 `network` 标记；公开网络 canary 必须显式运行。项目没有引入仅为“门面完整”而存在的 lint、覆盖率或发布机器人依赖。

目录说明：

```text
src/edge_lab/   公开数据、回放、证据与安全边界
src/            保留的通用 API、风控和模拟基础设施
scripts/        可直接运行的薄 CLI
tests/          离线回归测试与显式网络 canary
research/       固定研究输入、结果和决策记录
docs/           入门、配置、安全与架构说明
deploy/         历史部署资产；当前没有受支持的实盘部署
```

## 文档与协作

- [文档中心](docs/index.md)
- [快速开始](docs/user/getting-started.md)
- [复现指南](docs/user/reproduction.md)
- [安全边界](docs/user/safety.md)
- [架构说明](docs/developer/architecture.md)
- [贡献指南](CONTRIBUTING.md)
- [安全漏洞报告](SECURITY.md)
- [变更日志](CHANGELOG.md)

提交 Issue 或日志前请移除私钥、API 凭证、钱包地址、订单标识和本机绝对路径。历史冻结研究产物中保留的原始绝对路径仅用于复现审计，不代表需要这些本机路径才能运行。发布状态、支持范围和已知限制以仓库中的版本化文档为准。

## 许可证与免责声明

代码以 [MIT License](LICENSE) 发布。

这是高风险交易研究软件，不保证盈利，也不构成投资、法律或税务建议。当前明确结论是：**没有已验证盈利策略，不建议实盘交易。** 使用者应自行确认所在地法律、平台条款和数据许可。
