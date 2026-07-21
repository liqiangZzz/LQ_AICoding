"""后端安全校验模块。

这个文件提供的是 `LocalShellBackend` 之外的通用安全函数，主要覆盖三类风险：

1. 路径风险：
   - 防止 Agent 通过绝对路径或 `..` 访问工作区外文件。
2. 命令风险：
   - 限制模型只能执行少量项目需要的命令族。
   - 拦截管道、重定向、删除、关机、注册表等危险操作。
3. Git 参数风险：
   - 分支名、提交信息来自模型输出，必须在进入 shell 前做归一化和校验。

强调：
这里不是完整的企业沙箱，只是课程版本地 Windows/macOS backend 的安全收敛层。
真正生产环境还应结合容器、系统权限、审计、网络隔离和更严格的命令执行策略。
"""
import re
from pathlib import Path


# ── 路径权限 ──────────────────────────────────────────────────
class WorkspacePermissionError(PermissionError):
    """
    工作区或命令权限错误。

    继承自 PermissionError 的好处是：
    1. 调用方可以按标准权限异常处理逻辑进行统一处理
    2. middleware 能识别这是可恢复的安全拒绝，而不是系统崩溃。
    3. 错误语义比普通 'ValueError' 更明确，调用方可以更精确地判断错误类型。
    """


def assert_path_inside(path: Path, root: Path) -> Path:
    """
    确认 path 解析后仍位于 root 工作区内

    Args:
        path: 待检验路径，可以是相对路径和绝对路径
        root: 工作区根目录

    Returns:
        Path: 解析后的绝对呢路径

    Raises:
        WorkspacePermissionError: 如果 path 解析后位于 root 工作区外，则抛出此异常

    这是文件访问的基础安全边界，防止模型使用 '..' 、盘符绝对路径等方式读取或写入工作区域以外的文件。
    """

    #  resolve() 方法会将路径转换为绝对路径，并规范化路径中的 .. 和 .
    #  例如 /a/b/../c 会转换为 /a/c
    resolved_root = root.resolve()
    resolved_path = path.resolve()

    if resolved_path == resolved_root or resolved_root in resolved_path.parents:
        return resolved_path
    raise WorkspacePermissionError(f"Path is outside workspace: {resolved_path}")


# ── 命令权限 ──────────────────────────────────────────────────
def normalize_safe_command(command: str) -> str:
    """校验 Agent 准备执行的本地命令。

    白名单同时包含 Windows 与 macOS 的常见等价命令；进入真正的 shell 前，
    `LocalShellBackend` 会再根据当前操作系统转换命令名称。

    模型经常会在命令末尾追加 `2>&1` 或 `| tail -5`。
    前者是为了合并 stderr，后者是 Unix 查看末尾输出的习惯。
    课程版在 Python 中已经捕获 stdout/stderr，也会把完整输出返回给模型，
    所以这里剥离这两个尾部片段，既兼容模型习惯，又不放开任意管道/重定向能力。
    """

    normalized = command.strip()
    normalized = re.sub(r"\s+\|\s*tail\s+-?\d+\s*$", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+2>&1\s*$", "", normalized)
    lowered = normalized.lower()
    first_word = normalized.split(maxsplit=1)[0].lower() if normalized else ""
    # 课程版只放开和 Python/Git 项目验证相关的命令。
    # 如果未来要允许 npm、mvn、gradle 等命令，应在这里明确加白名单，
    # 同时补充对应的安全测试，而不是直接放开任意 shell。
    allowed_commands = {
        # 两端通用开发命令
        "git", "pytest", "ruff", "java", "javac",
        # Python 在不同系统和安装方式下可能使用的名称
        "python", "python3", "py", "pip", "pip3",
        # Windows cmd.exe 常用命令
        "dir", "type", "where", "cls",
        # macOS/Unix 常用命令
        "ls", "cat", "which", "pwd", "clear", "test",
    }
    if first_word not in allowed_commands:
        raise WorkspacePermissionError(f"Command is not allowed: {command}")

    # shell 操作符会显著扩大命令能力，例如管道、重定向、命令拼接、命令替换。
    # 课程版不允许模型组合复杂 shell 片段，避免绕过上面的命令白名单。
    shell_operators = [
        "&&",
        "||",
        "|",
        "&",
        ";",
        ">",
        "<",
        "`",
        "$(",
        "\n",
        "\r",
    ]
    if any(operator in normalized for operator in shell_operators):
        raise WorkspacePermissionError(f"Blocked shell operator in command: {command}")
    # 这些危险片段即使出现在白名单命令后面，也应直接拒绝。
    # 例如模型生成 `python -c "import os; os.system('del ...')"` 时，仍需要额外防线。
    blocked = [
        "format ",
        "shutdown",
        "restart-computer",
        "remove-item",
        "remove-item -recurse",
        "rm -rf",
        "reg delete",
        "del ",
        "del /s",
        "rmdir ",
        "rmdir /s",
        "cipher /w",
    ]
    if any(token in lowered for token in blocked):
        raise WorkspacePermissionError(f"Blocked dangerous command: {command}")
    return normalized
