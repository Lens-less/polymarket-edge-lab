# Releasing

`v0.2.x` 采用人工审核的 GitHub 源码检出离线 developer preview，不发布独立 wheel / PyPI 包；仓库内支持本地 editable install，安装后提供 `polymm` CLI。

## 发布前

1. 确认 `pyproject.toml` 与 `CHANGELOG.md` 版本一致。
2. 确认 README 引用的决策和研究文件都已被 Git 跟踪。
3. 确认对外仓库名、README clone 地址和 `project.urls` 一致。
4. 生成并检查冻结依赖：

   ```bash
   uv lock --check
   uv export --locked --extra dev --no-emit-project --format requirements-txt --output-file requirements.lock
   ```

5. 从仓库根目录运行 Python、CLI 与构建发布门：

   ```bash
   uv sync --locked --python 3.12 --extra dev
   uv run python -m compileall -q src scripts
   uv run python scripts/run_edge_capture.py --config research/edge_discovery_2026-07-24/FORWARD_CAPTURE_CONFIG.json --validate-only
   uv run polymm --help
   uv run polymm doctor --config config/v0.2/canary.template.json
   uv run ruff format --check src/profit_system tests/profit_system
   uv run ruff check src/profit_system tests/profit_system
   uv run mypy
   uv run python -m pytest -q
   uv build
   git diff --check
   ```

6. 运行前端发布门：

   ```bash
   cd apps/trade-desk
   npm ci
   npm run typecheck
   npm run test
   npm run build
   npx playwright install chromium
   npm run e2e
   ```

7. 回到仓库根目录检查 `git status`，形成一个没有意外生成物或本地数据的发布提交。
8. 推送提交，等待 Python 3.11/3.12 和 Frontend Node 22 CI 全绿。

## 创建 GitHub Release

在通过 CI 的准确提交上创建 annotated tag：

```bash
VERSION=v0.2.0  # 每次发布时改为 pyproject.toml 中的版本
git tag -a "$VERSION" -m "Polymarket Edge Lab $VERSION"
git push origin "$VERSION"
```

用 `CHANGELOG.md` 的对应章节创建 GitHub Release。Release 只附 GitHub 源码和校验过的 commit/tag；`uv build` 只作为构建校验，不作为独立 wheel 分发或 PyPI 发布。仓库安装后可以使用 `polymm` CLI，但这不等于 live readiness 或盈利性已验证。

发布后从全新目录按 README 完成离线验收，并确认 Release 链接、许可证和安全报告入口可用。
