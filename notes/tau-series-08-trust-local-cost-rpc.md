# 第八篇：从能跑到可信——trust、本地推理、成本与 RPC

前面七篇其实只讲了一件事：一次请求的状态如何可信。消息要对得上号，工具结果要回得来，会话要能重放，事件要能交给任何一个前端。这些是"能跑"的及格线，也是"跑一次"的可信。

但一个天天挂在终端里的工具，及格线不在单次请求上。项目目录里躺着的 AGENTS.md 和扩展是真会执行的代码，模型目录每天都在变，账单按 token 走，另一个进程还想把 Tau 当后端用。0.4.x 补的四块拼图——trust、模型来源、成本、RPC——回答的是同一个问题的长期版：跑了一千次之后，凭什么还信它。

> 本文快照于 Tau 0.4.1（2026 年 9 月）。代码持续演进，函数名与行级细节以仓库当前源码为准。

## trust：加载守卫，不是沙箱

项目目录不是中性的。里面的 `AGENTS.md` 会进 system prompt，`.tau/` 下的 skills 和 prompt template 会被加载，项目扩展是真在您机器上跑的 Python。Tau 把这些统称为"会被执行的输入"，进不进运行环境，要过一个门卫。

门卫是 `src/tau_coding/project_trust.py:484` 的 `ProjectTrustCoordinator`。它对一个项目目录按六级优先级裁决，先到先得：

```text
CLI 一次性 override
  → 目录里没有受保护资源，直接放行
  → 预 trust 扩展裁决（可记名、可持久化）
  → 最近的祖先目录保存过决策，继承
  → 全局默认值（非 ask 时直接生效）
  → 交互询问；headless 下无人可问，拒绝
```

保存的决策落在 `~/.tau/trust.json`。这块存储的写法比一般配置文件认真：先写 0600 权限的 undo journal，再 fsync 后原子替换；下次启动发现 journal 没清掉，就按 journal 回滚到旧状态，回滚失败则保留标记、宁可拒绝加载（`project_trust.py:462` 的 `_recover_unlocked`）。信任文件写坏一半的结果不是"这次先信着"，而是 fail-closed。

和 Pi 相比，Tau 在这里有意更严三处：项目 `AGENTS.md` 也受 gate（Pi 不 gate）；reload 永不隐式保存 trust 决策；项目扩展除了 trust 之外还要额外 `--project-extensions` 才加载。理由在第 7 篇说过：扩展是被执行的代码，来源、信任与生命周期必须明着管。

**反例：** 把项目 trust 当沙箱用——"我在不可信目录里点了拒绝，里面的恶意代码就碰不到我了"。碰得到。trust 只管输入加载：不信任的目录，它的指令不进 prompt、它的扩展不被 import。但如果您自己在里面跑 `bash` 工具、编译它的代码、执行它的脚本，运行时权限不归 trust 管。它是加载守卫，不是隔离边界；把它当沙箱，是把门卫当成了防弹衣。

## 模型从哪来：目录、订阅与本地 llama.cpp

模型列表看着是小事，实际是供应链问题：列表从哪来、多久刷新、坏了降级成什么。Tau 0.4.x 给了三条来源，规则各不相同。

第一条是构建期快照。`scripts/generate_models.py:32` 从 models.dev 生成 `models-dev-catalog.json` 打进包里，`src/tau_coding/models_dev.py:212` 的 `_is_eligible_model` 只收支持工具调用且未弃用的模型。运行时 `/model` 先渲染这份本地快照——离线也有得选——再后台刷新，4 小时节流、带 ETag、原子写（`src/tau_coding/models_dev_store.py:27`）。刷新失败不会让列表消失，最坏只是旧一点。

第二条是订阅账号的活目录。Codex 订阅实现了 `src/tau_ai/model_catalog.py:48` 的 `ModelCatalogProvider`：用账号凭证问一次"我现在能用什么"，拿到的清单只做进程内 overlay，绝不写回持久配置。活目录是账户事实，不是用户偏好；今天查到的不该变成明天启动时的假设。

第三条是本地 llama.cpp，接入方式值得单独说：它以"受信任的隐藏内置扩展"身份进来（`src/tau_coding/built_in_extensions.py:40` 的 `BuiltInExtension`），走的不是核心里的特殊分支，而是第 7 篇讲的通用扩展接缝——`extensions/builtins/llama_cpp/__init__.py` 里的 `setup()` 拿到的就是普通 `ExtensionAPI`，用它注册一个休眠的动态 Provider 和 `/local` 后端。探测不到的模型元数据一律标 `unknown`，不编参数；服务不在了也不静默卸载，状态留着等用户处置。核心代码里没有一个 `if llama_cpp`：本地推理证明了扩展接缝不是装饰，是真能承重的那根梁。

## 成本不是遥测，是上下文的一部分

缓存和计费的常见做法是事后算账：请求发完，用量写进报表，下次优化看仪表盘。Tau 把它当成会话事实来做，因为缓存命中取决于"这次请求长什么样"，是每次调用都要做的决策。

