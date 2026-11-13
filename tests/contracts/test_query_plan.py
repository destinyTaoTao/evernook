from __future__ import annotations

from dataclasses import asdict
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Event, MemoryRuntime, Source, initialize
from evernook.models import MemoryFailure
from evernook.query_plan import plan_query
from evernook.semantic import RecallPage, SemanticRecall
from evernook.service import dispatch
from test_semantic import SyntheticEmbedding


class QueryPlanTests(unittest.TestCase):
    def test_short_input_is_unchanged_and_long_tail_coordinates_are_exact(self) -> None:
        self.assertEqual(plan_query("原始短句").query, "原始短句")
        original = "🙂无关背景。" * 100 + "真正的问题：以前的饮食偏好？"
        plan = plan_query(original)
        self.assertTrue(plan.limited)
        self.assertEqual(plan.query, original[plan.represented_start:plan.represented_end])
        self.assertLessEqual(len(plan.query), 256)
        explicit = plan_query(original, "饮食偏好")
        self.assertEqual(explicit.kind, "explicit_intent")
        self.assertIsNone(explicit.represented_start)

    def test_invalid_encoding_empty_and_oversized_intents_are_rejected(self) -> None:
        for intent in ("", " ", "x" * 257):
            with self.assertRaisesRegex(MemoryFailure, "invalid_query_intent"):
                plan_query("正常", intent)
        with self.assertRaisesRegex(MemoryFailure, "invalid_encoding"):
            plan_query("\ud800")
        with self.assertRaisesRegex(MemoryFailure, "invalid_encoding"):
            plan_query("正常", "\ud800")

    def test_trailing_whitespace_does_not_replace_the_question_with_an_empty_tail(self) -> None:
        original = "有意义的问题？" + " \n" * 300
        plan = plan_query(original)
        self.assertEqual(plan.query, "有意义的问题？")
        self.assertEqual(plan.query, original[plan.represented_start:plan.represented_end])

    def test_service_direct_and_mcp_recall_use_the_configured_backend_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-query-plan-") as temporary:
            config, access = initialize(Path(temporary) / "runtime", client_id="synthetic", entry="test")
            runtime = MemoryRuntime(config)
            receipt = runtime.submit(access, Event(Source("test", "s", "b", "1"), "user", 1, None, "complete", "合成：辛辣"))
            backend = SyntheticEmbedding()
            SemanticRecall(runtime, backend).refresh(access)
            arguments = {"query": "无关背景。" * 100, "query_intent": "饮食偏好"}
            with patch("evernook.embedding.load_embedding", return_value=backend):
                for operation, args in (("recall", arguments), ("mcp", {"name": "evernook_v1_recall", "profile": "recall", "arguments": arguments})):
                    request = {"version": 1, "access": asdict(access), "operation": operation, "arguments": args}
                    result = dispatch(runtime, request, embedding_config=Path(temporary) / "synthetic-config.json")
                    assert isinstance(result, RecallPage)
                    self.assertEqual(result.hits[0].evidence.message_id, receipt.message_id)
                    cancel = threading.Event()
                    cancel.set()
                    with self.assertRaisesRegex(MemoryFailure, "recall_canceled"):
                        dispatch(runtime, request, cancel=cancel, embedding_config=Path(temporary) / "synthetic-config.json")

    def test_direct_stdio_handler_cancellation_reaches_the_running_recall(self) -> None:
        import anyio
        from mcp import types
        from mcp.server import Server
        import evernook.mcp_server as adapter
        with tempfile.TemporaryDirectory(prefix="evernook-mcp-cancel-") as temporary:
            config, access = initialize(Path(temporary) / "runtime", client_id="synthetic", entry="test")
            callbacks: dict[str, Any] = {}
            def capture(*args: Any, **kwargs: Any) -> Any:
                callbacks.update(kwargs)
                return Server(*args, **kwargs)
            with patch("evernook.mcp_server.Server", side_effect=capture):
                adapter.build_server(MemoryRuntime(config), access)
            entered, stopped, saw_cancel = threading.Event(), threading.Event(), threading.Event()
            def running(*args: object, cancel: threading.Event | None = None, **kwargs: object) -> object:
                entered.set()
                try:
                    if cancel is not None and cancel.wait(5):
                        saw_cancel.set()
                    elif cancel is None:
                        stopped.wait(2)
                    return {}
                finally:
                    stopped.set()
            async def exercise() -> None:
                task = asyncio.create_task(callbacks["on_call_tool"](None, types.CallToolRequestParams(
                    name="evernook_v1_recall", arguments={"query": "合成"})))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(await asyncio.to_thread(stopped.wait, 2))
                self.assertTrue(saw_cancel.is_set())
            with patch("evernook.mcp_server.SemanticRecall.recall", side_effect=running):
                anyio.run(exercise)


if __name__ == "__main__":
    unittest.main()
