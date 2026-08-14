# checkpoint_history.py 说明：可见消息恢复

> 对应源码：`agent/core/checkpoint_history.py`

这个文件从 LangGraph checkpoint 中提取用户和助手可见文本，供方案确认、需求恢复和 Dashboard 历史展示使用。

## 函数何时调用

| 函数 | 谁调用 | 调用时机 | 返回内容 |
|---|---|---|---|
| `visible_checkpoint_messages()` | `runtime.py` 中的方案恢复函数 | 用户确认方案、修改方案或需要恢复原始需求时 | 当前 thread 累计的用户/助手文本 |
| `_visible_message()` | `visible_checkpoint_messages()` | 遍历 checkpoint 的每条消息时 | 统一后的 `message_id/author/content` |
| `_message_content()` | `_visible_message()` | 消息可能是纯文本或 content block 时 | 可展示的纯文本 |

```text
runtime._latest_confirmable_plan_from_checkpoint()
runtime._latest_non_approval_user_prompt()
  -> visible_checkpoint_messages()
     -> get_checkpointer().get_tuple()
```

## 为什么只读取最新 checkpoint

最新 checkpoint 的 `channel_values.messages` 已包含当前 thread 的累计消息。如果遍历所有历史 checkpoint，会重复返回早期消息。

## 处理步骤

1. 使用 `thread_id` 调用 `get_checkpointer().get_tuple()`。
2. 读取 `checkpoint["channel_values"]["messages"]`。
3. 兼容 LangChain `BaseMessage` 和普通字典消息。
4. 从纯文本或多模态 content block 中提取文本。
5. 只保留 user/human 和 assistant/ai，忽略工具消息。

## 返回结构

```text
message_id
author: user | agent
content
```

该结构刻意保持简单，避免 runtime 依赖 LangChain 消息对象的内部细节。
