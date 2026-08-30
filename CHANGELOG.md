# Changelog

本文件记录面向使用者的软件变化。研究目录中的 v05–v08 表示实验协议或资产版本，不是软件发布版本。

## [Unreleased]

暂无。

## [0.2.0] - 2026-08-31

### Added

- `src/profit_system` V0.2 模块化单体，覆盖机会归一化、策略运行时、持久化、组合账本、执行、门禁和报告。
- `polymm` 离线安全 CLI，提供 `doctor`、`scan`、`replay`、`shadow`、`acceptance`、`desk` 和 `status` 的固定验收/演示表面。
- `apps/trade-desk` React/Vite 本地预览台，以及对应的 Vitest / Playwright 验证。
- V0.2 迁移说明、运行手册和门禁 / live probe / fault drill 报告 Schema。

### Changed

- GitHub 源码检出 `v0.2.0` 定位为离线 developer preview；支持仓库内 editable install，`uv build` 产物仅用于构建校验，不作为独立 wheel / PyPI 分发目标。
- 公开只读 HTTP 请求的 `User-Agent` 随软件版本更新到 `0.2.0`。

### Fixed

- 批量订单现在先按 market / event 聚合新增名义敞口再执行风险上限检查；GTD 订单在确认时会用新鲜时钟再次校验到期时间。
- live 对账会追踪恢复后的本地活动订单；若 open-orders、直接查询和完整成交历史都不能解释缺失订单，则保持 fail-closed 并报告 reconciliation mismatch。
- 组合账本按 `(token_id, cycle_id)` 选择显式 realized-outcome 行作为单一权威来源，避免成交成本和已实现交易 PnL 重复计入，同时保留缺失或修复历史的回退语义。

### Safety

- 默认 live 状态是 `LIVE_BLOCKED`；没有明确的当前环境授权、风险预算和外部证据时，不开放真实下单。
- 未经外部验证的本地 live probe / fault-drill JSON 只作诊断参考；即使字段全部标为 `READY`，离线 `doctor` 也不能据此解锁 Canary。
- legacy `assert_new_orders_disabled()` 仍然是无条件 fail-closed。
- BTC 5m/15m 结构线继续维持 `STRUCTURAL_STOP`，奖励线继续冻结为未验证研究假设。

### Known limitations

- 没有已验证盈利策略。
- `paper`、`shadow` 和离线 gate 结果都是证据，不是盈利证明。
- `scan`、`replay`、`shadow`、`status` 和 `desk` 仍是固定场景验收/本地预览，不是实时市场/配置驱动的生产工作流或打包好的前端启动器。
- 当前发行只支持 GitHub 源码检出流程，不支持独立 wheel 或 PyPI 安装。
- 真实 live canary 仍然是外部依赖，不能仅凭软件合并自动完成。

## [0.1.1] - 2026-08-30

### Fixed

- 主机监控刷新失败时不再复用旧的 CPU credit 余额或清除遥测故障标记，避免把超时、陈旧指标和配置错误误报为正常数据。
- 隔离容量证据测试与 Linux 主机自动刷新，使跨平台 CI 修复不再改变生产监控语义。

## [0.1.0] - 2026-08-30

首个 GitHub 源码发布候选，定位为公开数据研究 Alpha。

### Added

- Polymarket 公开 API、WebSocket/RTDS 采集、回放和数据质量审计。
- 天气、逻辑约束/Neg-Risk、奖励和标的价格延迟研究工具。
- 固定研究输入、manifest、机器可读结果和复现文档。
- Python 3.11/3.12 的最小 CI、冻结依赖和源码构建检查。
- 首发前统一对外名称为 `Polymarket Edge Lab`，去掉与当前定位不符的 `mm-bot` 语义。

### Safety

- 新订单入口保持无条件关闭；`DRY_RUN=false` 不能绕过。
- 公开研究命令不读取账户凭证、不签名、不下单。
- BTC 5m/15m 结构线已 `STRUCTURAL_STOP`；奖励线仍是未验证假设。
- 冻结依赖将 `h2` 更新到 4.4.1，修复 `GHSA-6hr6-w5qg-qmwg`。

### Known limitations

- 仓库中没有经过严格认定的盈利策略，不建议实盘交易。
- 本次发布面向 GitHub 源码，不发布或支持 PyPI 安装。
- `research/` 含约 2 GiB 的已跟踪紧凑证据，克隆和 GitHub 源码归档都较大；完整本地采集数据仍需按 `DATASETS.md` 单独管理。
- Windows 上复算最深层证据路径时应克隆到短目录（例如 `C:\src\polymarket-edge-lab`），避免旧式 260 字符路径限制。
- `deploy/` 是历史复现资产，不是受支持的生产部署。
- 部分冻结研究产物保留生成时的原始绝对路径，仅作为复现证据，不是运行时前提。

[Unreleased]: https://github.com/Lens-less/polymarket-edge-lab/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Lens-less/polymarket-edge-lab/releases/tag/v0.2.0
[0.1.1]: https://github.com/Lens-less/polymarket-edge-lab/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Lens-less/polymarket-edge-lab/releases/tag/v0.1.0
