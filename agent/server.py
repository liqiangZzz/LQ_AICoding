"""DeepAgent 组装入口。

详细调用链和权限设计见同目录 `server_说明.md`。
"""

import logging
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from langchain_core.runnables import RunnableConfig
from langgraph.store.base import BaseStore

from agent.backends.local_shell import LocalShellBackend
from agent.backends.workspace import Workspace
from agent.core.graph import get_checkpointer, get_langgraph_store, get_store
from agent.core.middleware.context_injection import ContextInjectionMiddleware
from agent.core.middleware.memory_update import MemoryUpdateMiddleware
from agent.core.middleware.message_sanitize import MessageSanitizeMiddleware
from agent.core.middleware.tool_error import ToolErrorMiddleware
from agent.core.middleware.tool_sanitize import SanitizeToolInputsMiddleware
from agent.core.model import make_main_model
from agent.core.repo_mapping import discover_repo_mapping
from agent.core.repo_memory import (
    build_repo_memory_namespace,
    ensure_repo_memory_initialized,
    repo_memory_store_key,
    repo_memory_virtual_path,
)
from agent.core.settings import WORKSPACE_ROOT
from agent.core.task_intent import TaskKind
from agent.prompt import get_system_prompt
from agent.tools import (
    add_review_finding,
    fetch_url,
    list_review_findings,
    open_github_pull_request,
    publish_github_pr_comment,
    web_search,
)
from agent.tools.github_api import parse_github_repo_url

logger = logging.getLogger(__name__)
DEFAULT_RECURSION_LIMIT = 2000
MODEL_CALL_RECURSION_LIMIT = 500
_BACKENDS: dict[str, LocalShellBackend] = {}  # 会话级的 LocalShellBackend


def ensure_backend_for_thread(thread_id: str) -> LocalShellBackend:
    """获取或创建绑定到 thread 的本地 backend。

    这个函数沿用早期版本的会话级后端复用思路，但只保留本地执行能力：
    - 不创建远程 sandbox；
    - 不处理 GitHub proxy；
    - 不接入 LangSmith metadata；
    - 只负责复用当前机器上配置的本地工作区。
    """

    backend = _BACKENDS.get(thread_id)
    if backend is None:
        logger.info("为 thread 创建 LocalShellBackend：%s", thread_id)
        backend = LocalShellBackend()
        _BACKENDS[thread_id] = backend
    else:
        logger.info("复用 thread 的 LocalShellBackend：%s", thread_id)
    return backend


def _task_kind_from_config(configurable: dict[str, Any]) -> TaskKind:
    """从 config 中读取任务类型，非法值统一回退为 coding。"""

    value = configurable.get("task_kind", "coding")
    if value in {"coding", "analysis", "planning", "qa", "sync", "inspect", "review"}:
        return value
    return "coding"


def graph_loaded_for_execution(config: RunnableConfig) -> bool:
    """判断当前 Agent 是否用于真实执行。

    LangGraph Server 通常会区分“图结构探测”和“真实运行”。本项目不使用
    langgraph dev，但保留这个判断，避免没有 thread_id 时误创建完整工具链。
    """

    configurable = (config or {}).get("configurable") or {}
    return bool(configurable.get("__is_for_execution__", False))


def create_repo_backend(
        *,
        local_backend: LocalShellBackend,
        store: BaseStore,
        owner: str,
        repo: str,
) -> CompositeBackend:
    """创建当前仓库专用的 CompositeBackend。

    - `/projects`、`/skills`、`/runtimes` 和 `execute()` 继续走 LocalShellBackend。
    - `/memories/` 走 DeepAgents 原生 StoreBackend，底层由 LangGraph Store 持久化。
    """
    namespace = build_repo_memory_namespace(owner, repo)
    # 创建 CompositeBackend 组合后端
    return CompositeBackend(
        # 默认走本地后端
        default=local_backend,
        routes={
            # 挂载仓库记忆
            "/memories/": StoreBackend(
                store=store,
                namespace=lambda _rt, _namespace=namespace: _namespace,
            )
        },
    )


