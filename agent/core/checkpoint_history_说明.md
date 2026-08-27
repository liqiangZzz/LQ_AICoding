# checkpoint_history.py 说明：可见消息恢复

> 对应源码：`agent/core/checkpoint_history.py`

这个模块从 LangGraph checkpoint 中提取"用户可见"的消息历史，是网页会话历史和方案确认链路的唯一正文数据源。它解决三个问题：

1. checkpoint 的 delta channel 里混有 tool、system、过程噪声消息，需要过滤；
2. 发给 Agent 的用户输入带有 runtime 包装（仓库地址、任务类型、内部规则），需要还原成用户在输入框里写的原始文本；
3. seed + writes 可能重复返回同一条消息，且前端需要跨多次 SSE 快照稳定的消息 id。

## 谁在调用

| 调用方 | 时机 | 用途 |
|---|---|---|
| `runtime.py:_latest_confirmable_plan_from_checkpoint()` | 用户确认方案时 | 倒序查找最近一条可确认的 agent 方案正文，作为 coding 任务的实施依据 |
| `runtime.py:_latest_non_approval_user_prompt()` | 方案确认组装 source_prompt 时 | 倒序还原最近一条非"确认/开始实施"的用户输入，避免把"确认实施"当成开发目标 |
| `api/dashboard_routes.py:_message_payload()` | 页面刷新后加载会话历史 | 把整理后的 user/agent 正文转成前端消息列表；实时过程走 SSE，稳定历史只认 checkpoint |

```text
runtime._latest_confirmable_plan_from_checkpoint()
runtime._latest_non_approval_user_prompt()
dashboard_routes._message_payload()
  -> visible_checkpoint_messages(thread_id)
       -> _delta_messages_from_checkpoint()
            -> get_checkpointer().get_delta_channel_history(channels=["messages"])
```

## 数据读取：为什么用 get_delta_channel_history

`SqliteSaver.get_tuple()` 的最新快照不一定直接包含 `channel_values["messages"]`。DeepAgents / LangGraph 新版本会把通道历史保存在 delta channel 里，因此这里使用：

```python
checkpointer.get_delta_channel_history(config, channels=["messages"])
```

返回结构分两部分：

- `seed`：基础快照，`value` 是一个消息列表；
- `writes`：后续增量列表，每项是元组，`write[2]` 是本次写入的消息（单个对象或列表）。

seed + writes 合并后可能重复返回同一条消息（不同版本行为不同），重复项由后面的内容去重兜底。读取失败时记录异常日志并返回空列表——历史恢复失败不阻塞方案确认或页面接口。

## 处理流水线

`visible_checkpoint_messages()` 按顺序执行以下步骤：

1. `_delta_messages_from_checkpoint()` 读取 seed + writes 并合并成原始消息列表。
2. `_message_role()` 识别角色：`HumanMessage → user`，`AIMessage → agent`；其余类型（tool、system 等）返回 None，直接丢弃。
3. `_content_to_text()` 把 message.content 统一转成纯文本：字符串直接使用；content block 列表抽取每个 block 的 `text`/`content` 字段后用换行拼接；空文本丢弃。
4. 按 role 分流：
   - **user**：`_extract_user_prompt()` 还原用户原始输入（见下节）；
   - **agent**：`_has_visible_markdown_value()` 判断是否有展示价值，短过程消息丢弃（见下节）。
5. 再次 strip，空文本丢弃。
6. 去重：`_compact_content()` 把所有空白序列压成单个空格后，以 `(author, 压缩正文)` 元组作为去重键；同一条正文即使 message id 不同、换行格式不同，也只保留第一次出现的版本。
7. 通过去重的消息生成稳定 id 后加入结果。

## 用户输入还原规则

runtime 包装后的 HumanMessage 大致形如：

```text
GitHub 仓库地址：...
用户任务：<用户原始需求>
这是只读任务...
```

`_extract_user_prompt()` 按三级优先级提取，命中即返回：

