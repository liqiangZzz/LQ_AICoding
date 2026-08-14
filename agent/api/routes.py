import asyncio
import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent.backends.permissions import WorkspacePermissionError
from agent.core import settings
from agent.core.logging_config import read_recent_log
from agent.core.runtime import get_task, list_tasks, run_agent_task
from agent.env_utils import get_env

router = APIRouter()
logger = logging.getLogger("agent.api")


class TaskCreateRequest(BaseModel):
    repo_url: str = Field(min_length=1, description="GitHub 仓库地址")
    prompt: str = Field(min_length=1, description="用户任务描述")
    thread_id: str | None = Field(default=None, description="可选的会话线程 ID")


@router.get("/health")
def health() -> dict[str, Any]:
    """后端健康检查。

    这个接口用于确认 FastAPI/Uvicorn 服务是否正常运行，
    同时展示最关键的项目目录、工作区、SQLite 文件和日志文件。
    注意：这里只返回密钥是否存在，不返回真实 API Key 或 Token。
    """

    return {
        "ok": True,
        "project_root": str(settings.PROJECT_ROOT),
        "workspace_root": str(settings.WORKSPACE_ROOT),
        "checkpoint_db": str(settings.CHECKPOINT_DB_PATH),
        "store_db": str(settings.STORE_DB_PATH),
        "log_dir": str(settings.LOG_DIR),
        "backend_log": str(settings.backend_log_path()),
        "agent_log": str(settings.agent_log_path()),
        "has_deepseek_key": bool(get_env("DEEPSEEK_API_KEY")),
        "deepseek_base_url": get_env("DEEPSEEK_BASE_URL"),
        "main_model": get_env("MAIN_MODEL", "deepseek-v4-pro"),
        "has_github_token": any(
            get_env(name).strip()
            for name in ("GITHUB_TOKEN", "GH_TOKEN", "SCM_GITHUB_TOKEN")
        ),
    }


@router.post("/api/tasks")
async def create_task(body: TaskCreateRequest) -> dict[str, Any]:
    """创建任务，并把同步 Agent 执行移到工作线程，避免阻塞事件循环。"""
    logger.info("收到任务请求: repo_url=%s, thread_id=%s", body.repo_url, body.thread_id)
    try:
        # 这里使用 asyncio.to_thread() 把阻塞的 run_agent_task() 移到工作线程，避免阻塞事件循环
        return await asyncio.to_thread(
            run_agent_task,
            repo_url=body.repo_url,
            prompt=body.prompt,
            thread_id=body.thread_id,
        )
    except (ValueError, WorkspacePermissionError) as exc:
        logger.warning("任务请求被拒绝: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # logger.exception 会自动附带当前异常堆栈，无需重复格式化异常对象。
        logger.exception("任务执行失败")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/tasks")
def tasks(limit: int = 50) -> list[dict[str, Any]]:
    # 读取最近任务列表，供页面展示历史运行记录
    return list_tasks(limit=limit)


@router.get("/api/tasks/{thread_id}")
def task_detail(thread_id: str) -> dict[str, Any]:
    # 根据线程ID获取任务详情
    task = get_task(thread_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _parse_log_date(date_text: str | None) -> date | None:
    """解析日志日期参数。

    接口使用 YYYY-MM-DD，和日志文本名保持一致。
    如果没有传 date，就默认读取当天日志。
    """
    if not date_text:
        return None

    try:
        return date.fromisoformat(date_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must use YYYY-MM-DD") from exc


@router.get("/api/logs/backend")
def backend_log(limit: int = 200, date: str | None = None) -> dict[str, Any]:
    """读取后端日志 最近的若干行。

    前端可以轮询这个接口展示服务运行状态；
    测试时也可以直接在浏览器中打开，观察请求和错误日志。
    date 参数用于读取历史日志，例如 2026-07-10
    """
    parsed_date = _parse_log_date(date)
    path = settings.backend_log_path(parsed_date)
    return {
        "path": str(path),
        "lines": read_recent_log(path, max_lines=limit),
    }


@router.get("/api/logs/agent")
def agent_logs(limit: int = 200, date: str | None = None) -> dict[str, Any]:
    """读取 Agent 任务日志最近若干行。

    当前日志文件是 agent-runs.log，历史轮转文件是 agent-runs.log.YYYY-MM-DD。
    """

    parsed_date = _parse_log_date(date)
    path = settings.agent_log_path(parsed_date)
    return {
        "path": str(path),
        "lines": read_recent_log(path, max_lines=limit),
    }
