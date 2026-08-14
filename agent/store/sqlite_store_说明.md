# 数据库表与业务方法

> 对应源码：`agent/store/sqlite_store.py`

## 一、文件整体职责

`LocalSqliteStore` 是项目中的本地 SQLite 业务数据存储层。

它负责保存平台运行过程中的业务数据，包括：

- 任务与会话
- Agent 每次运行记录
- Agent 实时执行步骤
- Dashboard 会话消息
- 编码前技术方案
- 代码审查结果
- 系统配置
- GitHub 仓库与本地目录映射

该类保存的是业务摘要数据，不负责保存完整的 LangGraph 状态。完整的 messages 和 LangGraph thread state 由 checkpoint 数据库负责。

## 常用方法何时调用

| 方法组 | 主要调用方 | 调用时机 |
|---|---|---|
| `upsert_thread()`、`update_thread_status()` | `core/runtime.py`、GitHub 工具 | 任务创建、完成、失败或 PR 创建时 |
| `record_run()`、`get_latest_run()` | `core/runtime.py`、查询 API | 每轮运行开始/结束以及 Dashboard 查询时 |
| `add_run_event()`、`finish_open_run_events()` | `core/events.py`、runtime | 工具、模型或任务步骤状态变化时 |
| `add_finding()`、`list_findings()` | `tools/reviewer_tools.py` | review Agent 记录和读取问题时 |
| `upsert_repo_mapping()`、`get_repo_mapping()` | `core/repo_mapping.py` | 仓库目录发现、验证和 clone 完成时 |
| `delete_thread()` | `runtime.delete_task()` | 用户删除任务时 |

```text
runtime/tools -> LocalSqliteStore -> store.sqlite
Dashboard API <- 查询 threads/runs/run_events/findings
```

下面各章节继续按“表结构 → 字段 → 方法 → 调用流程”展开。

---

# 二、`threads` 表：任务／会话主表

## 1. 表的作用

`threads` 是整个业务的主表。

一条数据代表一个 Dashboard 会话，或者一个用户提交的编码任务。

其他任务相关表主要通过：

```text
thread_id
```

与该表关联。

## 2. 字段说明

| 字段 | 类型 | 中文说明 |
|---|---|---|
| `thread_id` | `TEXT PRIMARY KEY` | 会话或任务唯一 ID |
| `title` | `TEXT NOT NULL` | 会话或任务标题 |
| `user_prompt` | `TEXT` | 用户最初提交的需求或提示词 |
| `repo_url` | `TEXT` | 当前任务对应的 GitHub 仓库地址 |
| `repo_owner` | `TEXT` | GitHub 仓库所有者 |
| `repo_name` | `TEXT` | GitHub 仓库名称 |
| `branch_name` | `TEXT` | Agent 创建或使用的 Git 分支名称 |
| `pr_url` | `TEXT` | 最终创建的 Pull Request 地址 |
| `latest_run_status` | `TEXT NOT NULL` | 当前任务最近一次运行状态 |
| `created_at` | `TEXT NOT NULL` | 任务创建时间 |
| `updated_at` | `TEXT NOT NULL` | 任务最近更新时间 |

## 3. 对应方法

### `upsert_thread()`

```python
def upsert_thread(...) -> None
```

用于新增任务，或者更新已有任务。

“upsert”表示：

```text
不存在时插入
存在时更新
```

主要处理字段：

- `thread_id`
- `title`
- `user_prompt`
- `repo_url`
- `repo_owner`
- `repo_name`
- `branch_name`
- `pr_url`
- `latest_run_status`

新增任务时：

- 写入任务基础信息
- 写入创建时间
- 写入更新时间

更新任务时：

- 保留原有 `created_at`
- 更新最新运行状态
- 更新仓库、分支和 PR 信息
- 更新 `updated_at`

代码使用 `COALESCE`，如果新传入的数据为 `None`，则保留数据库中的原值，不会把已有值覆盖为空。

