# Contributing

感谢你改进 Polymarket Edge Lab。项目接受 bug 修复、研究复现改进、测试和文档贡献；它不接受未经新预注册和明确授权的实盘下单功能。

## 开发环境

```bash
git clone https://github.com/Lens-less/polymarket-edge-lab.git
cd polymarket-edge-lab
uv sync --locked --python 3.12 --extra dev
```

提交前运行：

```bash
uv run python -m compileall -q src scripts
uv run python -m pytest -q
uv build
git diff --check
```

默认测试排除 `network` 标记。不要为了让测试通过而访问真实账户、写入固定研究产物或放宽新订单安全门。

## 变更原则

- 先复现问题，再做最小、可回退的修改。
- 新行为要有回归测试；网络测试必须显式标记。
- 金额和概率沿用现有 `Decimal` 模式。
- 区分合成、静态、shadow 和真实成交证据，不升级证据等级。
- 不覆盖 `research/` 中的固定产物；新实验使用新的输出目录和 manifest。
- 新增依赖前说明现有标准库或项目工具为什么不够。
- 用户可见命令变化时同步 README 和对应文档。

## Pull Request

PR 描述应包含：问题、选择的最小方案、验证命令及结果、未验证风险。请保持单一主题，不混入格式化整个仓库或无关重构。

报告问题时提供最小复现、Python/操作系统版本和脱敏后的错误信息。不要公开私钥、API 凭证、钱包地址、订单标识、认证代理 URL 或本机绝对路径。安全问题请按 [`SECURITY.md`](SECURITY.md) 私下报告。
