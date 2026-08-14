"""读取 LangGraph checkpoint 中面向用户可见的消息历史。

读取边界和返回结构见同目录 `checkpoint_history_说明.md`。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage

from agent.core.graph import get_checkpointer


def _message_content(content: Any) -> str:
    """兼容纯文本和多模态内容块，只提取可展示的文本。"""

    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    return str(content).strip() if content is not None else ""


def _visible_message(message: Any) -> dict[str, Any] | None:
    """把 LangChain 消息或字典消息统一为前端需要的简化结构。"""

    if isinstance(message, BaseMessage):
        message_type = str(message.type).lower()
        content = _message_content(message.content)
        message_id = message.id
    elif isinstance(message, dict):
        message_type = str(message.get("type") or message.get("role") or "").lower()
        content = _message_content(message.get("content"))
        message_id = message.get("id")
    else:
        return None

    author = {"human": "user", "user": "user", "ai": "agent", "assistant": "agent"}.get(message_type)
    if author is None or not content:
        return None
    return {"message_id": message_id, "author": author, "content": content}


def visible_checkpoint_messages(thread_id: str) -> list[dict[str, Any]]:
    """返回线程最新 checkpoint 中的用户与助手文本消息。"""

    config = {"configurable": {"thread_id": thread_id}}
    checkpoint_tuple = get_checkpointer().get_tuple(config)
    if checkpoint_tuple is None:
        return []
    # 最新 checkpoint 的 messages channel 已包含当前线程的累计消息，直接读取即可；
    # 不遍历每个历史 checkpoint，避免同一条消息被重复返回。
    channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})
    messages = channel_values.get("messages", []) if isinstance(channel_values, dict) else []
    visible: list[dict[str, Any]] = []
    for message in messages if isinstance(messages, list) else []:
        # 过滤掉不可见的消息
        normalized = _visible_message(message)
        if normalized is not None:
            visible.append(normalized)
    return visible
