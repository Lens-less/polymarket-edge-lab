# Polymarket Edge Lab v0.1.1

这是 `v0.1.0` 首发后的安全语义补丁，不增加功能，也不扩大项目的网络或交易边界。

修复内容：

- 主机 CPU credit 遥测刷新失败时，不再沿用旧余额并删除故障标记。
- CloudWatch 超时、陈旧指标、配置错误或 CLI 故障继续 fail closed，并生成明确的 `cpu_credit_telemetry_unavailable` 证据与告警。
- 容量证据测试不再隐式依赖 Linux `/proc` 自动刷新，保持 Windows/Linux 行为一致而不触碰生产语义。

发布边界保持不变：

- 只支持公开数据研究、回放与证据审计。
- 新订单入口无条件关闭。
- 不发布 PyPI 包，不声明存在已验证盈利策略。

验证：

- Python 3.11/3.12 GitHub Actions 全量 CI
- 主机监控定向回归测试
- README 离线验收、锁文件一致性、源码编译和源码构建
