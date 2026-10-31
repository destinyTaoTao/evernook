from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import (Change, ConsolidationDraft, ConsolidationWorkflow, Event, EvidenceSpan, HARNESS_DEFAULT_SCOPES,
                      MemoryFailure, MemoryRuntime, Source, initialize, pair_client)
from evernook.profile import ProfileRegistry


class ConsolidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-consolidation-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, _ = initialize(self.directory / "data", client_id="owner", entry="owner")
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / "mcp-access-owner.json"
        pair_client(self.config, client_id="owner", entry="owner", access_path=self.owner_path)
        self.access = self.runtime.access_from_file(self.owner_path)
        self.harness_path = self.config.parent / "mcp-access-harness.json"
        self.harness = pair_client(self.config, client_id="harness", entry="harness",
                                   access_path=self.harness_path, scopes=HARNESS_DEFAULT_SCOPES)
        self.worker_path = self.config.parent / "mcp-access-worker.json"
        self.worker = pair_client(self.config, client_id="worker", entry="worker",
                                  access_path=self.worker_path, scopes=("recall", "capture", "maintain", "accept"))
        self.workflow = ConsolidationWorkflow(self.runtime)
        self.number = 0

    def source(self, text: str, *, role: str = "user") -> EvidenceSpan:
        self.number += 1
        event = Event(Source("worker", "session", "main", str(self.number)), role, self.number,
                      "2026-10-30T10:00:00+08:00", "complete", text)
        receipt = self.runtime.submit(self.worker, event)
        return EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)

    def draft(self, kind: str, label: str, span: EvidenceSpan, *, category: str = "interest",
              confidence: float = 0.7) -> ConsolidationDraft:
        return ConsolidationDraft(kind, label, (span,), category=category, confidence=confidence)

    def invoke(self, command: str, payload: object) -> dict[str, Any]:
        process = subprocess.run([sys.executable, "-m", "evernook.cli", command, "--config", str(self.config),
                                  "--access", str(self.worker_path)],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True, timeout=30,
            cwd=Path(__file__).resolve().parents[2])
        result: dict[str, Any] = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(process.stderr, b"")
        self.assertEqual(process.returncode, 0 if result["ok"] else 1)
        return result

    def test_complete_lands_profile_proposals_and_queue_rows_without_entering_recall(self) -> None:
        span = self.source("合成：用户说最近迷上了胶片摄影")
        before_messages = self.runtime.status(self.access).messages
        lease = self.workflow.begin(self.worker, (span.message_id,))
        assert lease.token is not None
        result = self.workflow.complete(self.worker, lease.status.run_id, lease.token, (
            self.draft("profile_tag", "喜欢胶片摄影", span),
            self.draft("topic", "胶片摄影入门", span, category="topic"),
            self.draft("navigation_summary", "十月摄影话题摘录", span),
        ))
        self.assertEqual(result.run.state, "succeeded")
        self.assertEqual([row.state for row in result.drafts], ["pending_review", "pending_review", "pending_review"])
        proposal_ids = [row.proposal_id for row in result.drafts]
        self.assertIsInstance(proposal_ids[0], str)
        self.assertEqual(proposal_ids[1:], [None, None])
        profile = ProfileRegistry(self.runtime).read(self.access, proposal_ids[0])  # type: ignore[arg-type]
        self.assertEqual(profile.state, "draft")
        queue = self.workflow.queue(self.access)
        self.assertEqual(len(queue.rows), 3)
        self.assertEqual(self.runtime.status(self.access).messages, before_messages)
        searched = self.runtime.search(self.access, "胶片摄影", mode="lexical")
        self.assertEqual([hit.message_id for hit in searched.hits], [span.message_id])

    def test_rerun_is_idempotent_and_conflicting_results_are_rejected(self) -> None:
        span = self.source("合成：用户提到喜欢周末爬山")
        lease = self.workflow.begin(self.worker, (span.message_id,))
        assert lease.token is not None
        drafts = (self.draft("profile_tag", "喜欢爬山", span),)
        first = self.workflow.complete(self.worker, lease.status.run_id, lease.token, drafts)
        second = self.workflow.complete(self.worker, lease.status.run_id, lease.token, drafts)
        self.assertEqual([row.candidate_id for row in first.drafts], [row.candidate_id for row in second.drafts])
        self.assertEqual(first.drafts[0].proposal_id, second.drafts[0].proposal_id)
        self.assertEqual(len(self.workflow.queue(self.access).rows), 1)
        with self.assertRaisesRegex(MemoryFailure, "candidate_result_conflict"):
            self.workflow.complete(self.worker, lease.status.run_id, lease.token,
                                   (self.draft("profile_tag", "喜欢夜爬", span),))

    def test_suppressed_source_stales_the_run_and_blocks_completion(self) -> None:
        span = self.source("合成：将被抑制的沉淀来源")
        lease = self.workflow.begin(self.worker, (span.message_id,))
        assert lease.token is not None
        control = self.source("合成：抑制控制消息")
        self.runtime.change(self.worker, Change("consol-suppress", span.message_id, 1, "suppress",
                                                control.message_id, "合成"))
        status = self.workflow.status(self.worker, lease.status.run_id)
        self.assertEqual(status.state, "stale")
        with self.assertRaisesRegex(MemoryFailure, "stale_source"):
            self.workflow.complete(self.worker, lease.status.run_id, lease.token,
                                   (self.draft("profile_tag", "不该成立的标签", span),))

    def test_scope_guards_and_sensitive_labels(self) -> None:
        span = self.source("合成：权限测试来源")
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.workflow.begin(self.harness, (span.message_id,))
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.workflow.queue(self.harness)
        lease = self.workflow.begin(self.worker, (span.message_id,))
        assert lease.token is not None
        with self.assertRaisesRegex(MemoryFailure, "protected_consolidation_draft"):
            self.workflow.complete(self.worker, lease.status.run_id, lease.token,
                                   (self.draft("profile_tag", "password=synthetic-secret-3", span),))
        with self.assertRaisesRegex(MemoryFailure, "invalid_consolidation_draft"):
            self.workflow.complete(self.worker, lease.status.run_id, lease.token,
                                   (self.draft("mood", "不存在的类别", span),))

    def test_cli_end_to_end_begin_complete_queue(self) -> None:
        span = self.source("合成：CLI 沉淀来源，用户喜欢手冲咖啡")
        source_payload = {"message_id": span.message_id, "state_version": 1, "representation": "original",
                          "content_version": 1, "start": 0, "end": len(span.expected_text),
                          "expected_text": span.expected_text}
        begin = self.invoke("consolidation-begin", {"source_ids": [span.message_id]})
        run_id = begin["result"]["status"]["run_id"]
        token = begin["result"]["token"]
        completed = self.invoke("consolidation-complete", {"run_id": run_id, "token": token, "drafts": [
            {"kind": "profile_tag", "label": "喜欢手冲咖啡", "sources": [source_payload],
             "category": "preference", "confidence": 0.8},
            {"kind": "topic", "label": "咖啡话题", "sources": [source_payload]}]})
        self.assertEqual(completed["result"]["run"]["state"], "succeeded")
        queue = self.invoke("consolidation-queue", {})
        self.assertEqual(len(queue["result"]["rows"]), 2)
        profile_rows = [row for row in queue["result"]["rows"] if row["kind"] == "profile_tag"]
        self.assertEqual(len(profile_rows), 1)
        self.assertIsNotNone(profile_rows[0]["proposal_id"])


if __name__ == "__main__":
    unittest.main()
