import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.core import runtime
from agent.store import LocalSqliteStore


class RuntimeCrossPlatformSmokeTests(unittest.TestCase):
    def test_workspace_listing_uses_platform_neutral_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalSqliteStore(root / "store.sqlite")
            try:
                with (
                    patch.object(runtime, "WORKSPACE_ROOT", root / "workspace"),
                    patch.object(runtime, "PROJECTS_DIR", root / "workspace" / "projects"),
                    patch.object(runtime, "get_store", return_value=store),
                    patch.object(runtime, "record_event"),
                ):
                    result = runtime.run_workspace_listing_task(
                        repo_url="https://github.com/example/repo.git",
                        prompt="查看本地工作区",
                        thread_id="cross-platform-smoke",
                    )
            finally:
                store.close()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["projects"], [])


if __name__ == "__main__":
    unittest.main()
