import unittest
from unittest.mock import MagicMock, patch

from agent.tools import github_api


class GitHubApiReadTests(unittest.TestCase):
    @patch.object(github_api, "get_github_token", return_value="secret-token")
    @patch.object(github_api.httpx, "Client")
    def test_get_uses_authorization_header_not_query_token(
        self,
        client_class: MagicMock,
        _get_token: MagicMock,
    ) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"number": 7}
        client = client_class.return_value.__enter__.return_value
        client.get.return_value = response

        result = github_api.get_pull_request(owner="owner", repo="repo", number=7)

        self.assertEqual(result, {"number": 7})
        kwargs = client.get.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")
        self.assertNotIn("access_token", kwargs["params"])

    @patch.object(github_api, "_github_get")
    def test_paginated_reads_continue_until_short_page(self, github_get: MagicMock) -> None:
        github_get.side_effect = [[{"sha": str(index)} for index in range(100)], [{"sha": "last"}]]

        commits = github_api.list_pull_request_commits(owner="owner", repo="repo", number=7)

        self.assertEqual(len(commits), 101)
        self.assertEqual(github_get.call_args_list[0].kwargs["params"], {"per_page": 100, "page": 1})
        self.assertEqual(github_get.call_args_list[1].kwargs["params"], {"per_page": 100, "page": 2})

    @patch.object(github_api, "_github_get_all", return_value=[])
    def test_regular_comments_use_issues_endpoint(self, github_get_all: MagicMock) -> None:
        github_api.list_pull_request_comments(owner="owner", repo="repo", number=7)

        github_get_all.assert_called_once_with("/repos/owner/repo/issues/7/comments")


if __name__ == "__main__":
    unittest.main()
