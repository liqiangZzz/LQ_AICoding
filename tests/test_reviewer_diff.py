import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.tools.reviewer_diff import (
    get_local_diff_summary,
    parse_unified_diff,
    validate_finding_location,
)

DIFF_WITH_SPACE = """diff --git \"a/src/my file.py\" \"b/src/my file.py\"
--- \"a/src/my file.py\"
+++ \"b/src/my file.py\"
@@ -1,2 +1,3 @@
 old
+new
 tail
"""


class ReviewerDiffTests(unittest.TestCase):
    def test_parse_paths_with_spaces_and_changed_lines(self) -> None:
        summary = parse_unified_diff(DIFF_WITH_SPACE)

        self.assertEqual(summary.changed_file_paths, ("src/my file.py",))
        self.assertEqual(summary.changed_lines_for("src\\my file.py"), (2,))

    def test_validate_finding_requires_changed_line(self) -> None:
        summary = parse_unified_diff(DIFF_WITH_SPACE)

        self.assertEqual(
            validate_finding_location(summary, file="src/my file.py", line=2),
            (True, "finding 位置有效。"),
        )
        self.assertFalse(validate_finding_location(summary, file="src/my file.py", line=1)[0])
        self.assertFalse(validate_finding_location(summary, file="other.py", line=None)[0])

    def test_get_local_diff_uses_fixed_git_command(self) -> None:
        backend = MagicMock()
        backend.run.return_value = SimpleNamespace(
            exit_code=0,
            stdout=DIFF_WITH_SPACE,
            stderr="",
        )

        summary = get_local_diff_summary(
            backend,
            repo_dir="projects/demo",
            base="main",
            head="feature/review",
        )

        backend.run.assert_called_once_with(
            "git diff --unified=80 main...feature/review --",
            cwd="projects/demo",
        )
        self.assertEqual(summary.head, "feature/review")

    def test_rejects_unsafe_git_ref_before_execution(self) -> None:
        backend = MagicMock()

        with self.assertRaises(ValueError):
            get_local_diff_summary(backend, repo_dir="projects/demo", base="main;whoami")

        backend.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