---

### `get_thread()`

```python
def get_thread(
    self,
    thread_id: str,
) -> dict[str, Any] | None
```

根据 `thread_id` 查询单个任务。

找到时返回任务字典，例如：

```python
{
    "thread_id": "thread-001",
    "title": "增加登录功能",
    "latest_run_status": "running",
}
```

没有找到时返回：

```python
None
```

---

### `list_threads()`

```python
def list_threads(
    self,
    limit: int = 50,
) -> list[dict[str, Any]]
```

查询任务列表。

查询结果按照：

```sql
updated_at DESC
```

排序，也就是最近更新的任务排在最前面。

默认最多返回 50 条数据。

主要用于：

- Dashboard 左侧任务列表
- 最近任务列表
- 任务管理页面

---

### `update_thread_status()`

```python
def update_thread_status(
    self,
    thread_id: str,
    status: str,
    *,
    pr_url: str | None = None,
    branch_name: str | None = None,
) -> None
```

更新任务当前状态。

主要更新字段：

| 字段 | 说明 |
|---|---|
| `latest_run_status` | 更新任务当前状态 |
| `pr_url` | 可选更新 Pull Request 地址 |
| `branch_name` | 可选更新分支名称 |
| `updated_at` | 更新最后修改时间 |

可能使用的状态包括：

```text
pending
running
waiting_approval
completed
failed
```

具体状态值由上层业务决定，本文件没有强制定义枚举。

---

### `delete_thread()`

```python
def delete_thread(
    self,
    thread_id: str,
) -> bool
```

删除一个 Dashboard 任务及其相关业务数据。

删除前先查询任务是否存在。

不存在时返回：

```python
False
```

存在时，按照以下顺序删除：

1. `review_findings`
2. `thread_plans`
3. `thread_messages`
4. `run_events`
5. `runs`
6. `threads`

删除成功后返回：

```python
True
```

该方法不会删除：

- LangGraph checkpoint
- GitHub Pull Request
- Git 分支
- 本地代码目录
- 技术方案 Markdown 文件
- 仓库目录映射
- 系统配置

## 4. 该表的业务流程

```text
用户创建任务
    ↓
upsert_thread()
    ↓
任务写入 threads
    ↓
Agent 开始运行
    ↓
update_thread_status(status="running")
    ↓
Agent 创建分支或 PR
    ↓
update_thread_status(
    branch_name=...,
    pr_url=...
)
    ↓
任务完成或失败
    ↓
update_thread_status(status="completed" / "failed")
```

---

# 三、`runs` 表：Agent 运行记录表

## 1. 表的作用

`runs` 用来记录一个任务的每一次 Agent 执行。

一个任务可以执行多次，因此：

```text
一个 thread 可以对应多个 run
```

例如：

```text
第一次提交需求 → run-001
修改需求重新执行 → run-002
失败后重新执行 → run-003
```

## 2. 字段说明

| 字段 | 类型 | 中文说明 |
|---|---|---|
| `run_id` | `TEXT PRIMARY KEY` | 一次运行的唯一 ID |
| `thread_id` | `TEXT NOT NULL` | 本次运行所属的任务 ID |
| `status` | `TEXT NOT NULL` | 当前运行状态 |
| `started_at` | `TEXT NOT NULL` | 本次运行开始时间 |
| `finished_at` | `TEXT` | 本次运行结束时间，未结束时为空 |
| `error` | `TEXT` | 运行失败时记录错误原因 |

## 3. 对应方法

### `record_run()`

```python
def record_run(
    self,
    *,
    run_id: str,
    thread_id: str,
    status: str,
    error: str | None = None,
    finished: bool = False,
) -> None
```

新增或更新一次运行记录。

第一次传入新的 `run_id` 时：

- 创建运行记录
- 写入 `thread_id`
- 写入运行状态
- 记录 `started_at`

相同 `run_id` 再次调用时，更新：

