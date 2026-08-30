# 开发环境

本页描述的是 GitHub source-checkout 下的离线开发预览流程。`uv sync` 之后，常用
`uv run ...` 验证命令都从仓库根目录运行；只有前端段落需要切到 `apps/trade-desk/`。

## 安装

```bash
git clone https://github.com/Lens-less/polymarket-edge-lab.git
cd polymarket-edge-lab
uv sync --locked --python 3.12 --extra dev
```

不必激活 `.venv`；统一通过 `uv run` 调用项目环境。

## 验证

```bash
uv run polymm --help
uv run polymm doctor --config config/v0.2/canary.template.json
uv run ruff check src/profit_system tests/profit_system
uv run mypy src/profit_system
uv run python -m compileall -q src scripts
uv run python -m pytest -q
uv build
git diff --check
```

`polymm doctor` 是 fail-closed 的；离线输入不能授权 live。`uv build` 只做构建
验证，不表示仓库提供 PyPI 发布或独立 wheel 安装支持。

只跑相关测试：

```bash
uv run python -m pytest -q tests/profit_system
uv run python -m pytest -q tests/test_edge_lab.py
uv run python -m pytest -q tests/test_edge_lab_network_safety.py
```

默认 pytest 配置排除 `network` 标记。只有任务明确需要且环境允许时，才单独运行网络 canary：

```bash
uv run python -m pytest -q -m network tests/test_websocket_canary.py
```

前端验证在 `apps/trade-desk/` 中运行：

```bash
cd apps/trade-desk
npm ci
npm run typecheck
npm run test
npm run build
npx playwright install chromium
npm run e2e
```

`npm run e2e` 是 Playwright 入口，但当前这个 Windows checkout 里的 test
helper 会优先使用仓库 `.venv` 里的 Python 启动本地后端；`npm run e2e`
验证的是本地 paper/fake-live-lock/secret-hygiene 用例，只覆盖本地 fake/paper
路径，不是 live readiness，也不是盈利证明。`scan`、`replay`、`shadow`、
`status` 和 `desk` 的当前实现同样是确定性的固定验收/演示报告和本地预览表面，
不是实时市场或配置驱动的线上工作流。

仓库现在声明了 Ruff、Mypy、Vitest 和 Playwright；它们是发布门的一部分，但仍然只是软件质量验证，不是盈利证明。

## 约定

- 金额和概率计算沿用现有 `Decimal` 模式。
- 新行为应有最小回归测试；网络测试必须显式标记。
- 公开研究路径不得读取凭证、签名或提交订单。
- 不覆盖固定研究产物；新运行写入新的输出目录并保留 manifest。
- 更新用户可见命令时同步 README 和相应文档。
