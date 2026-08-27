from datetime import date, datetime
from pathlib import Path

from agent.env_utils import get_env

# ── 项目根目录 ────────────────────────────────────────────────
# 项目根目录固定为当前 LQ_AICoding 源码仓库的顶层目录。
# 后续所有本项目自己的数据文件、日志文件都默认放在这个目录下面，
# 避免 LangGraph dev 或第三方工具把文件散落到隐藏目录中。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ── SQLite 持久化 ─────────────────────────────────────────────
# SQLite 数据目录：默认使用项目内 data/。
# 用户可以通过 .env 覆盖；默认落在项目目录中，便于备份和排查
# checkpoint、业务 Store 与 LangGraph Store。
DATA_DIR = Path(get_env("LQ_AICODING_DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser().resolve()
CHECKPOINT_DB_PATH = Path(
    get_env("CHECKPOINT_DB_PATH", str(DATA_DIR / "checkpoints.sqlite"))
).expanduser().resolve()
STORE_DB_PATH = Path(get_env("STORE_DB_PATH", str(DATA_DIR / "store.sqlite"))).expanduser().resolve()
LANGGRAPH_STORE_DB_PATH = Path(
    get_env("LANGGRAPH_STORE_DB_PATH", str(DATA_DIR / "langgraph_store.sqlite"))
).expanduser().resolve()

# ── 日志与轮转 ────────────────────────────────────────────────
# 日志目录：所有后端日志和 Agent 运行日志都写入项目内 logs/。
# 这样既能在控制台实时查看，也能通过日志文件复盘 Agent 行为。
LOG_DIR = Path(get_env("LQ_AICODING_LOG_DIR", str(PROJECT_ROOT / "logs"))).expanduser().resolve()
LOG_LEVEL = get_env("LQ_AICODING_LOG_LEVEL", "INFO").upper()
LOG_ROTATION_WHEN = get_env("LQ_AICODING_LOG_WHEN", "midnight")
LOG_ROTATION_INTERVAL = int(get_env("LQ_AICODING_LOG_INTERVAL", "1"))
LOG_RETENTION_DAYS = int(get_env("LQ_AICODING_LOG_RETENTION_DAYS", "14"))


def log_date_text(target_date: date | None = None) -> str:
    """
    返回历史日志文件使用的日期文本。

    TimedRotatingFileHandler 当前写入固定文件名，例如 backend.log；
    历史文件默认追加 YYYY-MM-DD 后缀，例如 backend.log.2026-07-10。
    这个函数保留给日志 API 读取历史日期时使用。
    """
    return (target_date or datetime.now().astimezone().date()).isoformat()


def backend_log_path(target_date: date | None = None) -> Path:
    """
    返回后端日志路径

    不传日期时返回当前正在写入的固定文件名；传日期时则返回标准轮转后的历史文件名。
    """

    if target_date is None:
        return LOG_DIR / "backend.log"

    return LOG_DIR / f"backend.log.{log_date_text(target_date)}"


def agent_log_path(target_date: date | None = None) -> Path:
    """
    返回 Agent 运行日志路径
    """
    if target_date is None:
        return LOG_DIR / "agent-runs.log"
    return LOG_DIR / f"agent-runs.log.{log_date_text(target_date)}"


BACKEND_LOG_PATH = backend_log_path()
AGENT_LOG_PATH = agent_log_path()

# ── Agent 工作区 ──────────────────────────────────────────────
# Agent 操作真实代码仓库时使用的工作区，不放在当前项目源码目录中，
# 避免 Agent 误改自身。可通过 AI_WORKSPACE_ROOT 覆盖默认路径。
WORKSPACE_ROOT = Path(
    get_env("AI_WORKSPACE_ROOT", str(Path.home() / "ai_workspace"))
).expanduser().resolve()
PROJECTS_DIR = WORKSPACE_ROOT / "projects"

# DeepAgents skills 目录统一放在所选平台的工作区 skills 子目录中。
# 这样可以通过 DeepAgents 原生 backend route 暴露 '/skills'。
SKILLS_DIR = WORKSPACE_ROOT / "skills"
