# 内置的 Tau 自身知识

Tau 遵循 Pi 对产品知识采用的渐进披露方式。默认系统提示会标明已安装的文档和示例，然后将 Tau 相关请求路由到这些文件，而无需在每次对话中都加载它们。

## 打包资源

自身知识位于 `src/tau_coding/data/`：

```text
docs/       concise routing references and contributor workflows
examples/   readable extension examples
```

提示会直接指向扩展创建、技能、自定义及内置提供商/模型、CLI 命令、TUI 行为和架构。它会指示代理完整阅读相关文档和示例，并在实现前沿着其中的交叉引用继续阅读。

Tau 不会把自身维护工作流作为 Agent Skills 暴露。技能属于用户和项目资源，因此 Tau 的产品文档不会出现在技能侧边栏中，不会与用户技能名称冲突，也不会在禁用技能加载时消失。

## 原因

仓库中的 `AGENTS.md` 只有在 Tau 运行于自身源码检出目录时才有帮助。打包文档使安装后的 Tau 仍能保留产品知识，同时避免默认提示膨胀。将这些文档与技能分开，也能保持用户拥有的技能命名空间整洁，并与 Pi 的设计一致。

扩展和模型/提供商文档包含了此前由内置 `create-tau-extension` 和 `tau-model-catalog` 技能承载的详细工作流。打包的扩展示例仍可作为具体起点。

## 架构

自身文档属于 `tau_coding`，而不属于可移植的 `tau_agent` 框架。`self_docs.py` 解析已安装文档和示例的路径，`system_prompt.py` 则生成路由提示。`TauResourcePaths` 只发现用户和项目技能。Hatch 会将文档和示例作为 `tau_coding` 的一部分打包。

## 验证

```bash
uv run pytest tests/test_system_prompt.py tests/test_skills.py tests/test_resources.py tests/test_package_metadata.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
cd website && hugo --minify && npx --yes pagefind@latest --site public
```
