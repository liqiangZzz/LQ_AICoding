# Agent 运行时编排层

> 对应源码：`agent/core/runtime.py`

## 一、文件定位

`runtime.py` 是 FastAPI 版 LQ-AICODING 的 Agent 任务编排层。FastAPI 路由或后台任务将 `repo_url`、`prompt` 和可选的 `thread_id` 传入 runtime，runtime 本身不直接处理 HTTP 请求。

主要职责：

- 识别任务类型并选择执行分支。
- 维护 thread、run 和 run_events 等业务状态。
- 根据 `task_kind` 构造 Agent 运行配置。
- 控制 `Planning → Confirm → Coding` 的人在回路流程。
- 调用 Agent，归一化结果，更新业务数据和仓库长期记忆。
- 删除任务时协调清理业务 Store 和 LangGraph checkpoint。

runtime 不直接实现模型推理，也不直接组装模型、工具、中间件和后端；这些由 `agent.server.get_agent()` 统一构建。

### 核心入口何时调用

| 函数 | 谁调用 | 调用时机 | 后续去向 |
|---|---|---|---|
| `run_agent_task()` | `api/routes.py:create_task()` | 收到 `POST /api/tasks` 后 | 分类任务并选择下面某一条分支 |
| `run_workspace_listing_task()` | `run_agent_task()` | 用户只要求查看本地项目 | 直接读工作区，不调用模型 |
| `run_pull_only_task()` | `run_agent_task()` | 用户只要求 clone/fetch/pull | 准备仓库并更新映射，不进入 coding |
| `run_plan_response_task()` | `run_agent_task()` | 首次 coding 请求或用户要求修改方案（可选传递 `event_sink`） | 构建 planning Agent，输出待确认方案 |
| `_build_agent_for_runtime()` | 方案或通用 Agent 分支 | 即将调用模型时 | 调用 `server.get_agent()` 完成装配 |
| `get_task()` / `list_tasks()` | 查询 API | Dashboard 查询任务时 | 聚合业务 Store 中的展示数据 |
| `delete_task()` | 删除入口 | 用户删除任务时 | 同时清理业务数据和 checkpoint |

```text
routes.create_task()
  -> run_agent_task()
     ├─ inspect -> run_workspace_listing_task()
     ├─ sync    -> run_pull_only_task()
     ├─ coding（未确认）-> run_plan_response_task()
     └─ 其他/已确认 -> _build_agent_for_runtime() -> 事件流执行
```

---

## 二、核心组件与数据边界

### 1. LangGraph Checkpoint

Checkpoint 保存 Agent 真实的 thread 上下文，主要包括：

- 用户和 assistant 消息。
- Agent State。
- 工具调用及对应消息。

同一 `thread_id` 再次调用 Agent 时，LangGraph 会使用 checkpoint 恢复上下文。Dashboard 展示的完整用户/assistant 历史也以 checkpoint 为准，但会过滤 SystemMessage 和 ToolMessage。

### 2. 业务 Store

业务 Store 保存 Dashboard 和产品流程所需的结构化数据：

- thread 摘要和最新状态。
- run 执行记录。
- run_events 粗粒度运行步骤。
- reviewer findings 代码审查问题。
- 仓库与本地工作目录的映射。
- 分支、PR URL 等业务字段。

数据库中仍有旧版消息/方案表的兼容结构，但当前 runtime 不使用它们恢复正常聊天历史。

### 3. LangGraph Store

LangGraph Store 为 DeepAgents 提供仓库级长期记忆：

- 初始化时记录仓库标识、仓库 URL 和虚拟项目目录。
- planning 任务成功后追加最终技术方案。
- 通用 Agent 任务成功且存在最终 assistant 回答时，追加最终结论；可选附加分支名和 PR URL。

因此，长期记忆可能包含项目结构、历史修改经验或技术决策，但当前代码的实际存储形式是“仓库基础信息 + 各任务最终文本”，并非固定的结构化三分类。

### 4. Workspace

`Workspace` 负责工作区目录和路径边界：

- 管理工作区根目录。
- 解析虚拟路径与真实本地路径。
- 将操作限制在允许的工作区内。

`Workspace` 不直接执行 `git clone` 或 `git pull`。

### 5. LocalShellBackend

