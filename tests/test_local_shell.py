import tempfile
import unittest
from pathlib import Path

from agent.backends.local_shell import LocalShellBackend, _resolve_configured_subpath


class LocalShellBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.backend = LocalShellBackend(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_execute_applies_command_whitelist(self) -> None:
        response = self.backend.execute("rm -rf /projects")
        self.assertEqual(response.exit_code, 126)
        self.assertIn("命令被拒绝", response.output)

    def test_virtual_path_prefix_must_match_complete_root_name(self) -> None:
        reason = self.backend._deny_reason("cat /projects-private/file.txt")
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("outside workspace", reason)

    def test_platform_specific_runtime_paths(self) -> None:
        self.backend.platform = "macos"
        self.assertEqual(self.backend._venv_bin_dir().name, "bin")
        self.assertEqual(self.backend._venv_python_path().name, "python")
        self.assertEqual(self.backend._askpass_path().suffix, ".sh")

        self.backend.platform = "windows"
        self.assertEqual(self.backend._venv_bin_dir().name, "Scripts")
        self.assertEqual(self.backend._venv_python_path().name, "python.exe")
        self.assertEqual(self.backend._askpass_path().suffix, ".cmd")

    def test_configured_subpaths_reject_mac_windows_and_traversal_paths(self) -> None:
        root = Path(self.temp_dir.name).resolve()
        for value in ("/tmp/projects", "C:/projects", r"C:\projects", "../projects", r"..\projects"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _resolve_configured_subpath(root, value, env_name="TEST_PATH")

    def test_configured_subpath_is_resolved_inside_workspace(self) -> None:
        root = Path(self.temp_dir.name).resolve()
        result = _resolve_configured_subpath(
            root,
            "runtimes/python/default/.venv",
            env_name="TEST_PATH",
        )
        self.assertEqual(result, root / "runtimes/python/default/.venv")

    def test_read_only_backend_blocks_commands_and_writes(self) -> None:
        backend = LocalShellBackend(self.temp_dir.name, read_only=True)
        command = backend.execute("python --version")
        write = backend.write("/projects/example.txt", "example")

        self.assertEqual(command.exit_code, 126)
        self.assertIsNotNone(write.error)

    def test_read_only_backend_allows_repository_inspection(self) -> None:
        backend = LocalShellBackend(self.temp_dir.name, read_only=True)
        result = backend.execute("git status")

        self.assertNotEqual(result.exit_code, 126)


if __name__ == "__main__":
    unittest.main()
