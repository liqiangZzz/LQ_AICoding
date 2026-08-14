import unittest
from unittest.mock import patch

from agent.platform_utils import platform_display_name, resolve_local_shell_platform


class ResolveLocalShellPlatformTests(unittest.TestCase):
    def test_explicit_platforms_and_aliases(self) -> None:
        self.assertEqual(resolve_local_shell_platform("macos"), "macos")
        self.assertEqual(resolve_local_shell_platform("darwin"), "macos")
        self.assertEqual(resolve_local_shell_platform("windows"), "windows")
        self.assertEqual(resolve_local_shell_platform("win32"), "windows")

    def test_auto_uses_host_platform(self) -> None:
        with patch("agent.platform_utils.host_platform", return_value="windows"):
            self.assertEqual(resolve_local_shell_platform("auto"), "windows")

    def test_invalid_platform_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "LOCAL_SHELL_PLATFORM"):
            resolve_local_shell_platform("linux")

    def test_display_name(self) -> None:
        self.assertEqual(platform_display_name("macos"), "macOS")
        self.assertEqual(platform_display_name("windows"), "Windows")


if __name__ == "__main__":
    unittest.main()