| 优先级 | 标记 | 场景 | 截断规则 |
|---|---|---|---|
| 1 | `用户可见输入：` | "确认实施"类 follow-up。runtime 会把真正执行目标改写成上一轮技术方案 + 内部规则，并额外写入用户可见输入 | 截到 `\n\n内部执行上下文：` / `\n\nGitHub 仓库地址：` / `\n\n任务类型：` / `\n\n用户任务：` |
| 2 | `用户新的修改要求：`（取最后一次出现） | 方案修订。这类消息同时包含"原始需求""上一版方案""修改要求"，必须先于第 3 级匹配，否则会误截出大段内部包装 | 截到 `\n\n请基于上一版方案` / `\n\n不要只输出差异说明` 等方案 Agent 内部规则 |
| 3 | `用户任务：` / `原始用户需求：` / `用户需求：` | 普通任务的首次包装 | 截到 `\n\n这是只读任务` / `\n\n这是开发实现任务` / `\n\n请只生成技术方案` / `\n\n用户已经确认以下技术方案` / `\n\n任务类型：` |

三个级别都没有命中时，原文返回（兼容未包装的消息）。这样设计的原因：

- 网页历史只应展示用户真正输入的内容，内部规则属于模型上下文；
- "确认实施"如果不走优先级 1，历史会丢失这条关键指令；
- 修订场景如果先匹配"用户需求"，会把整段修订包装误当作用户输入。

## agent 正文价值过滤

DeepAgents 会产生大量短过程消息（如"现在读取文件"），这些已经通过事件流（SSE / run_events）实时展示过，不应再堆积进历史正文。`_has_visible_markdown_value()` 的判定规则：

- 文本长度 ≥ 200 字符：保留；
- 否则包含任一价值标记才保留：`# `、`## `、`技术方案`、`代码审查报告`、`审查报告`、`完成总结`、`任务完成总结`、`内容如下`、`整体架构`；
- 都不满足：丢弃。

标记列表是启发式的，与当前项目方案、报告、总结的输出模板对应。

## 去重与稳定消息 ID

### 为什么不按 index 或消息对象 id

- 旧实现用列表 index 作为前端消息 id。checkpoint 每追加一条消息，既有消息的 index 全部漂移，前端会把旧消息当成新消息，或被当前过程块覆盖。
- seed 和 writes 中同一条正文的 message id 也可能不同。

### 当前实现

- `_compact_content()`：`re.sub(r"\s+", " ", text.strip())`，换行、多空格等格式差异不影响比较；
- 去重键：`(author, 压缩正文)` 元组；
- `stable_history_message_id()`：`sha1("{author}\n{压缩正文}")[:16]`，生成 `{thread_id}-history-{author}-{digest}`。同一正文在任何一次恢复中得到的 id 相同，前端可以安全地用它做增量合并和覆盖判断。

## 返回结构

```python
{
    "message_id": "{thread_id}-history-{author}-{sha1 前 16 位}",
    "author": "user" | "agent",
    "content": "整理后的可见正文",
    "source": "checkpoint",
}
```

结构刻意保持简单，runtime 和 dashboard 不依赖 LangChain 消息对象的内部细节。checkpoint 读取失败或没有可用消息时返回空列表。

注意：聊天正文不再回退读取业务 Store。Store 只保存任务摘要、运行状态、run_events、review findings 等业务数据，避免 checkpoint 与 Store 双数据源导致页面重复、覆盖或乱序。

## 设计收益与维护注意

收益：

- 单一正文数据源，历史、方案确认、需求还原三处共用同一套过滤和去重逻辑；
- 内部包装文本不会泄露到网页历史；
- 稳定 id 让前端多次拉取历史时可以增量合并，不产生重复气泡。

维护注意：

- `_extract_user_prompt()` 的标记和 stop_patterns 与 `prompt.py` / `runtime.py` 的包装措辞强耦合，调整包装文案时必须同步修改这里，否则网页历史会开始显示内部规则；
- `_has_visible_markdown_value()` 的标记列表随输出模板演进，新增报告/方案模板时补充标记；
- LangGraph / DeepAgents 升级可能改变 delta channel 结构（`get_delta_channel_history` 返回格式、`writes` 元组位置 `write[2]`），升级后需回归测试；
- 去重按压缩后的正文比较，两条真实不同的消息若仅空白不同会被合并，当前视为可接受的取舍。
