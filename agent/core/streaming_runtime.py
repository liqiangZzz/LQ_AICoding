from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any

from langchain_core.messages import BaseMessage

from agent.core.events import record_event
from agent.core.middleware.run_limits import AgentRunLimitExceeded, AgentRunLimitTracker
from agent.env_utils import get_env
from agent.tools.github_api import mask_token

# 事件结构、Checkpoint 区别和前端写入策略见同目录 `streaming_runtime_说明.md`。
logger = logging.getLogger("agent.run.streaming")


def _safe_attr(value: Any, name: str, default: Any = None) -> Any:
    """安全读取官方流对象字段。

    Deep Agents / LangGraph 的 v3 streaming 协议还带有 experimental 提示，
    不同小版本的字段可能是属性，也可能是轻量对象方法。这里统一容错读取，
    避免某个字段缺失时直接打断整个 Agent 任务。
    """

    try:
        return getattr(value, name, default)
    except Exception:  # noqa: BLE001 - 第三方流对象的属性访问可能执行自定义描述符
        return default


def _safe_field(value: Any, name: str, default: Any = None) -> Any:
    """兼容官方事件对象和普通 dict。"""

    if isinstance(value, dict):
        return value.get(name, default)
    return _safe_attr(value, name, default)


def _stringify(value: Any, *, limit: int = 1200) -> str:
    """把事件对象中的输入、输出压缩成适合前端展示的短文本。

    前端步骤区只需要告诉讲课学员“正在做什么”，不应该塞入大段 token、
    大段文件内容或未脱敏的异常。真正的详细排查仍看后端日志。
    """

    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = repr(value)
    text = mask_token(text)
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text


def _message_text(message: Any) -> str:
    """从官方 message stream 事件中提取文本。

    文档里 message 暴露 `.text`；LangChain 消息对象也可能放在 `.content`。
    这里兼容两种形式，只保存一小段进度摘要。
    """

    text = _safe_attr(message, "text")
    if text:
        return _stringify(text, limit=1200)
    content = _safe_attr(message, "content")
    if isinstance(content, str):
        return _stringify(content, limit=1200)
    if isinstance(message, BaseMessage):
        return _stringify(message.content, limit=1200)
    return ""


def _merge_stream_text(previous: str, current: str) -> str:
    """合并官方 message 流文本。

    不同版本的 DeepAgents 可能返回“完整截至目前的文本”，也可能返回“本次新增片段”。
    - 如果 current 已经包含 previous，说明它是完整文本，直接用 current。
    - 如果 previous 已经以 current 结尾，说明是重复事件，保持 previous。
    - 否则把 current 作为增量追加。
    """

    if not current:
        return previous
    if not previous:
        return current
    if current == previous:
        return previous
    if current.startswith(previous):
        return current
    if previous.endswith(current):
        return previous
    return previous + current


def _event_payloads(event: Any) -> list[Any]:
    """从 raw protocol event 中取出 params.data。

    官方文档中的 messages 事件形态是 `event["params"]["data"][0]`。
    实际小版本中 data 可能是单个 dict，也可能是 list，这里统一规整成列表。
    """

    if not isinstance(event, dict):
        return []
    params = event.get("params")
    if not isinstance(params, dict):
        return []
    data = params.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, tuple):
        # LangGraph v3 的真实 messages 事件形态通常是：
        # params.data = (payload, metadata)。第 0 项才是 content-block-delta 等正文事件。
        return [data[0]] if data else []
    return [data]


def _text_delta_from_event(event: Any) -> str:
    """按官方 raw event 协议提取正文 token。

    Deep Agents 文档建议 UI 需要精确流式正文时直接读取 raw protocol events：
    method=messages、event=content-block-delta、delta.type=text-delta。
    """

    if not isinstance(event, dict) or event.get("method") != "messages":
        return ""
    deltas: list[str] = []
    for payload in _event_payloads(event):
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != "content-block-delta":
            continue
        block = payload.get("delta") or {}
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text-delta":
            deltas.append(str(block.get("text") or ""))
    return "".join(deltas)


def _message_event_payload(event: Any) -> dict[str, Any] | None:
    """读取 raw messages 事件中的第一个 payload。"""

    if not isinstance(event, dict) or event.get("method") != "messages":
        return None
    for payload in _event_payloads(event):
        if isinstance(payload, dict):
            return payload
    return None


