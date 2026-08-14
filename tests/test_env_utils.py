import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.env_utils import _apply_platform_path_profile


class PlatformPathProfileTests(unittest.TestCase):
    def test_windows_profile_overrides_generic_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "AI_WORKSPACE_ROOT_MACOS=/Users/test/ai_workspace\n"
                "AI_WORKSPACE_ROOT_WINDOWS=~/ai_workspace\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "LOCAL_SHELL_PLATFORM": "windows",
                    "AI_WORKSPACE_ROOT": "/old/path",
                },
                clear=False,
            ):
                _apply_platform_path_profile(env_file)
                self.assertEqual(os.environ["AI_WORKSPACE_ROOT"], "~/ai_workspace")

    def test_empty_platform_path_keeps_generic_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("LOCAL_SHELL_WORKSPACE_WINDOWS=\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "LOCAL_SHELL_PLATFORM": "windows",
                    "LOCAL_SHELL_WORKSPACE": "/old/path",
                },
                clear=False,
            ):
                _apply_platform_path_profile(env_file)
                self.assertEqual(os.environ["LOCAL_SHELL_WORKSPACE"], "/old/path")

    def test_langgraph_store_path_uses_platform_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "LANGGRAPH_STORE_DB_PATH_MACOS=/tmp/langgraph-store.sqlite\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"LOCAL_SHELL_PLATFORM": "macos"},
                clear=False,
            ):
                _apply_platform_path_profile(env_file)
                self.assertEqual(
                    os.environ["LANGGRAPH_STORE_DB_PATH"],
                    "/tmp/langgraph-store.sqlite",
                )


if __name__ == "__main__":
    unittest.main()