- `status`
- `finished_at`
- `error`

当：

```python
finished=False
```

时：

```text
finished_at = NULL
```

当：

```python
finished=True
```

时：

```text
finished_at = 当前 UTC 时间
```

如果运行失败，可以通过 `error` 保存：

- 模型调用错误
- Git 命令错误
- GitHub 接口错误
- 文件权限错误
- 测试失败原因
- 本地环境错误

---

### `get_latest_run()`

```python
def get_latest_run(
    self,
    thread_id: str,
) -> dict[str, Any] | None
```

查询某个任务最近一次运行记录。

按照：

```sql
started_at DESC
```

排序，只取第一条。

主要用于 Dashboard 展示：

- 最近一次运行状态
- 最近一次开始时间
- 最近一次结束时间
- 最近一次失败原因

## 4. 该表的业务流程

```text
Agent 准备开始执行
    ↓
record_run(
    status="running",
    finished=False
)
    ↓
Agent 执行任务
    ↓
执行成功
    ↓
record_run(
    status="completed",
    finished=True
)
```

失败时：

```text
Agent 执行失败
    ↓
record_run(
    status="failed",
    error="具体错误原因",
    finished=True
)
```

---

# 四、`run_events` 表：运行过程事件表

## 1. 表的作用

`run_events` 保存 Agent 执行过程中的简洁步骤。

它主要用于 Dashboard 实时展示：

```text
Agent 当前正在做什么
```

例如：

```text
正在分析项目结构
正在读取仓库文件
正在生成技术方案
正在修改代码
正在执行测试
正在提交代码
正在创建 Pull Request
```

它不是完整日志表，不适合保存大量命令输出。

## 2. 字段说明

| 字段 | 类型 | 中文说明 |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | 运行事件唯一 ID |
| `thread_id` | `TEXT NOT NULL` | 事件所属任务 ID |
| `kind` | `TEXT NOT NULL` | 事件类型 |
| `title` | `TEXT NOT NULL` | 前端展示的事件标题 |
| `status` | `TEXT NOT NULL` | 事件当前状态 |
| `detail` | `TEXT` | 事件简短说明 |
| `created_at` | `TEXT NOT NULL` | 事件创建时间 |
| `updated_at` | `TEXT NOT NULL` | 事件最近更新时间 |

## 3. 对应方法

### `add_run_event()`

```python
def add_run_event(
    self,
    *,
    event_id: str,
    thread_id: str,
    kind: str,
    title: str,
    status: str,
    detail: str | None = None,
) -> None
```

新增或更新一条运行事件。

第一次使用某个 `event_id` 时，创建事件。

例如：

```python
add_run_event(
    event_id="analyze-repo",
    thread_id="thread-001",
    kind="analysis",
    title="正在分析项目结构",
    status="in_progress",
)
```

执行完成后使用相同 `event_id` 再次调用：

```python
add_run_event(
    event_id="analyze-repo",
    thread_id="thread-001",
    kind="analysis",
    title="项目结构分析完成",
    status="completed",
)
```

相同事件 ID 会更新：

- `kind`
- `title`
- `status`
- `detail`
- `updated_at`

不会重复创建两条事件。

---

### `list_run_events()`

```python
def list_run_events(
    self,
    thread_id: str,
) -> list[dict[str, Any]]
```

查询一个任务的全部运行事件。

按照：

```sql
created_at ASC
```

排序。

也就是：

```text
最早执行的步骤在前
最后执行的步骤在后
```

主要用于：

- Dashboard 运行时间线
- SSE 实时步骤展示
- 任务过程详情

---

### `clear_run_events()`

```python
def clear_run_events(
    self,
    thread_id: str,
) -> None
```

清空某个任务的临时运行事件。

主要使用场景：

```text
上一轮执行结束
    ↓
用户重新运行任务
    ↓
clear_run_events()
    ↓
清除上一轮过程步骤
    ↓
展示本轮新的执行步骤
```