def _tool_chunk_from_message_event(event: Any) -> dict[str, Any] | None:
    """从 raw messages 事件中读取工具调用 chunk。

    当前 DeepAgents 版本会把工具调用参数作为 message content block 输出：
    content-block-delta -> delta.type=block-delta -> fields.type=tool_call_chunk。
    write_todos 的 JSON 参数会以 fields.args 逐步增长。
    """

    payload = _message_event_payload(event)
    if not payload:
        return None

    if payload.get("event") == "content-block-start":
        content = payload.get("content")
        if isinstance(content, dict) and content.get("type") == "tool_call_chunk":
            return content

    if payload.get("event") == "content-block-delta":
        delta = payload.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "block-delta":
            fields = delta.get("fields")
            if isinstance(fields, dict) and fields.get("type") == "tool_call_chunk":
                return fields

    if payload.get("event") == "content-block-finish":
        content = payload.get("content")
        if isinstance(content, dict) and content.get("type") == "tool_call":
            return content

    return None


def _should_flush_stream_text(*, accumulated_text: str, last_flushed_length: int, delta: str) -> bool:
    """判断是否需要把模型正文增量刷新到业务事件表。

    DeepAgents 的 raw message 事件可能按 token 级别返回，如果每个 token 都写一次 SQLite，
    页面虽然实时，但本地数据库提交会过于频繁。这里做轻量合并：
    - 首段内容立即展示，让用户知道模型已经开始输出；
    - 累计新增 24 个字符左右刷新一次；
    - 遇到换行也刷新，Markdown 标题、列表和段落会更快出现在页面上。
    """

    if not accumulated_text:
        return False
    if last_flushed_length == 0:
        return True
    if len(accumulated_text) - last_flushed_length >= 24:
        return True
    return "\n" in delta


def _tool_call_from_event(event: Any) -> Any | None:
    """尽量从 raw tool_calls event 中提取工具调用对象。

    工具事件在不同 DeepAgents 小版本中的字段可能不完全一致；本函数只做保守解析。
    解析不到时返回 None，具体文件、命令和 GitHub 工具仍会通过内部事件记录展示。
    """

    if not isinstance(event, dict) or event.get("method") != "tool_calls":
        return None
    for payload in _event_payloads(event):
        if isinstance(payload, dict):
            return payload
        if payload is not None:
            return payload
    return None


def _subagent_from_event(event: Any) -> Any | None:
    """尽量从 raw subagents event 中提取子智能体对象。"""

    if not isinstance(event, dict) or event.get("method") != "subagents":
        return None
    for payload in _event_payloads(event):
        if payload is not None:
            return payload
    return None


def _tool_title(tool_name: str) -> str:
    """把工具名映射成讲课友好的中文步骤名。"""

    mapping = {
        "ls": "查看目录",
        "read_file": "读取文件",
        "write_file": "写入文件",
        "edit_file": "修改文件",
        "glob": "匹配文件",
        "grep": "搜索文件内容",
        "execute": "执行命令",
        "list_files": "查看目录",
        "run_command": "执行命令",
        "sync_github_repo": "准备 GitHub 仓库",
        "create_pull_request": "创建或复用 Pull Request",
        "open_github_pull_request": "创建或复用 Pull Request",
        "publish_github_pr_comment": "发布 PR 评论",
        "get_github_pull_request_context": "读取 PR 审查上下文",
        "load_review_rules": "读取审查规则",
        "get_review_diff_summary": "读取审查 diff",
        "validate_review_finding_location": "校验审查位置",
        "add_review_finding": "记录审查发现",
        "list_review_findings": "列出审查发现",
        "web_search": "联网搜索资料",
        "fetch_url": "读取网页资料",
        "task": "委派子任务",
    }
    return mapping.get(tool_name, f"调用工具：{tool_name}")


def _tool_kind(tool_name: str) -> str:
    """把工具名映射成前端已有的工具分类。"""

    if tool_name == "read_file":
        return "read"
    if tool_name in {"write_file", "edit_file"}:
        return "edit"
    if tool_name in {"ls", "list_files", "glob", "grep"}:
        return "search"
    if tool_name in {
        "sync_github_repo",
        "create_pull_request",
        "open_github_pull_request",
        "publish_github_pr_comment",
        "get_github_pull_request_context",
    }:
        return "fetch"
    if tool_name in {"web_search", "fetch_url"}:
        return "fetch"
    if tool_name in {"load_review_rules", "get_review_diff_summary"}:
        return "read"
    if tool_name in {"execute", "run_command"}:
        return "execute"
    return "think"


