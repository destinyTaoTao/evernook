from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import Change, Event, HARNESS_DEFAULT_SCOPES, MemoryFailure, MemoryRuntime, SCOPES, Source, initialize, pair_client
from evernook.persona import PersonaRegistry, PersonaSet
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-scopes-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, owner_access = initialize(self.directory / "data", client_id="owner", entry="owner")
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / "mcp-access-owner.json"
        pair_client(self.config, client_id="owner", entry="owner", access_path=self.owner_path)
        self.harness_path = self.config.parent / "mcp-access-harness.json"
        self.harness = pair_client(self.config, client_id="harness", entry="harness",
                                   access_path=self.harness_path, scopes=HARNESS_DEFAULT_SCOPES)
        self.maintainer_path = self.config.parent / "mcp-access-maintainer.json"
        self.maintainer = pair_client(self.config, client_id="maintainer", entry="maintainer",
                                      access_path=self.maintainer_path,
                                      scopes=("recall", "capture", "maintain", "accept"))
        self.number = 0

    def message(self, text: str, *, role: str = "user", entry: str = "owner") -> str:
        self.number += 1
        event = Event(Source(entry, "session", "main", str(self.number)), role, self.number, None, "complete", text)
        access = self.runtime.access_from_file(self.owner_path if entry == "owner" else self.harness_path)
        return self.runtime.submit(access, event).message_id

    def test_owner_keeps_full_scopes_and_handshake_reports_scopes(self) -> None:
        owner = self.runtime._authorize(self.runtime.access_from_file(self.owner_path))
        self.assertEqual(owner.scopes, frozenset(SCOPES))
        harness = self.runtime._authorize(self.harness)
        self.assertEqual(harness.scopes, frozenset(HARNESS_DEFAULT_SCOPES))
        self.assertEqual(sorted(self.runtime.handshake(self.harness)["scopes"]), sorted(HARNESS_DEFAULT_SCOPES))
        self.assertNotEqual(owner.scopes, harness.scopes)

    def test_harness_default_pack_is_exactly_recall_and_capture(self) -> None:
        target = self.message("合成：供受限调用端读取", entry="harness")
        self.assertTrue(self.runtime.read(self.harness, target))
        self.assertTrue(self.runtime.search(self.harness, "合成", mode="lexical").hits)
        receipt = self.runtime.submit(self.harness, Event(Source("harness", "session", "main", "h1"), "user", 1,
            None, "complete", "合成：受限端写入"))
        self.assertTrue(receipt.message_id)
        denied: list[tuple[str, Any]] = [
            ("change", lambda: self.runtime.change(self.harness, Change("denied-change", target, 1, "suppress", receipt.message_id, "合成"))),
            ("mark_important", lambda: self.runtime.mark_important(self.harness, __import__("evernook").ImportantMark(
                receipt.message_id, "original", 1, "scope", None, None, "合成"))),
            ("refresh_lexical", lambda: self.runtime.refresh_lexical(self.harness)),
            ("rebuild_lexical", lambda: self.runtime.rebuild_lexical(self.harness)),
            ("purge_preview", lambda: __import__("evernook").PurgeWorkflow(self.runtime).preview(self.harness, "denied-purge", (target,))),
            ("persona_set", lambda: PersonaRegistry(self.runtime).set(self.harness, PersonaSet(
                "denied-persona", "tone", "合成", receipt.message_id, "合成", 0))),
            ("profile_decide", lambda: ProfileRegistry(self.runtime).decide(self.harness, ProfileDecision(
                "denied-profile", "denied-tag", 0, "propose",
                ProfileDraft("interest", "合成标签", 0.5, ())))),
        ]
        for name, operation in denied:
            with self.subTest(operation=name):
                with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
                    operation()

    def test_scoped_maintainer_can_maintain_but_not_admin_or_cross_entry_capture(self) -> None:
        target = self.message("合成：维护目标")
        control = self.message("合成：维护证据")
        state = self.runtime.change(self.maintainer, Change("scoped-change", target, 1, "suppress", control, "合成"))
        self.assertEqual(state.status, "suppressed")
        from evernook import PurgeWorkflow
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            PurgeWorkflow(self.runtime).preview(self.maintainer, "denied-admin", (target,))
        with self.assertRaisesRegex(MemoryFailure, "unauthorized_source"):
            self.runtime.submit(self.maintainer, Event(Source("harness", "session", "main", "x"), "user", 1,
                None, "complete", "合成：entry 不匹配"))

    def test_fact_acceptance_requires_accept_scope(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan
        from evernook.facts import FactDecision, FactRegistry
        target = self.message("合成：小林住在杭州")
        evidence = self.message("合成：确认小林住在杭州")
        registry = FactRegistry(self.runtime)
        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.maintainer, (target,), processor_version="synthetic-scopes-v1")
        assert lease.token is not None
        span = EvidenceSpan(target, 1, "original", 1, 0, len("合成：小林住在杭州"), "合成：小林住在杭州")
        workflow.complete(self.maintainer, lease.status.run_id, lease.token, (CandidateDraft("合成：小林住在杭州", "user_statement", (span,), False),))
        candidate = workflow.candidates(self.maintainer, lease.status.run_id)[0]
        request = FactDecision("scoped-fact", "synthetic-person", "residence", candidate.candidate_id, evidence, 0, "assert")
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            registry.decide(self.harness, request)
        record = registry.decide(self.maintainer, request)
        self.assertEqual(record.revision, 1)
        view = registry.query(self.harness, record.subject_id, record.predicate)
        self.assertEqual(view.fact, record)

    def test_repairing_scopes_via_repair_pairing_updates_identity(self) -> None:
        widened = pair_client(self.config, client_id="harness", entry="harness",
                              access_path=self.harness_path, scopes=("recall", "capture", "maintain"))
        self.assertEqual(widened, self.harness)
        identity = self.runtime._authorize(self.harness)
        self.assertEqual(identity.scopes, frozenset({"recall", "capture", "maintain"}))
        narrowed = pair_client(self.config, client_id="harness", entry="harness",
                               access_path=self.harness_path, scopes=HARNESS_DEFAULT_SCOPES)
        self.assertEqual(self.runtime._authorize(narrowed).scopes, frozenset(HARNESS_DEFAULT_SCOPES))
        with self.assertRaisesRegex(MemoryFailure, "invalid_scopes"):
            pair_client(self.config, client_id="harness", entry="harness",
                        access_path=self.harness_path, scopes=("recall", "launch"))
        unpaired = self.harness.__class__("harness", "bogus-token-0123456789abcdef0123")
        with self.assertRaisesRegex(MemoryFailure, "unauthorized"):
            self.runtime.read(unpaired, "synthetic-any")

    def test_cli_pair_grants_explicit_scopes(self) -> None:
        access_path = self.config.parent / "mcp-access-scoped-cli.json"
        process = subprocess.run([sys.executable, "-m", "evernook.cli", "pair", "--config", str(self.config),
                                  "--client-id", "scoped-cli", "--entry", "scoped", "--access", str(access_path),
                                  "--scopes", "recall,capture"],
            input=b"", capture_output=True, timeout=30, cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(process.returncode, 0, process.stderr)
        access = self.runtime.access_from_file(access_path)
        self.assertEqual(self.runtime._authorize(access).scopes, frozenset(HARNESS_DEFAULT_SCOPES))
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.runtime.refresh_lexical(access)


if __name__ == "__main__":
    unittest.main()
