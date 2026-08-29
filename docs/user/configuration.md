# 配置说明

公开研究入口优先使用显式 CLI 参数和版本化 JSON 配置，不依赖全局 `.env`。

## 默认路径

- `scripts/run_edge_lab.py`：公开 API 扫描、回放和兼容性审计
- `scripts/run_edge_capture.py`：前向公开数据采集
- `scripts/run_reward_experiment.py`：奖励市场公开数据实验
- `scripts/run_weather_experiment.py`：天气公开数据实验
- `research/`：冻结输入、配置、报告和 manifest
- `data/`：本地运行数据，已被 Git 忽略

先用每个命令的 `--help` 查看当前参数：

```bash
uv run python scripts/run_edge_lab.py --help
uv run python scripts/run_edge_capture.py --help
uv run python scripts/run_reward_experiment.py --help
```

## `.env.example`

公开研究不需要复制 `.env.example`。该文件只保留给通用账户/feed 基础设施开发，默认 `DRY_RUN=true`，凭证为空。即使设置 `DRY_RUN=false`，`src/edge_lab/compatibility.py` 仍无条件拒绝新订单。

如确需调试保留的认证读取或撤单代码：

1. 使用隔离测试钱包和最小权限。
2. 将 `.env.example` 复制为 `.env`，不要改动示例文件本身。
3. 不要把密钥、钱包地址、订单 ID 或认证代理 URL 写入 Issue、报告或提交。
4. 任务完成后撤销临时凭证并删除本地 `.env`。

## 网络与代理

支持代理的采集命令要求显式传参，并只接受无认证 loopback URL。代理不能用于绕过地域限制、平台条款或当地法律。`run_edge_lab.py audit` 会访问 Polymarket 的公开 geoblock 端点；其结果不是法律意见。

## 固定研究配置

`research/` 中的 JSON 是历史证据的一部分，可能包含固定日期、市场 ID 和当时的本机路径。不要原地覆盖它们。新实验应复制到新的输出目录，记录输入、时间和版本，并遵循对应复现文档的 append-only 约束。历史路径属于复现元数据，不是你当前机器必须复刻的目录结构。
