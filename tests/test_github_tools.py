import unittest
from unittest.mock import patch

from agent.tools import github_tools


class GitHubToolsPermissionTests(unittest.TestCase):
    @patch.object(github_tools, "post_pr_comment")
    @patch.object(github_tools, "runtime_is_read_only_task", return_value=True)
    def test_read_only_task_cannot_publish_comment(
        self,
        _is_read_only,
        post_pr_comment,
    ) -> None:
        result = github_tools.publish_github_pr_comment.invoke(
            {"owner": "owner", "repo": "repo", "number": 7, "body": "review"}
        )

        self.assertFalse(result["ok"])
        post_pr_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
