from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.experience import ExperienceDecision, ExperienceRegistry
from evernook.models import Access, AttachmentInput, Capture
from evernook.skill_drafts import SkillDraftProposer


class SkillDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="evernook-skill-draft-")
        self.addCleanup(temp.cleanup)
        self.config, self.owner = initialize(Path(temp.name) / "runtime", client_id="synthetic", entry="dsh")
        self.runtime = MemoryRuntime(self.config)
        self.proposer = SkillDraftProposer(self.runtime)
        self.sequence = 0

    def event(self, role: str, completion: str, text: str, *, entry: str = "dsh",
              session: str = "synthetic-session", branch: str = "synthetic-branch") -> Event:
        self.sequence += 1
        return Event(Source(entry, session, branch, str(self.sequence)), role, self.sequence, None, completion, text)

    def save_capture(self, event: Event, kind: str, input_id: str | None = None,
                     tool: str | None = None, attachments: tuple[AttachmentInput, ...] = (),
                     access: Access | None = None) -> str:
        return self.runtime.submit_capture(access or self.owner, Capture(event, kind, input_id, tool, attachments)).message.message_id

    def save_input(self, text: str = "合成请求：读取练习文件。") -> str:
        return self.save_capture(self.event("user", "complete", text), "message")

    def save_result(self, input_id: str, completion: str = "failed", text: str = "合成工具：读取失败。") -> str:
        return self.save_capture(self.event("tool", completion, text), "tool_result", input_id, "synthetic-tool")

    def test_captured_failed_and_canceled_results_create_exact_bounded_review_only_records(self) -> None:
        user_text = "合成原始请求🙂" * 100
        result_text = "合成工具结果🧪" * 100
        input_id = self.save_input(user_text)
        registry = ExperienceRegistry(self.runtime)
        for completion in ("failed", "canceled"):
            with self.subTest(completion=completion):
                result_id = self.save_result(input_id, completion, result_text)
                receipt = self.runtime.read_capture(self.owner, result_id)
                assert receipt is not None
                self.assertEqual((receipt.input_id, receipt.gaps, receipt.complete), (input_id, ("incomplete_output",), False))
                created = self.proposer.propose(self.owner, tool_result_id=result_id)
                assert created is not None
                self.assertEqual((created.state, created.revision, created.draft.kind), ("draft", 1, "skill_draft"))
                self.assertEqual(len(created.draft.sources), 2)
                for span, source_id, original in zip(created.draft.sources, (input_id, result_id), (user_text, result_text), strict=True):
                    self.assertEqual((span.message_id, span.state_version, span.content_version), (source_id, 1, 1))
                    self.assertEqual((span.representation, span.start, span.end, span.expected_text), ("original", 0, 512, original[:512]))
                view = registry.read(self.owner, created.experience_id)
                self.assertFalse(view.execution_authority)
                self.assertEqual(view.record, created)

    def test_retry_and_reopen_reuse_the_registry_without_reenabling_a_reviewed_record(self) -> None:
        input_id = self.save_input()
        result_id = self.save_result(input_id)
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = list(pool.map(lambda _: self.proposer.propose(self.owner, tool_result_id=result_id), range(8)))
        created = first[0]
        assert created is not None
        self.assertTrue(all(item == created for item in first))
        reopened = MemoryRuntime(self.config)
        registry = ExperienceRegistry(reopened)
        confirmation_id = self.save_input("合成用户拒绝展示的技能草案。")
        registry.decide(self.owner, ExperienceDecision("reject-synthetic-skill", created.experience_id, 1,
            "reject", confirmation_id=confirmation_id))
        self.assertEqual(SkillDraftProposer(reopened).propose(self.owner, tool_result_id=result_id), created)
        self.assertEqual(registry.read(self.owner, created.experience_id).state, "rejected")
        other = self.proposer.propose(self.owner, tool_result_id=self.save_result(input_id))
        assert other is not None
        self.assertNotEqual(other.experience_id, created.experience_id)
        self.assertNotEqual(other.request_id, created.request_id)

    def test_linked_original_user_input_can_predate_capture_support(self) -> None:
        event = self.event("user", "complete", "合成原话来自普通采集入口。")
        input_id = self.runtime.submit(self.owner, event).message_id
        result_id = self.save_result(input_id)
        created = self.proposer.propose(self.owner, tool_result_id=result_id)
        assert created is not None
        self.assertEqual(created.draft.sources[0].expected_text, event.text)

    def test_requires_recall_and_accept_and_skips_unsupported_or_missing_results(self) -> None:
        input_id = self.save_input()
        result_id = self.save_result(input_id)
        for scope in ("recall", "accept"):
            access = pair_client(self.config, client_id=f"only-{scope}", entry=f"only-{scope}",
                access_path=self.config.parent / f"mcp-access-only-{scope}.json", scopes=(scope,))
            with self.subTest(scope=scope), self.assertRaisesRegex(MemoryFailure, "^permission_denied$"):
                self.proposer.propose(access, tool_result_id=result_id)
        def capture_tool(event: Event, kind: str = "tool_result", tool: str = "synthetic-tool",
                         attachments: tuple[AttachmentInput, ...] = ()) -> str:
            return self.save_capture(event, kind, input_id, tool, attachments)
        invalid = {
            "missing": "synthetic-missing-result",
            "plain event": self.runtime.submit(self.owner, self.event("tool", "failed", "合成工具失败")).message_id,
            "complete": self.save_result(input_id, "complete"),
            "partial": self.save_result(input_id, "partial"),
            "tool call": capture_tool(self.event("tool", "failed", "合成工具失败"), "tool_call"),
            "assistant": capture_tool(self.event("assistant", "failed", "合成工具失败")),
            "generated": capture_tool(self.event("tool", "failed", "合成工具失败"), tool="evernook_memory_reflect"),
            "protected": capture_tool(replace(self.event("tool", "failed", "合成受保护文本"), protected_source=True)),
            "missing attachment": capture_tool(self.event("tool", "failed", "合成工具失败"), attachments=(
                AttachmentInput("a", "synthetic.txt", "text/plain", None),)),
        }
        for case, message_id in invalid.items():
            with self.subTest(case=case):
                self.assertIsNone(self.proposer.propose(self.owner, tool_result_id=message_id))
        with self.runtime._connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_events_v1'").fetchone())

    def test_rejects_nonuser_incomplete_cross_scope_and_mismatched_input_links(self) -> None:
        other_entry = pair_client(self.config, client_id="synthetic-other", entry="other",
            access_path=self.config.parent / "mcp-access-other.json", scopes=("capture",))
        input_id = self.save_input()
        candidates = {
            "different entry": self.save_capture(self.event("user", "complete", "合成其他入口", entry="other"), "message", access=other_entry),
            "different session": self.save_capture(self.event("user", "complete", "合成其他会话", session="other"), "message"),
            "different branch": self.save_capture(self.event("user", "complete", "合成其他分支", branch="other"), "message"),
            "assistant": self.save_capture(self.event("assistant", "complete", "合成助手文本"), "message"),
            "partial user input": self.save_capture(self.event("user", "partial", "合成部分输入"), "message"),
            "tool call": self.save_capture(self.event("tool", "complete", "合成工具调用"), "tool_call", input_id, "synthetic-tool"),
            "protected input": self.save_capture(replace(self.event("user", "complete", "合成私密输入"), sensitive=True), "message"),
        }
        for case, candidate in candidates.items():
            with self.subTest(case=case):
                self.assertIsNone(self.proposer.propose(self.owner, tool_result_id=self.save_result(candidate)))
        out_of_order = replace(self.event("tool", "failed", "合成顺序异常。"), sequence=0)
        out_of_order_id = self.save_capture(out_of_order, "tool_result", input_id, "synthetic-tool")
        self.assertIsNone(self.proposer.propose(self.owner, tool_result_id=out_of_order_id))
        mismatched = self.save_result(input_id)
        other_input = self.save_input()
        with self.runtime._connection() as db:
            db.execute("UPDATE captures SET input_id=? WHERE message_id=?", (other_input, mismatched))
        self.assertIsNone(self.proposer.propose(self.owner, tool_result_id=mismatched))

    def test_recall_revoked_after_source_read_prevents_the_proposal(self) -> None:
        result_id = self.save_result(self.save_input())
        reviewer_path = self.config.parent / "mcp-access-reviewer.json"
        reviewer = pair_client(self.config, client_id="reviewer", entry="reviewer",
            access_path=reviewer_path, scopes=("recall", "accept"))
        original_proposal = self.proposer._proposal

        def revoke_recall(db: sqlite3.Connection, tool_result_id: str) -> ExperienceDecision | None:
            decision = original_proposal(db, tool_result_id)
            self.assertIsNotNone(decision)
            changed = pair_client(self.config, client_id="reviewer", entry="reviewer",
                access_path=reviewer_path, scopes=("accept",))
            self.assertEqual(changed, reviewer)
            return decision

        with patch.object(self.proposer, "_proposal", side_effect=revoke_recall):
            with self.assertRaisesRegex(MemoryFailure, "^permission_denied$"):
                self.proposer.propose(reviewer, tool_result_id=result_id)
        self.assertEqual(ExperienceRegistry(self.runtime).queue(reviewer).items, ())

    def test_registry_revalidates_sources_before_commit_and_after_suppression(self) -> None:
        input_id = self.save_input()
        result_id = self.save_result(input_id)
        evidence = self.save_input("合成用户停止召回该来源。")
        registry = ExperienceRegistry(self.runtime)
        original_proposal = self.proposer._proposal
        def change_source(db: sqlite3.Connection, tool_result_id: str) -> ExperienceDecision | None:
            decision = original_proposal(db, tool_result_id)
            self.runtime.change(self.owner, Change("race", input_id, 1, "suppress", evidence, "synthetic"))
            return decision
        with patch.object(self.proposer, "_proposal", side_effect=change_source):
            with self.assertRaisesRegex(MemoryFailure, "^stale_experience_source$"):
                self.proposer.propose(self.owner, tool_result_id=result_id)
        self.assertIsNone(self.proposer.propose(self.owner, tool_result_id=result_id))
        stable = self.save_result(self.save_input())
        created = self.proposer.propose(self.owner, tool_result_id=stable)
        assert created is not None
        self.runtime.change(self.owner, Change("after", stable, 1, "suppress", evidence, "synthetic"))
        self.assertIsNone(self.proposer.propose(self.owner, tool_result_id=stable))
        view = registry.read(self.owner, created.experience_id)
        self.assertEqual((view.state, view.record), ("source_unavailable", None))
