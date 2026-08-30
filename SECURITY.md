# Security policy

## Supported versions

| Version | Supported |
|---|---|
| `0.2.0` | Yes |
| `0.1.x` | Yes |
| `< 0.1.0` | No |

`v0.2.0` 是当前 GitHub 源码检出离线 developer preview；仓库内支持本地 editable install 和 `polymm` CLI，但 `uv build` 产物不作为独立 wheel / PyPI 安装目标。`v0.1.x` 只保留历史/兼容的公开数据研究语义，新订单入口无条件关闭；历史部署资产和实验协议不属于受支持的生产系统。

## Reporting a vulnerability

请优先使用仓库的 [GitHub private vulnerability report](https://github.com/Lens-less/polymarket-edge-lab/security/advisories/new)。如果该入口不可用，请通过维护者的 GitHub 主页请求一个私密联系方式，但不要在公开 Issue 中披露漏洞细节。

报告中请包含受影响版本、最小复现、影响和建议修复。移除真实私钥、API secret、钱包地址、订单 ID、认证代理地址和其他个人信息。维护者会先确认收到，再根据风险和可复现性协调修复与披露时间。

## Credential exposure

如果凭证曾进入工作区、日志或 Git 历史：

1. 立即在上游服务撤销或轮换凭证。
2. 停止相关进程并核对账户活动。
3. 不要只依赖“删除最新提交”；Git 历史和缓存仍可能保留内容。
4. 通过私密渠道报告暴露范围。

盈利能力、市场损失、平台地域限制和研究结论分歧不属于软件安全漏洞；但绕过新订单硬门、凭证泄露、签名/撤单错误、敏感日志或公开网络边界失效属于安全问题。
