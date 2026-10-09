# 受信任的隐藏内置扩展

## 第 2 阶段新增内容

Issue #606 增加了一种通用方式，让 Tau 运行打包在已安装软件包中的扩展代码。它不包括 llama.cpp、启动时提供商准备、`/local`、本地后端 API 或网络工作。

声明有意保持精简：

```python
BuiltInExtension(name="capability", setup=setup, hidden=True)
```

声明位于 `tau_coding.built_in_extensions.BUILT_IN_EXTENSIONS` 中。后续阶段可以向该元组添加产品能力，而无需让加载器了解它们的名称。第 2 阶段的生产元组为空；确定性测试会注入一个最小伪声明。

## 为什么内置功能使用扩展

打包能力仍需要正常的生命周期和失败边界。如果它通过宿主私有分支注册工具、命令或提供商，那么重新加载和源清理将不同于用户扩展，并且每项能力都会让核心更加依赖具体提供商。

因此，内置功能接收普通的 `ExtensionAPI`。第 2 阶段的伪实现证明一个 setup 函数可以注册：

- 一个 `AgentTool`；
- 一个斜杠命令；
- 一个第 1 阶段 `DynamicProvider` 层。

真实的 `ExtensionRuntime` 会组合这三者。没有使用仅供测试的加载器绕过路径。

## 加载顺序

`ExtensionRuntime.load()` 首先调用每代只执行一次的内置加载器，然后进行常规文件系统发现：

```text
trusted built-in declarations
→ user extension directory
→ explicit -e paths
→ trusted, opted-in project extension directory (later staged call)
```

实现 `--no-extensions` 的 `include_resource_dirs=False` 会跳过用户和项目目录发现，但不会跳过内置功能或显式路径。会话现在始终调用 `ExtensionRuntime.load()`，即使发现已禁用，因此内置功能不会在该模式下意外消失。

项目扩展仍作为信任解析后的独立加载步骤。内置功能是已安装软件包中的直接可调用对象，不是文件系统候选项，因此它们既不会增加受保护输入类别，也不会触发信任提示。只有当项目已有受保护输入时，它们才可使用现有的信任前钩子；本阶段的伪实现不会这样做。

## 来源与隐藏元数据

文件系统发现现在将每个来源标记为 `user`、`explicit` 或 `project`。内置功能使用：

```text
source      = built-in
source_id   = built-in:<declaration name>
path        = None
hidden      = declaration.hidden
```

运行时将这些信息保存在不可变的 `ExtensionSourceMetadata` 中。工具、命令、处理器和提供商层的所有者是宿主拥有的源 ID，而不是显示名称。

`extension_names` 仍是普通可见列表，并省略隐藏的内置功能。`extension_metadata` 是详细诊断视图，同时包含可见和隐藏来源。因此，隐藏表示“不作为已安装/已发现扩展宣传”，绝不表示“未加载”。

## 故障隔离

内置 setup 与导入扩展通过相同的 `_setup_extension()` 边界运行。运行时创建一个限定到来源的 API，记录来源，然后同步调用 setup。如果 setup 抛出异常：

1. 从活动扩展元数据中移除失败来源；
2. 移除其工具、命令、指南、渲染器和处理器；
3. 注销其全部动态提供商层和刷新工作；
4. 保留一条 `built-in setup failed` 扩展诊断；
5. 继续处理下一条声明和正常启动。

这使内置功能故障保持可见，而不会将其转化为宿主启动故障。

## 代际与清理生命周期

每个暂存的 `ExtensionRuntime` 都会对声明元组创建快照，并创建全新的 `ExtensionGeneration` 和第 1 阶段提供商注册表。在同一运行时上重复调用 `load()` 不会重新运行内置 setup；这一点很重要，因为项目扩展会在信任解析后的第二次调用中加载。

重新加载、恢复、新建会话和 cwd 替换已经会暂存新的运行时。因此，每种操作都会使用新的 API 和提供商代际重新运行内置 setup。旧运行时退役时会：

- 退役并分离每个动态提供商层；
- 请求取消其拥有的提供商刷新/发现工作；
- 使每个已捕获的 API/上下文/UI 外观失效；
- 取消订阅 harness 监听器；
- 移除扩展处理器、工具、命令、指南和渲染器；
- 丢弃绑定会话和轮次请求回调。

替换代码会在后继运行时向共享桥挂载 UI 之前清除旧的宿主 UI。退役过程有意不再清理该桥：如果在后继运行时的 `session_start` 之后清理，就会删除新部件。最终关闭会话时没有后继运行时，因此会先清除扩展组件，再退役运行时。

随后，异步关闭会排空协作式提供商工作，或报告第 1 阶段的有界遏制结果。测试会启动一个伪内置提供商刷新并退役运行时，然后断言取消、陈旧 API 拒绝、空来源元数据，以及工具、命令和提供商均已移除。

## 安全边界

“受信任”表示代码已经审查并随 Tau 一起发布，并不表示沙箱。与每个扩展一样，内置功能在 Tau 进程中执行 Python。隐藏状态只是显示元数据。项目信任仍是环境项目输入防护，不会隔离内置功能、提供商、工具、网络、文件系统、凭据或子进程。

## 与 Pi 对齐以及 Tau 的有意选择

这遵循 Pi 的隐藏打包扩展模式，以及重新加载时的来源失效机制。Tau 有意保留自身绑定 cwd/信任的新运行时暂存、感知来源/代际的第 1 阶段提供商注册表和详细元数据视图。不存在进程全局内置运行时，加载器中也没有按能力名称分支。

## 验证方法

针对性的确定性覆盖：

```bash
uv run pytest tests/test_extensions.py tests/test_extension_providers.py \
  tests/test_project_trust.py
```

测试覆盖声明验证、真实运行时注册、隐藏/来源元数据、加载顺序、`--no-extensions`、setup 仅执行一次、setup 回滚、不触发信任提示、重新加载/新建/恢复时的新代际、陈旧 API 失效，以及提供商任务退役。

完整仓库门禁仍为：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
hugo --source website --minify
uv build
```
