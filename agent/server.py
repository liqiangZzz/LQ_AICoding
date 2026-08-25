"""DeepAgent 组装入口。

详细调用链和权限设计见同目录 `server_说明.md`。
"""

import logging
from typing import Any

from deepagents import FilesystemPermission, SubAgent, create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.middleware import create_summarization_tool_middleware
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.store.base import BaseStore

from agent.backends.local_shell import LocalShellBackend
from agent.backends.workspace import Workspace
from agent.core.graph import get_checkpointer, get_langgraph_store, get_store
from agent.core.middleware.context_injection import ContextInjectionMiddleware
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
from agent.tools.github_tools import get_github_pull_request_context
from agent.tools.reviewer_tools import (
    get_review_diff_summary,
    load_default_review_rules,
    validate_review_finding_location,
)

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

def _general_purpose_subagent(model: BaseChatModel) -> SubAgent:
    """构建 之前项目 风格的通用分析子 Agent。

    子 Agent 只负责阅读、分析、总结和给主 Agent 提供建议。它不能直接修改
    `/projects` 下的源码，也不能改 `/skills`、`/policies`、`/runtimes`。
    这样可以让主 Agent 保持最终执行权，降低子 Agent 误改代码的风险。
    """

    return {
        "name": GENERAL_PURPOSE_SUBAGENT["name"],
        "description": GENERAL_PURPOSE_SUBAGENT["description"],
        "system_prompt": GENERAL_PURPOSE_SUBAGENT["system_prompt"],
        "model": model,
        "skills": ["/skills/"],
        "permissions": [
            FilesystemPermission(
                operations=["read"],
                paths=[
                    "/projects/**",
                    "/skills/**",
                    "/policies/**",
                    "/reviews/**",
                    "/runtimes/**",
                    "/logs/**",
                    "/tmp/**",
                    "/memories/**",
                ],
                mode="allow",
            ),
            FilesystemPermission(
                operations=["write"],
                paths=["/reviews/**", "/tmp/**"],
                mode="allow",
            ),
            FilesystemPermission(
                operations=["write"],
                paths=[
                    "/projects/**",
                    "/skills/**",
                    "/policies/**",
                    "/runtimes/**",
                    "/logs/**",
                ],
                mode="deny",
            ),
            FilesystemPermission(
                operations=["read", "write"],
                paths=["/**"],
                mode="deny",
            ),
        ],
    }


def _code_reviewer_subagent(model: BaseChatModel) -> SubAgent:
    """构建只读代码审查子 Agent。

    Reviewer 子 Agent 的职责是读取规则、读取 GitHub PR 上下文、分析 diff、
    记录结构化 finding，并输出中文审查报告。第一版不允许它修改 `/projects`
    中的源码，也不允许提交、push 或创建 PR，避免“审查”和“修复”职责混在一起。
    """

    return {
        "name": "code_reviewer",
        "description": (
            "用于审查 GitHub Pull Request 或本地分支 diff 的子 Agent。"
            "它会读取审查规则、PR 上下文和变更文件，记录结构化 finding，"
            "最后输出中文审查报告。"
        ),
        "system_prompt": (
            "你是 LQ-AICODING 的代码审查子 Agent，只负责 review，不负责修改代码。\n"
            "你必须使用中文输出，代码标识符、路径、命令和 API 名称可以保留英文。\n"
            "审查流程：\n"
            "1. 按 code-review skill 先用 read_file 读取工作区规则和仓库规则；读不到时调用 load_default_review_rules。\n"
            "2. 如果用户提供 GitHub PR 编号，调用 get_github_pull_request_context 读取 PR 详情、提交、文件和评论。\n"
            "3. 调用 get_review_diff_summary 获取本地 diff 摘要和变更行号。\n"
            "4. 只记录会导致真实风险的问题，不记录纯风格偏好。\n"
            "5. finding 必须包含 file、line、severity、title、description。\n"
            "6. 记录 finding 前，尽量确认文件属于本次 diff；无法确认行号时使用文件级 finding。\n"
            "7. 使用 add_review_finding 保存结构化发现，再用 list_review_findings 汇总。\n"
            "8. 最终报告必须包含结论、阻塞问题、高风险问题、一般建议和测试建议。\n"
            "9. 不要修改文件、不要提交、不要 push、不要创建 Pull Request。\n"
        ),
        "model": model,
        "tools": [
            get_github_pull_request_context,
            load_default_review_rules,
            get_review_diff_summary,
            validate_review_finding_location,
            add_review_finding,
            list_review_findings,
        ],
        "skills": ["/skills/"],
        "permissions": [
            FilesystemPermission(
                operations=["read"],
                paths=[
                    "/projects/**",
                    "/skills/**",
                    "/policies/**",
                    "/reviews/**",
                    "/memories/**",
                    "/tmp/**",
                ],
                mode="allow",
            ),
            FilesystemPermission(
                operations=["write"],
                paths=["/reviews/**", "/tmp/**"],
                mode="allow",
            ),
            FilesystemPermission(
                operations=["write"],
                paths=["/projects/**", "/skills/**", "/policies/**", "/memories/**"],
                mode="deny",
            ),
            FilesystemPermission(
                operations=["read", "write"],
                paths=["/**"],
                mode="deny",
            ),
        ],
    }


