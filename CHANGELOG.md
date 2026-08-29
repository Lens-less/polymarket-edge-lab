# Changelog

本文件记录面向使用者的软件变化。研究目录中的 v05–v08 表示实验协议或资产版本，不是软件发布版本。

## [Unreleased]

暂无。

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

[Unreleased]: https://github.com/Lens-less/polymarket-edge-lab/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Lens-less/polymarket-edge-lab/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Lens-less/polymarket-edge-lab/releases/tag/v0.1.0