def _normalize_todo_status(status: Any) -> str:
    """把 DeepAgents / LangChain 的 todo 状态规整为前端支持的三种状态。"""

    text = str(status or "pending").lower()
    if text in {"in_progress", "in-progress", "active", "doing"}:
        return "in_progress"
    if text in {"completed", "complete", "done"}:
        return "completed"
    return "pending"


def _extract_todos(tool_call: Any) -> list[dict[str, str]]:
    """从 write_todos 官方 tool_call 中提取任务清单。"""

    call_input = _safe_field(tool_call, "input")
    if call_input is None:
        call_input = _safe_field(tool_call, "args")
    if isinstance(call_input, str):
        try:
            call_input = json.loads(call_input)
        except ValueError:
            return [{"content": call_input, "status": "pending"}] if call_input.strip() else []

    raw_todos: Any
    if isinstance(call_input, dict):
        raw_todos = call_input.get("todos") or call_input.get("items") or []
    else:
        raw_todos = call_input

    if not isinstance(raw_todos, list):
        return []

    todos: list[dict[str, str]] = []
    for item in raw_todos:
        if isinstance(item, str):
            content = item.strip()
            status = "pending"
        elif isinstance(item, dict):
            content = str(item.get("content") or item.get("task") or item.get("title") or "").strip()
            status = _normalize_todo_status(item.get("status"))
        else:
            content = str(item).strip()
            status = "pending"
        if content:
            todos.append({"content": content, "status": status})
    return todos


def _record_write_todos(thread_id: str, run_id: str, tool_call: Any, index: int) -> bool:
    """只把 DeepAgents 内置 write_todos 转成结构化任务清单事件。"""

    tool_name = str(_safe_field(tool_call, "tool_name", "") or _safe_field(tool_call, "name", "") or "")
    if tool_name != "write_todos":
        return False
    todos = _extract_todos(tool_call)
    if not todos:
        return True
    call_id = str(_safe_field(tool_call, "id", "") or _safe_field(tool_call, "tool_call_id", "") or index)
    record_event(
        thread_id,
        f"todos:{run_id}:{call_id}",
        "任务清单",
        kind="todo",
        status="completed",
        detail=json.dumps({"todos": todos}, ensure_ascii=False),
    )
    return True


def _decode_json_string_fragment(value: str) -> str:
    """解码正则截取出的 JSON 字符串片段。"""

    try:
        return json.loads(f'"{value}"')
    except ValueError:
        return value


def _todos_from_args_text(args_text: str) -> list[dict[str, str]]:
    """从 write_todos 的参数文本中提取已形成的 todo。

    args_text 在 raw chunk 中经常是“不完整但逐步增长”的 JSON 字符串。完整时直接
    json.loads；不完整时用保守正则提取已经闭合的 content/status 对象，让前端能更早
    看到任务计划逐项出现。
    """

    if not args_text.strip():
        return []
    try:
        parsed = json.loads(args_text)
    except ValueError:
        parsed = None

    if isinstance(parsed, dict):
        return _extract_todos({"input": parsed})

    todos: list[dict[str, str]] = []
    pattern = re.compile(
        r'\{\s*"content"\s*:\s*"(?P<content>(?:\\.|[^"\\])*)"\s*,\s*"status"\s*:\s*"(?P<status>[^"]*)"',
        re.DOTALL,
    )
    for match in pattern.finditer(args_text):
        content = _decode_json_string_fragment(match.group("content")).strip()
        status = _normalize_todo_status(match.group("status"))
        if content:
            todos.append({"content": content, "status": status})
    return todos


def _record_todos(thread_id: str, run_id: str, call_id: str, todos: list[dict[str, str]], *, status: str) -> None:
    """写入结构化任务计划事件。"""

    if not todos:
        return
    record_event(
        thread_id,
        f"todos:{run_id}:{call_id}",
        "任务清单",
        kind="todo",
        status=status,
        detail=json.dumps({"todos": todos}, ensure_ascii=False),
    )


def _message_dict(message: Any) -> dict[str, Any]:
    """把最终输出中的 LangChain 消息对象转换为普通字典。"""

    if isinstance(message, BaseMessage):
        return {"type": message.type, "content": message.content}
    return {"type": type(message).__name__, "content": str(message)}


