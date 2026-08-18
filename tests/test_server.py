import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import server


class ServerAgentPermissionsTests(unittest.TestCase):
    """验证任务类型会落实到后端权限，而不只是停留在提示词。"""

    def _build(self, task_kind: str) -> SimpleNamespace:
        backend = SimpleNamespace(read_only=False)
        agent = MagicMock()
        agent.with_config.return_value = agent
        with (
            patch.object(server, "ensure_backend_for_thread", return_value=backend),
            patch.object(
                server,
                "_prepare_repo_backend_context",
                return_value=(backend, None, None),
            ),
            patch.object(server, "make_main_model", return_value=MagicMock()),
            patch.object(
                server,
                "create_summarization_tool_middleware",
                return_value=MagicMock(),
            ),
            patch.object(server, "get_checkpointer", return_value=MagicMock()),
            patch.object(server, "get_langgraph_store", return_value=MagicMock()),
            patch.object(server, "create_deep_agent", return_value=agent) as create_deep_agent,
        ):
            server.get_agent(
                {
                    "configurable": {
                        "thread_id": "thread-1",
                        "task_kind": task_kind,
                        "__is_for_execution__": True,
                    }
                }
            )
        self.create_agent_call = create_deep_agent.call_args
        return backend

    def test_planning_agent_uses_read_only_backend(self) -> None:
        self.assertTrue(self._build("planning").read_only)

    def test_coding_agent_uses_writable_backend(self) -> None:
        self.assertFalse(self._build("coding").read_only)

    def test_agent_loads_workspace_skills(self) -> None:
        self._build("review")

        self.assertEqual(self.create_agent_call.kwargs["skills"], ["/skills/"])

    def test_review_agent_permissions_do_not_allow_project_or_memory_writes(self) -> None:
        self._build("review")

        permissions = self.create_agent_call.kwargs["permissions"]
        write_allow_paths = {
            path
            for permission in permissions
            if permission.mode == "allow" and "write" in permission.operations
            for path in permission.paths
        }
        self.assertNotIn("/projects/**", write_allow_paths)
        self.assertNotIn("/memories/**", write_allow_paths)

    def test_coding_agent_permissions_allow_project_writes_only(self) -> None:
        self._build("coding")

        permissions = self.create_agent_call.kwargs["permissions"]
        write_allow_paths = {
            path
            for permission in permissions
            if permission.mode == "allow" and "write" in permission.operations
            for path in permission.paths
        }
        self.assertIn("/projects/**", write_allow_paths)
        self.assertNotIn("/memories/**", write_allow_paths)


if __name__ == "__main__":
    unittest.main()
