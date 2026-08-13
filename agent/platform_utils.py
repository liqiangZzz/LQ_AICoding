"""本地 Shell 运行平台识别。"""

from __future__ import annotations

import os
import sys
from typing import Literal

from agent.env_utils import get_env

LocalShellPlatform = Literal["macos", "windows"]

_PLATFORM_ALIASES: dict[str, LocalShellPlatform] = {
    "mac": "macos",
    "macos": "macos",
    "darwin": "macos",
    "win": "windows",
    "win32": "windows",
    "windows": "windows",
}


def host_platform() -> LocalShellPlatform:
    """返回当前宿主机平台；项目目前只支持 macOS 和 Windows。"""

    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    raise RuntimeError(f"Unsupported local shell host platform: {sys.platform}")


def resolve_local_shell_platform(value: str | None = None) -> LocalShellPlatform:
    """解析 ``auto|macos|windows`` 平台配置。"""

    configured = value if value is not None else get_env("LOCAL_SHELL_PLATFORM", "auto")
    normalized = configured.strip().lower() or "auto"
    if normalized == "auto":
        return host_platform()
    try:
        return _PLATFORM_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(
            "LOCAL_SHELL_PLATFORM must be one of: auto, macos, windows"
        ) from exc


def platform_display_name(platform: LocalShellPlatform) -> str:
    return "Windows" if platform == "windows" else "macOS"
