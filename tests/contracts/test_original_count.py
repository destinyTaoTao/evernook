from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evernook import (
    Change, Event, EvidenceSpan, MemoryFailure, MemoryRuntime, OriginalCountScope, PurgeDecision, PurgeWorkflow, Source,
    initialize, pair_client,
)
from evernook.observations import source_contexts


class OriginalCountTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-original-count-')
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / 'runtime', client_id='synthetic', entry='test')
        self.runtime = MemoryRuntime(self.config)
        self.scope = OriginalCountScope('test', 'ledger', 'main')
        self.sequence = 0

    def save(self, *, session: str = 'ledger', branch: str = 'main', role: str = 'user',
             completion: str = 'complete', sensitive: bool = False, protected_source: bool = False) -> EvidenceSpan:
        self.sequence += 1
        event = Event(Source('test', session, branch, str(self.sequence)), role, self.sequence, None,
            completion, '合成登记条目', sensitive=sensitive, protected_source=protected_source)
        receipt = self.runtime.submit(self.access, event)
        self.assertEqual(self.runtime.submit(self.access, event), receipt)
        return EvidenceSpan(receipt.message_id, 1, 'original', receipt.content_version, 0, 2, '合成')

    def test_four_closed_and_open_packet_shapes_count_unique_original_ids(self) -> None:
        first = self.save()
        second_span = replace(first, start=2, end=4, expected_text='登记')
        packet: tuple[EvidenceSpan, ...] = (first, second_span)

        closed_one = self.runtime.count_user_originals(self.access, self.scope, packet)
        self.assertEqual((closed_one.decision, closed_one.count), ('answer', 1))
        second = self.save()
        open_one = self.runtime.count_user_originals(self.access, self.scope, packet)
        self.assertEqual((open_one.decision, open_one.count, open_one.reason), ('abstain', None, 'incomplete_evidence'))

        packet += (second,)
        closed_two = self.runtime.count_user_originals(self.access, self.scope, packet)
        self.assertEqual((closed_two.decision, closed_two.count), ('answer', 2))
        self.save()
        open_two = self.runtime.count_user_originals(self.access, self.scope, packet)
        self.assertEqual((open_two.decision, open_two.count, open_two.reason), ('abstain', None, 'incomplete_evidence'))

    def test_scope_versions_and_quotes_are_checked(self) -> None:
        original, other_session, other_branch = self.save(), self.save(session='other'), self.save(branch='other')
        for bad in (other_session, other_branch, replace(original, state_version=2),
                    replace(original, content_version=2), replace(original, expected_text='伪造')):
            with self.subTest(bad=bad):
                result = self.runtime.count_user_originals(self.access, self.scope, (original, bad))
                self.assertEqual((result.decision, result.count), ('abstain', None))
        for scope in (replace(self.scope, entry='other'), replace(self.scope, session='absent')):
            with self.subTest(scope=scope):
                result = self.runtime.count_user_originals(self.access, scope, ())
                self.assertEqual((result.decision, result.count), ('abstain', None))

    def test_only_current_active_complete_public_user_originals_are_in_scope(self) -> None:
        old, replacement = self.save(), self.save()
        assistant = self.save(role='assistant')
        private = self.save(sensitive=True)
        protected = self.save(protected_source=True)
        incomplete = self.save(completion='partial')
        for excluded in (assistant, private, protected, incomplete):
            with self.subTest(excluded=excluded):
                result = self.runtime.count_user_originals(self.access, self.scope, (old, replacement, excluded))
                self.assertEqual((result.decision, result.count, result.reason), ('abstain', None, 'incomplete_evidence'))
        corrected = self.runtime.change(self.access, Change('correct', old.message_id, 1, 'correct', replacement.message_id, 'synthetic'))
        self.assertEqual((corrected.status, corrected.version), ('corrected', 2))
        self.assertIsNone(self.runtime.count_user_originals(self.access, self.scope, (old, replacement)).count)
        self.assertEqual(self.runtime.count_user_originals(self.access, self.scope, (replacement,)).count, 1)
        confirmation = self.save(session='outside')
        self.runtime.change(self.access, Change('revoke', replacement.message_id, 1, 'revoke', confirmation.message_id, 'synthetic'))
        self.assertIsNone(self.runtime.count_user_originals(self.access, self.scope, (replacement,)).count)
        self.assertIsNone(self.runtime.count_user_originals(self.access, self.scope, ()).count)

    def test_suppressed_and_purged_originals_are_unavailable(self) -> None:
        hidden, removed, retained = self.save(), self.save(), self.save()
        confirmation = self.save(session='outside')
        self.runtime.change(self.access, Change('suppress', hidden.message_id, 1, 'suppress', confirmation.message_id, 'synthetic'))
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, 'purge', (removed.message_id,))
        approval = self.save(session='outside')
        purge.purge(self.access, PurgeDecision(preview.request_id, approval.message_id))
        self.assertEqual(self.runtime.count_user_originals(self.access, self.scope, (retained,)).count, 1)
        for span in (hidden, removed):
            result = self.runtime.count_user_originals(self.access, self.scope, (retained, span))
            self.assertEqual((result.decision, result.count, result.reason), ('abstain', None, 'incomplete_evidence'))

    def test_untrusted_completion_marker_is_not_a_certificate(self) -> None:
        original = self.save()
        self.save()
        forged = {'message_id': original.message_id, 'coverage': 'complete', 'scope_complete': True}
        with self.assertRaisesRegex(MemoryFailure, 'invalid_original_count_request'):
            self.runtime.count_user_originals(self.access, self.scope, (forged,))  # type: ignore[arg-type]
        self.assertIsNone(self.runtime.count_user_originals(self.access, self.scope, (original,)).count)

    def test_budgets_do_not_return_lower_bounds(self) -> None:
        packet = tuple(self.save() for _ in range(32))
        self.assertEqual(self.runtime.count_user_originals(self.access, self.scope, packet).count, 32)
        result = self.runtime.count_user_originals(self.access, self.scope, (packet[0],) * 101)
        self.assertEqual((result.decision, result.count, result.reason), ('abstain', None, 'evidence_budget'))
        oversized = replace(packet[0], end=16_385, expected_text='字' * 16_385)
        result = self.runtime.count_user_originals(self.access, self.scope, (oversized,))
        self.assertEqual((result.decision, result.count, result.reason), ('abstain', None, 'evidence_budget'))
        self.save()
        result = self.runtime.count_user_originals(self.access, self.scope, packet)
        self.assertEqual((result.decision, result.count, result.reason), ('abstain', None, 'evidence_budget'))

    def test_authentication_cancellation_and_model_free_path(self) -> None:
        original = self.save()
        capture = pair_client(self.config, client_id='writer', entry='other',
            access_path=self.config.parent / 'mcp-access-writer.json', scopes=('capture',))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.runtime.count_user_originals(capture, self.scope, (original,))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(MemoryFailure, 'original_count_canceled'):
            self.runtime.count_user_originals(self.access, self.scope, (original,), cancel=cancel)
        with (patch('evernook.reflection.ReflectionEngine.reflect', side_effect=AssertionError('model path')),
              patch('evernook.semantic.SemanticRecall.recall', side_effect=AssertionError('recall path')),
              patch('socket.create_connection', side_effect=AssertionError('network path'))):
            self.assertEqual(self.runtime.count_user_originals(self.access, self.scope, (original,)).count, 1)

    def test_scope_and_spans_are_read_from_the_same_snapshot(self) -> None:
        original = self.save()

        def add_source_after_enumeration(db: sqlite3.Connection, spans: tuple[EvidenceSpan, ...], stance: str) -> object:
            self.save()
            return source_contexts(db, spans, stance)

        with patch('evernook.original_count.source_contexts', side_effect=add_source_after_enumeration):
            self.assertEqual(self.runtime.count_user_originals(self.access, self.scope, (original,)).count, 1)
        self.assertIsNone(self.runtime.count_user_originals(self.access, self.scope, (original,)).count)

    def test_recall_scope_is_verified_again_before_publication(self) -> None:
        original = self.save()
        access_file = self.config.parent / 'mcp-access-reader.json'
        reader = pair_client(self.config, client_id='reader', entry='client', access_path=access_file, scopes=('recall',))
        self.assertEqual(self.runtime.count_user_originals(reader, self.scope, (original,)).count, 1)

        def remove_recall_scope(db: sqlite3.Connection, spans: tuple[EvidenceSpan, ...], stance: str) -> object:
            pair_client(self.config, client_id='reader', entry='client', access_path=access_file, scopes=('capture',))
            return source_contexts(db, spans, stance)

        with patch('evernook.original_count.source_contexts', side_effect=remove_recall_scope):
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                self.runtime.count_user_originals(reader, self.scope, (original,))

    def test_timeout_and_in_flight_cancellation_never_publish_a_number(self) -> None:
        original = self.save()
        clock = iter((0., 0., 2.))
        with patch('evernook.original_count.time', SimpleNamespace(monotonic=lambda: next(clock))):
            result = self.runtime.count_user_originals(self.access, self.scope, (original,), timeout=1)
        self.assertEqual((result.decision, result.count, result.reason), ('abstain', None, 'timeout'))

        cancel = threading.Event()
        with patch('evernook.original_count.source_contexts', side_effect=lambda *_: cancel.set()):
            with self.assertRaisesRegex(MemoryFailure, 'original_count_canceled'):
                self.runtime.count_user_originals(self.access, self.scope, (original,), cancel=cancel)


if __name__ == '__main__':
    unittest.main()