def _prepare_repo_backend_context(
        *,
        repo_url: Any,
        backend: LocalShellBackend,
) -> tuple[Any, list[str] | None, str | None]:
    """为指定 GitHub 仓库准备 repo 级 backend 和长期记忆。

    `LocalShellBackend` 负责真实的 macOS / Windows 文件和命令执行；如果任务绑定了
    GitHub 仓库，这里再把 `/memories/` 路径挂到 DeepAgents `StoreBackend` 上。
    同时读取记忆文件内容返回，避免后续 middleware 重复查询数据库。
    返回: (CompositeBackend, [memory_virtual_path], memory_content_str | None)
    """

    if not isinstance(repo_url, str) or not repo_url.strip():
        return backend, None, None

    # 解析仓库 URL
    repo = parse_github_repo_url(repo_url)
    # 获取 LangGraph Store
    langgraph_store = get_langgraph_store()
    # 发现仓库映射
    mapping = discover_repo_mapping(
        repo_url=repo.clone_url,
        workspace=Workspace(WORKSPACE_ROOT),
        store=get_store(),
    )
    # 初始化仓库记忆文件
    ensure_repo_memory_initialized(
        store=langgraph_store,
        repo=repo,
        project_dir=str(mapping.project_dir).replace("\\", "/"),
    )
    # 顺带读一次记忆内容，传给 ContextInjectionMiddleware 避免重复查询
    memory_path = repo_memory_virtual_path(repo.owner, repo.repo)
    # 初始化仓库记忆文件
    memory_content: str | None = None
    # 从 LangGraph Store 读取仓库记忆
    memory_item = langgraph_store.get(
        build_repo_memory_namespace(repo.owner, repo.repo),
        repo_memory_store_key(repo.owner, repo.repo),
    )
    # 解析记忆内容
    if memory_item is not None:
        # 获取记忆内容
        content = str(memory_item.value.get("content") or "").strip()
        # 如果有内容则赋值
        if content:
            memory_content = content
    # 返回仓库后端、记忆路径和记忆内容
    return (
        create_repo_backend(
            local_backend=backend,
            store=langgraph_store,
            owner=repo.owner,
            repo=repo.repo,
        ),
        [memory_path],
        memory_content,
    )


def get_agent(config: RunnableConfig):
    """按照指定 thread 构建 DeepAgent。

    本项目只使用本地配置和本地工作区，因此工厂函数保持同步实现，方便
    FastAPI 后台任务直接调用。
    """
    config = dict(config or {})
    configurable = dict(config.get("configurable") or {})
    thread_id = configurable.get("thread_id")
    config["configurable"] = configurable
    # langchain ：默认递归深度为 20
    config["recursion_limit"] = config.get("recursion_limit", DEFAULT_RECURSION_LIMIT)

    if not isinstance(thread_id, str) or not thread_id or not graph_loaded_for_execution(config):
        logger.info("没有 thread_id 或不是执行态，返回空 Agent")
        return create_deep_agent(system_prompt='', tools=[]).with_config(config)

    # Agent 每轮重新构建，但同一会话复用 LocalShellBackend，避免重复初始化工作区。
    backend = ensure_backend_for_thread(thread_id)

    # 任务类型同时决定系统提示词和允许注册的外部写工具。
    task_kind = _task_kind_from_config(configurable)

    # 除 coding 外都启用文件只读模式。sync 仍可执行后端白名单中的 clone/fetch/pull，
    # 但不能借文件工具修改业务源码；同一 thread 从 planning 切到 coding 时会重新解除只读。
    backend.read_only = task_kind != "coding"
    repo_url = configurable.get("repo_url")

    # 预先初始化并读取仓库记忆，再交给中间件注入，避免一次请求重复查询 Store。
    agent_backend, memory_paths, repo_memory_content = _prepare_repo_backend_context(
        repo_url=repo_url,
        backend=backend,
    )

    # 如果有仓库记忆内容则注入
    if repo_memory_content:
        configurable["_repo_memory_content"] = repo_memory_content

    # PR 创建和评论属于远端写操作，只向 coding Agent 注册。
    tools = [fetch_url, web_search, add_review_finding, list_review_findings]
    if task_kind == "coding":
        # coding 任务注册 PR 创建和评论工具
        tools.extend([open_github_pull_request, publish_github_pr_comment])

    # 顺序代表调用链：先清洗模型历史和注入上下文，再校验工具输入并兜底工具异常。
    middleware = [
        MessageSanitizeMiddleware(),  # 清洗模型历史和用户输入中的无效内容
        ContextInjectionMiddleware(),  # 注入仓库记忆内容
        SanitizeToolInputsMiddleware(backend=backend),  # 校验工具输入
        ToolErrorMiddleware(backend=backend),  # 统一工具异常处理
        MemoryUpdateMiddleware(),  # 更新仓库记忆
    ]
    # 创建 DeepAgent
    return create_deep_agent(
        model=make_main_model(),  # 使用主模型
        tools=tools,  # 注册工具
        system_prompt=get_system_prompt(task_kind),  # 使用任务类型对应的系统提示词
        middleware=middleware,  # 使用中间件
        backend=agent_backend,  # 使用仓库后端
        memory=memory_paths,  # 使用仓库记忆
        checkpointer=get_checkpointer(),  # 使用检查点
        store=get_langgraph_store(),  # 使用 LangGraph Store
    ).with_config(config)
