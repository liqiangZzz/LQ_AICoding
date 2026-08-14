import tempfile
import unittest
from pathlib import Path

from agent.store import LocalSqliteStore


class LocalSqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = LocalSqliteStore(Path(self.temp_dir.name) / "store.sqlite")

    def tearDown(self) -> None:
        # Windows 不允许删除仍被 SQLite 连接占用的数据库文件；
        # 先显式关闭 Store，再清理临时目录，macOS 与 Windows 行为才一致。
        self.store.close()
        self.temp_dir.cleanup()

    def test_run_event_is_upserted(self) -> None:
        common = {
            "event_id": "thread-1:read:file.py",
            "thread_id": "thread-1",
            "key": "read:file.py",
            "title": "读取文件",
            "kind": "read",
        }
        self.store.add_run_event(**common, status="in_progress")
        self.store.add_run_event(**common, status="completed", detail="ok")

        events = self.store.list_run_events("thread-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(events[0]["detail"], "ok")

    def test_finding_round_trip(self) -> None:
        self.store.add_finding(
            finding_id="finding-1",
            thread_id="thread-1",
            file="agent/app.py",
            line=12,
            severity="major",
            title="示例问题",
            description="示例描述",
        )

        findings = self.store.list_findings("thread-1")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["id"], "finding-1")
        self.assertEqual(findings[0]["line"], 12)
        self.assertEqual(findings[0]["status"], "open")

    def test_thread_status_preserves_existing_pr_fields(self) -> None:
        self.store.update_thread_status(
            "thread-1",
            "pr_created",
            pr_url="https://github.com/example/repo/pull/1",
            branch="feature/example",
        )
        self.store.update_thread_status("thread-1", "completed")

        status = self.store.get_thread_status("thread-1")
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["branch"], "feature/example")
        self.assertEqual(status["pr_url"], "https://github.com/example/repo/pull/1")

    def test_repo_memory_round_trip(self) -> None:
        self.assertIsNone(self.store.get_repo_memory("example", "repo"))
        self.store.upsert_repo_memory("example", "repo", "项目使用 FastAPI。")
        self.assertEqual(
            self.store.get_repo_memory("example", "repo"),
            "项目使用 FastAPI。",
        )


if __name__ == "__main__":
    unittest.main()
