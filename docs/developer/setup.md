# 开发环境搭建

面向想要为项目贡献代码的开发者。

## 前置要求

- Python 3.11+
- Git
- 文本编辑器或 IDE（推荐 VS Code）

## 初始设置

```bash
# 克隆仓库
git clone <仓库URL>
cd polymarket-mm-bot

# 创建虚拟环境
python -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
pip install -r requirements-dev.txt  # 如果有的话
```

## 运行测试

```bash
# 所有测试
pytest tests/ -v

# 特定模块
pytest tests/test_smart_mm.py -v
pytest tests/test_safety.py -v

# 带覆盖率
pytest --cov=src tests/
```

## 代码风格

- 全程使用类型提示
- 金额使用 Decimal
- 使用数据类进行数据传输
- 运行检查：`flake8 src/`

## 调试技巧

### 启用调试日志
```bash
export LOG_LEVEL=DEBUG
```
（`run_mm.py`/`run_tui.py` 已随通用做市机器人一起删除，见 `../../CLAUDE.md`）

### 使用模拟数据测试
```python
from src.feed.mock import MockFeed

feed = MockFeed()
feed.set_midpoint("token", Decimal("0.50"))
```

## 常见开发任务

### 添加新配置变量

1. 添加 到 `src/config.py`
2. 添加到 `.env.example`
3. 在 README 中记录

### 添加新风险检查

1. 添加到 `RiskManager._run_checks()`
2. 添加配置阈值到 `src/config.py`
3. 添加测试

---

*贡献指南：参见 CONTRIBUTING.md 或 docs/developer/contributing.md*