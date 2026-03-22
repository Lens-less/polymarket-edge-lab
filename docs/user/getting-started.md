# 快速开始指南

本指南将帮助您完成 Polymarket 做市商机器人的首次设置和运行。

## 前置要求

在开始之前，您需要准备以下内容：

### 1. Python 3.11 或更高版本

- 从 [python.org](https://www.python.org/downloads/) 下载安装
- 验证安装：`python3 --version`

### 2. Polymarket 账户（如需实盘交易）

- 在 [polymarket.com](https://polymarket.com) 注册账户
- 实盘交易需要申请 API 凭证

## 安装步骤

### 第一步：克隆代码仓库

```bash
git clone <仓库URL>
cd polymarket-mm-bot
```

### 第二步：创建虚拟环境

```bash
# macOS / Linux
python3 -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

### 第三步：安装依赖

```bash
pip install -r requirements.txt
```

### 第四步：配置环境变量

```bash
cp .env.example .env
```

使用文本编辑器打开 `.env` 文件，默认设置适合首次测试。

## 运行机器人（DRY_RUN 模式）

DRY_RUN 模式即模拟交易模式，机器人会模拟真实交易但不真正下单，这是测试和学习的安全方式。

```bash
python run_mm.py
```

正常运行后您应该看到类似输出：

```
INFO: Starting SmartMarketMaker in DRY_RUN mode
INFO: Connected to market feed
INFO: Placing quotes on [token_id]...
```

**恭喜！** 您的机器人已在模拟交易模式下运行。

## 验证机器人是否正常运行

- 检查控制台输出中的交易活动
- 观察挂单消息
- 监控成交通知

## 下一步

1. **阅读配置指南** - 根据您的账户自定义设置
2. **了解交易模式** - 学习 DRY_RUN 与 LIVE 的区别
3. **查看安全指南** - 实盘交易前的风险警告

## 常见问题排查

### "Module not found" 错误

- 解决方案：重新运行 `pip install -r requirements.txt`

### "No markets found"（未找到市场）

- 解决方案：检查网络连接，等待市场数据

### 机器人无法挂单

- 解决方案：确认 .env 中 `DRY_RUN=true`

## 切换到实盘交易

在熟悉 DRY_RUN 模式后：

1. 阅读[交易模式](trading-modes.md)文档
2. 查看[安全指南](safety.md)
3. 在 .env 中配置 API 凭证
4. 从小额开始测试

---

**相关链接**: [配置指南](user/configuration.md) | [常见问题](user/faq.md)