def _messages_from_output(output: Any) -> list[dict[str, Any]]:
    """从 stream.output 中提取最终 messages。

    官方 Deep Agents 返回值通常是 `{"messages": [...]}`；为了课程版稳定运行，
    这里也兼容对象属性和其它返回结构。
    """

    if isinstance(output, dict):
        messages = output.get("messages") or []
    else:
        messages = _safe_attr(output, "messages", []) or []
    if not isinstance(messages, Iterable) or isinstance(messages, (str, bytes)):
        return []
    return [_message_dict(message) for message in messages]


def _record_stream_message(thread_id: str, run_id: str, text: str) -> None:
    """把累计正文写成前端可展示的临时文本事件。"""

    if not text.strip():
        return
    record_event(
        thread_id,
        f"stream:{run_id}:message",
        "正在生成内容",
        kind="other",
        status="in_progress",
        detail=json.dumps({"text": text}, ensure_ascii=False),
    )


def _tool_event_from_raw(event: Any) -> dict[str, Any] | None:
    """读取 raw tools 生命周期事件。"""

    if not isinstance(event, dict) or event.get("method") != "tools":
        return None
    params = event.get("params")
    if not isinstance(params, dict):
        return None
    data = params.get("data")
    return data if isinstance(data, dict) else None


def _summarize_raw_event(event: Any) -> dict[str, Any]:
    """生成 raw event 的安全摘要，用于诊断真实 DeepAgents 事件形态。"""

    if not isinstance(event, dict):
        return {"type": type(event).__name__, "repr": _stringify(event, limit=800)}
    summary: dict[str, Any] = {
        "keys": list(event.keys()),
        "method": event.get("method"),
        "event": event.get("event"),
    }
    params = event.get("params")
    if isinstance(params, dict):
        summary["params_keys"] = list(params.keys())
        summary["namespace"] = params.get("namespace")
        data = params.get("data")
        if isinstance(data, list):
            summary["data_type"] = "list"
            summary["data_len"] = len(data)
            sample = data[0] if data else None
        else:
            summary["data_type"] = type(data).__name__
            sample = data
        if isinstance(sample, dict):
            summary["data_sample_keys"] = list(sample.keys())
            summary["data_sample_event"] = sample.get("event")
            delta = sample.get("delta")
            if isinstance(delta, dict):
                summary["delta_keys"] = list(delta.keys())
                summary["delta_type"] = delta.get("type")
                if delta.get("text"):
                    summary["delta_text_preview"] = _stringify(delta.get("text"), limit=120)
        elif sample is not None:
            summary["data_sample_type"] = type(sample).__name__
            summary["data_sample_repr"] = _stringify(sample, limit=300)
    data = event.get("data")
    if isinstance(data, dict):
        summary["top_data_keys"] = list(data.keys())
        chunk = data.get("chunk")
        if chunk is not None:
            summary["top_data_chunk_type"] = type(chunk).__name__
            summary["top_data_chunk_repr"] = _stringify(chunk, limit=300)
    return summary


def _debug_raw_stream_events(*, agent: Any, thread_id: str, content: str) -> None:
    """按开关记录真实 raw event 结构。

    该诊断会额外启动一条极短 DeepAgent 流，只在 `LQ_AICODING_DEBUG_STREAM_EVENTS=1`
    时启用。它不参与正式任务结果，只用于确认 DeepAgents 当前版本真实事件字段。
    """

    if get_env("LQ_AICODING_DEBUG_STREAM_EVENTS") != "1":
        return
    debug_thread_id = f"{thread_id}:debug-stream"
    try:
        stream = agent.stream_events(
            {"messages": [{"role": "user", "content": "请只回复：stream-debug"}]},
            version="v3",
            config={"configurable": {"thread_id": debug_thread_id}},
        )
        for index, event in enumerate(stream):
            if index >= 30:
                break
            logger.info(
                "raw stream event debug thread_id=%s index=%s summary=%s",
                thread_id,
                index,
                json.dumps(_summarize_raw_event(event), ensure_ascii=False),
            )
    except Exception:
        logger.exception("raw stream event debug failed: thread_id=%s", thread_id)


def _record_subagent(thread_id: str, run_id: str, subagent: Any, index: int) -> None:
    """记录 Deep Agents 子智能体生命周期。

    第一版 UI 不单独做子智能体卡片，只用一条简洁步骤展示 delegated task。
    """

    name = str(_safe_field(subagent, "name", "") or "subagent")
    status = str(_safe_field(subagent, "status", "") or "started")
    event_status = "completed" if status == "completed" else "error" if status == "failed" else "in_progress"
    path = _safe_field(subagent, "path")
    record_event(
        thread_id,
        f"stream:{run_id}:subagent:{index}:{name}",
        f"子智能体：{name}",
        kind="think",
        status=event_status,
        detail=_stringify(path, limit=500) or None,
    )


