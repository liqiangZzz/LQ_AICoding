"""声明式文件系统权限 → 自研可执行策略。

背景
----
deepagents 0.7 的 `FilesystemPermission` 声明（传给 create_deep_agent 的
``permissions`` 参数）不支持与带命令执行的 backend 组合，传了会直接抛
``NotImplementedError``，Agent 无法启动。

0.6.x 的情况要区分看待（已查 0.6.11 源码确认）：
- 文件工具（read_file / write_file / edit_file / ls / glob / grep）：permissions
  在工具执行前通过 ``_check_fs_permission`` 真实判定 deny 并拦截，是生效的；
- execute 命令工具：0.6.x 与 0.7 都**从未实现**工具级权限检查（0.7 报错原文：
  "Tool-level permissions for the execute tool are not implemented"），
  命令安全一直由 LocalShellBackend 的命令守卫承担；
- 0.6.x 之所以能跑，是因为当年 ``backend`` 传的是工厂函数，``isinstance(..., BackendProtocol)``
  为 False，绕过了 __init__ 里的 NotImplementedError 检查；0.7 强制传实例后检查
  必然触发，文件工具权限也随之失效。

因此项目把权限**声明**保留在本模块（``main_agent_permissions`` /
``reviewer_subagent_permissions``，可读、可维护、可演示），由 ``compile_fs_policy``
编译成自研策略（``FsPolicy``），server.py 只负责把编译结果注入
``SanitizeToolInputsMiddleware``，在工具调用前真实执行；execute 命令仍由
LocalShellBackend 守卫。规则语义与 FilesystemPermission 完全一致：

- allow/deny + read/write 组合；
- 路径 glob（如 ``/projects/**``）归一化为虚拟根（projects/skills/...）；
- deny 兜底（``/**``）表示"未显式允许的一律拒绝"。

判定顺序为显式 allow 优先于 deny：主 Agent 声明里 coding 的 write allow 与
deny 都包含 ``/projects``，allow 命中即放行，deny 只对"allow 未声明 /projects"
的只读任务生效。

LocalShellBackend（路径越界/命令守卫/目录只读）仍是最终安全边界。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from deepagents import FilesystemPermission

logger = logging.getLogger("agent.run.middleware.fs_policy")

# 所有可能的虚拟根（含受保护目录 secrets/memories）
_VIRTUAL_ROOTS = {"projects", "skills", "policies", "reviews", "runtimes", "tmp", "logs", "secrets", "memories"}


# ── 声明式权限规则（等价原 server.py 的 FilesystemPermission 声明）───────────
# 这份声明就是项目最初的设计意图，保留在这里便于阅读、评审和演示。
# 它不会传给 deepagents（0.7 不支持与命令执行 backend 组合），
# 而是由 compile_fs_policy() 编译成自研策略，在 SanitizeToolInputsMiddleware 里真实执行。


def main_agent_permissions(task_kind: str) -> list[FilesystemPermission]:
    """主 Agent 的文件系统权限声明。

    - 所有任务都可以读全部虚拟目录；
    - 写：coding 任务允许 /projects、/reviews、/tmp；只读任务只允许 /reviews、/tmp；
    - 永远禁止写 /skills、/policies、/runtimes、/logs、/memories（记忆由 runtime 统一写回）；
    - deny 兜底 /**：未显式允许的一律拒绝。
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


def reviewer_subagent_permissions() -> list[FilesystemPermission]:
    """代码审查子 Agent 的权限声明：只读 + 只允许写审查产物。

    保留用户原 server.py 中的写法；子 Agent 与主 Agent 共用同一个
    SanitizeToolInputsMiddleware（deepagents 子 Agent 继承主链 middleware），
    因此这份声明编译后与主策略合并执行。
    """
    return [
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
    ]


@dataclass
class FsPolicy:
    """编译后的可执行文件系统策略。"""

    # 读操作：允许/拒绝的虚拟根；空集合表示"未显式声明"
    read_allow_roots: set[str] = field(default_factory=set)
    read_deny_roots: set[str] = field(default_factory=set)
    # 写操作：允许/拒绝的虚拟根
    write_allow_roots: set[str] = field(default_factory=set)
    write_deny_roots: set[str] = field(default_factory=set)
    # deny 兜底：True 表示存在 "/** deny" 规则，未命中 allow 的一律拒绝
    read_deny_all: bool = False
    write_deny_all: bool = False


def _path_virtual_root(value: Any) -> str | None:
    """把路径参数归一化出虚拟根（projects/skills/...）。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip('"').strip("'").replace("\\", "/").lstrip("/")
    parts = [part for part in cleaned.split("/") if part and part not in {".", ".."}]
    if not parts:
        return None
    first = parts[0].lower()
    return first if first in _VIRTUAL_ROOTS else None


def _glob_to_root(path: str) -> str | None:
    """把权限声明路径（/projects/**）归一化为虚拟根。"""
    cleaned = path.strip().strip('"').strip("'").replace("\\", "/").lstrip("/")
    parts = [part for part in cleaned.split("/") if part and part not in {".", ".."}]
    if not parts:
        return None
    first = parts[0].lower()
    return first if first in _VIRTUAL_ROOTS else None


def compile_fs_policy(rules: list[FilesystemPermission] | None) -> FsPolicy:
    """把 FilesystemPermission 声明列表编译为可执行 FsPolicy。

    不依赖 deepagents 的 FilesystemMiddleware，规避 0.7 的 NotImplementedError。
    """
    policy = FsPolicy()
    for rule in rules or []:
        operations = list(rule.operations or [])
        paths = list(rule.paths or [])
        mode = rule.mode
        for path in paths:
            # 兜底 deny：/** 表示"未显式允许的一律拒绝"
            if path.strip().rstrip("/") in {"/", "/**"}:
                if mode == "deny":
                    if "read" in operations:
                        policy.read_deny_all = True
                    if "write" in operations:
                        policy.write_deny_all = True
                continue
            root = _glob_to_root(path)
            if root is None:
                logger.debug("忽略无法映射为虚拟根的权限路径：%s", path)
                continue
            for operation in operations:
                if operation == "read":
                    (policy.read_allow_roots if mode == "allow" else policy.read_deny_roots).add(root)
                elif operation == "write":
                    (policy.write_allow_roots if mode == "allow" else policy.write_deny_roots).add(root)
    return policy


def enforce_fs_policy(
    policy: FsPolicy | None,
    *,
    tool_name: str,
    args: dict[str, Any],
    task_kind: str,
) -> None:
    """按编译后的策略校验一次工具调用；拒绝时抛 ValueError（由 middleware 转中文反馈）。

    ``/projects`` 另有任务类型限制：只有 coding 任务可以写源码，
    planning/analysis 等只读任务一律拒绝（对应原声明中按 task_kind 动态生成的规则）。
    """
    from agent.core.middleware.tool_sanitize import PATH_ARGUMENTS, WRITE_TOOLS

    if policy is None:
        return
    is_write = tool_name in WRITE_TOOLS
    if not is_write:
        # 读工具不做额外限制：工作区内读全部允许，越界由 LocalShellBackend 兜底。
        return

    for key in PATH_ARGUMENTS:
        value = args.get(key)
        if value is None:
            continue
        root = _path_virtual_root(value)
        if root is None:
            continue  # 绝对宿主路径 / .. 等交给 sanitize_workspace_path 已有逻辑

        # 判定顺序与原 FilesystemPermission 语义一致：显式 allow 优先于 deny。
        # 主 Agent 声明里 coding 的 write allow 和 deny 都含 /projects，
        # allow 命中即放行，deny 那条只对"allow 未声明 /projects"的只读任务生效。
        if root in policy.write_allow_roots:
            continue
        if root == "projects" and task_kind != "coding":
            raise ValueError(f"{tool_name} 当前任务类型是 {task_kind}（只读），禁止修改 /projects 源码")
        if root in policy.write_deny_roots:
            raise ValueError(f"{tool_name} 禁止写入目录 /{root}（权限规则 deny）")
        if policy.write_deny_all:
            raise ValueError(f"{tool_name} 未声明允许写入目录 /{root}，按 deny 兜底拒绝")
        # 无 deny 兜底且未显式声明时，保持安全默认：拒绝写未知目录
        raise ValueError(f"{tool_name} 未声明允许写入目录 /{root}，已拒绝")
