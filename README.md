# Polymarket Edge Lab

[![CI](https://github.com/Lens-less/polymarket-edge-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/Lens-less/polymarket-edge-lab/actions/workflows/ci.yml)
[![GitHub Release](https://img.shields.io/github/v/release/Lens-less/polymarket-edge-lab)](https://github.com/Lens-less/polymarket-edge-lab/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Public-data-first Polymarket research, replay, and evidence-audit lab. V0.2 is
a GitHub source-checkout offline developer preview: it adds a modular
profit-accountability monolith in `src/profit_system`, an offline-safe `polymm`
operator CLI, and a React/Vite local trade desk. The repository is still
designed to be reproducible and fail-closed: no validated profitability is
claimed, V0.2 live trading stays blocked until its separate Canary prerequisites
are met, and every legacy new-order entry remains unconditionally hard-disabled.
Wheel and sdist artifacts are build-validation outputs only; this repo does not
promise PyPI distribution or a standalone wheel installation path.

面向 Polymarket 的公开数据研究、成交感知回放与证据审计工具箱。项目用于验证交易假设，重点是可复现和防止把理论机会、静态盘口或合成成交误写成真实利润；它不是可直接实盘的交易机器人，也不是 Polymarket 官方项目。

English-first project metadata is used for the repository/package name, while
most long-form research notes and user guides currently remain in Chinese.

## V0.2 软件表面

- `src/profit_system/`：机会归一化、策略运行时、持久化、组合账本、执行、就绪性和报告的模块化单体
- `polymm`：`doctor`、`scan`、`replay`、`shadow`、`desk`、`status` 的离线安全 CLI；当前只暴露确定性的固定验收/演示报告和本地预览表面，不是实时市场/配置驱动的 scanner、replay、shadow，也不是打包好的前端启动器
- `apps/trade-desk/`：Vite/React 本地预览台，用于离线查看门禁状态、报告和证据
- `src/edge_lab/compatibility.py`：历史实盘兼容边界，仍然是无条件 fail-closed

V0.2 的业务北极星仍然只有一条：realized net PnL after all costs。`paper` 和 `shadow` 只是证据，不是盈利证明；当前默认 live 状态是 `LIVE_BLOCKED`。

> **截至 2026-08-22：BTC 5m/15m 结构线已 `STRUCTURAL_STOP`，仓库中没有经过严格认定的盈利策略，不建议实盘交易。**
>
> 独立复算确认结构对公允价值为 `1 + b`（`b >= 0`）：三个有官方 TWAP 的到期、三种执行口径的正 floor 均为 0。不要再为这条线换主机、跑 Gate 0、采 200 expiry 或写实盘适配器。见[结构线停手证明](research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md)。
>
> 选择性流动性奖励线同样未获准成为实盘策略，只作为未验证研究假设冻结。见[最终投资决策](STRATEGY_DECISION_2026-08-22.md)。

## 快速开始

前置条件：Git、Python 3.11+；持续集成验证 Python 3.11 和 3.12。推荐使用 [uv](https://docs.astral.sh/uv/)。公开研究和下面的验收命令都不需要账户、钱包或 `.env`。`uv sync` 完成后，下面的 `uv run ...` 命令都默认在仓库根目录执行。

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

`uv run` 无需激活虚拟环境，在 macOS、Linux 和 Windows 上命令一致。`uv build` 只用于验证打包元数据和构建输出，不表示仓库提供 PyPI 发布或独立 wheel 安装支持。

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

### V0.2 运营面

```bash
# 查看 V0.2 CLI 和子命令
uv run polymm --help

# 默认返回 LIVE_BLOCKED 的 fail-closed 预检
uv run polymm doctor --config config/v0.2/canary.template.json

# 进入前端交易台
cd apps/trade-desk
npm ci
npm run dev
npm run typecheck
npm run test
npm run build
npx playwright install chromium
npm run e2e
```

`npm run e2e` 会在本仓库虚拟环境里启动本地 trade-desk 后端并运行 4 条
Playwright 用例；它验证本地 fake/paper/fake-live-lock 路径和 secret hygiene，
但不代表 live 已就绪，也不构成真实盈利证明。

`polymm doctor` 是 fail-closed 的；离线输入不能授权 live。`scan`、`replay`、
`shadow`、`desk` 和 `status` 现在只服务于固定验收、演示和本地预览，不是
真实市场或配置驱动的线上工作流。

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
- `src/profit_system` 的机会、策略、执行、账本、门禁和报告模块
- `polymm doctor/scan/replay/shadow/desk/status` 的 V0.2 离线安全入口，当前只提供确定性的固定验收/演示报告和本地预览表面
- `apps/trade-desk` 的前端与 Playwright 端到端验收

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
uv run polymm --help
uv run polymm doctor --config config/v0.2/canary.template.json
uv run ruff format --check src/profit_system tests/profit_system
uv run ruff check src/profit_system tests/profit_system
uv run mypy
uv run python -m compileall -q src scripts
uv run python -m pytest -q
uv build
git diff --check
```

前端验证：

```bash
cd apps/trade-desk
npm ci
npm run typecheck
npm run test
npm run build
npx playwright install chromium
npm run e2e
```

常规测试默认排除 `network` 标记；公开网络 canary 必须显式运行。仓库现在也声明了 `ruff`、`mypy` 和前端 Playwright 路径，但它们都只是软件验证，不是盈利证明。

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
- [V0.2 迁移说明](docs/user/v0_2_migration.md)
- [V0.2 运行手册](docs/user/v0_2_operator_runbook.md)
- [V0.2 live readiness report](docs/reports/live_readiness_report.md)
- [V0.2 strategy performance report](docs/reports/strategy_performance_report.md)
- [V0.2 acceptance report](docs/reports/v0_2_acceptance_report.md)
- [门禁报告 Schema](docs/reports/gate_report_schema.md)
- [Live 预检结果 Schema](docs/reports/live_probe_result_schema.md)
- [故障演练结果 Schema](docs/reports/fault_drill_result_schema.md)
- [贡献指南](CONTRIBUTING.md)
- [安全漏洞报告](SECURITY.md)
- [变更日志](CHANGELOG.md)

提交 Issue 或日志前请移除私钥、API 凭证、钱包地址、订单标识和本机绝对路径。历史冻结研究产物中保留的原始绝对路径仅用于复现审计，不代表需要这些本机路径才能运行。发布状态、支持范围和已知限制以仓库中的版本化文档为准。

## 许可证与免责声明

代码以 [MIT License](LICENSE) 发布。

这是高风险交易研究软件，不保证盈利，也不构成投资、法律或税务建议。当前明确结论是：**没有已验证盈利策略，不建议实盘交易。** 使用者应自行确认所在地法律、平台条款和数据许可。