`LocalShellBackend` 在 Workspace 路径边界内执行 Shell 和文件操作。Git 同步分支中实际执行：

- `git remote set-url origin ...`
- `git fetch --all`
- `git pull --ff-only`
- `git clone ...`

---

## 三、任务路由

`run_agent_task()` 是普通任务总入口，按以下顺序选择分支：

```text
用户输入
  ↓
工作区查询？ ─是→ 直接列出项目，不调用模型
  │否
  ↓
仅 Git 同步？ ─是→ 直接 fetch/pull 或 clone，不调用模型
  │否
  ↓
本地关键词分类
  ├─ coding 且未确认方案 → 生成技术方案
  ├─ 确认了当前方案     → coding
  └─ planning / analysis / qa  → 只读 Agent 任务
```

当前 `TaskKind` 为：

- `coding`
- `analysis`
- `planning`
- `qa`
- `sync`
- `inspect`
- `review`

`review` 是独立的只读任务类型，可以调用 Reviewer 子 Agent、diff 校验和 finding 工具，
但不能修改源码、提交、推送或创建 Pull Request。

任务分类先由 `task_intent.py` 的确定性规则识别明显意图；需要进一步判断时再进入
模型分类流程。无论分类来源是什么，最终权限仍由 `server.py` 和 Backend 强制执行。

---

## 四、Planning → Confirm → Coding

### 1. 首轮 Coding 请求

当关键词分类结果为 `coding`，且本轮没有找到已确认方案时，runtime 不直接允许写代码，而是调用 `run_plan_response_task()`：

```text
开发需求
  ↓
Planning Agent（只读）
  ↓
读取仓库并生成完整技术方案
  ↓
输出“是否确认实施该方案？”
  ↓
等待用户后续输入
```

Planning 阶段是只读任务，禁止修改文件、commit、push 和创建 PR。

注意：用户明确请求“只制定方案”且未包含 coding 关键词时，分类结果也可能是 `planning`，但这类请求走通用只读 Agent 分支，不一定进入“等待确认”状态。

### 2. 确认与方案修订

当已有 thread 且用户输入命中确认规则，例如“确认”、“同意实施”或“开始实施”，runtime 会从 checkpoint 的可见 assistant 历史中向前查找最近一条可确认方案。

- 找到方案：保留方案正文，将本轮切换为 `coding`。
- 没有找到方案：不会误进入 coding，而是将确认文本重新按普通输入分类。
- 用户明确说“修改方案”、“补充方案”等：读取最近方案并重新生成完整新版方案，不进入 coding。
- 存在待确认方案，但用户提出普通问答或分析请求：按新请求正常分类，不会被旧方案强制转为方案修订。

### 3. 方案和需求的恢复

方案确认时的数据来源是：

1. 从 checkpoint 读取最近一条包含方案特征关键词的 assistant 正文。
2. 尝试从方案 metadata 获取 `source_prompt`。
3. metadata 中缺少 `source_prompt` 时，从 checkpoint 向前查找最近一条非确认类用户消息；如果仍无可用消息，回退到 thread 中的 `user_prompt`。

当前 checkpoint 中的用户消息是 runtime 传给 Agent 的完整任务文本，可能同时包含仓库 URL、任务类型和权限说明；因此这个回退值不一定等于前端最初提交的纯原文。

### 4. Coding 执行

找到待确认方案后，runtime 将：

```text
使用同一 thread_id 恢复 checkpoint 上下文
  ↓
将 task_kind 切换为 coding
  ↓
把恢复的任务目标和已确认方案一起传给 Coding Agent
  ↓
Agent 按方案实施，并根据任务执行修改、验证、提交、push 和创建或复用 PR
```

runtime 负责允许并要求这些行为，但不能保证每一步一定成功；实际结果取决于 Agent 执行、仓库状态、权限和外部服务。

---

## 五、具体执行分支

### 1. 工作区查询

```text
创建或更新 thread
  ↓
清理上一轮临时 run_events
  ↓
创建 run
  ↓
通过 Workspace + LocalShellBackend 列出 projects
  ↓
更新 thread/run 状态并返回项目列表
```

该分支不构建 Agent，不调用模型，但仍会写入 thread、run 和 run_events。

### 2. Git 同步

