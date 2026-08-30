# 架构说明

仓库当前采用“冻结研究线 + V0.2 模块化单体 + fail-closed 兼容边界”的结构。`src/edge_lab` 保留历史研究、回放和证据资产，`src/profit_system` 承担新的 V0.2 研究到上线软件表面，`apps/trade-desk/` 提供离线本地预览台。当前这条线是 GitHub source-checkout 离线开发预览，不是独立分发的 PyPI/wheel 产品。

```text
src/profit_system/   V0.2 模块化单体：机会、策略、执行、账本、就绪性、报告
apps/trade-desk/     React/Vite 交易台
src/edge_lab/        冻结研究线与历史兼容层
docs/                规格、运行手册、迁移说明和报告 Schema
research/            冻结证据、预注册和最终决策
```

## 主要边界

### 机会与策略

`src/profit_system/opportunities/` 负责候选机会的归一化、排序、解释和风险约束；`src/profit_system/strategies/` 负责 Track A / Track B 的策略运行时、Research / Replay / Paper / Shadow 语义和门禁。这里的 `GO` / `NO_GO` 是接受性状态，不是盈利承诺。

### 执行、账本和组合

`src/profit_system/execution/`、`src/profit_system/persistence/` 和 `src/profit_system/portfolio/` 负责订单生命周期、持久化、组合账本、对账和 kill 语义。实现目标是把真实成交、纸面成交、shadow 证据和 replay 证据分开，任何一层都不能被误写成另一层的盈利证明。

### 门禁与报告

`src/profit_system/readiness.py`、`src/profit_system/gates.py` 和 `src/profit_system/reports.py` 负责 `LIVE_BLOCKED` / `LIVE_CANARY_READY`、GateReport、live probe 报告和 fault drill 报告。默认状态必须是 fail-closed；缺失的外部条件必须显式显示为 blocker，而不是被环境变量或布尔值伪装成 ready。

### 交易台

`apps/trade-desk/` 只展示服务端计算的快照、门禁结果和报告数据，不在浏览器中计算策略、风控、订单资格或权威 PnL。浏览器可以按 SPEC 发起 `Review`、`Confirm`、`Cancel`、`Cancel All` 和 `Kill` 操作员命令，但命令必须经过同源/CSRF 防护并回到模块化单体；策略资格、一次性 release permit、实时 preflight、风险/kill 状态、幂等和持久化均由服务端执行。交易台不是可以绕过门禁的第二个决策面。前端验证命令是 `npm run typecheck`、`npm run test`、`npm run build` 和 `npm run e2e`。`scan`、`replay`、`shadow`、`status` 和 `desk` 在当前版本里提供的是确定性的固定验收/演示报告和本地预览表面，而不是实时市场/配置驱动的 scanner、replay、shadow 或打包启动器。

### 历史兼容层

`src/edge_lab/compatibility.py` 是历史 live 发布边界，仍然无条件 fail-closed。V0.2 不会通过环境变量、配置开关或“先试一把”的路径重新打开这里。`assert_new_orders_disabled()` 的语义必须保持不变。

V0.2 新路径不复用该 legacy 函数作为自身发布门，否则新系统即使完成 Canary Gate 也会被永久阻断。V0.2 的新单权限由持久化策略资格、完整 Canary 证据、短时一次性 release permit、实时 venue preflight 和风险/kill 状态共同控制；这些条件不能解除或改变任何 legacy 入口的硬门。

## 版本边界

当前软件线是 `v0.2.0` 的 GitHub source-checkout 离线开发预览。研究目录中的 `v05`、`v06`、`v07`、`v08` 表示特定实验协议或资产版本，不表示软件已经发布到同一大版本。

## 冻结事实

- BTC 5m/15m 结构线已于 2026-08-22 `STRUCTURAL_STOP`
- 选择性流动性奖励线仍是未验证研究假设，继续冻结
- 没有已验证盈利策略
- 真实 live canary 仍然需要明确的当前环境授权、风险预算、资格和对账证据；否则默认 `LIVE_BLOCKED`

更细的实验设计、结算规则和复现顺序以对应 `research/` 目录中的预注册和报告为准。
