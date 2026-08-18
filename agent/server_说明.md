# server.py 说明：Agent 构建与权限链路

> 对应源码：`agent/server.py`

## 文件定位

`agent/server.py` 是运行时编排层和 DeepAgents 框架之间的装配入口。它不决定任务流程，而是根据 `RunnableConfig` 把本轮需要的组件组装成可执行 Agent。

## 关键函数何时调用

| 函数 | 谁调用 | 调用时机 | 结果流向 |
|---|---|---|---|
| `get_agent()` | `core/runtime.py:_build_agent_for_runtime()` | planning、coding、analysis 等任务准备执行时 | 返回配置完成的 DeepAgent 给事件流运行器 |
| `ensure_backend_for_thread()` | `get_agent()` | 每次构建真实执行 Agent 时 | 创建或复用当前 thread 的 `LocalShellBackend` |
| `_task_kind_from_config()` | `get_agent()` | Backend 创建后、工具注册前 | 决定只读状态、提示词和 GitHub 写工具范围 |
| `_prepare_repo_backend_context()` | `get_agent()` | 本轮配置包含 `repo_url` 时 | 返回组合 Backend、记忆路径和预读内容 |
| `create_repo_backend()` | `_prepare_repo_backend_context()` | 仓库记忆初始化后 | 把 `/memories/` 路由到 LangGraph Store |
| `_general_purpose_subagent()` | `get_agent()` | 创建主 Agent 前 | 构建只读分析子 Agent |
| `_code_reviewer_subagent()` | `get_agent()` | 创建主 Agent 前 | 构建只读 Reviewer 子 Agent及审查工具链 |
| `_agent_filesystem_permissions()` | `get_agent()` | 调用 `create_deep_agent()` 时 | 声明主 Agent 虚拟目录读写边界 |

```text
runtime._build_agent_for_runtime()
  -> server.get_agent()
     -> ensure_backend_for_thread()
     -> _prepare_repo_backend_context()
        -> create_repo_backend()
     -> create_deep_agent()
  -> streaming_runtime.run_agent_with_event_stream()
```

## `get_agent()` 的输入

核心配置位于 `configurable`：

- `thread_id`：绑定 checkpoint、日志与业务任务。
- `task_kind`：决定只读权限、系统提示词和工具范围。
- `repo_url`：用于定位本地仓库、挂载仓库记忆。
- `__is_for_execution__`：区分真实执行和图结构探测。

缺少有效 `thread_id` 或不是执行态时，函数只返回不带业务工具的空 Agent，避免探测过程初始化完整工作区。

## 构建步骤

```text
解析 config
  -> 取得或复用 LocalShellBackend
  -> 根据 task_kind 设置 read_only
  -> 解析 GitHub 仓库与本地目录
  -> 初始化并挂载 /memories/
  -> 按权限选择工具
  -> 注册中间件
  -> 注入模型、提示词、skills、checkpoint 和 store
```

## 为什么复用 Backend、重建 Agent

`_BACKENDS` 按 `thread_id` 缓存 `LocalShellBackend`，避免每轮重复创建工作区结构和 AskPass 文件。Agent 本身每轮重建，以便系统提示词、任务类型和工具权限随本轮配置变化。

同一 thread 可能经历 `planning -> coding`，因此每次构建都会重新设置 `backend.read_only`，不能把第一次的权限永久缓存。

## 工具权限

所有任务都可使用资料读取、GitHub PR 上下文和审查工具。Reviewer 的典型顺序是：

```text
读取规则 -> 读取 GitHub PR 上下文（可选） -> 获取本地 diff
        -> 校验 finding 文件/行号 -> 保存 finding -> 汇总 finding
```

只有 `coding` 注册以下远端写工具：

- `open_github_pull_request`
- `publish_github_pr_comment`

远端写操作不注册给只读 Agent，比仅在提示词中要求“不要调用”更可靠。

文件权限同样按任务类型生成：只有 `coding` 可以写 `/projects`；其他任务只能写
`/reviews` 和 `/tmp`。`/memories` 对模型始终只读，由 runtime 在成功后统一更新。

## 中间件顺序

1. `MessageSanitizeMiddleware`：清理不兼容的历史消息块。
2. `ContextInjectionMiddleware`：注入仓库标识和长期记忆。
3. `SanitizeToolInputsMiddleware`：在工具执行前清洗路径、URL 和整数参数。
4. `SummarizationToolMiddleware`：注册显式压缩上下文工具，长任务需要时由 Agent 调用。
5. `ModelCallLimitMiddleware`：限制单次运行的模型调用数，超限后结束本轮。
6. `ToolErrorMiddleware`：把工具执行栈中的异常转换成模型可处理的 `ToolMessage`。

`MemoryUpdateMiddleware` 文件仍保留，但当前没有在 `server.py` 注册。仓库记忆由
`core/runtime.py` 在任务成功后统一更新，避免中间件和 runtime 双写。

## Backend 组合

`CompositeBackend` 的默认路由仍是本地 Shell；只有 `/memories/` 被路由到 `StoreBackend`：

```text
/projects、/skills、execute -> LocalShellBackend
/memories/                  -> LangGraph StoreBackend
```

这样模型看到的长期记忆像文件，但内容实际保存在独立 SQLite Store 中。

## Skills 和工作区记忆

- `LocalShellBackend` 启动时把应用内置 skills 同步到工作区 `/skills`。
- `create_deep_agent(skills=["/skills/"])` 让 DeepAgents 扫描该目录并按需加载 `SKILL.md`。
- `prompt.get_system_prompt()` 会读取 `agent/memory/workspace.md`，把macOS/Windows 共用的虚拟目录约定注入每轮系统提示词。

## 修改注意事项

- 新增远端写工具时，必须同时考虑 `task_kind` 注册范围和工具内部权限检查。
- 调整中间件顺序前，要确认前后依赖关系。
- 不要把 Token 放进 Agent config、命令或日志；认证由 Backend 环境变量注入。
- 新增任务类型时，要同步修改 `TaskKind`、任务分类、只读判断、提示词和 server 工具注册。
