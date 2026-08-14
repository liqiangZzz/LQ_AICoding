import tempfile
import unittest

from agent.backends.local_shell import LocalShellBackend
from agent.core.middleware.tool_sanitize import (
    ToolInputRejected,
    sanitize_tool_kwargs,
    sanitize_workspace_path,
)


class ToolSanitizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.backend = LocalShellBackend(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_virtual_workspace_path_is_allowed(self) -> None:
        result = sanitize_workspace_path(
            "/projects/example/app.py",
            argument_name="file_path",
            backend=self.backend,
        )
        self.assertEqual(result, "/projects/example/app.py")

    def test_host_path_outside_workspace_is_rejected(self) -> None:
        with self.assertRaises(ToolInputRejected):
            sanitize_workspace_path(
                "/Users/example/private.txt",
                argument_name="file_path",
                backend=self.backend,
            )

    def test_github_url_is_normalized_and_credentials_are_removed(self) -> None:
        result = sanitize_tool_kwargs(
            "clone_repo",
            {"repo_url": "https://secret@github.com/example/repo"},
            backend=self.backend,
        )
        self.assertEqual(result["repo_url"], "https://github.com/example/repo.git")

    def test_non_github_url_is_rejected(self) -> None:
        with self.assertRaises(ToolInputRejected):
            sanitize_tool_kwargs(
                "clone_repo",
                {"repo_url": "https://gitlab.com/example/repo.git"},
                backend=self.backend,
            )


if __name__ == "__main__":
    unittest.main()
