# 开发环境

## 安装

```bash
git clone https://github.com/Lens-less/polymarket-edge-lab.git
cd polymarket-edge-lab
uv sync --locked --python 3.12 --extra dev
```

不必激活 `.venv`；统一通过 `uv run` 调用项目环境。

## 验证

```bash
uv run python -m compileall -q src scripts
uv run python -m pytest -q
uv build
git diff --check
```

只跑相关测试：

```bash
uv run python -m pytest -q tests/test_edge_lab.py
uv run python -m pytest -q tests/test_edge_lab_network_safety.py
```

默认 pytest 配置排除 `network` 标记。只有任务明确需要且环境允许时，才单独运行网络 canary：

```bash
uv run python -m pytest -q -m network tests/test_websocket_canary.py
```

项目当前没有声明 Ruff、Mypy、Flake8 或覆盖率插件；不要把本机偶然安装的工具写成仓库发布门。新增依赖前应先说明收益和维护成本。

## 约定

- 金额和概率计算沿用现有 `Decimal` 模式。
- 新行为应有最小回归测试；网络测试必须显式标记。
- 公开研究路径不得读取凭证、签名或提交订单。
- 不覆盖固定研究产物；新运行写入新的输出目录并保留 manifest。
- 更新用户可见命令时同步 README 和相应文档。
