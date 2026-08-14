"""项目环境变量加载。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # 项目根目录
LOCAL_ENV = PROJECT_ROOT / ".env"

_PLATFORM_PATH_ENV_NAMES = (
    "AI_WORKSPACE_ROOT",
    "LQ_AICODING_DATA_DIR",
    "CHECKPOINT_DB_PATH",
    "STORE_DB_PATH",
    "LANGGRAPH_STORE_DB_PATH",
    "LQ_AICODING_LOG_DIR",
    "LOCAL_SHELL_WORKSPACE",
)

# ── 内部加载逻辑 ──────────────────────────────────────────────
def _load_non_empty_env(path: Path, *, override: bool) -> None:
    r"""加载 .env 中的非空变量。

    python-dotenv 默认会把 `DEEPSEEK_API_KEY=` 这种空值也写入环境变量。
    项目的 `.env` 通常会保留空字段作为模板，如果直接加载空值，

    所以这里采用自定义加载规则：
    - 空值不写入环境变量。
    - `.env` 只有填写了非空值时才覆盖当前环境。
    """

    if not path.exists():
        return
    for key, value in dotenv_values(path).items():
        if value is None or value.strip() == "":
            continue
        if override or key not in os.environ or os.environ.get(key, "").strip() == "":
            os.environ[key] = value


def _active_platform_profile() -> str:
    """返回平台配置后缀，避免在环境加载层反向依赖 platform_utils。"""

    configured = os.environ.get("LOCAL_SHELL_PLATFORM", "auto").strip().lower() or "auto"
    if configured == "auto":
        if os.name == "nt" or sys.platform == "win32":
            return "WINDOWS"
        if sys.platform == "darwin":
            return "MACOS"
        raise RuntimeError(f"Unsupported local shell host platform: {sys.platform}")
    aliases = {
        "mac": "MACOS",
        "macos": "MACOS",
        "darwin": "MACOS",
        "win": "WINDOWS",
        "win32": "WINDOWS",
        "windows": "WINDOWS",
    }
    try:
        return aliases[configured]
    except KeyError as exc:
        raise ValueError(
            "LOCAL_SHELL_PLATFORM must be one of: auto, macos, windows"
        ) from exc


def _apply_platform_path_profile(path: Path) -> None:
    """把当前平台的 ``*_MACOS`` / ``*_WINDOWS`` 路径映射到通用变量。"""

    if not path.exists():
        return
    values = dotenv_values(path)
    profile = _active_platform_profile()
    for name in _PLATFORM_PATH_ENV_NAMES:
        profile_name = f"{name}_{profile}"
        if profile_name not in values:
            continue
        value = values[profile_name]
        if value is None or value.strip() == "":
            # 空平台值不覆盖通用值；两者都为空时 settings.py 会使用默认路径。
            continue
        os.environ[name] = value


def load_environment() -> None:
    """加载项目运行需要的环境变量。

    加载顺序：
    1. 加载 `.env` 中的非空配置；
    2. 按 `LOCAL_SHELL_PLATFORM` 应用 macOS / Windows 路径配置；
    3. 补充 tracing 默认值。
    """
    _load_non_empty_env(LOCAL_ENV, override=True)
    _apply_platform_path_profile(LOCAL_ENV)
    # 默认关闭 LangSmith/LangChain tracing，避免未显式授权时上传运行数据。
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGSMITH_TRACING", "false")
    os.environ.setdefault("LANGCHAIN_API_KEY", "")


# ── 对外读取接口 ──────────────────────────────────────────────
def get_env(name: str, default: str = "") -> str:
    """读取环境变量。

    这个函数每次读取前都会调用 load_environment，
    目的是让脚本、测试、Uvicorn 启动入口都能得到一致的配置加载行为。
    """

    load_environment()
    return os.environ.get(name, default)


def require_env(name: str) -> str:
    """读取必填环境变量。

    DeepSeek API Key、DeepSeek Base URL、GitHub Token 这类关键配置缺失时，
    应该尽早抛出明确错误，而不是等到模型调用或 Git push 时才失败。
    """

    value = get_env(name).strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
