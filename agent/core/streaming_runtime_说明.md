# streaming_runtime.py 说明：事件流处理

> 主要对应源码：`agent/core/streaming_runtime.py`
> Checkpoint 读取部分另见：`agent/core/checkpoint_history_说明.md`

## 关键函数何时调用

| 函数 | 谁调用 | 调用时机 | 作用 |
|---|---|---|---|
| `run_agent_with_event_stream()` | `core/runtime.py` | Agent 已构建、即将执行模型任务时 | 创建 v3 事件流，接收 `event_sink` 参数并传入事件消费者，返回最终消息 |
| `_consume_raw_event_stream()` | `run_agent_with_event_stream()` | 事件流创建后 | 单次遍历正文、todo 工具参数块和子智能体事件；`event_sink` 传给 `_record_todos()` 和 `_record_assistant_stream_message()` 用于实时推送 |
| `_text_delta_from_event()` | `_consume_raw_event_stream()` | 收到每个 `messages` 事件时 | 提取正文增量 |
| `_tool_chunk_from_message_event()` | `_consume_raw_event_stream()` | 当前事件不是正文增量时 | 提取逐步生成的工具参数（三阶段：start / delta / finish） |
| `_record_assistant_stream_message()` | `_consume_raw_event_stream()` | 正文首次出现、达到刷新阈值或流结束时 | 把某段 assistant 文本写入 `run_events`，并通过 `event_sink` 实时推 `message_start` + `text_delta` 给前端 |
| `_record_todos()` | `_consume_raw_event_stream()` | 检测到 `write_todos` 参数变化时 | 写入结构化任务清单，并通过 `event_sink` 实时推送 `todo_delta` 事件 |
| `_debug_raw_stream_events()` | `run_agent_with_event_stream()` | 调试开关为 `1` 时 | 额外请求短调试流，正常环境不执行 |

```text
runtime
  -> run_agent_with_event_stream(event_sink)
     -> agent.stream_events(version="v3")
     -> _consume_raw_event_stream(event_sink)
        -> record_event() -> LocalSqliteStore.run_events
        -> event_sink() -> SSE 实时推送（message_start / text_delta / todo_delta）
     -> 提取 stream.output
```

注意：`_consume_raw_event_stream()` 只是消费执行过程产生的事件。具体工具早已由 DeepAgents 工具节点调度，并通过中间件链中的 `handler(request)` 执行；事件消费者不会再次执行工具。

## 两条不同的数据链

Checkpoint 保存完整 Agent thread state，用于多轮对话恢复。业务事件表保存适合前端展示的短步骤，用于页面实时反馈。它们不是重复数据：

```text
模型消息与工具历史 -> LangGraph Checkpoint
步骤、进度和正文快照 -> run_events 业务表
```

## Checkpoint 读取

`checkpoint_history.visible_checkpoint_messages()` 读取最新 checkpoint 的 `channel_values.messages`，只保留用户和助手文本。

最新 checkpoint 已包含线程累计消息，所以不遍历全部历史 checkpoint，否则相同消息会被重复返回。

runtime 使用这些可见消息完成：

- 找回最近一份可确认方案。
- 用户输入“确认实施”时恢复真实原始需求。
- 多次修改方案时沿用上一版 source prompt。

## 事件流入口

`run_agent_with_event_stream()` 调用 DeepAgents `stream_events(version="v3")`，并把 raw protocol 转换为业务事件。

它接受一个可选的 `event_sink: StreamEventSink` 参数——一个 `(event_type, data) -> None` 回调——由 FastAPI SSE 层传入。`event_sink` 让解析出的业务事件绕过数据库直接推送到前端 SSE 通道，实现实时更新。

主要处理四类信息：

1. `messages` 文本 delta：累计成 Markdown 正文。通过 `event_sink` 推送 `message_start` + `text_delta`（`mode: "replace"` 表示推送的是累计全文，前端应整体替换，避免重复拼接）。
2. `write_todos` 工具参数块（三阶段流式解析）：`content-block-start` 获取工具名、`content-block-delta` 获取参数增量、`content-block-finish` 获取完整工具调用。通过 `event_sink` 推送 `todo_delta` 实时更新任务清单。
3. 工具生命周期：展示正在读取、修改或执行什么。
4. 子智能体生命周期：展示委派任务摘要。

## 为什么正文不是每个 Token 都写数据库

raw event 可能按 token 返回。如果每个 token 都写一次 SQLite，会造成大量事务和页面抖动。当前策略是：

- 第一段正文立即写入。
- 新增约 24 个字符后刷新。
- 遇到换行立即刷新，让 Markdown 标题和列表及时出现。
- 流结束时补写最后一段。

## `write_todos` 为什么复杂

不同 DeepAgents 版本可能把工具参数作为字典、完整工具调用或逐步增长的 JSON 字符串返回。代码按 `call_id` 保存已展示内容，只有内容变化时才更新事件，避免同一个 todo 列表重复刷屏。

## 运行限制

`AgentRunLimitTracker` 在消费每个事件前执行检查：

- 限制总运行时长。
- 统计工具调用次数。
- 超限时记录错误事件并中止当前运行。

它属于“观察和中止”机制，不属于工具执行机制。当前检查发生在事件到达消费者时，因此语义是阻止后续事件和后续调用继续推进，而不是由 Tracker 亲自启动或包裹具体工具。

限制放在事件消费层，是因为这里能观察整轮模型和工具生命周期，而不是单个中间件调用。

## 调试开关

`LQ_AICODING_DEBUG_STREAM_EVENTS=1` 会额外启动一条短调试流并记录事件结构。它只用于适配 DeepAgents 版本变化，正常环境应保持关闭，因为它会产生额外模型请求。

## 修改注意事项

- 调整 raw event 解析时要保留对 dict、tuple 和对象字段的兼容。
- 事件详情必须脱敏和限长，不能把完整源码或 Token 写入前端事件表。
- event key 应包含 `run_id`，避免同一 thread 多轮运行互相覆盖。
- Checkpoint 和业务 Store 的删除需要同步处理，避免页面记录已删但对话状态仍残留。