```text
创建或更新 thread/run
  ↓
解析 GitHub 仓库
  ↓
计算项目目录（repo_project_dir）
  ↓
初始化仓库长期记忆（不覆盖已有内容，使用 repo_project_dir 而非 mapping.project_dir）
  ↓
发现仓库映射（discover_repo_mapping）
  ↓
检查本地目标目录
  ├─ 已有 Git 仓库：remote set-url → fetch --all → pull --ff-only
  └─ 不存在：git clone
  ↓
保存/刷新仓库与工作目录映射
  ↓
更新 thread/run/run_events
```

`fetch` 或 `pull` 如遇 `.git/FETCH_HEAD` 的特定权限异常，runtime 会尝试删除该临时文件后重试一次。

### 3. 方案生成

`run_plan_response_task()` 始终以 `planning` 构建只读 Agent，使用专用方案提示词调用 Agent，并对最终方案做两项兜底：

- 没有可用方案文本时将任务标记为失败。
- 方案没有包含“是否确认实施该方案”时，在用于记忆更新的方案文本后追加该句。

方案正文由 Agent/checkpoint 管理，不再写入 `thread_plans`、`thread_messages` 或单独 Markdown 方案文件。

### 4. 通用 Agent 任务

runtime 负责：

- 构造 thread/run 业务记录。
- 构建带 `thread_id`、`task_kind` 和 `repo_url` 的 Agent config。
- 组装带任务边界的用户内容。
- 调用 `run_agent_with_event_stream()`。
- 根据结果更新状态和长期记忆。

`streaming_runtime.py` 当前是 Agent 调用与结果序列化适配层。尽管函数名是 `run_agent_with_event_stream()`，当前实现使用同步 `agent.invoke()`，并只记录粗粒度的模型开始/完成事件，尚未消费或解析模型文本 chunk 流。

---

## 六、任务结束流程

### 1. 方案和通用 Agent 任务成功

```text
Agent 返回
  ↓
关闭仍处于 in_progress 的 run_events
  ↓
thread = completed
  ↓
run = completed
  ↓
记录完成事件
  ↓
提取最终方案或 assistant 回答
  ↓
按条件更新仓库长期记忆
```

### 2. 方案和通用 Agent 任务失败

```text
抛出异常
  ↓
将未完成 run_events 关闭为 error
  ↓
thread = failed
  ↓
run = failed，保存脱敏后的错误
  ↓
记录失败事件并继续向上抛出异常
```

### 3. 轻量分支差异

工作区查询和 Git 同步分支会直接更新 thread/run 并记录失败事件。当前异常分支没有调用 `finish_open_run_events()`，因此异常发生前已记录的某个 `in_progress` 事件可能仍保留原状态。

---

## 七、Dashboard 查询

`get_task(thread_id)` 返回：

- thread 摘要。
- findings。
- latest_run。
- run_events。

`list_tasks(limit)` 返回最近 thread 列表，并为每条 thread 附加 latest_run 和 run_events。

这两个接口都不读取完整 checkpoint 正文。完整可见聊天历史由 `checkpoint_history.visible_checkpoint_messages()` 提供。

---

## 八、删除任务

`delete_task(thread_id)` 先删除业务 Store 中的相关数据，再删除 checkpoint：

### 业务 Store

- thread。
- runs。
- run_events。
- reviewer findings。
- 旧版 thread_messages 和 thread_plans 兼容数据。

### Checkpoint

- LangGraph thread state。
- 历史消息和工具上下文。

只有两边都清理成功，任务上下文才真正完全消失。当前实现在业务 Store 删除成功、checkpoint 删除失败时仍向调用方返回成功，同时记录异常日志；此时 checkpoint 可能有残留。

---

## 九、总结

`runtime.py` 是 AI Coding 系统的产品流程控制层，核心职责是：

1. 使用本地规则判断任务路径。
2. 控制 `Planning → Confirm → Coding` 的写权限门禁。
3. 通过同一 `thread_id` 复用 checkpoint 上下文。
4. 构建 Agent 运行配置并调用执行适配层。
5. 维护 thread、run 和 run_events 业务状态。
6. 将符合条件的最终结论写入仓库长期记忆。
7. 删除任务时协调清理业务数据和 Agent 上下文。

> 一句话：runtime 决定任务走哪条路径、何时允许写代码、如何维护可追踪状态，以及如何复用和清理 Agent 上下文。
