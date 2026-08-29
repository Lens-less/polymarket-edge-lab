# Polymarket Edge Lab v0.1.0

首个正式 GitHub 源码发布，定位为公开数据研究 Alpha。

本版完成了首发所需的最小开源收尾：

- 统一项目名称为 `Polymarket Edge Lab`，移除与当前定位不符的 `mm-bot` 语义。
- 重写 README、快速开始、配置、安全、贡献和发布文档，使仓库可直接离线验收。
- 保持新订单入口无条件关闭，明确项目不提供已验证盈利策略，也不支持实盘启动。
- 补齐最小 CI、冻结依赖、源码构建检查与发布流程文档。
- 固化研究结论、停手证明、数据边界和证据分级，避免把静态盘口或合成结果误写成真实利润。

已验证：

- `uv sync --locked --python 3.12 --extra dev`
- `uv run python -m compileall -q src scripts`
- `uv run python -m pytest -q`
- `uv build`
- `git diff --check`
- README 离线验收命令返回 `{"new_orders_disabled": true, "valid": true}`

已知边界：

- 截至 2026-08-22，BTC 5m/15m 结构线已 `STRUCTURAL_STOP`，仓库中没有经过严格认定的盈利策略。
- 本次发布面向 GitHub 源码，不发布 PyPI 包。
- `research/` 中部分冻结产物保留生成时的原始绝对路径，仅作为复现证据，不是运行前提。