该方法只删除 `run_events`，不会删除：

- 任务
- 运行记录
- 会话消息
- 技术方案
- 代码审查结果

---

### `finish_open_run_events()`

```python
def finish_open_run_events(
    self,
    thread_id: str,
    *,
    status: str = "completed",
) -> None
```

任务结束时，把仍未关闭的事件统一收尾。

会处理以下状态：

```text
pending
in_progress
```

默认更新为：

```text
completed
```

也可以由上层传入：

```text
failed
cancelled
```

主要用于避免：

```text
任务已经结束
但 Dashboard 仍显示某一步“运行中”
```

## 4. 该表的业务流程

```text
Agent 开始某一步
    ↓
add_run_event(status="in_progress")
    ↓
Dashboard 显示“正在执行”
    ↓
步骤完成
    ↓
add_run_event(status="completed")
    ↓
Dashboard 显示“已完成”
```

任务整体结束时：

```text
finish_open_run_events()
```

统一关闭遗漏的运行中事件。

---

# 五、`thread_messages` 表：Dashboard 会话消息表

## 1. 表的作用

`thread_messages` 保存用户和 Agent 在 Dashboard 中真正展示的消息正文。

它与 `run_events` 的区别是：

```text
thread_messages
保存用户和 Agent 的正式问答

run_events
保存 Agent 的执行过程步骤
```

## 2. 字段说明

| 字段 | 类型 | 中文说明 |
|---|---|---|
| `message_id` | `TEXT PRIMARY KEY` | 消息唯一 ID |
| `thread_id` | `TEXT NOT NULL` | 消息所属任务 ID |
| `run_id` | `TEXT` | 可选，关联某次 Agent 运行 |
| `author` | `TEXT NOT NULL` | 消息发送方，例如 user 或 assistant |
| `content` | `TEXT NOT NULL` | 消息正文 |
| `metadata` | `TEXT` | JSON 格式的扩展信息 |
| `created_at` | `TEXT NOT NULL` | 消息创建时间 |

## 3. 对应方法

### `add_thread_message()`

```python
def add_thread_message(
    self,
    *,
    message_id: str,
    thread_id: str,
    author: str,
    content: str,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None
```

新增或更新一条会话消息。

写入前会执行：

```python
content.strip()
```

删除消息前后的空白字符。

如果消息发送方是：

```text
user
```

系统会查询该任务最近一条用户消息。

如果最新一条用户消息内容与本次内容完全相同，则直接返回，避免重复保存同一条用户输入。

相同 `message_id` 再次写入时，更新：

- `content`
- `metadata`

`metadata` 会通过：

```python
json.dumps()
```

转换成 JSON 字符串保存。

---

### `list_thread_messages()`

```python
def list_thread_messages(
    self,
    thread_id: str,
) -> list[dict[str, Any]]
```

查询某个任务的所有会话消息。

按照：

```sql
created_at ASC
```

排序。

保证消息按照实际对话顺序显示：

```text
用户第一条消息
Agent 第一条回复
用户第二条消息
Agent 第二条回复
```

## 4. 该表的业务流程

```text
用户发送需求
    ↓
add_thread_message(author="user")
    ↓
Agent 执行并生成回复
    ↓
add_thread_message(author="assistant")
    ↓
Dashboard 调用 list_thread_messages()
    ↓
按时间顺序展示完整会话
```

---

# 六、`thread_plans` 表：编码技术方案表

## 1. 表的作用

`thread_plans` 保存 Agent 在正式修改代码前生成的技术方案。

支持的核心流程是：

```text
先生成方案
用户确认方案
再开始编码
```

一个任务可以生成多份技术方案，因此可以保留方案历史。

## 2. 字段说明

