# 快速开始

本页带你从新克隆的仓库完成一次离线验收。不会访问 Polymarket、读取凭证或提交订单。

## 前置条件

- Git
- Python 3.11+（CI 验证 3.11 和 3.12）
- 推荐使用 `uv`

Windows 用户如果要复算 `research/` 中最深层的冻结证据，建议克隆到短路径（例如 `C:\src\polymarket-edge-lab`）。普通快速验收不受影响。

## 安装与验收

```bash
git clone https://github.com/Lens-less/polymarket-edge-lab.git
cd polymarket-edge-lab
uv sync --locked --python 3.12 --extra dev
uv run python scripts/run_edge_capture.py --config research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json --validate-only
```

成功输出应包含：

```json
{
  "new_orders_disabled": true,
  "valid": true
}
```

这里使用的是冻结研究配置，只做 schema 和安全门校验。它包含历史市场与历史采集路径，不能直接当作新的实时采集计划。

`uv run` 在 Windows、macOS 和 Linux 上命令一致，也不要求手工激活 `.venv`。pip 安装方式见[根 README](../../README.md#快速开始)。

## 接下来

```bash
# 查看所有主命令；离线
uv run python scripts/run_edge_lab.py --help

# 合成账本诊断；离线，不是盈利证据
uv run python scripts/run_backtest.py --mock

# SDK 与地域限制检查；访问公开网络，但不读取凭证、不下单
uv run python scripts/run_edge_lab.py audit
```

真实数据采集前，请先读[安全边界](safety.md)、[配置说明](configuration.md)和[复现说明](../../research/edge_discovery_2026-07-24/REPRODUCTION.md)。当前没有受支持的实盘启动命令。
