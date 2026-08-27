"""把 Agent 最终回答提炼为可复用的仓库长期记忆。

本模块不调用模型，而是用保守规则抽取技术栈、测试命令、关键文件和最近结论。
这样既避免额外模型成本，也避免把完整对话、临时错误或敏感信息写入长期记忆。
完整数据流见同目录 `repo_memory_说明.md`。
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.store.base import BaseStore

from agent.core.repo_memory import (
    build_repo_memory_namespace,
    get_repo_memory_item,
    repo_memory_store_key,
)
from agent.tools.github_api import GitHubRepo, mask_token

logger = logging.getLogger("agent.run.repo_memory_update")

MAX_RECENT_ITEMS = 20
MAX_FACT_CHARS = 350

# 敏感标记
SENSITIVE_MARKERS = (".env", ".secrets", "api_key", "apikey", "private key", "私钥")


@dataclass(frozen=True)
class RepoMemoryUpdate:
    """一次仓库记忆更新所需的稳定事实"""

    task_kind: str
    final_text: str
    branch_name: str | None = None
    pr_url: str | None = None


def _contains_sensitive_text(text: str) -> bool:
    """判断文本是否包含不应写入长期记忆的敏感标记。"""

    lowered = text.lower()
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def _compact_line(text: str, *, limit: int = MAX_FACT_CHARS) -> str:
    """把最终回答压缩成适合写入'最近结论' 的单行文本。"""

    # 去除敏感标记
    compacted = " ".join(mask_token(text).split())
    # 截断
    if len(compacted) > limit:
        compacted = compacted[:limit].rstrip() + "..."
    return compacted


def _extract_bullets(text: str, *, keywords: tuple[str, ...], limit: int = 8) -> list[str]:
    """从最终回答中提取包含关键词的列表项。

    这里只做规则提取，不调用模型。它适合抓取 Agent 最终总结里的测试命令、
    关键文件、已完成能力等稳定信息。
    """
    results: list[str] = []
    for raw_line in text.splitlines():
        # 必须是列表项
        if not re.match(r"^\s*(?:[-*]|\d+[.)、])\s+", raw_line):
            continue

        # 去除行首的空格和制表符
        line = raw_line.strip()
        if not line:
            continue

        # 去除列表项符号
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+[.)、]\s*", "", line)
        # 去除行首的注释符号
        if line.startswith(("#", "|", "```")):
            continue

        #  文本存在并且 判断文本是否包含不应写入长期记忆的敏感标记
        if not line or _contains_sensitive_text(line):
            continue

        # 转换为小写
        lowered = line.lower()
        # 必须包含关键词
        if any(keyword.lower() in lowered for keyword in keywords):
            # 去除敏感标记
            clean = mask_token(line)
            if clean not in results:
                results.append(clean)
        if len(results) >= limit:
            break
    return results


def _extract_code_items(text: str, *, suffixes: tuple[str, ...], limit: int = 10) -> list[str]:
    """从 Markdown 反引号内容中提取文件名或命令"""

    results: list[str] = []
    # 提取反引号内容
    for item in re.findall(r"`([^`]+)`", text):
        # 去除行首和行尾的空格和制表符
        clean = item.strip()
        # 文本存在并且 判断文本是否包含不应写入长期记忆的敏感标记
        if not clean or _contains_sensitive_text(clean):
            continue
        # 文件名或命令
        if (any(clean.endswith(suffix) for suffix in suffixes) or any(
                suffix in clean for suffix in suffixes)) and clean not in results:
            results.append(mask_token(clean))
        if len(results) >= limit:
            break
    return results


def _extract_test_commands(text: str, *, limit: int = 5) -> list[str]:
    """提取明确可执行的测试命令，避免把 git log 或说明文字误当命令。"""

    results: list[str] = []
    # 提取反引号内容
    for item in re.findall(r"`([^`]+)`", text):
        # 去除行首和行尾的空格和制表符
        clean = item.strip()
        # 文本存在并且 判断文本是否包含不应写入长期记忆的敏感标记
        lowered = clean.lower()
        # 命令以 python -m pytest 或 pytest 开头
        if lowered.startswith(("python -m pytest", "pytest")) and not _contains_sensitive_text(clean):
            if clean not in results:
                results.append(mask_token(clean))
        if len(results) >= limit:
            return results

    # 提取行内内容
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.lower()

        # 命令以 python -m pytest 或 pytest 开头
        if lowered.startswith(("python -m pytest", "pytest")) and not _contains_sensitive_text(line):
            clean = mask_token(line)
            if clean not in results:
                results.append(clean)
        if len(results) >= limit:
            break

    # 提取行内内容
    if len(results) < limit:
        # 提取行内内容
        # 命令以 python -m pytest 或 pytest 开头
        for match in re.findall(
                r"(?:python\s+-m\s+pytest|pytest)(?:\s+[A-Za-z0-9_./\\:-]+)?",
                text,
                flags=re.I,
        ):
            clean = " ".join(match.strip().split())

            #  判断文本是否包含不应写入长期记忆的敏感标记
            if clean and clean not in results and not _contains_sensitive_text(clean):
                results.append(mask_token(clean))
            if len(results) >= limit:
                break
    return results


def _extract_file_names(text: str, *, limit: int = 10) -> list[str]:
    """从普通说明文字中提取常见项目文件名"""

    results: list[str] = []
    for match in re.findall(
            r"[\w.-]+\.(?:py|md|txt|html|json|toml)", text, flags=re.I
    ):
        clean = match.strip()

        #  判断文本是否包含不应写入长期记忆的敏感标记
        if clean and clean not in results and not _contains_sensitive_text(clean):
            results.append(mask_token(clean))
        if len(results) >= limit:
            break
    return results


def _detect_stack(text: str) -> list[str]:
    """从最终回答中识别常见技术栈关键词。"""

    candidates = {
        "FastAPI": ("fastapi",),
        "SQLite": ("sqlite",),
        "pytest": ("pytest",),
        "uvicorn": ("uvicorn",),
        "JWT": ("jwt", "python-jose"),
        "passlib/bcrypt": ("passlib", "bcrypt"),
    }
    # 转换为小写
    lowered = text.lower()
    # 识别关键词
    return [name for name, markers in candidates.items() if any(marker in lowered for marker in markers)]


def _replace_section(memory: str, heading: str, items: list[str]) -> str:
    """用列表项替换 Markdown 二级标题下的内容"""

    if not items:
        return memory
    # 构建列表项
    section = f"{heading}\n" + "\n".join(f"- {item}" for item in items) + "\n\n"
    # 构建正则表达式模式
    pattern = re.compile(
        rf"(^## {re.escape(heading.removeprefix('## '))}\n)(.*?)(?=^## |\Z)",
        re.M | re.S,
    )
    # 替换内容
    if pattern.search(memory):
        return pattern.sub(section, memory, count=1)
    return f"{memory.rstrip()}\n\n{section}"


def _append_recent(memory: str, *, task_kind: str, fact: str) -> str:
    """追加“最近结论”，并限制条数。"""

    # 空内容或判断内容是否包含不应写入长期记忆的敏感标记
    if not fact or _contains_sensitive_text(fact):
        return memory

    # 构建列表项
    entry = f"- {datetime.now(UTC).date().isoformat()} ({task_kind}) : {fact}"
    heading = "## 最近结论"
    # 列表项已存在
    if entry in memory:
        return memory
    # 二级标题不存在
    if heading not in memory:
        return f"{memory.rstrip()}\n\n{heading}\n{entry}\n"

    # 分割二级标题下的内容
    before, after = memory.split(heading, 1)
    lines = []

    # 过滤掉空行和注释行
    for line in after.strip().splitlines():
        stripped = line.strip()
        if not stripped or stripped == "- 暂无":
            continue

        # 过滤掉空行和注释行
        # 早期版本可能把整段 Markdown、表格或代码块塞进最近结论。
        # 长期记忆只保留短事实，避免下一轮上下文被历史噪声污染。
        if len(stripped) > 520 or "```" in stripped or "| 项目 |" in stripped:
            continue
        lines.append(stripped)

    # 限制条数
    recent_items = [entry, *lines][:MAX_RECENT_ITEMS]
    # 构建列表项
    return f"{before.rstrip()}\n\n{heading}\n" + "\n".join(recent_items) + "\n"


def _metadata_items(*, branch_name: str | None, pr_url: str | None) -> list[str]:
    """整理分支和 PR 这类线程元数据。"""

    items: list[str] = []
    if branch_name:
        items.append(f"最近分支：`{branch_name}`")
    if pr_url:
        items.append(f"最近 Pull Request：{pr_url}")
    return items


def build_updated_repo_memory(memory: str, update: RepoMemoryUpdate) -> str:
    """根据任务最终输出生成更新后的仓库记忆正文。

    处理顺序刻意保持固定：先统一脱敏并执行敏感内容熔断，再分别抽取稳定字段，
    最后追加有数量上限的最近结论。任何一步没有可信结果时都保留原章节。
    """

    text = mask_token(update.final_text or "")
    # 空内容或判断内容是否包含不应写入长期记忆的敏感标记
    if not text.strip() or _contains_sensitive_text(text):
        return memory

    # 第一阶段：提取可覆盖固定章节的稳定事实。
    stack = _detect_stack(text)
    # 提取测试命令
    test_commands = _extract_test_commands(text, limit=5)
    # 提取关键文件
    key_files = _extract_code_items(text, suffixes=(".py", ".md", ".txt", ".html", ".json", ".toml"), limit=10)
    # 补充关键文件
    for file_name in _extract_file_names(text, limit=10):
        if file_name not in key_files:
            key_files.append(file_name)
        if len(key_files) >= 10:
            break

    # 第二阶段：从面向用户的列表项中提取已经完成的能力。
    completed_features = _extract_bullets(
        text,
        keywords=("接口", "新增", "完成", "实现", "测试通过", "passed"),
        limit=8,
    )
    # 过滤掉一些无关内容
    completed_features = [
        item
        for item in completed_features
        if not any(skip in item for skip in ("分支", "Pull Request", " PR", "Fast-forward", "merge"))
    ]

    # 第三阶段：只替换成功提取到内容的章节，避免用空结果覆盖已有记忆。
    updated = memory

    # 替换或追加内容
    if stack:
        updated = _replace_section(updated, "## 技术栈", stack)
    # 替换或追加内容
    if test_commands:
        updated = _replace_section(updated, "## 测试命令", test_commands)
    # 替换或追加内容
    if key_files:
        updated = _replace_section(updated, "## 关键文件", key_files)

    # 构建元数据
    metadata = _metadata_items(branch_name=update.branch_name, pr_url=update.pr_url)
    # 替换或追加内容
    if metadata:
        updated = _replace_section(updated, "## 分支与 PR", metadata)
    # 替换或追加内容
    if completed_features:
        updated = _replace_section(updated, "## 已完成能力", completed_features)

    # 最近结论采用追加并限长的策略，保留近期上下文但避免记忆无限增长。
    return _append_recent(updated, task_kind=update.task_kind, fact=_compact_line(text))


def update_repo_memory_from_text(
        *,
        store: BaseStore,
        repo: GitHubRepo,
        update: RepoMemoryUpdate,
) -> bool:
    """把任务最终输出写回仓库级长期记忆。

    返回值表示是否真的修改了记忆文件。调用方可以据此记录日志或前端事件。
    """

    namespace = build_repo_memory_namespace(repo.owner, repo.repo)
    item = get_repo_memory_item(store, namespace)
    if item is None:
        logger.info("仓库记忆不存在，跳过结构化更新：repo=%s/%s", repo.owner, repo.repo)
        return False

    current = str(item.value.get("content") or "")
    # 构建更新后的内容
    updated = build_updated_repo_memory(current, update)
    if updated == current:
        logger.info("仓库记忆无新增稳定结论：repo=%s/%s task_kind=%s", repo.owner, repo.repo, update.task_kind)
        return False

    value = dict(item.value)
    value["content"] = updated
    store.put(namespace, repo_memory_store_key(repo.owner, repo.repo), value)
    logger.info("仓库记忆已结构化更新：repo=%s/%s task_kind=%s", repo.owner, repo.repo, update.task_kind)
    return True