| 字段 | 类型 | 中文说明 |
|---|---|---|
| `plan_id` | `TEXT PRIMARY KEY` | 技术方案唯一 ID |
| `thread_id` | `TEXT NOT NULL` | 方案所属任务 ID |
| `run_id` | `TEXT` | 可选，关联生成该方案的运行 |
| `status` | `TEXT NOT NULL` | 方案状态，例如 pending、approved |
| `prompt` | `TEXT NOT NULL` | 生成方案时使用的用户需求 |
| `plan_text` | `TEXT NOT NULL` | 技术方案正文 |
| `plan_path` | `TEXT NOT NULL` | 技术方案 Markdown 文件路径 |
| `created_at` | `TEXT NOT NULL` | 方案创建时间 |
| `approved_at` | `TEXT` | 用户确认方案的时间 |

## 3. 对应方法

### `add_thread_plan()`

```python
def add_thread_plan(
    self,
    *,
    plan_id: str,
    thread_id: str,
    prompt: str,
    plan_text: str,
    plan_path: str,
    run_id: str | None = None,
    status: str = "pending",
) -> None
```

新增或更新一份技术方案。

默认状态：

```text
pending
```

表示方案尚未确认。

保存内容包括：

- 用户需求
- 技术方案正文
- Markdown 文件路径
- 所属任务
- 所属运行
- 当前方案状态
- 创建时间

写入前会对：

```text
prompt
plan_text
```

执行 `strip()`，去掉首尾空白。

相同 `plan_id` 再次写入时更新：

- `status`
- `prompt`
- `plan_text`
- `plan_path`

---

### `get_latest_thread_plan()`

```python
def get_latest_thread_plan(
    self,
    thread_id: str,
    *,
    status: str | None = None,
) -> dict[str, Any] | None
```

查询某个任务最新的一份技术方案。

不传 `status` 时：

```text
查询最新方案，不限制状态
```

传入：

```python
status="approved"
```

时：

```text
只查询最新一份已确认方案
```

按照：

```sql
created_at DESC
```

排序，只返回第一条。

---

### `list_thread_plans()`

```python
def list_thread_plans(
    self,
    thread_id: str,
) -> list[dict[str, Any]]
```

查询某个任务的全部技术方案。

按照：

```sql
created_at ASC
```

排序。

可以用于展示：

- 第一版方案
- 第二版调整方案
- 后续修改方案
- 最终确认方案

---

### `approve_thread_plan()`

```python
def approve_thread_plan(
    self,
    plan_id: str,
) -> dict[str, Any] | None
```

把指定技术方案标记为已确认。

更新字段：

| 字段 | 更新内容 |
|---|---|
| `status` | 更新为 `approved` |
| `approved_at` | 写入当前 UTC 时间 |

更新后重新查询并返回该方案。

业务意义：

```text
用户确认了该方案
Agent 可以按照该方案继续编码
```

## 4. 该表的业务流程

```text
用户提交编码需求
    ↓
Agent 分析项目
    ↓
生成技术方案
    ↓
add_thread_plan(status="pending")
    ↓
Dashboard 展示方案
    ↓
用户确认
    ↓
approve_thread_plan()
    ↓
status = approved
    ↓
Agent 开始修改代码
```

---

# 七、`review_findings` 表：代码审查问题表

## 1. 表的作用

`review_findings` 保存 Reviewer Agent 发现的代码问题。

一条记录表示一个独立的审查问题，例如：

```text
某个文件某一行存在安全风险
某段代码可能发生异常
某个实现不符合规范
某个逻辑存在性能问题
```

## 2. 字段说明

| 字段 | 类型 | 中文说明 |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | 审查问题唯一 ID |
| `thread_id` | `TEXT NOT NULL` | 问题所属任务 ID |
| `file` | `TEXT NOT NULL` | 问题所在文件路径 |
| `line` | `INTEGER` | 问题所在行号，可以为空 |
| `severity` | `TEXT NOT NULL` | 问题严重程度 |
| `title` | `TEXT NOT NULL` | 问题标题 |
| `description` | `TEXT NOT NULL` | 问题详细描述 |
| `status` | `TEXT NOT NULL` | 问题当前状态 |
| `created_at` | `TEXT NOT NULL` | 问题创建时间 |
| `updated_at` | `TEXT NOT NULL` | 问题最近更新时间 |

