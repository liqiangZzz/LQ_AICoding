import unittest

from agent.backends.permissions import WorkspacePermissionError, normalize_safe_command


class NormalizeSafeCommandTests(unittest.TestCase):
    def test_allows_windows_development_commands(self) -> None:
        self.assertEqual(normalize_safe_command("py -m pytest repo"), "py -m pytest repo")
        self.assertEqual(normalize_safe_command("dir repo"), "dir repo")
        self.assertEqual(normalize_safe_command("where git"), "where git")

    def test_still_blocks_shell_operators(self) -> None:
        with self.assertRaises(WorkspacePermissionError):
            normalize_safe_command("dir repo && type repo\\.env")


if __name__ == "__main__":
    unittest.main()
