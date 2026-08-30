# V0.2 新会话 `/goal` 启动提示词

> 本文件是继续完成 V0.2 目标合同的未来工作提示词，不是 v0.2.0 已完成证明。v0.2.0 只发布源码检出离线 preview，尚未完成 production scanner/live desk、外部 attestation verifier、真实 Canary 或盈利验证。

## 使用方法

1. 在 `poly-mm` 项目中创建一个新的 Codex 会话。
2. 将下面“主 Goal”代码块完整复制为新会话的第一条消息。
3. Goal 运行期间，用 `/goal` 查看状态；需要时使用 `/goal pause`、`/goal resume` 或 `/goal clear`。
4. 主 Goal 的完成面是“软件实现完成且 Canary-ready”，不是未经明确授权自动提交真钱订单。

## 主 Goal

```text
/goal 完整实现 docs/specs/V0_2_PROFIT_TRADING_SYSTEM_SPEC.md，将当前 poly-mm 仓库升级为 V0.2 Research-to-Live Profit Trading System；持续工作直到 SPEC 第 18 节 Definition of Done 对所有仓库内可完成事项均已满足，所有离线测试、lint、typecheck、构建、前端测试和 Playwright E2E 全部通过，利润导向交易台可运行，Track A 与 Track B 的固定可复算验收场景至少各有一条完整走通 Research -> Replay -> Paper -> Shadow，Polymarket live adapter 达到 LIVE_CANARY-ready，并且 legacy live 硬门仍保持 fail-closed。开始前完整阅读 AGENTS.md、本 SPEC、README.md、PROJECT_CONTEXT.md、STRATEGY_DECISION_2026-08-22.md、docs/index.md、docs/roadmap.md、docs/developer/architecture.md、src/edge_lab/compatibility.py、src/edge_lab/execution.py 和 src/edge_lab/btc_twap_execution_probe.py。以 realized net PnL after all costs 为唯一业务北极星，不以页面数量、回测收益、Paper PnL、Shadow 理论收益或未确认奖励冒充盈利；当前 BTC 5m/15m STRUCTURAL_STOP 和 2026-08-22 已冻结的 rewards thesis 均不得自动恢复。按 WP0 -> WP6 检查点推进，独立工作可并行使用原生 subagents，但主会话负责集成和最终验证；每个检查点只报告已完成内容、验证证据、剩余项和 blocker。优先复用现有代码和测试，保持模块化单体，避免多 venue、多账户、微服务、通用插件平台和无决策价值的复杂页面。不得在浏览器、日志或提交中暴露任何 secret。不得仅用环境变量或布尔值绕过 live release gate。没有用户对当前目标环境的明确真钱授权、凭据和风险预算时，不得提交真实订单；此时仍须完成所有软件工作，并生成机器可读和人类可读的 LIVE_BLOCKED 报告，准确列出剩余的外部条件，不能伪造 live 或盈利证据。遇到可修复的测试、构建、类型、E2E 或合同失败时继续迭代，不要提前结束；只有当仓库内工作全部完成且验证通过，或同一外部 blocker 已按 goal 规则构成真正阻塞时才停止。
```

## Goal 检查点

主 Goal 应按以下顺序维护进度：

1. `WP0`：合同、目录、类型、配置、Venue adapter tests、基线。
2. `WP1`：统一经济模型、机会 schema、扫描、排名和解释。
3. `WP2`：套利与 Maker 策略、回放和 Research Gate。
4. `WP3`：现实 Paper、Shadow、证据包和 Shadow Gate。
5. `WP4`：实盘 adapter、订单生命周期、账本、对账、kill。
6. `WP5`：单页交易台、前端测试和 Playwright E2E。
7. `WP6`：doctor、fault drills、runbook、Canary-ready 和 LIVE_BLOCKED/READY 报告。
8. `Final`：全量验证、代码审阅、文档、迁移说明和最终 DoD 对照表。

## 真实 Canary 的独立 Goal

只有主 Goal 已完成、系统报告 `LIVE_CANARY_READY`，并且用户明确授权当前环境使用真钱时，才使用下面的第二个 Goal。执行前必须把方括号中的风险预算替换为明确数值。

```text
/goal 按 docs/specs/V0_2_PROFIT_TRADING_SYSTEM_SPEC.md 第 6.4 节和 WP6，在已明确授权的当前环境执行一次受控 LIVE_CANARY；严格限制为策略 [strategy_id]、市场白名单 [market_ids]、单笔最大名义 [amount]、总风险预算 [amount]、最大日损 [amount]，禁止自动放量。开始前重新运行 polymm doctor 和全部 Canary Gate 检查；任何检查失败、geoblock/账户状态不允许、数据陈旧、订单状态不确定、对账不一致、heartbeat/user stream 异常或 kill 条件触发时，不提交新单或立即停止，并保留完整报告。持续到预注册 Canary 计划完成且订单、成交、余额、仓位、费用和 realized net PnL 全部对账，或安全停止条件触发；不得为了完成 Goal 放宽风险预算或验证门槛。
```

## Goal 完成报告要求

最终报告必须包含：

- 实现的 WP 和改动文件。
- 所有验证命令及结果。
- SPEC 第 18 节逐项 PASS/FAIL/EXTERNAL。
- Track A、Track B 的 Research/Shadow 状态。
- live 状态：`LIVE_BLOCKED`、`LIVE_CANARY_READY` 或经授权后的 Canary 结果。
- 已知风险和仍需用户完成的外部动作。
- 明确声明是否曾提交真实订单和是否发生真实资金变化。