def _agent_filesystem_permissions(task_kind: TaskKind) -> list[FilesystemPermission]:
    """主 Agent 的文件系统权限。

    只有 coding 主 Agent 可以修改 `/projects`；所有任务都只能把临时产物写入
    `/reviews` 和 `/tmp`。`/memories` 始终只读，由 runtime 在任务成功后统一写回。
    最终本地边界仍由 LocalShellBackend 做 macOS/Windows 路径校验与写入保护。
    """

    return [
        FilesystemPermission(
            operations=["read"],
            paths=[
                "/projects/**",
                "/skills/**",
                "/policies/**",
                "/reviews/**",
                "/runtimes/**",
                "/logs/**",
                "/tmp/**",
                "/memories/**",
            ],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=(
                ["/projects/**", "/reviews/**", "/tmp/**"]
                if task_kind == "coding"
                else ["/reviews/**", "/tmp/**"]
            ),
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=[
                "/projects/**",
                "/skills/**",
                "/policies/**",
                "/runtimes/**",
                "/logs/**",
                "/memories/**",
            ],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        ),
    ]

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

    # 如果没有 thread_id 或不是执行态，返回空 Agent
    if not isinstance(thread_id, str) or not thread_id or not graph_loaded_for_execution(config):
        logger.info("没有 thread_id 或不是执行态，返回空 Agent")
        return create_deep_agent(system_prompt='', tools=[]).with_config(config)

    # Agent 每轮重新构建，但同一会话复用 LocalShellBackend，避免重复初始化工作区。
    backend = ensure_backend_for_thread(thread_id)

    # 从配置读取任务类型，任务类型同时决定系统提示词和允许注册的外部写工具。
    task_kind = _task_kind_from_config(configurable)

    # 除 coding 外都启用文件只读模式。sync 仍可执行后端白名单中的 clone/fetch/pull，
    # 但不能借文件工具修改业务源码；同一 thread 从 planning 切到 coding 时会重新解除只读。
    backend.read_only = task_kind != "coding"
    repo_url = configurable.get("repo_url")

    # 预先初始化并读取仓库记忆，再交给中间件注入，避免一次请求重复查询 Store。
    # server.py 在创建 Agent 之前，已经顺手读到了当前仓库记忆内容；
    # 那就把内容放进 config，后面的 ContextInjectionMiddleware 直接用，
    # 避免 middleware 再查一次 LangGraph Store。
    agent_backend, memory_paths, repo_memory_content = _prepare_repo_backend_context(
        repo_url=repo_url,
        backend=backend,
    )

    # 如果有仓库记忆内容则注入
    if repo_memory_content:
        configurable["_repo_memory_content"] = repo_memory_content

    def backend_factory(_runtime: object, _thread_id: str = thread_id) -> Any:
        # DeepAgents 0.6.x 直接传入带命令执行能力的 backend 时，和 permissions
        # 组合仍有兼容限制；因此这里保留 factory 形式，同时返回已经准备好的
        # thread 级 backend。Agent 实例可以按请求重建，backend 和工作区继续复用。
        return agent_backend

    # PR 创建和评论属于远端写操作，只向 coding Agent 注册。
    tools = [
        fetch_url, web_search, add_review_finding, list_review_findings,
        get_github_pull_request_context, load_default_review_rules,
        get_review_diff_summary, validate_review_finding_location,
    ]
    if task_kind == "coding":
        # coding 任务注册 PR 创建和评论工具
        tools.extend([open_github_pull_request, publish_github_pr_comment])

    # 主 Agent 与子 Agent 共用同一模型客户端，避免重复初始化连接与配置。
    main_model = make_main_model()

    # 顺序代表调用链：先清洗模型历史和注入上下文，再校验工具输入并兜底工具异常。
    middleware = [
        MessageSanitizeMiddleware(),  # 清洗模型历史和用户输入中的无效内容
        ContextInjectionMiddleware(),  # 注入仓库记忆内容
        SanitizeToolInputsMiddleware(backend=backend),  # 校验工具输入
        create_summarization_tool_middleware(main_model, agent_backend),
        ModelCallLimitMiddleware(run_limit=MODEL_CALL_RECURSION_LIMIT, exit_behavior="end"),
        ToolErrorMiddleware(backend=backend),  # 统一工具异常处理
    ]

    logger.info("返回带 backend 的 Agent：thread_id=%s task_kind=%s", thread_id, task_kind)

    # 创建 DeepAgent
    return create_deep_agent(
        model=main_model,  # 使用主模型
        tools=tools,  # 注册工具
        system_prompt=get_system_prompt(task_kind),  # 使用任务类型对应的系统提示词
        subagents=[_general_purpose_subagent(main_model), _code_reviewer_subagent(main_model)], # 注册子 Agent
        middleware=middleware,  # 使用中间件
        backend=backend_factory,  # 使用仓库后端
        permissions=_agent_filesystem_permissions(task_kind),  # 主 Agent 的文件权限
        skills=["/skills/"],  # 从工作区加载内置和用户扩展的 skills
        # 使用仓库记忆
        # memory 只声明 Agent 可以访问的长期记忆文件路径；
        # 具体读写会通过上面的 /memories/ StoreBackend 路由完成。
        memory=memory_paths,
        # 使用检查点
        # checkpointer 是聊天历史、工具消息和图状态的权威来源。
        # 前端历史恢复应读取 checkpoint，不应读取业务 Store 事件。
        checkpointer=get_checkpointer(),
        store=get_langgraph_store(),  # 使用 LangGraph Store
    ).with_config(config)