## 3. 对应方法

### `add_finding()`

```python
def add_finding(
    self,
    *,
    finding_id: str,
    thread_id: str,
    file: str,
    line: int | None,
    severity: str,
    title: str,
    description: str,
    status: str = "open",
) -> None
```

新增或更新一条代码审查问题。

参数说明：

| 参数 | 中文说明 |
|---|---|
| `finding_id` | 问题唯一 ID |
| `thread_id` | 所属任务 ID |
| `file` | 问题所在文件 |
| `line` | 问题所在行号 |
| `severity` | 严重程度 |
| `title` | 问题标题 |
| `description` | 问题描述 |
| `status` | 问题状态，默认 `open` |

相同 `finding_id` 再次写入时更新：

- 文件路径
- 行号
- 严重程度
- 标题
- 描述
- 状态
- 更新时间

可用于保存：

- Bug
- 安全漏洞
- 性能问题
- 可维护性问题
- 代码规范问题
- 潜在异常

---

### `list_findings()`

```python
def list_findings(
    self,
    thread_id: str,
) -> list[dict[str, Any]]
```

查询某个任务的全部代码审查问题。

按照：

```sql
created_at ASC
```

排序。

主要用于：

- Dashboard 审查结果页面
- 按任务展示所有问题
- 展示问题严重程度
- 展示文件和行号
- 展示问题当前状态

## 4. 该表的业务流程

```text
Agent 完成代码修改
    ↓
Reviewer Agent 开始审查
    ↓
发现代码问题
    ↓
add_finding()
    ↓
问题写入 review_findings
    ↓
Dashboard 调用 list_findings()
    ↓
展示审查问题列表
```

---

# 八、`settings` 表：系统配置表

## 1. 表的作用

`settings` 用于保存少量系统级键值配置。

它不是任务专属表，不通过 `thread_id` 关联任务。

适合保存：

```text
默认分支
是否开启代码审查
重试次数
模型配置
功能开关
项目级参数
```

## 2. 字段说明

| 字段 | 类型 | 中文说明 |
|---|---|---|
| `key` | `TEXT PRIMARY KEY` | 配置项名称 |
| `value` | `TEXT NOT NULL` | JSON 字符串形式的配置值 |
| `updated_at` | `TEXT NOT NULL` | 配置最近更新时间 |

## 3. 对应方法

### `set_setting()`

```python
def set_setting(
    self,
    key: str,
    value: Any,
) -> None
```

新增或更新一个配置项。

配置值先通过：

```python
json.dumps(
    value,
    ensure_ascii=False,
)
```

转换为 JSON 字符串。

因此可以保存：

- 字符串
- 整数
- 浮点数
- 布尔值
- 列表
- 字典
- `None`

如果 `key` 已存在，则更新：

- `value`
- `updated_at`

---

### `get_setting()`

```python
def get_setting(
    self,
    key: str,
    default: Any = None,
) -> Any
```

根据配置名读取配置值。

找到数据时，通过：

```python
json.loads()
```

恢复成原来的 Python 类型。

例如数据库中保存：

```json
{"enabled": true}
```

读取后得到：

```python
{"enabled": True}
```

找不到配置时，返回：

```python
default
```

默认值为：

```python
None
```

## 4. 该表的业务流程

```text
系统保存配置
    ↓
set_setting()
    ↓
配置转成 JSON 写入 settings
    ↓
其他模块读取配置
    ↓
get_setting()
    ↓
JSON 恢复成 Python 数据
```

---

# 九、`repo_workspace_mappings` 表：仓库与本地目录映射表

## 1. 表的作用

`repo_workspace_mappings` 保存 GitHub 仓库和本地项目目录之间的对应关系。

例如：

