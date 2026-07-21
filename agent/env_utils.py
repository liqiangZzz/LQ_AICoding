"""项目环境变量加载与平台配置选择。

配置流程：先读取项目根目录的 `.env`，再根据 `APP_PLATFORM` 将
`变量名_MAC` 或 `变量名_WINDOWS` 映射为业务代码使用的通用变量名。
这样其他模块不需要重复判断当前配置平台。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # 项目根目录
LOCAL_ENV = PROJECT_ROOT / ".env"

# ── 平台变量映射规则 ──────────────────────────────────────────
# APP_PLATFORM 接受常见别名，但实际配置后缀统一为 MAC / WINDOWS。
PLATFORM_ALIASES = {
    "mac": "MAC",
    "macos": "MAC",
    "darwin": "MAC",
    "window": "WINDOWS",
    "windows": "WINDOWS",
    "win": "WINDOWS",
    "win32": "WINDOWS",
}
PLATFORM_ENV_NAMES = (
    "AI_WORKSPACE_ROOT",
    "LQ_AICODING_DATA_DIR",
    "CHECKPOINT_DB_PATH",
    "STORE_DB_PATH",
    "LQ_AICODING_LOG_DIR",
    "LOCAL_SHELL_WORKSPACE",
    "LOCAL_SHELL_SHARED_PYTHON_VENV",
)


# ── 内部加载逻辑 ──────────────────────────────────────────────
def _load_non_empty_env(path: Path, *, override: bool) -> None:
    r"""加载 .env 中的非空变量。

    python-dotenv 默认会把 `DEEPSEEK_API_KEY=` 这种空值也写入环境变量。
    课程项目的 `.env` 通常会保留空字段作为模板，如果直接加载空值，

    所以这里采用自定义加载规则：
    - 空值不写入环境变量。
    - open-swe 的 .env 先作为默认值加载。
    - 本项目 .env 只有填写了非空值时才覆盖默认值。
    """

    if not path.exists():
        return
    for key, value in dotenv_values(path).items():
        if value is None or value.strip() == "":
            continue
        if override or key not in os.environ or os.environ.get(key, "").strip() == "":
            os.environ[key] = value


def _apply_platform_environment() -> None:
    """把所选平台的路径变量映射为业务代码读取的通用变量。

    例如 `APP_PLATFORM=windows` 时：
    `AI_WORKSPACE_ROOT_WINDOWS` 会写入 `AI_WORKSPACE_ROOT`。
    平台专用值为空时不会覆盖已有的通用变量，兼容外部环境变量注入。
    """

    platform = os.environ.get("APP_PLATFORM", "mac").strip().lower()
    suffix = PLATFORM_ALIASES.get(platform)
    if suffix is None:
        supported = ", ".join(sorted(PLATFORM_ALIASES))
        raise RuntimeError(
            f"Unsupported APP_PLATFORM: {platform!r}. Supported values: {supported}"
        )

    for name in PLATFORM_ENV_NAMES:
        # 业务模块继续读取不带后缀的变量，平台差异只在这里集中处理。
        value = os.environ.get(f"{name}_{suffix}", "").strip()
        if value:
            os.environ[name] = value


def load_environment() -> None:
    r"""加载课程项目运行需要的环境变量。

    加载顺序：
    1. 加载 `.env` 中的非空配置；
    2. 应用 Mac/Windows 平台映射；
    3. 补充 tracing 默认值。
    """
    _load_non_empty_env(LOCAL_ENV, override=True)
    _apply_platform_environment()

    # 课程版默认关闭 LangSmith/LangChain tracing。
    # 这样学生启动项目时不需要额外配置 LangSmith，也不会把运行数据发到外部观测平台。
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