def _consume_interleaved_stream(*, stream: Any, thread_id: str, run_id: str) -> tuple[int, int]:
    """使用 DeepAgents 投影流消费消息、工具和子智能体。

    这是当前稳定主流程。write_todos 任务计划依赖 `tool_calls` 投影，
    因此不能用尚未确认真实字段的 raw event 替代它。
    """

    tool_call_index = 0
    subagent_index = 0
    last_message_text = ""
    accumulated_message_text = ""
    for name, item in stream.interleave("messages", "tool_calls", "subagents"):
        if name == "messages":
            text = _message_text(item)
            if text and text != last_message_text:
                last_message_text = text
                accumulated_message_text = _merge_stream_text(accumulated_message_text, text)
                _record_stream_message(thread_id, run_id, accumulated_message_text)
        elif name == "tool_calls":
            tool_call_index += 1
            if _record_write_todos(thread_id, run_id, item, tool_call_index):
                continue
            logger.debug("忽略官方 tool_call 展示事件：thread_id=%s index=%s item=%s", thread_id, tool_call_index, item)
        elif name == "subagents":
            subagent_index += 1
            _record_subagent(thread_id, run_id, item, subagent_index)
    return tool_call_index, subagent_index


def _consume_raw_event_stream(*, stream: Any, thread_id: str, run_id: str, task_kind: str | None = None) -> tuple[int, int]:
    """按官网 raw protocol event 消费 DeepAgents 输出。

    这个函数解决“技术方案正文只能最终一次性展示”的问题：
    1. 直接读取 `method=messages` 的 `content-block-delta/text-delta`，把累计正文写入
       `stream:message`，前端 SSE 会持续拿到越来越完整的 Markdown 正文。
    2. 同时继续读取 `method=tool_calls` 和 `method=subagents`，保留 write_todos 任务计划、
       工具步骤和子 Agent 生命周期展示。

    如果某个 DeepAgents 小版本没有在 raw event 中暴露 tool_calls，工具内部的 record_event
    仍然会记录读文件、命令、GitHub 等步骤；但 write_todos 只有 raw tool_calls 可见时才会出现。
    """

    tool_call_index = 0
    subagent_index = 0
    accumulated_message_text = ""
    last_flushed_length = 0
    write_todo_args_by_call: dict[str, str] = {}
    write_todo_last_payload_by_call: dict[str, str] = {}
    saw_write_todos = False
    limit_tracker = AgentRunLimitTracker(task_kind=task_kind)

    # 单次遍历同时处理四类事件。判断顺序不能随意调整：文本 delta 最常见，先处理并
    # continue 可以避免同一 message 事件又被误判成工具事件。
    for event in stream:
        limit_tracker.observe_event(event)

        # 1. 模型正文：按字符阈值合并后写入事件表，降低 SQLite 写入频率。
        delta = _text_delta_from_event(event)
        if delta:
            accumulated_message_text += delta
            if _should_flush_stream_text(
                accumulated_text=accumulated_message_text,
                last_flushed_length=last_flushed_length,
                delta=delta,
            ):
                _record_stream_message(thread_id, run_id, accumulated_message_text)
                last_flushed_length = len(accumulated_message_text)
            continue

        # 2. 工具参数流：write_todos 的 JSON 可能分多次到达，需要按 call_id 累积状态。
        tool_chunk = _tool_chunk_from_message_event(event)
        if tool_chunk is not None:
            tool_name = str(tool_chunk.get("name") or "")
            if tool_name == "write_todos":
                saw_write_todos = True
                call_id = str(tool_chunk.get("id") or tool_chunk.get("tool_call_id") or "write_todos")
                args = tool_chunk.get("args")
                if isinstance(args, dict):
                    todos = _extract_todos({"input": args})
                    payload_text = json.dumps(todos, ensure_ascii=False)
                    if payload_text != write_todo_last_payload_by_call.get(call_id):
                        _record_todos(thread_id, run_id, call_id, todos, status="completed")
                        write_todo_last_payload_by_call[call_id] = payload_text
                elif isinstance(args, str):
                    write_todo_args_by_call[call_id] = args
                    todos = _todos_from_args_text(args)
                    payload_text = json.dumps(todos, ensure_ascii=False)
                    if todos and payload_text != write_todo_last_payload_by_call.get(call_id):
                        _record_todos(thread_id, run_id, call_id, todos, status="in_progress")
                        write_todo_last_payload_by_call[call_id] = payload_text
            continue

        # 3. 完整工具调用及工具生命周期事件：主要用于前端任务清单展示。
        tool_call = _tool_call_from_event(event)
        if tool_call is not None:
            tool_call_index += 1
            if _record_write_todos(thread_id, run_id, tool_call, tool_call_index):
                saw_write_todos = True
                continue
            logger.debug(
                "忽略官方 raw tool_call 展示事件：thread_id=%s index=%s item=%s",
                thread_id,
                tool_call_index,
                tool_call,
            )
            continue

        tool_event = _tool_event_from_raw(event)
        if tool_event is not None:
            tool_name = str(tool_event.get("tool_name") or "")
            if tool_name == "write_todos":
                saw_write_todos = True
                call_id = str(tool_event.get("tool_call_id") or "write_todos")
                tool_input = tool_event.get("input")
                if isinstance(tool_input, dict):
                    todos = _extract_todos({"input": tool_input})
                    payload_text = json.dumps(todos, ensure_ascii=False)
                    if todos and payload_text != write_todo_last_payload_by_call.get(call_id):
                        event_status = "completed" if tool_event.get("event") == "tool-finished" else "in_progress"
                        _record_todos(thread_id, run_id, call_id, todos, status=event_status)
                        write_todo_last_payload_by_call[call_id] = payload_text
            continue

        # 4. 子智能体事件：当前只记录生命周期摘要，不保存其完整内部消息。
        subagent = _subagent_from_event(event)
        if subagent is not None:
            subagent_index += 1
            _record_subagent(thread_id, run_id, subagent, subagent_index)

    if accumulated_message_text and last_flushed_length != len(accumulated_message_text):
        _record_stream_message(thread_id, run_id, accumulated_message_text)
    return tool_call_index if saw_write_todos else 0, subagent_index


