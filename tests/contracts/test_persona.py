from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.persona import PERSONA_BLOCKS, PersonaRegistry, PersonaRollback, PersonaSet


class PersonaTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-persona-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, self.access = initialize(self.directory / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.personas = PersonaRegistry(self.runtime)
        self.access_path = self.config.parent / "mcp-access-persona.json"
        from evernook import pair_client
        pair_client(self.config, client_id="synthetic", entry="test", access_path=self.access_path)
        self.number = 0

    def evidence(self, text: str, *, role: str = "user") -> str:
        self.number += 1
        event = Event(Source("test", "session", "main", str(self.number)), role, self.number, None, "complete", text)
        receipt = self.runtime.submit(self.access, event)
        if role != "user":
            self.number += 1
            control = Event(Source("test", "session", "main", str(self.number)), "user", self.number, None, "complete", "合成：隐藏上一条")
            control_receipt = self.runtime.submit(self.access, control)
            from evernook import Change
            self.runtime.change(self.access, Change("suppress-" + receipt.message_id, receipt.message_id, 1, "suppress", control_receipt.message_id, "合成"))
        return receipt.message_id

    def set_block(self, block: str, text: str, *, request_id: str | None = None, expected: int = 0,
                  evidence: str | None = None) -> Any:
        return self.personas.set(self.access, PersonaSet(request_id or f"set-{block}-{expected}-{hash(text) & 0xffff}",
            block, text, evidence or self.evidence(f"合成：请把{block}设为{text}"), "合成：用户确认", expected))

    def invoke(self, command: str, payload: object = None) -> dict[str, Any]:
        process = subprocess.run([sys.executable, "-m", "evernook.cli", command, "--config", str(self.config),
                                  "--access", str(self.access_path)],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True, timeout=30,
            cwd=Path(__file__).resolve().parents[2])
        result: dict[str, Any] = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(process.stderr, b"")
        self.assertEqual(process.returncode, 0 if result["ok"] else 1)
        return result

    def test_cli_block_roundtrip_history_and_traceable_rollback(self) -> None:
        evidence = self.evidence("合成：请把你的性格语气设为温和，爱用句号。")
        saved = self.invoke("persona-set", {"request_id": "cli-tone-1", "block": "tone",
            "text": "温和，爱用句号。", "evidence_id": evidence, "reason": "合成：初始设定", "expected_revision": 0})
        self.assertEqual(saved["result"]["block"], "tone")
        self.assertEqual(saved["result"]["revision"], 1)
        read = self.invoke("persona-read", {})
        self.assertEqual(read["result"]["blocks"]["tone"]["text"], "温和，爱用句号。")
        self.assertEqual(read["result"]["blocks"]["identity"], None)
        history = self.invoke("persona-history", {"block": "tone"})
        self.assertEqual([item["revision"] for item in history["result"]["records"]], [1])
        updated = self.invoke("persona-set", {"request_id": "cli-tone-2", "block": "tone",
            "text": "活泼，爱用感叹号！", "evidence_id": evidence, "reason": "合成：用户改设定", "expected_revision": 1})
        self.assertEqual(updated["result"]["revision"], 2)
        head_seq = read["result"]["head_seq"]
        after = self.invoke("persona-read", {})
        target = after["result"]["head_seq"]
        rollback = self.invoke("persona-rollback", {"request_id": "cli-rollback-1", "target_seq": head_seq,
            "expected_seq": target, "evidence_id": evidence, "reason": "合成：撤销刚才的修改"})
        self.assertEqual(rollback["result"]["restored"][0]["text"], "温和，爱用句号。")
        self.assertEqual(rollback["result"]["restored"][0]["restored_from"], head_seq)
        self.assertEqual(rollback["result"]["restored"][0]["action"], "rollback")
        rolled = self.invoke("persona-read", {})
        self.assertEqual(rolled["result"]["blocks"]["tone"]["text"], "温和，爱用句号。")
        historical = self.invoke("persona-read", {"as_of_seq": target})
        self.assertEqual(historical["result"]["blocks"]["tone"]["text"], "活泼，爱用感叹号！")
        full_history = self.invoke("persona-history", {"block": "tone"})
        self.assertEqual([item["revision"] for item in full_history["result"]["records"]], [1, 2, 3])
        self.assertEqual([item["action"] for item in full_history["result"]["records"]], ["set", "set", "rollback"])

    def test_idempotent_retry_conflict_and_cas_revision_guards(self) -> None:
        evidence = self.evidence("合成：设定身份背景")
        request = PersonaSet("synthetic-idem", "identity", "合成身份A", evidence, "合成", 0)
        first = self.personas.set(self.access, request)
        self.assertEqual(self.personas.set(self.access, request), first)
        with self.assertRaisesRegex(MemoryFailure, "persona_request_conflict"):
            self.personas.set(self.access, replace(request, text="合成身份B"))
        with self.assertRaisesRegex(MemoryFailure, "persona_revision_conflict"):
            self.set_block("identity", "合成身份C", expected=0)
        self.assertEqual(self.personas.view(self.access).blocks["identity"].text, "合成身份A")  # type: ignore[union-attr]
        with self.assertRaisesRegex(MemoryFailure, "persona_revision_conflict"):
            self.set_block("identity", "合成身份C", expected=2)

    def test_evidence_and_content_validation_reject_nonuser_and_sensitive_input(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, "persona_user_confirmation_required"):
            self.set_block("tone", "合成语气", evidence=self.evidence("合成：助手说的不算", role="assistant"))
        with self.assertRaisesRegex(MemoryFailure, "message_not_found"):
            self.set_block("tone", "合成语气", evidence="synthetic-missing-message")
        with self.assertRaisesRegex(MemoryFailure, "protected_persona"):
            self.set_block("tone", "password=synthetic-secret-1")
        with self.assertRaisesRegex(MemoryFailure, "persona_too_large"):
            self.set_block("tone", "合" * 17000)
        with self.assertRaisesRegex(MemoryFailure, "invalid_persona"):
            self.set_block("mood", "合成：不存在的分块")

    def test_rollback_semantics_guard_target_seq_and_report_unchanged_blocks(self) -> None:
        self.set_block("identity", "合成身份")
        tone = self.set_block("tone", "合成语气v1")
        address = self.set_block("address", "称呼：小合成")
        view = self.personas.view(self.access)
        head_seq = view.head_seq
        with self.assertRaisesRegex(MemoryFailure, "persona_seq_conflict"):
            self.personas.rollback(self.access, PersonaRollback("synthetic-stale", tone.seq, head_seq + 5,
                self.evidence("合成：回滚"), "合成"))
        with self.assertRaisesRegex(MemoryFailure, "invalid_persona_target"):
            self.personas.rollback(self.access, PersonaRollback("synthetic-future", head_seq + 1, head_seq,
                self.evidence("合成：回滚"), "合成"))
        result = self.personas.rollback(self.access, PersonaRollback("synthetic-rollback-1", tone.seq, head_seq,
            self.evidence("合成：回滚到语气v1时点"), "合成"))
        self.assertEqual([record.block for record in result.restored], ["address"])
        self.assertEqual(result.unchanged, ("identity", "tone", "boundary", "state"))
        rolled = self.personas.view(self.access)
        self.assertEqual(rolled.blocks["tone"].text, "合成语气v1")  # type: ignore[union-attr]
        self.assertEqual(rolled.blocks["identity"].text, "合成身份")  # type: ignore[union-attr]
        self.assertEqual(rolled.blocks["address"].text, "")  # type: ignore[union-attr]
        self.assertEqual(rolled.blocks["address"].revision, 2)  # type: ignore[union-attr]
        self.assertEqual(rolled.blocks["address"].restored_from, tone.seq)  # type: ignore[union-attr]
        as_of = self.personas.view(self.access, as_of_seq=tone.seq)
        self.assertEqual(as_of.blocks["identity"].text, "合成身份")
        self.assertEqual(as_of.blocks["address"], None)
        second = self.personas.rollback(self.access, PersonaRollback("synthetic-rollback-2", tone.seq,
            rolled.head_seq, self.evidence("合成：再次回滚"), "合成"))
        self.assertEqual(second.restored, ())
        self.assertEqual(len(second.unchanged), len(PERSONA_BLOCKS))

    def test_concurrent_sets_serialize_and_losing_revision_conflicts(self) -> None:
        evidence = self.evidence("合成：并发设定状态")
        requests = [PersonaSet(f"synthetic-race-{index}", "state", f"合成状态{index}", evidence, "合成", 0)
                    for index in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(lambda request: self._attempt_set(request), requests))
        winners = [record for record in outcomes if not isinstance(record, MemoryFailure)]
        losses = [record for record in outcomes if isinstance(record, MemoryFailure)]
        self.assertEqual(len(winners), 1)
        self.assertEqual({error.code for error in losses}, {"persona_revision_conflict"})
        self.assertEqual(self.personas.view(self.access).blocks["state"].revision, 1)  # type: ignore[union-attr]
        self.assertEqual(self.personas.history(self.access, "state").records[0].request_id, winners[0].request_id)

    def _attempt_set(self, request: PersonaSet) -> Any:
        try:
            return self.personas.set(self.access, request)
        except MemoryFailure as error:
            return error

    def test_history_pagination_and_clear_block_by_empty_text(self) -> None:
        evidence = self.evidence("合成：状态多次调整")
        for index in range(5):
            self.personas.set(self.access, PersonaSet(f"synthetic-page-{index}", "state", f"状态{index}", evidence, "合成", index))
        page_one = self.personas.history(self.access, "state", limit=2)
        self.assertEqual([record.revision for record in page_one.records], [1, 2])
        self.assertEqual(page_one.next_offset, 2)
        page_last = self.personas.history(self.access, "state", limit=2, offset=4)
        self.assertEqual([record.revision for record in page_last.records], [5])
        self.assertIsNone(page_last.next_offset)
        cleared = self.personas.set(self.access, PersonaSet("synthetic-clear", "state", "", evidence, "合成：清空当前状态", 5))
        self.assertEqual(cleared.revision, 6)
        self.assertEqual(self.personas.view(self.access).blocks["state"].text, "")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
