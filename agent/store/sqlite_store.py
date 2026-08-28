"""本地业务 SQLite Store。

数据表、字段和方法对应关系见同目录 `sqlite_store_说明.md`。
"""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """统一使用 UTC 时间存储，避免本地时区变化影响排序和排查。"""

    return datetime.now(UTC).isoformat()


class LocalSqliteStore:
    """
    业务数据 Store

    这个类中负责保存 “平台业务摘要”，不保存完整聊天历史：
    - threads： 任务列表和当前状态
    - runs： 每次运行的开始、结束、失败原因。
    - thread_plans: 编码前的技术方案、确认状态和 Markdown 归档路径。
    - review_findings： Reviewer Agent 发现的问题。
    - setting： 项目的少量键值对配置。

    完整 messages 和 LangGraph thread state 由 checkpoint 数据库保存
    """

    def __init__(self, db_path: Path) -> None:
        # check_same_thread= False 允许 FastAPI 后台任务、SSE 读取和工具写事件跨线程访问。
        # 但 sqlite3 的同一个链接不能无锁并发提交，所以必须用 RLock 串行化所有数据库操作。
        self.db_path = db_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30
        )
        self._closed = False
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._init_schema()

    def _configure_connection(self) -> None:
        """配置 SQLite 连接，提升本地多线程读写稳定性。

        WAL 模式（Write-Ahead Logging）允许读写更好地并发； busy_timeout 可以让短时间锁等待自动重试。
        避免 Agent 正在写事件时前端 SSE 读取刚好撞上锁就失败。
        """

        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            # 事件记录有时会早于 thread 主记录写入；这里不启用外键强校验，
            # 由 delete_thread 主动清理附属记录即可，避免展示事件影响主任务
            self._conn.execute("PRAGMA foreign_keys=OFF")

    def close(self) -> None:
        """关闭 SQLite 连接，主要供独立验证脚本释放临时数据库文件。"""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, sqlite3.Error):
            pass

    def _init_schema(self) -> None:
        """初始化业务表。

        CREATE TABLE IF NOT EXISTS 让服务可以重复启动；
        第一次启动会创建表，后续启动只复用已有 SQLite 文件。
        """
        with self._lock:
            self._conn.executescript(
                """
                -- threads 表：会话/任务主表
                CREATE TABLE IF NOT EXISTS threads (
                  thread_id TEXT PRIMARY KEY, --  会话/任务 ID
                  title TEXT NOT NULL, --  会话/任务标题
                  user_prompt TEXT, --  用户提示词
                  repo_url TEXT,    --  仓库 URL
                  repo_owner TEXT,  --  仓库所有者
                  repo_name TEXT,   --  仓库名称
                  branch_name TEXT, --  分支名称
                  pr_url TEXT,      --  Pull Request URL
                  latest_run_status TEXT NOT NULL, --  最新运行状态
                  created_at TEXT NOT NULL, --  创建时间
                  updated_at TEXT NOT NULL  --  更新时间
                );

                -- runs 表：运行记录表
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY, --  运行 ID
                  thread_id TEXT NOT NULL, --  所属线程 ID
                  status TEXT NOT NULL, --  运行状态
                  started_at TEXT NOT NULL, --  开始时间
                  finished_at TEXT, --  结束时间
                  error TEXT, --  错误信息
                  FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                );

                -- run_events 表：运行事件日志表
                CREATE TABLE IF NOT EXISTS run_events (
                  id TEXT PRIMARY KEY, --  事件 ID
                  thread_id TEXT NOT NULL, --  所属线程 ID
                  kind TEXT NOT NULL, --  事件种类
                  title TEXT NOT NULL, --  事件标题
                  status TEXT NOT NULL, --  事件状态
                  detail TEXT, --  详细描述
                  created_at TEXT NOT NULL, --  创建时间
                  updated_at TEXT NOT NULL, --  更新时间
                  FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                );

                -- thread_messages 表：消息记录表
                CREATE TABLE IF NOT EXISTS thread_messages (
                  message_id TEXT PRIMARY KEY, --  消息 ID
                  thread_id TEXT NOT NULL, --  所属线程 ID
                  run_id TEXT, --  关联的运行 ID
                  author TEXT NOT NULL, --  作者
                  content TEXT NOT NULL, --  消息内容
                  metadata TEXT, --  元数据（JSON）
                  created_at TEXT NOT NULL, --  创建时间
                  FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                );

                -- thread_plans 表：计划/方案表
                CREATE TABLE IF NOT EXISTS thread_plans (
                  plan_id TEXT PRIMARY KEY, --  计划 ID
                  thread_id TEXT NOT NULL, --  所属线程 ID
                  run_id TEXT, --  关联的运行 ID
                  status TEXT NOT NULL, --  计划状态
                  prompt TEXT NOT NULL, --  生成提示词
                  plan_text TEXT NOT NULL, --  计划文本
                  plan_path TEXT NOT NULL, --  计划文件路径
                  created_at TEXT NOT NULL, --  创建时间
                  approved_at TEXT, --  审批通过时间
                  FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                );

                -- review_findings 表：审查结果表
                CREATE TABLE IF NOT EXISTS review_findings (
                  id TEXT PRIMARY KEY, --  发现项 ID
                  thread_id TEXT NOT NULL, --  所属线程 ID
                  file TEXT NOT NULL, --  文件路径
                  line INTEGER, --  行号
                  severity TEXT NOT NULL, --  严重程度
                  title TEXT NOT NULL, --  问题标题
                  description TEXT NOT NULL, --  详细描述
                  status TEXT NOT NULL, --  问题状态
                  created_at TEXT NOT NULL, --  创建时间
                  updated_at TEXT NOT NULL, --  更新时间
                  FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                );

                -- settings 表：系统配置表
                CREATE TABLE IF NOT EXISTS settings (
                  key TEXT PRIMARY KEY, --  配置键
                  value TEXT NOT NULL, --  配置值
                  updated_at TEXT NOT NULL --  更新时间
                );

                -- repo_workspace_mappings 表：仓库与本地工作区映射表
                CREATE TABLE IF NOT EXISTS repo_workspace_mappings (
                  id TEXT PRIMARY KEY, --  映射 ID
                  repo_url TEXT NOT NULL, --  仓库 URL
                  repo_owner TEXT NOT NULL, --  仓库所有者
                  repo_name TEXT NOT NULL, --  仓库名称
                  project_dir TEXT NOT NULL, --  项目目录
                  local_path TEXT, --  本地绝对路径
                  is_active INTEGER NOT NULL DEFAULT 1, --  是否激活
                  source TEXT NOT NULL, --  来源
                  notes TEXT, --  备注
                  created_at TEXT NOT NULL, --  创建时间
                  updated_at TEXT NOT NULL, --  更新时间
                  last_verified_at TEXT --  最后验证时间
                );

                CREATE TABLE IF NOT EXISTS repo_memories (
                  owner TEXT NOT NULL,
                  repo TEXT NOT NULL,
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (owner, repo)
                );

                -- 唯一索引：确保每个仓库只有一条激活映射
                CREATE UNIQUE INDEX IF NOT EXISTS idx_repo_workspace_active
                  ON repo_workspace_mappings(repo_url)
                  WHERE is_active = 1;
                """
            )
            # 增量迁移：保证旧表有 user_prompt 字段
            self._ensure_column("threads", "user_prompt", "TEXT")
            self._conn.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        """为旧 SQLite 数据库补充新增列

        项目会不断迭代字段，不能要求每次都删除 data/store.sqlite
        PRAGMA table_info 可以判断列是否存在，缺失时用 ALTER TABLE 做轻量迁移。
        """
        with self._lock:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            existing_columns = {row["name"] for row in rows}
            if column not in existing_columns:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

    def upsert_thread(
        self,
        *,
        thread_id: str,
        title: str,
        repo_url: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        branch_name: str | None = None,
        pr_url: str | None = None,
        user_prompt: str | None = None,
        latest_run_status: str = "pending",
    ) -> None:
        """更新或插入一个会话/任务。"""
        with self._lock:
            now = utc_now()
            existing = self.get_thread(thread_id)
            created_at = existing["created_at"] if existing else now
            self._conn.execute(
                """
                INSERT INTO threads (
                  thread_id, title, user_prompt, repo_url, repo_owner, repo_name, branch_name, pr_url,
                  latest_run_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  title=threads.title,
                  user_prompt=COALESCE(excluded.user_prompt, threads.user_prompt),
                  repo_url=COALESCE(excluded.repo_url, threads.repo_url),
                  repo_owner=COALESCE(excluded.repo_owner, threads.repo_owner),
                  repo_name=COALESCE(excluded.repo_name, threads.repo_name),
                  branch_name=COALESCE(excluded.branch_name, threads.branch_name),
                  pr_url=COALESCE(excluded.pr_url, threads.pr_url),
                  latest_run_status=excluded.latest_run_status,
                  updated_at=excluded.updated_at
                """,
                (
                    thread_id,
                    title,
                    user_prompt,
                    repo_url,
                    repo_owner,
                    repo_name,
                    branch_name,
                    pr_url,
                    latest_run_status,
                    created_at,
                    now,
                ),
            )
            self._conn.commit()

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            return self._row_to_dict(row)

    def list_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM threads ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def update_thread_status(
        self,
        thread_id: str,
        status: str,
        *,
        pr_url: str | None = None,
        branch_name: str | None = None
    ) -> None:
        """更新会话/任务状态，并兼容旧调用方使用的 ``branch`` 参数。"""
        with self._lock:
            existing = self.get_thread(thread_id)
            # 运行事件可能比任务初始化更早到达。这里补建最小 thread 记录，
            # 避免状态更新静默丢失，后续 upsert 会再补齐仓库和标题信息。
            if existing is None:
                self.upsert_thread(
                    thread_id=thread_id,
                    title=thread_id,
                    pr_url=pr_url,
                    branch_name=branch_name,
                    latest_run_status=status,
                )
                return
            self._conn.execute(
                """
                UPDATE threads
                SET latest_run_status = ?, pr_url = COALESCE(?, pr_url),
                    branch_name = COALESCE(?, branch_name), updated_at = ?
                WHERE thread_id = ?
                """,
                (status, pr_url, branch_name, utc_now(), thread_id),
            )
            self._conn.commit()

    def record_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        status: str,
        error: str | None = None,
        finished: bool = False,
    ) -> None:
        """
        记录一个运行的开始或结束。
        """
        with self._lock:
            now = utc_now()
            self._conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, status, started_at, finished_at, error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status=excluded.status,
                  finished_at=excluded.finished_at,
                  error=excluded.error
                """,
                (run_id, thread_id, status, now, now if finished else None, error),
            )
            self._conn.commit()

    def add_run_event(
        self,
        *,
        event_id: str,
        thread_id: str,
        kind: str,
        title: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        """记录 Agent 运行过程中的简洁步骤。

        这些事件用于 Dashboard 实时展示“正在做什么”，不保存大段命令输出。
        相同 event_id 可以被更新，例如先写 in_progress，完成后改成 completed。
        """
        with self._lock:
            now = utc_now()
            self._conn.execute(
                """
                INSERT INTO run_events (
                    id, thread_id, kind, title, status, detail, created_at,updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  kind=excluded.kind,
                  title=excluded.title,
                  status=excluded.status,
                  detail=excluded.detail,
                  updated_at=excluded.updated_at
                """,
                (event_id, thread_id, kind, title, status, detail, now, now),
            )
            self._conn.commit()

    def list_run_events(self, thread_id: str) -> list[dict[str, Any]]:
        """按创建顺序读取运行步骤。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM run_events
                WHERE thread_id = ?
                ORDER BY created_at ASC
                """,
                (thread_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def clear_run_events(self, thread_id: str) -> None:
        """清空某个 thread 的临时过程事件。

        run_events 只服务当前/最近一次运行的过程展示；历史回答正文保存在 thread_messages,不依赖这里的 临时事件。
        """
        with self._lock:
            self._conn.execute(
                "DELETE FROM run_events WHERE thread_id = ?", (thread_id,)
            )
            self._conn.commit()

    def add_thread_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        author: str,
        content: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """追加保存一条 dashboard 会话消息。

        thread_messages 保存的是用户能看到的回答正文，和 run_events 的过程步骤分开。
        这样继续输入新问题时不会覆盖上一轮问题和回答。
        """
        with self._lock:
            # 去除内容前后的空白字符
            normalized_content = content.strip()
            if author == "user" and normalized_content:
                latest_user = self._conn.execute(
                    """
                    SELECT *
                    FROM thread_messages
                    WHERE thread_id = ? AND author = 'user'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (thread_id,),
                ).fetchone()
                if (
                    latest_user is not None
                    and str(latest_user["content"]).strip() == normalized_content
                ):
                    return

            self._conn.execute(
                """
                INSERT INTO thread_messages (
                  message_id, thread_id, run_id, author, content, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                  content=excluded.content,
                  metadata=excluded.metadata
                """,
                (
                    message_id,
                    thread_id,
                    run_id,
                    author,
                    normalized_content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )
            self._conn.commit()

    def list_thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        """按写入顺序读取 dashboard 会话消息。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM thread_messages
                WHERE thread_id = ?
                ORDER BY created_at ASC
                """,
                (thread_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def add_thread_plan(
        self,
        *,
        plan_id: str,
        thread_id: str,
        prompt: str,
        plan_text: str,
        plan_path: str,
        run_id: str | None = None,
        status: str = "pending",
    ) -> None:
        """保存一份编码前的技术方案。 默认状态为 pending，表示等待确认。

        plan_text 用于前端快速展示；plan_path 指向 data/plans 下的 Markdown 文件，
        方便直接打开，也方便后续让 Agent 读取已确认的方案。
        """
        with self._lock:
            now = utc_now()
            self._conn.execute(
                """
               INSERT INTO thread_plans (
                  plan_id, thread_id, run_id, status, prompt, plan_text, plan_path, created_at, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(plan_id) DO UPDATE SET
                  status=excluded.status,
                  prompt=excluded.prompt,
                  plan_text=excluded.plan_text,
                  plan_path=excluded.plan_path
                """,
                (
                    plan_id,
                    thread_id,
                    run_id,
                    status,
                    prompt.strip(),
                    plan_text.strip(),
                    plan_path,
                    now,
                ),
            )
            self._conn.commit()

    def get_latest_thread_plan(
        self, thread_id: str, *, status: str | None = None
    ) -> dict[str, Any] | None:
        """读取某个 thread 最新一份技术方案。"""
        with self._lock:
            query = "SELECT * FROM thread_plans WHERE thread_id = ?"
            params = (thread_id,)
            if status is not None:
                query += " AND status = ?"
                params += (status,)
            query += " ORDER BY created_at DESC LIMIT 1"
            row = self._conn.execute(query, params).fetchone()
            return self._row_to_dict(row)

    def list_thread_plans(self, thread_id: str) -> list[dict[str, Any]]:
        """按创建顺序读取某个 thread 的所有技术方案。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM thread_plans
                WHERE thread_id = ?
                ORDER BY created_at ASC
                """,
                (thread_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def approve_thread_plan(self, plan_id: str) -> dict[str, Any] | None:
        """把指定方案标记为已确认，并返回确认后的方案记录。"""
        with self._lock:
            self._conn.execute(
                """
                UPDATE thread_plans
                SET status = 'approved', approved_at = ?
                WHERE plan_id = ?
                """,
                (utc_now(), plan_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM thread_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            return self._row_to_dict(row)

    def finish_open_run_events(
        self, thread_id: str, *, status: str = "completed"
    ) -> None:
        """把仍处于运行中的展示事件收尾。

        Agent 的官方 tool_calls 流有时只发起始事件，真正完成状态由工具内部事件记录。
        为避免前端在任务结束后还显示“运行中...”，任务完成或失败时统一关闭残留事件。
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE run_events
                SET status = ?,
                    updated_at = ?
                WHERE thread_id = ?
                  AND status IN ('pending', 'in_progress')
                """,
                (status, utc_now(), thread_id),
            )
            self._conn.commit()

    def get_latest_run(self, thread_id: str) -> dict[str, Any] | None:
        """读取某个 thread 最近一次运行记录。

        Dashboard 摘要需要把失败原因展示给前端，否则用户只能看到 error 状态，
        不知道是模型、git、github 还是本地权限导致的问题。
        """

        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM runs
                WHERE thread_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            return self._row_to_dict(row)

    def get_thread_status(self, thread_id: str) -> dict[str, Any] | None:
        """返回兼容旧调用方的任务状态结构。"""

        thread = self.get_thread(thread_id)
        if thread is None:
            return None
        return {
            "thread_id": thread_id,
            "status": thread["latest_run_status"],
            "pr_url": thread.get("pr_url"),
            "branch": thread.get("branch_name"),
            "updated_at": thread.get("updated_at"),
        }

    def get_repo_memory(self, owner: str, repo: str) -> str | None:
        """读取仓库长期记忆的兼容存储。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM repo_memories WHERE owner = ? AND repo = ?",
                (owner, repo),
            ).fetchone()
            return str(row["content"]) if row is not None else None

    def upsert_repo_memory(self, owner: str, repo: str, content: str) -> None:
        """新增或更新仓库长期记忆。"""

        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO repo_memories (owner, repo, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner, repo) DO UPDATE SET
                  content = excluded.content,
                  updated_at = excluded.updated_at
                """,
                (owner, repo, content, now, now),
            )
            self._conn.commit()

    def delete_thread(self, thread_id: str) -> bool:
        """删除一个 dashboard 会话及其业务附属记录。

        这里清理的是业务 Store，不直接清理 LangGraph checkpoint。
        这一阶段列表页只读取 threads/runs/review_findings,
        所以删除这些记录后，前端侧边栏会立即干净。
        """
        with self._lock:
            existing = self.get_thread(thread_id)
            if existing is None:
                return False

            # 删除会话关联的审查发现项
            self._conn.execute(
                "DELETE FROM review_findings WHERE thread_id = ?", (thread_id,)
            )
            # 删除会话关联的技术方案/计划
            self._conn.execute(
                "DELETE FROM thread_plans WHERE thread_id = ?", (thread_id,)
            )
            # 删除会话关联的消息记录
            self._conn.execute(
                "DELETE FROM thread_messages WHERE thread_id = ?", (thread_id,)
            )
            # 删除会话关联的运行事件日志
            self._conn.execute(
                "DELETE FROM run_events WHERE thread_id = ?", (thread_id,)
            )
            # 删除会话关联的运行记录
            self._conn.execute(
                "DELETE FROM runs WHERE thread_id = ?", (thread_id,)
            )
            # 删除会话主记录（最后删除，防止外键约束冲突）
            self._conn.execute(
                "DELETE FROM threads WHERE thread_id = ?", (thread_id,)
            )
            self._conn.commit()
            return True

    def add_finding(
        self,
        *,
        finding_id: str,
        thread_id: str,
        file: str,
        line: int | None,
        severity: str,
        title: str,
        description: str,
        status: str = "open",
    ) -> None:
        """添加一个代码审查发现。
        Args:
            finding_id:  发现ID
            thread_id:会话ID
            file: 文件名
            line:  行号
            severity:  严重程度
            title:  标题
            description:  描述
            status:  状态
        """
        with self._lock:
            self._conn.execute(
                """
                 INSERT INTO review_findings (
                  id, thread_id, file, line, severity, title, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  file=excluded.file,
                  line=excluded.line,
                  severity=excluded.severity,
                  title=excluded.title,
                  description=excluded.description,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (
                    finding_id,
                    thread_id,
                    file,
                    line,
                    severity,
                    title,
                    description,
                    status,
                    utc_now(),
                    utc_now(),
                ),
            )
            self._conn.commit()

    def list_findings(self, thread_id: str) -> list[dict[str, Any]]:
        """列出某个会话的所有代码审查发现。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM review_findings
                WHERE thread_id = ?
                ORDER BY created_at ASC
                """,
                (thread_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_setting(self, key: str, value: Any) -> None:
        """设置一个配置项。
        Args:
            key: 配置项键
            value: 配置项值
        """
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), utc_now()),
            )
            self._conn.commit()

    def get_setting(self, key: str, default: Any = None) -> Any:
        """获取一个配置项的值。
        Args:
            key: 配置项键
            default: 默认值
        Returns:
            配置项值，如果不存在则返回默认值
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def upsert_repo_mapping(
        self,
        *,
        mapping_id: str,
        repo_url: str,
        repo_owner: str,
        repo_name: str,
        project_dir: str,
        local_path: str | None,
        source: str,
        notes: str | None = None,
        is_active: bool = True,
        verified: bool = False,
    ) -> dict[str, Any]:
        """保存或更新 GitHub 仓库与本地 projects 目录的映射关系。

        一个标准 repo_url 同一时间只允许有一个 active 映射。
        用户后续手动调整 project_dir 时，先把旧映射停用，再写入新映射，避免 Agent 在多个目录间切换。

        Args:
            mapping_id: 映射ID
            repo_url: 仓库URL
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            project_dir: 项目目录
            local_path: 本地路径
            source: 源
            notes: 备注
            is_active: 是否活动
            verified: 是否验证

        Returns:
            映射记录  dict[str, Any]
        """

        with self._lock:
            now = utc_now()
            if is_active:
                self._conn.execute(
                    """
                    UPDATE repo_workspace_mappings
                    SET is_active = 0,
                        updated_at = ?
                    WHERE repo_url = ?
                      AND id != ?
                      AND is_active = 1
                    """,
                    (utc_now(), repo_url, mapping_id),
                )
            self._conn.execute(
                """
                 INSERT INTO repo_workspace_mappings (
                  id, repo_url, repo_owner, repo_name, project_dir, local_path,
                  is_active, source, notes, created_at, updated_at, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  repo_url=excluded.repo_url,
                  repo_owner=excluded.repo_owner,
                  repo_name=excluded.repo_name,
                  project_dir=excluded.project_dir,
                  local_path=excluded.local_path,
                  is_active=excluded.is_active,
                  source=excluded.source,
                  notes=excluded.notes,
                  updated_at=excluded.updated_at,
                  last_verified_at=COALESCE(excluded.last_verified_at, repo_workspace_mappings.last_verified_at)
                """,
                (
                    mapping_id,
                    repo_url,
                    repo_owner,
                    repo_name,
                    project_dir,
                    local_path,
                    1 if is_active else 0,
                    source,
                    notes,
                    now,
                    now,
                    now if verified else None,
                ),
            )
            self._conn.commit()

            row = self._conn.execute(
                "SELECT *  FROM repo_workspace_mappings WHERE id = ? ", (mapping_id,)
            ).fetchone()

            result = self._row_to_dict(row)
            if result is None:
                raise RuntimeError("仓库映射保存后读取失败")
            return result

    def get_repo_mapping(self, repo_url: str) -> dict[str, Any] | None:
        """按标准化 repo_url 读取当前启用的目录映射。"""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM repo_workspace_mappings
                WHERE  repo_url = ? AND is_active = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (repo_url,),
            ).fetchone()
            return self._row_to_dict(row)

    def list_repo_mappings(
        self, *, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        """读取全部仓库目录映射，供工具或后续管理页面展示"""

        with self._lock:
            if include_inactive:
                rows = self._conn.execute(
                    "SELECT * FROM repo_workspace_mappings ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM repo_workspace_mappings  WHERE is_active = 1 ORDER BY updated_at DESC"
                ).fetchall()

            return [dict(row) for row in rows]

    def mark_repo_mapping_verified(
        self, mapping_id: str, *, notes: str | None = None
    ) -> None:
        """更新映射最近一次验证时间
        Args:
            mapping_id: 映射ID
            notes: 备注
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE repo_workspace_mappings
                SET last_verified_at = ?,
                    notes =  COALESCE(?, notes),
                    updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), notes, utc_now(), mapping_id),
            )
            self._conn.commit()