```text
GitHub 仓库：
https://github.com/example/demo

本地目录：
projects/demo

本地绝对路径：
/data/projects/demo
```

Agent 在修改代码之前，需要确认仓库对应的本地目录，避免操作错误的项目。

## 2. 字段说明

| 字段 | 类型 | 中文说明 |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | 映射记录唯一 ID |
| `repo_url` | `TEXT NOT NULL` | GitHub 仓库地址 |
| `repo_owner` | `TEXT NOT NULL` | GitHub 仓库所有者 |
| `repo_name` | `TEXT NOT NULL` | GitHub 仓库名称 |
| `project_dir` | `TEXT NOT NULL` | 本地项目目录名称 |
| `local_path` | `TEXT` | 本地项目绝对路径 |
| `is_active` | `INTEGER NOT NULL` | 当前映射是否启用，1 表示启用，0 表示停用 |
| `source` | `TEXT NOT NULL` | 映射信息来源 |
| `notes` | `TEXT` | 映射备注 |
| `created_at` | `TEXT NOT NULL` | 映射创建时间 |
| `updated_at` | `TEXT NOT NULL` | 映射最近更新时间 |
| `last_verified_at` | `TEXT` | 最近一次验证映射有效性的时间 |

数据库还创建了唯一索引：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_repo_workspace_active
ON repo_workspace_mappings(repo_url)
WHERE is_active = 1
```

它保证：

```text
同一个 repo_url
同一时间最多只能有一条启用中的映射
```

## 3. 对应方法

### `upsert_repo_mapping()`

```python
def upsert_repo_mapping(
    self,
    *,
    mapping_id: str,
    repo_url: str,
    repo_owner: str,
    repo_name: str,
    project_dir: str,
    local_path: str | None,
    source: str,
    notes: str | None = None,
    is_active: bool = True,
    verified: bool = False,
) -> dict[str, Any]
```

新增或更新仓库与本地目录的映射。

参数说明：

| 参数 | 中文说明 |
|---|---|
| `mapping_id` | 映射唯一 ID |
| `repo_url` | 仓库 URL |
| `repo_owner` | 仓库所有者 |
| `repo_name` | 仓库名称 |
| `project_dir` | 本地项目目录名 |
| `local_path` | 本地绝对路径 |
| `source` | 映射来源 |
| `notes` | 备注 |
| `is_active` | 当前映射是否启用 |
| `verified` | 当前映射是否已验证 |

执行流程：

第一步，查找同一 `repo_url` 下其他启用映射，并设置：

```text
is_active = 0
```

第二步，新增或更新当前映射。

第三步，如果：

```python
verified=True
```

则记录：

```text
last_verified_at = 当前时间
```

第四步，重新读取保存后的映射。

如果保存后无法读取，抛出：

```text
仓库映射保存后读取失败
```

---

### `get_repo_mapping()`

```python
def get_repo_mapping(
    self,
    repo_url: str,
) -> dict[str, Any] | None
```

根据仓库 URL 查询当前启用的映射。

查询条件：

```text
repo_url = 指定仓库地址
is_active = 1
```

按照：

```sql
updated_at DESC
```

排序，只返回最新的一条。

找到时返回映射字典，找不到时返回：

```python
None
```

---

### `list_repo_mappings()`

```python
def list_repo_mappings(
    self,
    *,
    include_inactive: bool = False,
) -> list[dict[str, Any]]
```

查询仓库目录映射列表。

默认：

```python
include_inactive=False
```

只返回：

```text
is_active = 1
```

的当前有效映射。

传入：

```python
include_inactive=True
```

时，返回：

- 当前有效映射
- 历史停用映射

按照：

```sql
updated_at DESC
```

排序。

---

### `mark_repo_mapping_verified()`

```python
def mark_repo_mapping_verified(
    self,
    mapping_id: str,
    *,
    notes: str | None = None,
) -> None
```

标记某条映射已经重新验证。

更新字段：

| 字段 | 说明 |
|---|---|
| `last_verified_at` | 写入当前验证时间 |
| `updated_at` | 更新记录修改时间 |
| `notes` | 如果传入新备注则更新，否则保留原备注 |

典型验证内容包括：

- 本地目录是否存在
- 本地目录是否可访问
- Git 仓库是否正常
- Git remote 是否与 `repo_url` 一致
- 当前项目是否为正确仓库

## 4. 该表的业务流程

```text
任务包含 GitHub 仓库地址
    ↓
