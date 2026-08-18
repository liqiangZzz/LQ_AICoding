# 代码导读：源码与说明文档对照

这份文档用于回答两个问题：第一次阅读项目应从哪里开始，以及一次任务在代码中如何流转。

## 文档命名规则

- 单个源码文件：在源码旁使用 `<源码文件名>_说明.md`。
- 两个紧密相关的源码：文档标题和开头必须明确列出两个对应文件。
- 整个目录的综合说明：使用 `总览.md`，避免误认为只对应某一个文件。

## 源码与文档对照

| 源码 | 对应说明文档 |
|---|---|
| `agent/server.py` | `agent/server_说明.md` |
| `agent/core/runtime.py` | `agent/core/runtime_说明.md` |
| `agent/core/streaming_runtime.py` | `agent/core/streaming_runtime_说明.md` |
| `agent/core/checkpoint_history.py` | `agent/core/checkpoint_history_说明.md` |
| `agent/core/repo_mapping.py` | `agent/core/repo_mapping_说明.md` |
| `agent/core/repo_memory.py`、`repo_memory_update.py` | `agent/core/repo_memory_说明.md` |
| `agent/backends/local_shell.py` | `agent/backends/local_shell_说明.md` |
| `agent/store/sqlite_store.py` | `agent/store/sqlite_store_说明.md` |
| `agent/core/middleware/*.py` | `agent/core/middleware/中间件总览.md` |
| `agent/tools/*.py` | `agent/tools/工具包总览.md` |
| `agent/tools/reviewer_diff.py` | `agent/tools/reviewer_diff_说明.md` |

## 推荐阅读顺序

1. `agent/app.py`：FastAPI 启动入口，负责加载环境、日志和路由。
2. `agent/api/routes.py`：HTTP API，把任务交给运行时编排层。
3. `agent/core/runtime.py`：任务总路由，决定走工作区查询、同步、方案生成还是编码执行。
4. `agent/server.py`：真正组装 DeepAgent，包括模型、工具、中间件、后端和持久化组件。
5. `agent/core/middleware/中间件总览.md`：读完 `server.py` 后阅读，理解 Agent 构建时注册的上下文注入、参数清洗、异常处理和记忆写回分别在什么时候触发。
6. `agent/tools/工具包总览.md`：理解中间件之后阅读，了解模型能调用哪些 GitHub、搜索、网页读取和审查工具，以及工具如何访问运行上下文和业务 Store。
   - 正在定位 Reviewer 的文件和行号校验时，再读 `agent/tools/reviewer_diff_说明.md`。
7. `agent/backends/local_shell.py`：文件与命令执行边界，也是 macOS/Windows 兼容的核心。
8. `agent/core/streaming_runtime.py`：消费模型事件流并转换成前端可展示的运行事件。
9. `agent/store/sqlite_store.py`：任务、运行、事件、仓库映射和审查结果的业务数据库。

其中第 5、6 项属于“横切能力总览”：它们不是程序中单独的一步，而是帮助理解 `server.py` 组装进去的中间件和工具如何贯穿整轮任务。第一次完整阅读建议查看；以后定位具体问题时按需阅读即可。

## 一次任务的主调用链

```text
POST /api/tasks
  -> routes.create_task()
  -> runtime.run_agent_task()
     -> classify_task_kind()
     -> 简单任务直接执行，复杂任务进入 planning/coding
     -> server.get_agent()
        -> LocalShellBackend
        -> 模型、工具、中间件
        -> Checkpointer 与 LangGraph Store
     -> streaming_runtime.run_agent_with_event_stream()
     -> 更新业务 Store 与仓库长期记忆
```

## 三类“状态”不要混淆

| 状态来源 | 保存内容 | 主要入口 |
|---|---|---|
| 业务 Store | 任务列表、运行状态、前端事件、PR、仓库映射 | `core/graph.py:get_store()` |
| Checkpoint | Agent 消息和 LangGraph thread state | `core/graph.py:get_checkpointer()` |
| LangGraph Store | `/memories/...` 仓库长期记忆文件 | `core/graph.py:get_langgraph_store()` |

业务 Store 面向产品页面，Checkpoint 面向对话恢复，LangGraph Store 面向跨会话长期知识。三者使用不同数据库和数据结构。

## 权限链路

权限不是只靠系统提示词保证，而是逐层收紧：

```text
task_intent 判定任务类型
  -> server 设置 backend.read_only
  -> 只给 coding 注册 GitHub 写工具
  -> tool_sanitize 清洗路径和 URL
  -> LocalShellBackend 校验工作区、命令白名单和危险操作
```

`planning/analysis/qa/review/inspect/sync` 使用只读后端。`sync` 只额外允许仓库准备所需的 `clone/fetch/pull`，不能写业务文件、提交或推送。

## 跨平台入口

- `platform_utils.py`：识别 macOS / Windows。
- `env_utils.py`：把 `*_MACOS`、`*_WINDOWS` 配置映射到通用变量。
- `local_shell.py`：处理 Shell、路径分隔符、虚拟环境目录、输出编码和 Git AskPass。
- `.gitattributes`：统一文本换行，避免两个系统来回开发产生整文件差异。

## 遇到问题时先查哪里

- 服务无法启动：`app.py`、`settings.py`、`logging_config.py`。
- 任务路由不符合预期：`task_intent.py`、`runtime.py`。
- Agent 没有工具或权限错误：`server.py`、`middleware/`、`local_shell.py`。
- 页面没有运行步骤：`streaming_runtime.py`、`events.py`、`sqlite_store.py`。
- 仓库目录识别错误：`repo_mapping.py`。
- 多轮对话或方案确认丢失：`checkpoint_history.py`、`runtime.py`。
- 长期记忆内容异常：`repo_memory.py`、`repo_memory_update.py`。

## 相关专题文档

- `agent/server_说明.md`
- `agent/core/runtime_说明.md`
- `agent/core/streaming_runtime_说明.md`
- `agent/core/checkpoint_history_说明.md`
- `agent/core/repo_mapping_说明.md`
- `agent/core/repo_memory_说明.md`
- `agent/backends/local_shell_说明.md`
- `agent/core/middleware/中间件总览.md`
- `agent/store/sqlite_store_说明.md`
- `agent/tools/工具包总览.md`
