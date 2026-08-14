"""受控的本地 Shell 后端。

本模块在 macOS 和 Windows 上对 DeepAgents 暴露统一的虚拟文件路径和命令执行接口。
业务工具通过 `/projects` 等虚拟路径访问受控工作区。
详细目录、命令和跨平台设计见同目录 `local_shell_说明.md`。
"""

import fnmatch
import hashlib
import locale
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from agent.backends.permissions import normalize_safe_command
from agent.backends.workspace import Workspace
from agent.core.settings import PROJECT_ROOT, WORKSPACE_ROOT
from agent.env_utils import get_env
from agent.platform_utils import (
    host_platform,
    platform_display_name,
    resolve_local_shell_platform,
)

logger = logging.getLogger('agent.run.shell')

# ── 虚拟路径与安全匹配规则 ────────────────────────────────────
# Agent 内部通过 /projects、/skills 等虚拟路径访问文件，
# 这里定义所有虚拟路径的映射关系。
VIRTUAL_PROJECTS = "/projects"
VIRTUAL_SKILLS = "/skills"
VIRTUAL_POLICIES = "/policies"
VIRTUAL_REVIEWS = "/reviews"
VIRTUAL_RUNTIMES = "/runtimes"
VIRTUAL_TMP = "/tmp"
VIRTUAL_LOGS = "/logs"

