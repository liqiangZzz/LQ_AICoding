import logging
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from langchain_core.runnables import RunnableConfig
from langgraph.store.base import BaseStore

from agent.backends.local_shell import LocalShellBackend
from agent.backends.workspace import Workspace
from agent.core.graph import get_langgraph_store, get_store
from agent.core.repo_mapping import discover_repo_mapping
from agent.core.repo_memory import ensure_repo_memory_initialized, build_repo_memory_namespace, repo_memory_store_key
from agent.core.settings import WORKSPACE_ROOT
from agent.core.task_intent import TaskKind
from agent.tools.gitee_api import parse_gitee_repo_url

logger = logging.getLogger(__name__)
DEFAULT_RECURSION_LIMIT = 2000
MODEL_CALL_RECURSION_LIMIT = 500
_BACKENDS: dict[str, LocalShellBackend] = {}  # 会话级的 LocalShellBackend

def ensure_backend_for_thread(thread_id: str) -> LocalShellBackend:
    """获取或创建绑定到 thread 的本地 backend。

    这个函数对应 之前项目 的 `ensure_sandbox_for_thread`，但做了功能减法：
    - 不创建远程 sandbox；
    - 不处理 GitHub proxy；
    - 不接入 LangSmith metadata；
    - 只负责复用当前机器上的 `E:\\ai_workspace` 工作区。
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

    之前项目 在 LangGraph Server 中会区分“图结构探测”和“真实运行”。
    LX_AICODING 不使用 langgraph dev，但保留这个判断，可以让课程代码结构
    尽量贴近 之前项目，并避免没有 thread_id 时误创建完整工具链。
    """

    configurable = (config or {}).get("configurable") or {}
    return bool(configurable.get("__is_for_execution__", False))

def repo_memory_virtual_path(owner: str, repo: str) -> str:
    """Agent 可见的记忆文件路径，按 owner/repo 命名，多仓库各自独立。"""
    return f"/memories/{owner}/{repo}.md"

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
    return CompositeBackend(
        default=local_backend,
        routes={
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
    """为指定 Gitee 仓库准备 repo 级 backend 和长期记忆。

    `LocalShellBackend` 负责真实的 Windows 文件和命令执行；如果任务绑定了
    Gitee 仓库，这里再把 `/memories/` 路径挂到 DeepAgents `StoreBackend` 上。
    同时读取记忆文件内容返回，避免后续 middleware 重复查询数据库。
    返回: (CompositeBackend, [memory_virtual_path], memory_content_str | None)
    """

    if not isinstance(repo_url, str) or not repo_url.strip():
        return backend, None, None

    repo = parse_gitee_repo_url(repo_url)
    langgraph_store = get_langgraph_store()
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
    memory_content: str | None = None
    memory_item = langgraph_store.get(
        build_repo_memory_namespace(repo.owner, repo.repo),
        repo_memory_store_key(repo.owner, repo.repo),
    )
    if memory_item is not None:
        content = str(memory_item.value.get("content") or "").strip()
        if content:
            memory_content = content
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
    """按照 指定 thread 构建 DeepAgent。

    之前项目 的入口是 `async def get_agent(config)`，因为它需要异步解析用户身份、
    远程 sandbox、团队模型配置等。课程版全部使用本地配置，因此这里保留同名
    工厂函数，但实现为同步函数，方便 FastAPI 后台任务直接调用。
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

    # agent是多例的，同一个回话共享一个后端agent_backend
    backend = ensure_backend_for_thread(thread_id)
    # 用户任务分类
    task_kind = _task_kind_from_config(configurable)
    # 得到仓库地址： repo_url
    repo_url = configurable.get("repo_url")


    # server.py 在创建 Agent 之前，已经顺手读到了当前仓库记忆内容；
    # 那就把仓库记忆文件内容放进 config，后面的 ContextInjectionMiddleware 直接用，
    # 避免 middleware 再查一次 LangGraph Store。
    # 创建了一个混合路由的 Backend，用于处理 /memories/ 路径。+ 记忆文件初始化 + 记忆文件的读取
    agent_backend, memory_paths, repo_memory_content = _prepare_repo_backend_context(repo_url=repo_url, backend=backend)
    if repo_memory_content:
        configurable["_repo_memory_content"] = repo_memory_content

    # 子agent
    return create_deep_agent().with_config(config)