get_repo_mapping(repo_url)
    ↓
找到有效映射
    ↓
Agent 使用对应本地目录
```

没有映射时：

```text
系统识别或用户指定本地目录
    ↓
upsert_repo_mapping()
    ↓
停用旧映射
    ↓
保存新映射
    ↓
Agent 使用新目录
```

验证目录后：

```text
mark_repo_mapping_verified()
```

记录最近验证时间。

---

# 十、表之间的整体关系

```text
threads
│
├── runs
│   一个任务可以运行多次
│
├── run_events
│   一个任务可以有多个实时执行步骤
│
├── thread_messages
│   一个任务可以有多条用户和 Agent 消息
│
├── thread_plans
│   一个任务可以生成多份技术方案
│
└── review_findings
    一个任务可以发现多个代码审查问题


repo_workspace_mappings
│
└── 根据 repo_url 管理 GitHub 仓库和本地目录关系


settings
│
└── 保存系统级通用配置
```

虽然表结构中声明了外键，但代码执行了：

```sql
PRAGMA foreign_keys=OFF
```

所以数据库不会自动执行外键约束和级联删除。

关联关系和数据清理由代码负责。

---

# 十一、完整业务流程

```text
1. 用户创建编码任务
   ↓
   threads
   upsert_thread()

2. 保存用户输入
   ↓
   thread_messages
   add_thread_message()

3. 查找仓库对应的本地目录
   ↓
   repo_workspace_mappings
   get_repo_mapping()
   upsert_repo_mapping()

4. 创建一次 Agent 运行
   ↓
   runs
   record_run()

5. 更新任务状态为运行中
   ↓
   threads
   update_thread_status()

6. 保存 Agent 实时执行步骤
   ↓
   run_events
   add_run_event()

7. Agent 生成编码技术方案
   ↓
   thread_plans
   add_thread_plan()

8. 用户确认技术方案
   ↓
   thread_plans
   approve_thread_plan()

9. Agent 执行编码、测试、提交和创建 PR
   ↓
   threads
   update_thread_status(
       branch_name=...,
       pr_url=...
   )

10. Reviewer Agent 审查代码
    ↓
    review_findings
    add_finding()

11. 保存 Agent 最终回答
    ↓
    thread_messages
    add_thread_message()

12. 结束运行
    ↓
    runs
    record_run(finished=True)

13. 收尾未完成事件
    ↓
    run_events
    finish_open_run_events()

14. 更新任务最终状态
    ↓
    threads
    update_thread_status()
```

---

# 十二、总结

这个文件以 `threads` 表作为业务核心。

每张表分别负责：

| 表 | 主要职责 |
|---|---|
| `threads` | 保存任务主信息和当前状态 |
| `runs` | 保存任务每次运行的开始、结束和错误 |
| `run_events` | 保存 Agent 实时执行步骤 |
| `thread_messages` | 保存用户与 Agent 的正式消息 |
| `thread_plans` | 保存编码前技术方案和确认状态 |
| `review_findings` | 保存代码审查问题 |
| `settings` | 保存系统级配置 |
| `repo_workspace_mappings` | 保存仓库与本地项目目录映射 |

每张表对应的方法都围绕以下操作设计：

```text
新增或更新
单条查询
列表查询
状态更新
业务收尾
关联数据清理
```

整个类为 AI 编码 Agent 的 Dashboard 提供了完整的本地业务数据持久化能力。