# 匹配 POSIX 绝对路径，同时避开 https:// 这类 URL。
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9:/])(/[^\s\"';&|<>]+)")
# 匹配 Windows 盘符绝对路径，如 C:\\workspace\\repo。
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]:[\\/][^\s\"';&|<>]+)")
# 匹配命令中的虚拟路径（如 /projects/my-repo），用于自动转换为真实路径
_VIRTUAL_ROOT_NAMES = ("projects", "skills", "policies", "reviews", "runtimes", "tmp", "logs")
_VIRTUAL_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9])/({'|'.join(_VIRTUAL_ROOT_NAMES)})(/[^\s\"';&|<>]*)?"
)
# 危险命令黑名单 —— 匹配到的命令直接拒绝执行
_DANGEROUS_PATTERNS = (
    r"\bformat\b",
    r"\bshutdown\b",
)


def _resolve_configured_subpath(root: Path, value: str, *, env_name: str) -> Path:
    """将配置的工作区子路径安全解析到 root 内。

    同时按 POSIX 和 Windows 语义识别绝对路径，防止在 macOS 上漏掉
    ``C:/...``，或在 Windows 上漏掉反斜杠形式的路径穿越。
    """

    # 去除首尾空格，将反斜杠替换为正斜杠
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        raise ValueError(f"{env_name} cannot be empty")

    # 绝对路径检查
    if PurePosixPath(normalized).is_absolute() or PureWindowsPath(normalized).is_absolute():
        raise ValueError(f"{env_name} must be relative to LOCAL_SHELL_WORKSPACE")
    # 路径穿越检查
    parts = PurePosixPath(normalized).parts
    # 检查路径中是否包含 ..
    if ".." in parts:
        raise ValueError(f"{env_name} cannot escape LOCAL_SHELL_WORKSPACE")

    # 解析路径
    resolved = (root / Path(*parts)).resolve()
    try:
        # 检查解析后的路径是否在 root 内
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{env_name} cannot escape LOCAL_SHELL_WORKSPACE") from exc
    return resolved


def _mask_token(text: str) -> str:
    """
    把文本中的 GitHub Token 替换为 ***，防止泄露到日志和返回值里。
    """
    masked = text
    # 遍历所有可能的 Token 环境变量
    for token_name in ("GITHUB_TOKEN", "GH_TOKEN", "SCM_GITHUB_TOKEN"):
        # 获取环境变量值并去除首尾空格
        token = get_env(token_name).strip()
        # 如果环境变量值不为空
        if token:
            # 替换文本中的 Token 为 ***
            masked = re.sub(re.escape(token), "***", masked)
    return masked


@dataclass
class CommandResult:
    """
    命令执行结果，兼容旧版 Git 工具。
    """
    command: str # 命令
    stdout: str # 标准输出
    stderr: str # 错误输出
    exit_code: int # 退出码
    cwd: str # 当前工作目录


class LocalShellBackend(BaseSandbox):
    """
    macOS / Windows 本地 DeepAgents backend。

    核心职责：
    1. 实现 DeepAgents 文件协议：`ls/read/write/edit/glob/grep/upload/download`；
    2. 实现 `execute()`，负责平台命令适配、路径转换、超时和脱敏；
    3. 维护 `/projects`、`/skills`、`/runtimes` 等虚拟工作区；
    4. 通过 `GIT_ASKPASS` 实现 GitHub Git 非交互认证；
    5. 保留 `run/read_file/write_file/list_files` 旧接口。

    这个类不是“完全可信的 shell 代理”，而是这个项目里的受控本地执行层。
    真正企业生产环境还应叠加容器隔离、系统用户隔离、审计日志和更完整的权限策略。
    """

    def __init__(
            self,
            workspace: Workspace | str | os.PathLike[str] | None = None,
            *,
            timeout: int = 3600,
            read_only: bool = False,

    ) -> None:

        # 检查是否启用本地 shell 沙箱
        sandbox_type = os.environ.get("SANDBOX_TYPE", "local_shell").strip().lower()
        # 如果不是本地 shell 沙箱，则抛出异常
        if sandbox_type != "local_shell":
            raise ValueError(
                f"Unsupported SANDBOX_TYPE={sandbox_type!r}; current version only supports local_shell"
            )

        # 解析本地 shell 平台
        self.platform = resolve_local_shell_platform()
        # 获取本地 shell 平台显示名称
        self.platform_name = platform_display_name(self.platform)
        # 检查本地 shell 平台是否匹配主机平台
        actual_platform = host_platform()
        # 如果本地 shell 平台与主机平台不匹配，则抛出异常
        if self.platform != actual_platform:
            raise RuntimeError(
                f"LOCAL_SHELL_PLATFORM={self.platform} does not match host platform "
                f"{actual_platform}; use auto or select the current host platform"
            )

        # 工作区优先级：调用方传入 > 环境变量 > settings.py 默认值。
        if isinstance(workspace, Workspace):
            # 如果工作区是 Workspace 对象，则直接使用
            self.workspace = workspace
            # 配置的工作区根目录
            configured_root = workspace.root
        else:
            # 如果工作区不是 Workspace 对象，则创建一个
            configured_root = workspace or os.environ.get("LOCAL_SHELL_WORKSPACE") or str(WORKSPACE_ROOT)
            self.workspace = Workspace(Path(configured_root))

        # 配置的工作区根目录展开并解析
        self.root = Path(configured_root).expanduser().resolve()

        # 配置的工作区根目录
        # macOS 默认使用 UTF-8；Windows 默认跟随系统代码页，也允许显式覆盖。
        self.output_encoding = (
                os.environ.get("LOCAL_SHELL_OUTPUT_ENCODING", "").strip()
                or (locale.getpreferredencoding(False) if self.platform == "windows" else "utf-8")
        )

        # `/projects` 是 DeepAgents 对外约定的固定虚拟目录，不提供自定义子目录，
        # 避免文件工具、Shell 默认 cwd 和仓库映射对同一仓库产生不同路径。
        self.projects_dir = self.root / "projects"
        self.skills_dir = self.root / "skills"
        self.policies_dir = self.root / "policies"
        self.reviews_dir = self.root / "reviews"
        self.runtimes_dir = self.root / "runtimes"
        self.tmp_dir = self.root / "tmp"
        self.logs_dir = self.root / "logs"
        self.secrets_dir = self.root / "secrets"  # 存放 Git askpass 脚本等敏感文件
        # 共享 Python 虚拟环境目录
        self.shared_python_venv = _resolve_configured_subpath(
            self.root,
            # 默认值为 runtimes/python/default/.venv
            os.environ.get(
                "LOCAL_SHELL_SHARED_PYTHON_VENV",
                "runtimes/python/default/.venv",
            ),
            env_name="LOCAL_SHELL_SHARED_PYTHON_VENV",
        )
        self.default_timeout = timeout
        self.read_only = read_only
        # 命令安全守卫默认开启；兼容曾经使用过的旧变量名。
        guard_value = os.environ.get(
            "LOCAL_SHELL_ENABLE_COMMAND_GUARD",
            os.environ.get("LOCAL_SHELL_COMMAND_GUARD_ENABLED", "true"),
        )
        # 命令安全守卫默认开启
        self.command_guard_enabled = (
                guard_value.strip().lower() not in {"0", "false", "no"}
        )
        self._venv_error: str | None = None
        # 确保工作区布局
        self._ensure_layout()

    @property
    def id(self) -> str:
        """返回不暴露真实工作区路径的稳定 Sandbox 标识。"""

        # 使用工作区根目录的 SHA-256 值作为标识
        root_digest = hashlib.sha256(os.fsencode(self.root)).hexdigest()[:16]
        return f"local-shell-{root_digest}"

    # ── DeepAgents 命令协议 ────────────────────────────────────
    def execute(
            self,
            command: str,
            *,
            timeout: int | None = None,
    ) -> ExecuteResponse:
        """
        执行 DeepAgents 原生命令工具请求。

        处理顺序：
        1. 安全检查，拒绝危险命令和工作区外路径；
        2. 做命令预处理，将虚拟路径和跨平台命令转换为当前系统可执行形式；
        3. 通过 subprocess 执行并合并 stdout/stderr。

        注意：这个方法不向上抛异常，而是按 DeepAgents 协议返回结构化结果。
        模型可以根据 exit_code 和 output 继续修正命令或解释失败原因。

        Args:
            command: 要执行的命令。
            timeout : 命令执行超时时间，单位为秒。默认为 None，表示使用默认超时时间。

        Returns:
            ExecuteResponse: 命令执行结果，包含输出、退出码和截断标志。
        """

        if self.read_only and not self._read_only_command_allowed(command):
            return ExecuteResponse(
                output="当前任务是只读模式，该 Shell 命令不在允许列表中。",
                exit_code=126,
                truncated=False,
            )

        # 第一层安全守卫：在命令改写之前检查原始输入。
        if self.command_guard_enabled:
            # 拦截危险命令
            denied = self._deny_reason(command)
            if denied:
                return ExecuteResponse(output=f"命令被拒绝：{denied}", exit_code=126, truncated=False)
            try:
                # 命令改写
                command = normalize_safe_command(command)
            except PermissionError as exc:
                return ExecuteResponse(output=f"命令被拒绝：{exc}", exit_code=126, truncated=False)

        try:
            # 命令预处理也可能因为路径越界而失败，因此包含在结构化异常处理内。
            prepared_command = self._prepare_command(command)

            # shell=True 在 macOS 上使用 /bin/sh，在 Windows 上使用 COMSPEC（通常为 cmd.exe）。
            completed = subprocess.run(
                prepared_command,  # 最终执行的命令字符串，已经完成虚拟路径转换、Git 认证注入等预处理
                cwd=self.projects_dir,  # 子进程工作目录，默认限制在 projects 目录下执行
                capture_output=True,  # 捕获 stdout 和 stderr，避免命令输出直接写到后端进程控制台
                text=True,  # 以文本模式返回输出内容，而不是 bytes
                encoding=self.output_encoding,
                errors="replace",  # 遇到无法解码的字符时用替代字符处理，避免解码异常中断任务
                timeout=timeout or self.default_timeout,  # 单次命令执行超时时间，防止长时间阻塞 Agent 运行线程
                env=self._execution_env(),  # 子进程环境变量，包含 PATH、虚拟环境、Git 非交互认证等配置
                check=False,  # 不因非 0 退出码抛异常，保留 exit_code 交给 Agent 判断后续处理
                shell=True,  # 通过当前平台系统 shell 执行内置命令和 Git 命令
            )

            # 命令输出处理
            output = completed.stdout or ''
            # stderr 内容拼到 output 里，方便 LLM 统一查看
            if completed.stderr and completed.stderr.strip():
                stderr = completed.stderr.strip()
                output = f"{output}\n<stderr>{stderr}</stderr>" if output else stderr
            return ExecuteResponse(
                output=_mask_token(output.rstrip()),
                exit_code=completed.returncode,
                truncated=False,
            )
        except subprocess.TimeoutExpired as exc:
            # 命令超时处理
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            return ExecuteResponse(
                output=_mask_token(f"命令执行超时：{timeout or self.default_timeout} 秒\n{stdout}{stderr}".strip()),
                exit_code=124,
                truncated=False,
            )
        # 命令异常处理
        except Exception as exc:  # noqa: BLE001 - Sandbox 协议要求异常转换为结构化响应
            # 协议要求：所有异常都要转成结构化结果，不能抛出去
            return ExecuteResponse(output=f"命令执行失败：{_mask_token(str(exc))}", exit_code=1, truncated=False)

    # ── DeepAgents 文件协议 ────────────────────────────────────
    def ls(self, path: str) -> LsResult:
        """
        列出虚拟路径下文件和目录。path:  /projects/test_ject/
        """
        try:
            # 解析虚拟路径为实际路径
            resolved = self._resolve_virtual_path(path)
            if not resolved.exists():
                return LsResult(entries=None, error=f"路径不存在：{path}")
            if not resolved.is_dir():
                return LsResult(entries=None, error=f"当前路径不是目录：{path}")
            return LsResult(
                entries=[ # type: ignore
                    {
                        "path": self._to_virtual_path(child), # 虚拟路径
                        "is_dir": child.is_dir(), # 是否目录
                        "size": child.stat().st_size if child.is_file() else 0, # 文件大小
                        "modified_at": datetime.fromtimestamp(child.stat().st_mtime, UTC).isoformat(), # 修改时间
                    }
                    for child in sorted(resolved.iterdir(), key=lambda p: p.name.lower())
                ]
            )
        except PermissionError as exc:
            return LsResult(entries=None, error=str(exc))

    # 虚拟路径读取
    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """ 读取文件内容（支持偏移和行数限制）。

        默认先按 UTF-8届吗，失败后会推到 Latin-1.
        这个回退策略的目标不是精准识别编码，而是保证模型在遇到非 UTF-8 文件时仍能看到内容。
        避免因为一次编码异常中断整个 Agent 任务
        """
        try:
            # 解析虚拟路径为实际路径
            resolved = self._resolve_virtual_path(file_path)
            if not resolved.exists():
                return ReadResult(error=f"文件不存在：{file_path}")
            if resolved.is_dir():
                return ReadResult(error=f"当前路径是目录，不能读取为文件：{file_path}")

            # 读取文件内容
            raw = resolved.read_bytes()
            try:
                text = raw.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                # 非 UTF-8 文件回退用 Latin-1，保证任何二进制都能读
                text = raw.decode("latin-1")
                encoding = "latin-1"

            # 按行分割内容
            lines = text.splitlines()
            if offset or limit:
                text = "\n".join(lines[int(offset): int(offset) + int(limit)])

            # 获取文件元数据
            stat = resolved.stat()
            return ReadResult(
                file_data={
                    "content": text,
                    "encoding": encoding,
                    "created_at": datetime.fromtimestamp(stat.st_ctime, UTC).isoformat(),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                }
            )
        except PermissionError as exc:
            return ReadResult(error=str(exc))

    # 虚拟路径写入
    def write(self, file_path: str, content: str) -> WriteResult:
        """新建文件。如果文件已存在则报错（修改请用 edit）。"""
        if self.read_only:
            return WriteResult(error="当前任务是只读模式，禁止写入文件")
        try:
            # 解析虚拟路径为实际路径
            resolved = self._resolve_virtual_path(file_path)
            # 写入前检查
            denied = self._write_deny_reason(resolved)
            if denied:
                return WriteResult(error=denied)
            if resolved.exists():
                return WriteResult(error=f"文件已存在，修改文件请使用 edit_file：{file_path}")
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8", newline="")
            return WriteResult(path=file_path)
        except PermissionError as exc:
            return WriteResult(error=str(exc))

    # 虚拟路径编辑
    def edit(
            self,
            file_path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
    ) -> EditResult:
        """替换文件中的文本 —— 相当于精细化的 find & replace。

        Args:
            file_path (str): 文件路径。
            old_string (str): 要替换的旧字符串。
            new_string (str): 新字符串。
            replace_all (bool, optional): 是否替换所有出现的旧字符串。默认为 False，只替换第一个。

        Returns:
            EditResult: 文件修改结果，包含修改的文件路径和替换的行数。
        """
        if self.read_only:
            return EditResult(error="当前任务是只读模式，禁止修改文件")
        try:
            # 解析虚拟路径为实际路径
            resolved = self._resolve_virtual_path(file_path)
            # 检查文件存在
            if not resolved.exists():
                return EditResult(error=f"文件不存在：{file_path}")
            # 检查文件是否为目录
            if resolved.is_dir():
                return EditResult(error=f"当前路径是目录，不能修改为文件：{file_path}")

            # 写入前检查
            denied = self._write_deny_reason(resolved)
            if denied:
                return EditResult(error=denied)
            # 读取文件内容
            text = resolved.read_text(encoding="utf-8")
            # 统计旧字符串出现次数
            count = text.count(old_string)

            if count == 0:
                return EditResult(error=f"没有找到要替换的内容：{old_string}")
            if count > 1 and not replace_all:
                return EditResult(error=f"要替换的内容出现 {count} 次，请设置 replace_all=True 或提供更精确片段。")
            # 替换内容
            updated = text.replace(old_string, new_string, -1 if replace_all else 1)
            # 写入修改后的内容
            resolved.write_text(updated, encoding="utf-8", newline="")
            return EditResult(path=file_path, occurrences=count if replace_all else 1)
        except PermissionError as exc:
            return EditResult(error=str(exc))

    # 虚拟路径搜索
    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """递归搜索匹配模式的文件路径（支持 fnmatch 通配符）。"""
        try:
            # 解析虚拟路径为实际路径
            base = self._resolve_virtual_path(path or "/")
            matches = []
            for child in base.rglob("*"):
                #  获取虚拟路径
                virtual = self._to_virtual_path(child)
                #  匹配模式
                if fnmatch.fnmatch(virtual, pattern) or fnmatch.fnmatch(child.name, pattern):
                    matches.append(
                        {
                            "path": virtual,
                            "is_dir": child.is_dir(),
                            "size": child.stat().st_size if child.is_file() else 0,
                            "modified_at": datetime.fromtimestamp(child.stat().st_mtime, UTC).isoformat(),
                        }
                    )
            return GlobResult(matches=matches)
        except PermissionError as exc:
            return GlobResult(matches=None, error=str(exc))

    # 虚拟路径搜索
    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """在文件中搜索关键词，返回匹配的行号和内容。"""
        try:
            # 解析虚拟路径为实际路径
            base = self._resolve_virtual_path(path or VIRTUAL_PROJECTS)
            # 获取文件列表
            files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
            matches = []
            for file in files:
                if glob and not fnmatch.fnmatch(file.name, glob):
                    continue
                try:
                    # 读取文件内容并搜索关键词
                    for line_no, line in enumerate(file.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if pattern in line:
                            matches.append({"path": self._to_virtual_path(file), "line": line_no, "text": line})
                except OSError:
                    continue
            return GrepResult(matches=matches)
        except PermissionError as exc:
            return GrepResult(matches=None, error=str(exc))

    # 文件上传下载
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """批量上传文件（二进制内容直接写入）。"""
        if self.read_only:
            return [FileUploadResponse(path=path, error="read_only") for path, _ in files]

        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                # 解析虚拟路径为实际路径
                resolved = self._resolve_virtual_path(path)
                # 写入前检查
                denied = self._write_deny_reason(resolved)
                if denied:
                    responses.append(FileUploadResponse(path=path, error=denied))
                    continue
                # 创建目录
                resolved.parent.mkdir(parents=True, exist_ok=True)
                # 写入文件内容
                resolved.write_bytes(content)
                responses.append(FileUploadResponse(path=path, error=None))
            except PermissionError:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
            except IsADirectoryError:
                responses.append(FileUploadResponse(path=path, error="is_directory"))
            except (OSError, ValueError):
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
        return responses

    # 文件上传下载
    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """批量下载文件，返回二进制内容。"""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                # 解析虚拟路径为实际路径
                resolved = self._resolve_virtual_path(path)
                # 检查文件存在
                if not resolved.exists():
                    responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
                # 检查文件是否为目录
                elif resolved.is_dir():
                    responses.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
                else:
                    responses.append(FileDownloadResponse(path=path, content=resolved.read_bytes(), error=None))
            except PermissionError:
                responses.append(FileDownloadResponse(path=path, content=None, error="permission_denied"))
            except (OSError, ValueError):
                responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
        return responses

    # ── 旧兼容接口 ──────────────────────────────────────────
    # 下面的 read_file / write_file / list_files / run
    # 是为旧版 Git 工具保留的入口，内部调上面原生方法。
    def read_file(self, path: str) -> str:
        """读取文本文件（兼容旧工具）。"""
        result = self.read(self._normalize_compat_path(path), offset=0, limit=200_000)
        if result.error:
            if "目录" in result.error or "is_directory" in result.error:
                raise IsADirectoryError(result.error)
            raise FileNotFoundError(result.error)
        return str(result.file_data["content"]) if result.file_data else ""

    def write_file(self, path: str, content: str) -> str:
        """写入文本文件（兼容旧工具，可新建也可覆盖）。"""
        if self.read_only:
            raise PermissionError("当前任务是只读模式，禁止写入文件")

        # 解析虚拟路径
        virtual_path = self._normalize_compat_path(path)
        # 解析虚拟路径为实际路径
        resolved = self._resolve_virtual_path(virtual_path)
        # 检查写入权限
        denied = self._write_deny_reason(resolved)
        if denied:
            raise PermissionError(denied)
        # 检查文件是否为目录
        if resolved.exists() and resolved.is_dir():
            raise IsADirectoryError(f"write_file 必须写入具体文件，当前路径是目录: {path}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8", newline="")
        return str(resolved)

    def list_files(self, path: str = ".") -> list[str]:
        """列出目录下一层内容（兼容旧工具，返回相对路径）。"""

        # 解析虚拟路径
        virtual_path = self._normalize_compat_path(path)
        # 解析虚拟路径为实际路径
        resolved = self._resolve_virtual_path(virtual_path)
        if not resolved.exists():
            return []
        if resolved.is_file():
            return [str(resolved.relative_to(self.root)).replace("\\", "/")]
        return [str(child.relative_to(self.root)).replace("\\", "/") for child in sorted(resolved.iterdir())]

    def run(self, command: str, cwd: str = ".", timeout: int = 300) -> CommandResult:
        """受控执行命令（给旧 Git 工具用，带日志和 Token 屏蔽）。

        与 `execute()` 的区别：
        - `execute()` 面向 DeepAgents 原生协议，返回 `ExecuteResponse`；
        - `run()` 面向项目里的历史工具，返回 `CommandResult`；
        - `run()` 允许指定 cwd，并兼容旧工具常见的 `cd xxx && git ...` 写法。

        这是一层迁移适配，不建议新工具继续绕过 DeepAgents 原生协议调用它。
        """
        if self.read_only:
            raise PermissionError("当前任务是只读模式，禁止执行 Shell 命令")

        # 准备执行命令
        cwd_path, command_text = self._prepare_run_command(command, cwd)
        logger.info("执行命令：cwd=%s command=%s timeout=%s", cwd_path, _mask_token(command_text), timeout)
        # 执行命令
        completed = subprocess.run(
            command_text,
            cwd=str(cwd_path),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            encoding=self.output_encoding,
            errors="replace",
            env=self._execution_env(),
            check=False,
        )
        logger.info(
            "命令结束：exit_code=%s command=%s stdout_bytes=%s stderr_bytes=%s",
            completed.returncode,
            _mask_token(command_text),
            len(completed.stdout.encode("utf-8", errors="replace")),
            len(completed.stderr.encode("utf-8", errors="replace")),
        )
        return CommandResult(
            command=command_text,
            cwd=str(cwd_path),
            exit_code=completed.returncode,
            stdout=_mask_token(completed.stdout),
            stderr=_mask_token(completed.stderr),
        )

    # ── 初始化相关 ───────────────────────────────────────────
    def _ensure_layout(self) -> None:
        """创建工作区目录结构，写入 workspace 标记文件。

        后端启动时自动创建以下目录，让第一次启动就具备稳定的文件系统语义：
        - `projects`：模型实际修改的业务仓库；
        - `skills`：DeepAgents skills，只读，防止任务过程中被模型篡改；
        - `policies`：工作区说明和安全规则，只读，作为长期规则的一部分；
        - `reviews/tmp/logs`：审查产物、临时文件和运行日志；
        - `secrets`：Git AskPass 脚本等敏感辅助文件，不允许模型读写。
        """
        for directory in (
                self.root,
                self.projects_dir,
                self.skills_dir,
                self.policies_dir,
                self.reviews_dir,
                self.runtimes_dir,
                self.tmp_dir,
                self.logs_dir,
                self.secrets_dir,
                self.shared_python_venv.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        # 创建共享 Python 虚拟环境
        if os.environ.get("LOCAL_SHELL_CREATE_PYTHON_VENV", "false").lower() not in {"0", "false", "no"}:
            self._ensure_shared_python_venv()

        # 创建默认策略文件
        self._ensure_policy_files()
        # 将随应用发布的内置 skills 同步到工作区，供 DeepAgents 从 `/skills` 加载。
        self._ensure_builtin_skills()
        # 创建 GitHub AskPass 认证脚本。
        self._ensure_github_askpass_files()
        # 写一个 .ai_coding_workspace.json 标记文件，方便外部工具识别工作区
        state = {
            "backend": "local_shell",
            "platform": self.platform,
            "root": str(self.root),
            "updated_at": datetime.now(UTC).isoformat(),
            "virtual_dirs": [
                VIRTUAL_PROJECTS,
                VIRTUAL_SKILLS,
                VIRTUAL_POLICIES,
                VIRTUAL_REVIEWS,
                VIRTUAL_RUNTIMES,
                VIRTUAL_TMP,
                VIRTUAL_LOGS,
            ],
        }
        (self.root / ".ai_coding_workspace.json").write_text(
            __import__("json").dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _ensure_policy_files(self) -> None:
        """ 补全默认的 policy 文件（工作区说明、Git 规范、安全规范）。

        这些文件相当于“文件化的长期规则”：
        - 不依赖数据库即可持久化；
        - 可以被 Prompt 或工具读取后注入 Agent 上下文；
        - 可以随着企业规范演进做版本管理。

        注意这里采用“文件不存在才创建”的策略，避免覆盖用户后续维护过的规则内容。
        """
        defaults = {
            "workspace.md": "# 工作区目录说明\n\n- /projects：GitHub 项目源码目录。\n- /skills：DeepAgents 技能目录。\n- /runtimes：本机运行环境目录。\n- /reviews：审查结果目录。\n- /tmp：临时文件目录。\n- /logs：运行日志目录。\n",
            "git.md": "# Git 规范\n\n- 代码托管平台使用 GitHub。\n- 修改代码前先确认仓库目录和当前分支。\n- 提交前必须运行必要测试。\n",
            "security.md": "# 安全规范\n\n- 不读取或输出 /secrets 目录内容。\n- 不提交密钥、Token、私钥或 .env 文件。\n",
        }
        for name, content in defaults.items():
            # 创建策略文件
            path = self.policies_dir / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")

    def _ensure_builtin_skills(self) -> None:
        """把随应用发布的 DeepAgents skills 同步到工作区。

        内置 skill 由当前应用版本管理，启动时会更新同名文件；
        用户自己新建的其他 skill 目录不会被删除或覆盖。
        """

        source_root = PROJECT_ROOT / "agent" / "skills"
        if not source_root.is_dir():
            logger.warning("内置 skills 目录不存在：%s", source_root)
            return

        for source in source_root.rglob("*"):
            destination = self.skills_dir / source.relative_to(source_root)
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

    def _ensure_shared_python_venv(self) -> None:
        """创建当前平台的共享 Python 虚拟环境。

        macOS 检查 `.venv/bin/python`，Windows 检查 `.venv/Scripts/python.exe`。
        创建功能由 LOCAL_SHELL_CREATE_PYTHON_VENV 控制，默认关闭。
        """
        if self._venv_python_path().exists():
            return

        # 检查 Python 可执行文件
        python_names = (
            ("py", "python", "python3")
            if self.platform == "windows"
            else ("python3", "python")
        )

        # 检查 Python 可执行文件
        python = next((path for name in python_names if (path := shutil.which(name))), None)
        if not python:
            self._venv_error = "python executable not found on PATH"
            return
        try:
            # 创建虚拟环境
            completed = subprocess.run(
                [python, "-m", "venv", str(self.shared_python_venv)],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding=self.output_encoding,
                errors="replace",
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._venv_error = str(exc)
            return
        if completed.returncode != 0:
            self._venv_error = (completed.stderr or completed.stdout or "venv creation failed").strip()
            return
        self._venv_error = None

    def _ensure_github_askpass_files(self) -> None:
        """生成 Git 非交互认证脚本。

        因为 Agent 后台不能弹窗让用户输入密码，所以预先创建 AskPass 脚本，
        Git 需要凭据时会自动调用它，从环境变量读取 token。

        这个实现的核心价值是把认证从命令参数里移出去：
        - 命令日志里不会出现 token；
        - Git 命令仍然可以走标准 HTTPS 认证流程；
        - 后端可以统一通过 `_execution_env()` 注入认证环境变量。
        """
        # 创建 AskPass 脚本
        askpass = self._askpass_path()
        if askpass.exists():
            return

        # Windows 系统，写入 AskPass 脚本
        if self.platform == "windows":
            askpass.write_text(
                "@echo off\n"
                "echo %~1 | %SystemRoot%\\System32\\findstr.exe /I \"Username\" >nul\n"
                "if not errorlevel 1 (\n"
                "  echo %GITHUB_ASKPASS_USERNAME%\n"
                ") else (\n"
                "  echo %GITHUB_ASKPASS_TOKEN%\n"
                ")\n",
                encoding="utf-8",
                newline="\r\n",
            )
            return
        # Mac 系统，写入 AskPass 脚本
        askpass.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *[Uu]sername*) printf '%s' \"$GITHUB_ASKPASS_USERNAME\" ;;\n"
            "  *) printf '%s' \"$GITHUB_ASKPASS_TOKEN\" ;;\n"
            "esac\n",
            encoding="utf-8",
            newline="\n",
        )
        askpass.chmod(0o700)

    def _venv_bin_dir(self) -> Path:
        """返回当前平台虚拟环境的可执行文件目录。"""
        return self.shared_python_venv / ("Scripts" if self.platform == "windows" else "bin")

    def _venv_python_path(self) -> Path:
        """返回当前平台虚拟环境的 Python 可执行文件。"""
        return self._venv_bin_dir() / ("python.exe" if self.platform == "windows" else "python")

    def _askpass_path(self) -> Path:
        """返回当前平台可执行的 Git AskPass 脚本。"""
        suffix = ".cmd" if self.platform == "windows" else ".sh"
        return self.secrets_dir / f"github_askpass{suffix}"

    # ── 路径处理 ──────────────────────────────────────────────

    def _normalize_compat_path(self, path: str | os.PathLike[str]) -> str:
        """把旧工具传过来的路径统一成虚拟路径格式（/ 开头）。

        旧工具可能传入 `.`、`projects/a.py` 等形式。
        后端内部统一转换成 `/projects/a.py` 这种虚拟路径，后续再走同一套解析和权限检查。

        Args:
            path : 路径
        Returns:
            str: 虚拟路径
        """
        raw = str(path).strip()
        if raw in {"", "."}:
            return "/"

        # 先识别 DeepAgents 虚拟路径。在 macOS 上 `/projects` 也会被 Path
        # 视为宿主机绝对路径，因此这个判断必须放在 is_absolute() 之前。
        normalized = raw.replace("\\", "/")
        virtual_prefixes = tuple(f"/{name}" for name in _VIRTUAL_ROOT_NAMES)
        if normalized == "/" or any(
            normalized == prefix or normalized.startswith(f"{prefix}/")
            for prefix in virtual_prefixes
        ):
            return normalized

        # 展开当前宿主机的真实绝对路径，并映射回虚拟路径。
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return self._to_virtual_path(candidate)

        # 其他形式是旧工具传入的工作区相对路径。
        return "/" + normalized.lstrip("/")

    def _resolve_virtual_path(self, path: str | os.PathLike[str]) -> Path:
        """把虚拟路径（如 `/projects/my-repo`）转成真实文件系统路径。

        这是文件读写权限的关键入口。路径最终必须解析成真实路径，
        并确认它仍然位于 `self.root` 工作区内。
        """
        # 路径标准化
        raw = str(path).strip() or "/"
        # 去掉前缀 /
        normalized = raw.removeprefix("/")
        # 解析成真实路径
        resolved = (self.root / normalized).resolve()
        # 安全检查：必须落在工作区范围内
        if not self._is_under_root(resolved):
            raise PermissionError(f"path outside local workspace is denied: {path}")
        return resolved

    def _to_virtual_path(self, path: Path) -> str:
        """把真实路径转回虚拟路径（/ 开头）。

        DeepAgents 和模型不应该直接感知宿主机真实磁盘路径。
        返回虚拟路径可以降低环境耦合，也能避免把本机目录结构暴露给模型上下文。
        """

        # 解析成真实路径
        resolved = path.resolve()
        # 安全检查：必须落在工作区范围内
        if not self._is_under_root(resolved):
            raise PermissionError(f"path outside local workspace is denied: {path}")

        # 计算相对路径
        rel = resolved.relative_to(self.root).as_posix()
        # 转换成虚拟路径
        return "/" + rel if rel else "/"

    def _is_under_root(self, path: Path) -> bool:
        """判断路径是否在工作区根目录之下（安全工作区边界检查）。"""
        try:
            # 计算相对路径
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    def _is_under(self, path: Path, parent: Path) -> bool:
        """判断 path 是否在 parent 目录之下。"""
        try:
            # 计算相对路径
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    # ── 安全控制 ──────────────────────────────────────────────

    def _write_deny_reason(self, path: Path) -> str | None:
        """检查目录是否禁止写入。

        项目只允许模型主要修改 `/projects` 下的业务仓库。
        `skills/policies/runtimes/logs/secrets` 都属于后端运行基础设施或规则资产，
        不允许在普通任务中被模型写入，避免 Agent 自己改掉工具、规则、运行时或敏感文件。
        """

        # 检查目录是否在禁止写入的目录之下

        if self._is_under(path, self.policies_dir):
            return "write denied: policies are read-only"
        if self._is_under(path, self.skills_dir):
            return "write denied: skills are read-only"
        if self._is_under(path, self.runtimes_dir):
            return "write denied: runtimes are read-only"
        if self._is_under(path, self.logs_dir):
            return "write denied: logs are read-only"
        if self._is_under(path, self.secrets_dir):
            return "write denied: secrets are protected"
        return None

    def _is_writable(self, path: Path) -> bool:
        """测写入权限：尝试写一个临时文件，成功就表示可写。"""
        try:
            # 创建一个临时文件
            probe = path / ".write_test"
            # 尝试写入临时文件
            probe.write_text("ok", encoding="utf-8")
            # 删除临时文件
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def _deny_reason(self, command: str) -> str | None:
        """安全守卫：检测命令是否越界或使用了危险操作。

        这里是 'normalize_safe_command()' 之前的第一层粗粒度拒绝：
        1. 拦截 '..' 路径穿越
        2. 拦截明显危险的系统命令
        3. 识别 POSIX 路径，拦截工作区外绝对路径。

        两层校验的分工是： 本函数做场景拒绝， 'normalize_safe_command()' 做命令白名单和语法收敛。
        """

        # 命令小写化
        lowered = command.lower()

        # 检测路径穿越
        if "../" in command or "..\\" in command:
            return "path traversal outside workspace is denied"

        # 检测危险命令模式
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, lowered):
                return f"dangerous command pattern matched: {pattern}"
        # 虚拟路径会在下一阶段映射到工作区，可以放行；其他绝对路径必须校验。
        for match in _POSIX_PATH_RE.finditer(command):
            raw_path = match.group(1)
            if any(
                    raw_path == f"/{name}" or raw_path.startswith(f"/{name}/")
                    for name in _VIRTUAL_ROOT_NAMES
            ):
                continue
            # 虚拟路径会在下一阶段映射到工作区，可以放行；其他绝对路径必须校验。
            candidate = Path(raw_path).expanduser().resolve()
            # 安全检查：必须落在工作区范围内
            if not self._is_under_root(candidate):
                return f"absolute path outside workspace: {candidate}"

        # 检测 Windows 路径
        for match in _WINDOWS_PATH_RE.finditer(command):
            # 检测 Windows 绝对路径
            candidate = Path(match.group(1)).expanduser().resolve()
            # 安全检查：必须落在工作区范围内
            if not self._is_under_root(candidate):
                return f"absolute path outside workspace: {candidate}"
        return None

    @staticmethod
    def _read_only_command_allowed(command: str) -> bool:
        """只读任务仅允许查看命令和仓库准备命令。

        clone/fetch/pull 会改变 Sandbox 中的仓库副本，但不会修改业务源码内容；
        它们是完成代码审查前同步远端状态所必需的准备动作。文件写入接口仍由
        read_only 单独禁止，因此不能借此编辑项目文件。
        """

        # 命令小写化
        words = command.strip().lower().split()
        # 检测空命令
        if not words:
            return False
        if words[0] in {"ls", "cat", "pwd", "which", "dir", "type", "where"}:
            return True
        if len(words) < 2 or words[0] not in {"git", "git.exe"}:
            return False

        # 检测 Git 子命令
        subcommand = words[1]
        if subcommand in {
            "clone",
            "diff",
            "fetch",
            "log",
            "ls-files",
            "pull",
            "rev-parse",
            "show",
            "status",
        }:
            return True
        # 检测 Git 分支命令
        if subcommand == "branch":
            return not any(flag in words[2:] for flag in ("-d", "-m"))
        # 检测 Git 远程命令
        if subcommand == "remote":
            return len(words) == 2 or words[2] in {"-v", "get-url"}
        return False

    # ── 命令预处理 ───────────────────────────────────────────

    def _prepare_command(self, command: str) -> str:
        """
        执行前的命令预处理：替换虚拟路径并注入 Git AskPass 配置。
        """

        # 替换虚拟路径
        prepared = _VIRTUAL_PATH_RE.sub(self._virtual_command_path_replacement, command)
        # 注入 Git AskPass 配置
        return self._prepare_git_command(prepared)

    def _prepare_git_command(self, command: str) -> str:
        """
        给 git 命令注入 askpass 配置，避免弹窗或使用过期的凭据管理器

        通过 'git -c credential.helper= -c core.askPass=....'  临时覆盖 Git 配置
        可以让单次命令稳定使用当前 Agent 后端注入的 GitHub token。
        这样不会污染用户全局 Git 配置，也不会依赖桌面凭据管理器。
        """

        # 去掉前缀 whitespace
        stripped = command.strip()
        # 获取前缀 whitespace
        leading = command[:len(command) - len(stripped)]

        # 分离可执行文件和参数
        executable, separator, rest = stripped.partition(" ")
        # 检测可执行文件是否为 git
        if executable.lower() not in {"git", "git.exe"}:
            return command
        # 检测参数是否为空
        if not separator:
            # 如果参数为空，则将 rest 设置为空字符串
            rest = ""

        # cmd.exe 可以接受带反斜杠的原生 Windows 路径；POSIX shell 使用普通路径。
        askpass = str(self._askpass_path())

        # 构造新的命令字符串
        return f'{leading}{executable} -c credential.helper= -c core.askPass="{askpass}" {rest}'.rstrip()

    def _prepare_run_command(self, command: str, cwd: str) -> tuple[Path, str]:
        """准备 `run()` 的命令：解析 cwd、兼容 `cd`、安全检查、规范化。

        旧工具常把命令写成 `cd repo && git status`。
        这里不会直接把整段 shell 原样执行，而是提取 `cd` 后的目录作为 subprocess 的 cwd，
        再对后半段命令做安全检查和白名单归一化。
        """

        # 解析 cwd
        cwd_path = self._resolve_virtual_path(self._normalize_compat_path(cwd))
        # 去掉前缀 whitespace
        command = re.sub(r"\s+2>&1(?=\s*(?:&&|\|\||$))", "", command.strip())
        # 分离出第一个命令部分
        first_part = command.split("&&", 1)[0].strip()
        # 检测是否为 cd 命令
        cd_match = re.fullmatch(r"cd\s+(.+)", first_part, flags=re.IGNORECASE)
        if cd_match:
            # 兼容 `cd xxx && command`，但不把 `cd` 作为真正的 shell 片段执行。
            # 这样可以减少 shell 拼接能力，同时保留旧工具的使用习惯。
            cwd_path = self._resolve_virtual_path(
                # 兼容引号
                self._normalize_compat_path(cd_match.group(1).strip().strip('"').strip("'")))
            # 去掉 cd 命令部分
            command = command.split("&&", 1)[1].strip() if "&&" in command else ""
        if not command:
            raise ValueError("Command cannot be empty")
        if self.command_guard_enabled:
            # 第一层安全校验：场景拒绝，快速拦截明显危险的命令。
            denied = self._deny_reason(command)
            if denied:
                raise PermissionError(f"命令被拒绝：{denied}")
        # 第二层安全校验：命令白名单、危险 shell 操作符和危险片段收敛。
        safe_command = normalize_safe_command(command)
        return cwd_path, self._prepare_command(safe_command)

    def _virtual_command_path_replacement(self, match: re.Match[str]) -> str:
        """把命令中的 `/projects/xxx` 等虚拟路径替换为真实路径（带引号防空格）。

        这个函数只用于命令文本中的路径替换；文件工具的路径解析走 `_resolve_virtual_path()`。
        两者都必须确认最终路径仍在工作区内，避免模型通过构造路径逃逸。
        """
        roots = {
            "projects": self.projects_dir,
            "skills": self.skills_dir,
            "policies": self.policies_dir,
            "reviews": self.reviews_dir,
            "runtimes": self.runtimes_dir,
            "tmp": self.tmp_dir,
            "logs": self.logs_dir,
        }
        root = roots[match.group(1)]
        suffix = (match.group(2) or "").lstrip("/")
        path = (root / Path(*suffix.split("/"))).resolve() if suffix else root
        if not self._is_under_root(path):
            raise PermissionError(f"virtual path escapes workspace: {match.group(0)}")
        return f'"{path}"'

    def _execution_env(self) -> dict[str, str]:
        """
        构造子进程环境变量：注入 venv、Git 安全配置、GitHub 认证信息。

        统一处理以下内容：
        1. 将共享 Python venv 的可执行目录放到 PATH 最前面；
        2. 禁止 Git 弹出交互式认证窗口；
        3. 通过当前平台的 GIT_ASKPASS 脚本注入 GitHub token。

        token 只存在于子进程环境变量中，不会拼接到命令文本或日志里。
        """
        env = os.environ.copy()
        # 构造虚拟环境脚本目录
        scripts = self._venv_bin_dir()

        if scripts.exists():
            env["PATH"] = f"{scripts}{os.pathsep}{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(self.shared_python_venv)

        # 禁止 Git 交互式弹窗，出错直接返回
        env["GIT_TERMINAL_PROMPT"] = "0"
        github_token = (
                get_env("GITHUB_TOKEN").strip()
                or get_env("GH_TOKEN").strip()
                or get_env("SCM_GITHUB_TOKEN").strip()
        )
        if github_token:
            env["GIT_ASKPASS"] = str(self._askpass_path())
            env["GIT_ASKPASS_REQUIRE"] = "force"
            env["GITHUB_ASKPASS_USERNAME"] = "x-access-token"
            env["GITHUB_ASKPASS_TOKEN"] = github_token
        return env