def run_agent_with_event_stream(
    *,
    agent: Any,
    thread_id: str,
    run_id: str,
    content: str,
    task_kind: str | None = None,
) -> dict[str, Any]:
    """使用官方 v3 event streaming 驱动 DeepAgent。

    这个函数是 FastAPI 版本替代 `langgraph dev` 的核心桥接层：
    - DeepAgent 继续按官方 `stream_events(version="v3")` 运行。
    - 后端把 message、tool_calls、subagents 转成课程项目的 `run_events`。
    - 每一轮运行都把 run_id 写进事件 id，保证 plan、coding、review 多轮内容
      不会在前端互相覆盖或拼接到同一个消息里。
    - 前端仍只消费我们自己的 `/dashboard/api/.../stream`，不用绑定 LangGraph 本地服务。
    """

    stream = agent.stream_events(
        {"messages": [{"role": "user", "content": content}]},
        version="v3",
        config={"configurable": {"thread_id": thread_id}},
    )
    record_event(thread_id, "model", "调用 deepseek-v4-pro", kind="other", status="in_progress")
    _debug_raw_stream_events(agent=agent, thread_id=thread_id, content=content)
    # raw protocol events 是当前版本里唯一能拿到 token/chunk 的通道。
    # 这里同时解析 text-delta 和 write_todos 的 tool_call_chunk，保证正文和任务计划都能流式更新。
    try:
        tool_call_index, subagent_index = _consume_raw_event_stream(
            stream=stream,
            thread_id=thread_id,
            run_id=run_id,
            task_kind=task_kind,
        )
    except AgentRunLimitExceeded as exc:
        record_event(
            thread_id,
            "agent:run-limit",
            "达到运行保护上限",
            kind="other",
            status="error",
            detail=str(exc),
        )
        record_event(thread_id, "model", "调用 deepseek-v4-pro", kind="other", status="error", detail=str(exc))
        raise

    output = stream.output
    record_event(thread_id, "model", "调用 deepseek-v4-pro", kind="other", status="completed")
    logger.info(
        "官方事件流消费完成：thread_id=%s tool_calls=%s subagents=%s",
        thread_id,
        tool_call_index,
        subagent_index,
    )
    return {"messages": _messages_from_output(output), "raw_output": output}
