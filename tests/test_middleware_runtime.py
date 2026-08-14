import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import ToolMessage

from agent.backends.local_shell import LocalShellBackend
from agent.core.middleware.run_limits import (
    AgentRunLimitExceeded,
    AgentRunLimits,
    AgentRunLimitTracker,
)
from agent.core.middleware.tool_error import ToolErrorMiddleware
from agent.core.middleware.tool_sanitize import (
    SanitizeToolInputsMiddleware,
    ToolInputRejected,
)
from agent.tools.runtime_context import get_runtime_task_kind, runtime_is_read_only_task


class MiddlewareRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.backend = LocalShellBackend(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sync_tool_error_is_returned_as_tool_message(self) -> None:
        middleware = ToolErrorMiddleware(backend=self.backend)
        request = SimpleNamespace(
            tool_call={"id": "call-1", "name": "read_file", "args": {"path": "missing.py"}}
        )

        result = middleware.wrap_tool_call(
            request,
            lambda _: (_ for _ in ()).throw(FileNotFoundError("missing.py")),
        )

        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.tool_call_id, "call-1")
        self.assertEqual(result.status, "error")
        self.assertEqual(json.loads(result.content)["error_type"], "FileNotFoundError")

    def test_sanitize_rejection_records_event_when_thread_exists(self) -> None:
        middleware = SanitizeToolInputsMiddleware(backend=self.backend)
        request = SimpleNamespace(
            runtime=SimpleNamespace(config={"configurable": {"thread_id": "thread-1"}})
        )

        with patch("agent.core.middleware.tool_sanitize.record_event") as record_event:
            middleware._record_rejection(
                request,
                "read_file",
                ToolInputRejected("invalid path"),
            )

        record_event.assert_called_once()
        self.assertEqual(record_event.call_args.args[0], "thread-1")


class AgentRunLimitTrackerTests(unittest.TestCase):
    def test_tools_event_payload_is_counted(self) -> None:
        tracker = AgentRunLimitTracker(
            AgentRunLimits(max_tool_calls=2, max_seconds=60, task_kind="test")
        )

        tracker.observe_event(
            {"method": "tools", "params": {"data": [{"event": "tool-started"}]}}
        )

        self.assertEqual(tracker.tool_calls, 1)

    def test_malformed_tools_event_is_ignored(self) -> None:
        tracker = AgentRunLimitTracker(
            AgentRunLimits(max_tool_calls=2, max_seconds=60, task_kind="test")
        )

        tracker.observe_event({"method": "tools", "params": None})

        self.assertEqual(tracker.tool_calls, 0)

    def test_exact_limit_is_allowed_and_next_call_is_blocked(self) -> None:
        tracker = AgentRunLimitTracker(
            AgentRunLimits(max_tool_calls=2, max_seconds=60, task_kind="test")
        )
        event = {"method": "tool_calls", "params": {"data": {}}}

        tracker.observe_event(event)
        tracker.observe_event(event)
        with self.assertRaises(AgentRunLimitExceeded):
            tracker.observe_event(event)


class RuntimeContextTests(unittest.TestCase):
    def test_review_task_keeps_read_only_permission(self) -> None:
        with patch(
            "agent.tools.runtime_context.get_runtime_configurable",
            return_value={"task_kind": "review"},
        ):
            self.assertEqual(get_runtime_task_kind(), "review")
            self.assertTrue(runtime_is_read_only_task())


if __name__ == "__main__":
    unittest.main()
