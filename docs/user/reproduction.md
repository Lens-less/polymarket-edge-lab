# 复现指南

本页是 `v0.2.0` 的 GitHub 源码检出离线 developer preview 复现入口。仓库支持
本地 editable install；安装后可直接使用 `polymm` CLI。命令均从仓库根目录运行，
不依赖维护者的本机路径；除最后单独标出的网络 canary 外，默认不读取 `.env`、
不使用凭证、不签名，也不提交订单。

## 准备环境

```bash
git clone https://github.com/Lens-less/polymarket-edge-lab.git
cd polymarket-edge-lab
uv sync --locked --python 3.12 --extra dev
uv run polymm --help
```

`uv sync` 会把当前源码安装为可编辑包，并注册 `polymm` 命令；`uv build` 只用于
确认发行构建可通过，不代表独立 wheel/PyPI 分发被支持。

Windows 上复算最深层证据时建议使用短检出路径，例如
`C:\src\polymarket-edge-lab`。

## 离线发布验收

```bash
uv run python scripts/run_edge_capture.py --config research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json --validate-only
uv run python -m pytest -q tests/test_btc_twap_edge_readiness_v08_assets.py
uv run python -m pytest -q tests/test_edge_lab_final_bundle.py
```

第一条命令应包含：

```json
{
  "new_orders_disabled": true,
  "valid": true
}
```

这些检查只验证已提交配置、固定场景资产和安全门，不会把合成或静态结果升级为
真实成交证据；它们是 developer preview 的验收面，不是生产扫描器、生产回放器
或完整 live desk 启动器。

## 固定证据入口

| 内容 | 仓库相对路径 |
|---|---|
| 最终报告 | `research/edge_discovery_2026-07-24/FINAL_REPORT.md` |
| 机器可读结果 | `research/edge_discovery_2026-07-24/BACKTEST_RESULTS.json` |
| 实验注册表 | `research/edge_discovery_2026-07-24/EXPERIMENT_REGISTRY.jsonl` |
| 数据清单 | `research/edge_discovery_2026-07-24/capture_freeze_entity_clock_strict_v3/DATA_MANIFEST.json` |
| 数据质量 | `research/edge_discovery_2026-07-24/capture_freeze_entity_clock_strict_v3/DATA_QUALITY.json` |
| 结构线停手证明 | `research/btc_5m_15m_edge_readiness_v08_2026-08-18/STRUCTURAL_LINE_STOP.md` |

生成最终证据包时使用的完整命令、哈希和历史绝对路径保留在
[`research/edge_discovery_2026-07-24/REPRODUCTION.md`](../../research/edge_discovery_2026-07-24/REPRODUCTION.md)。
其中的绝对路径是冻结 provenance，不是运行要求。

## 未随 Git 发布的数据

完整前向采集位于本地 `data/`，不随 Git 发布。需要这些输入的 Phase 2
生命周期重放无法仅靠源码归档完成；可用的 manifest、哈希、排除原因和重建
边界记录在 [`DATASETS.md`](../../DATASETS.md)。缺失原始数据时必须报告
`insufficient_data`，不能以 fixture 或公开快照代替。

## 可选公开网络 canary

下面的命令会访问外部公共 WebSocket，不属于离线发布验收或盈利验证：

```bash
uv run python -m pytest -q -m network tests/test_websocket_canary.py
```

网络 canary 不读取账户凭证，但仍可能受所在地访问限制、代理和上游可用性影响。
