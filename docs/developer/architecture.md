# 架构说明

仓库采用“薄 CLI + 深模块 + 固定证据”的结构，历史演进较长，但当前发布边界很简单。

```text
scripts/          参数解析和命令入口
    |
src/edge_lab/     公开 API、采集、回放、经济性与证据审计
    |
research/         版本化配置、manifest、报告和决策

src/              保留的账户/feed/风控/模拟基础设施
deploy/           已归档的历史 AWS 部署资产
```

## 主要边界

### 公开数据

`src/edge_lab/public_api.py`、`sources.py`、recorder 与各实验 runner 负责公开端点。网络层应拒绝敏感参数、认证代理和重定向，并只持久化脱敏错误。

### 成交与回放

`execution.py`、`replay.py` 及策略专用 replay 模块使用逐档流动性、费用、滑点、延迟和队列边界。合成、静态、shadow 和真实成交在证据模型中分开，不能互相升级。

### 数据与证据

`data_store.py`、`data_manifest.py`、`evidence.py` 和 `final_bundle.py` 管理 append-only 原始记录、manifest、哈希、质量审计和最终报告。`research/` 中的固定结果是审计输入，不是普通缓存。

### 安全

`compatibility.py` 是新订单发布边界，当前无条件 fail-closed。公开 CLI 不读取 `src/config.py` 中的账户凭证。保留的 `src/trading.py` 等通用模块不是受支持的主程序。

## 版本边界

软件包/仓库版本从 `v0.1.0` 开始。研究目录中的 v05、v06、v07、v08 表示特定实验协议或资产版本，不表示软件已经发布到相同大版本。

更细的实验设计、结算规则和复现顺序以对应 `research/` 目录中的预注册和报告为准。