Anthropic 侧，`src/tau_ai/anthropic.py:425` 构造消息 payload 时会把 4 个 `cache_control` 断点花满：system 和工具之外，消息尾部一个断点，再用 `_previous_request_boundary`（`anthropic.py:551`）找回上一次请求的结尾位置放一个，让本轮前缀尽量命中上一轮的缓存。订阅 OAuth 走 1 小时 TTL，API key 走默认 short TTL——凭证类型不同，缓存策略跟着换。OpenAI 系没有显式断点，就用会话 ID 做 `prompt_cache_key` 亲和（`src/tau_ai/openai_cache.py:11`），让同一 session 的请求尽量落到同一缓存桶。

计时也不靠墙钟。agent loop 用单调时钟记录首输出与全程耗时（`src/tau_agent/loop.py:227`），写进 `AssistantMessage` 的 `timeToFirstOutputMs` 和 `totalDurationMs`——它们和文本、tool call 一样，是消息的一部分，会跟着会话持久化。`src/tau_coding/session_stats.py:35-64` 再把这些事实汇总成缓存命中率与 token 加权 TPS。

原则就一句：用量数字和工具结果一样，是会话事实，不是事后报表。仪表盘可以明天再画，事实必须今天写对地方。

## RPC：第三个前端，互换在进程边界

第 6 篇说 TUI 是适配器，第 6 篇的证据是 print mode 和 Textual 消费同一事件流。0.4.x 给了这个论断第二次证明：RPC 模式。

`src/tau_coding/rpc.py:796` 的 `run_rpc_session` 把一个已经配好的 `CodingSession` 挂上 stdin/stdout：每行一条严格 JSON，命令进来、response 和事件出去。两个细节说明它是按"进程边界上的协议"设计的，不是按"调试口"设计的：所有写出过一把 `anyio.Lock` 串行化（`rpc.py:156`），并发 prompt 任务的事件不会字节交错；stdin 读到 EOF 时取消所有活动任务再关闭（`rpc.py:185`），宿主进程挂断不会留下半跑的 loop。

除此之外它没有自己的任何东西。不读原始 session JSONL，不复制工具循环，不理解消息语义——它和 print renderer、Textual app 一样，只消费 `CodingSession` 的公开事件。Electron 宿主要靠 `{"agent_runtime": "tau"}` 一个设置在 Tau 和 Pi 两个子进程间切换，靠的就是这份"无自己东西"：协议面之外，两边没有需要宿主迁就的私货。会话持久文件则故意互不兼容，谁也不假装能读对方的存档；协议里不支持的能力返回确定性失败，而不是悄悄改掉语义。

第 6 篇那句"换前端不用动循环"，当时听像设计愿望。RPC 把它变成了可以测试的事实——`tests/test_rpc.py` 不启动 TUI，就能断言另一个进程拿到关联 response 和同一事件流。

## 与 Pi 对齐到哪，差在哪

| 层面 | Tau | Pi 对齐情况 |
| --- | --- | --- |
| 消息/事件协议 | `WireModel` 输出 camelCase（`src/tau_agent/messages.py:23-32`） | 对齐 |
| 会话 JSONL | "canonical Pi wire shape"（`src/tau_agent/session/jsonl.py:20`） | 协议对齐，持久文件故意不互读 |
| CLI flags | `--session`/`-p`/`--mode`/`-e`/`--export` 全面对齐（见 `dev-notes/cli-mirror-pi-flags.md`） | 对齐；`--mode` 多出 transcript，`--session-id` 拒绝已存在 id |
| RPC | JSONL 命令/事件契约 | 对齐 Pi 公布的 RPC 契约 |
| 扩展 | follows Pi's extension system，清单用 `pyproject.toml` 的 `tool.tau.extensions` | 机制对齐，载体换 Python 生态 |
| prompt 模板变量 | `$1`/`$ARGUMENTS`/`${@:N:L}`（`src/tau_coding/prompt_templates.py:18-22`） | 对齐 |
| TUI | 自有，基于 Textual | 不对齐，各自实现 |
| 本地推理 | 自有，llama.cpp 内置扩展 | Pi 无对应能力 |
| trust | gate 项目 AGENTS.md，reload 不隐式保存 | Tau 更严 |

一句话读这张表：对齐的是协议与契约，自有的是语言生态与产品取舍。

## 小结

四块拼图收回一条主线：trust 让输入可信——项目目录里的东西进不进运行环境，有门卫、有账本、有 fail-closed；目录与本地推理让模型来源可信——快照保底、活目录不落盘、本地能力走受控接缝；成本让账单可信——缓存断点和计时是每次请求的一部分，不是月底的惊喜；RPC 让前端可信——第三个消费者证明事件流真的够用，互换发生在进程边界而不是口号上。

"能跑"是单次的，"可信"是结构性的。0.4.x 做的事，是把前七篇在单次请求里证明过的那套纪律，推广到输入、模型、账单和前端这四个长期暴露面。

## 三分钟核对

- `uv run pytest tests/test_project_trust.py -q`——trust 的优先级裁决、journal 恢复与 fail-closed；
- `uv run pytest tests/test_rpc.py -q`——RPC 的关联 response、事件流与 EOF 取消；
- `uv run pytest tests/test_session_stats.py -q`——缓存命中率与 token 加权 TPS 的口径。

这个系列从第 0 篇那张三层图出发：`tau_ai` 管不可靠的外部智能，`tau_agent` 管可移植的循环与状态，`tau_coding` 管真实世界里的项目与信任。八篇走完，结论没变，只是分量变了：三层各自把自己的事实交出来——消息对得上、输入有门卫、账单有出处、前端换得起——整体才配得上"可信"两个字。
