# Releasing

`v0.1.0` 采用人工审核的 GitHub 源码发布，不发布 PyPI 包，也不需要自动发布机器人。

## 发布前

1. 确认 `pyproject.toml` 与 `CHANGELOG.md` 版本一致。
2. 确认 README 引用的决策和研究文件都已被 Git 跟踪。
3. 确认对外仓库名、README clone 地址和 `project.urls` 一致。
4. 生成并检查冻结依赖：

   ```bash
   uv lock --check
   uv export --locked --extra dev --no-emit-project --format requirements-txt --output-file requirements.lock
   ```

5. 运行发布门：

   ```bash
   uv sync --locked --python 3.12 --extra dev
   uv run python -m compileall -q src scripts
   uv run python -m pytest -q
   uv build
   git diff --check
   ```

6. 检查 `git status`，形成一个没有意外生成物或本地数据的发布提交。
7. 推送提交，等待 Python 3.11/3.12 CI 全绿。

## 创建 GitHub Release

在通过 CI 的准确提交上创建 annotated tag：

```bash
git tag -a v0.1.0 -m "Polymarket Edge Lab v0.1.0"
git push origin v0.1.0
```

用 `CHANGELOG.md` 的对应章节创建 GitHub Release。首次发布只附 GitHub 源码和校验过的 commit/tag；不要上传当前 wheel/sdist，也不要宣称存在可安装的控制台 CLI。

发布后从全新目录按 README 完成离线验收，并确认 Release 链接、许可证和安全报告入口可用。
