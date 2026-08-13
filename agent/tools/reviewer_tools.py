"""代码审查结果工具。

这些工具把模型发现的问题保存到业务 SQLite Store 中，供前端、任务详情页或后续流程读取。
与直接把审查结论写在自然语言回复中相比，结构化保存可以支持筛选、排序、状态流转和二次汇总。
"""
import uuid
from typing import Any

from langchain_core.tools import tool

from agent.store import get_local_store
from agent.tools.runtime_context import get_runtime_thread_id


@tool
def add_review_finding(
        file: str,
        line: int | None,
        severity: str,
        title: str,
        description: str
) -> dict[str, str]:
    """ 添加代码审查发现。把代码审查发现记录到本地 SQLite Store

    Args:
        file: 问题所在文件路径，通常是仓库内相对路径。
        line: 问题所在行号；无法定位到具体行时可以为空。
        severity: 严重级别，例如 blocker、major、minor、info。
        title: 简短标题，用于列表展示。
        description: 详细说明，包含风险、触发条件和建议修复方式。

    Returns:
         成功时返回 finding id 和初始状态；缺少 thread_id 时返回 error。
    """

    thread_id = get_runtime_thread_id()
    if not thread_id:
        return {"status": "error", "error": "缺少 thread_id，无法记录审查发现。"}

    # 使用短 UUID 作为本地发现项 id，避免依赖数据库自增 id 暴露给模型。
    finding_id = f"finding-{uuid.uuid4().hex[:8]}"
    # 所有审查发现都绑定当前 thread_id，保证不同任务之间的数据不会串联。
    get_local_store().add_finding(
        finding_id=finding_id,
        thread_id=thread_id,
        file=file,
        line=line,
        severity=severity,
        title=title,
        description=description,
    )
    return {"id": finding_id, "status": "open"}


@tool
def list_review_findings() -> list[dict[str, Any]]:
    """
    列出当前 thread 的代码审查发现

    该工具用于让模型在最终回复中重新读取已记录的问题，避免遗漏前面阶段保存的发现项。
    """

    thread_id = get_runtime_thread_id()
    if not thread_id:
        return [{"status": "error", "error": "缺少 thread_id，无法读取审查发现。"}]
    # Store 层负责具体 SQL 查询和结果结构化，这里只传递当前任务的 thread_id。
    return get_local_store().list_findings(thread_id)
