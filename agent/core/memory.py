from __future__ import annotations

import logging

from agent.core.settings import PROJECT_ROOT

logger = logging.getLogger("agent.memory")

# 内存文件路径
MEMORY_DIR = PROJECT_ROOT / "agent" / "memory"
# 工作区记忆文件路径
WORKSPACE_MEMORY_PATH = MEMORY_DIR / "workspace.md"


def load_workspace_memory() -> str:
    """读取本地工作区长期记忆。

    这份记忆描述 `E:\ai_workspace` 下各个固定目录的用途。
    它不依赖 SQLite checkpoint，进程重启后仍然会被注入系统提示词，
    让 Agent 在每一轮任务开始时都知道哪些目录是仓库、运行环境、规范、
    日志或敏感凭据目录。
    """

    try:
        return WORKSPACE_MEMORY_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning("工作区记忆文件不存在：%s", WORKSPACE_MEMORY_PATH)
        return ""
