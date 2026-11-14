from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest

from evernook import Change, Event, ImportantMark, MemoryFailure, MemoryRuntime, SemanticRecall, Source, Transcription, initialize
from evernook.context import ContextBuilder, ContextPolicy
from evernook.transcription import ATTACHMENT, read, submit


class Counter:
    counter_id = 'synthetic-codepoints'
    def count(self, text: str) -> int:
        return len(text)


class TranscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-asr-synthetic-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / 'memory', client_id='synthetic', entry='synthetic', allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.material = Transcription(Source('synthetic', 'session', 'voice', 'event-1'), 1,
            '2026-11-14T00:00:00Z', 'complete', '合成转写：喜欢散步。', 'synthetic-asr', 'model-v1', 'zh-CN',
            'local', 300, 1400, 0.75, speaker='user')

    def test_unverified_transcript_roundtrips_and_preserves_evidence_provenance(self) -> None:
        result = submit(self.runtime, None, self.access, self.material)
        self.assertEqual(result['storage'], 'ordinary')
        receipt = result['receipt']
        self.assertTrue(receipt['complete'])
        message_id = receipt['message']['message_id']
        self.assertEqual(submit(self.runtime, None, self.access, self.material), result)
        with self.assertRaisesRegex(MemoryFailure, 'source_conflict'):
            submit(self.runtime, None, self.access, replace(self.material, confidence=0.4))
        material = read(self.runtime, self.access, message_id)
        assert material is not None
        self.assertEqual(material['message']['event']['text'], self.material.text)
        self.assertEqual(material['message']['event']['role'], 'tool')
        self.assertEqual(material['transcription']['confirmation'], 'unverified')
        self.assertEqual(material['transcription']['confidence'], 0.75)
        self.assertEqual(material['transcription']['speaker'], 'user')
        hit = self.runtime.search(self.access, '散步', mode='exact').hits[0]
        self.assertEqual(asdict(hit)['transcription']['model'], 'model-v1')
        recall = SemanticRecall(self.runtime).recall(self.access, '散步', as_of='2026-11-14T01:00:00Z')
        self.assertEqual(asdict(recall.hits[0])['evidence']['transcription']['confirmation'], 'unverified')
        builder = ContextBuilder(self.runtime, SemanticRecall(self.runtime), ContextPolicy(6000, 12000, 4000, 7, 8192), Counter())
        context = builder.build(self.access, '散步', as_of='2026-11-14T01:00:00Z', mode='normal', timeout=20)
        self.assertIn('asr_transcription', context.payload)
        self.assertIn('unverified', context.payload)
        pin = self.runtime.mark_important(self.access, ImportantMark(message_id, 'original', 1, 'synthetic', 0,
            len(self.material.text), self.material.text))
        context = ContextBuilder(self.runtime, SemanticRecall(self.runtime),
            ContextPolicy(6000, 12000, 4000, 7, 8192, pinned_ids=(pin.important_id,)), Counter()).build(
                self.access, '', as_of='2026-11-14T01:00:00Z', mode='normal', timeout=20)
        self.assertIn('asr_transcription', context.payload)

    def test_missing_metadata_reports_unverified_and_revoke_hides_original(self) -> None:
        message_id = submit(self.runtime, None, self.access, self.material)['receipt']['message']['message_id']
        with self.runtime._connection() as db:
            db.execute('DELETE FROM attachments WHERE message_id=? AND attachment_id=?', (message_id, ATTACHMENT))
        value = read(self.runtime, self.access, message_id)
        assert value is not None
        self.assertEqual(value['transcription']['metadata'], 'unavailable')
        self.assertEqual(asdict(self.runtime.search(self.access, '散步', mode='exact').hits[0])['transcription']['metadata'], 'unavailable')
        evidence = self.runtime.submit(self.access, Event(Source('synthetic', 'session', 'main', 'confirm'),
            'user', 2, None, 'complete', '合成撤回确认。'))
        self.runtime.change(self.access, Change('suppress-asr', message_id, 1, 'suppress', evidence.message_id, 'synthetic'))
        self.assertIsNone(read(self.runtime, self.access, message_id))
        self.assertEqual(self.runtime.search(self.access, '散步', mode='exact').hits, ())

    def test_metadata_change_during_tokenization_cannot_deliver_stale_provenance(self) -> None:
        message_id = submit(self.runtime, None, self.access, self.material)['receipt']['message']['message_id']
        runtime = self.runtime
        class RemovingCounter(Counter):
            removed = False
            def count(self, text: str) -> int:
                if 'asr_transcription' in text and not self.removed:
                    self.removed = True
                    with runtime._connection() as db:
                        db.execute('DELETE FROM attachments WHERE message_id=? AND attachment_id=?', (message_id, ATTACHMENT))
                return len(text)
        result = ContextBuilder(runtime, SemanticRecall(runtime), ContextPolicy(6000, 12000, 4000, 7, 8192), RemovingCounter()).build(
            self.access, '散步', as_of='2026-11-14T01:00:00Z', mode='normal', timeout=20)
        self.assertNotIn(self.material.text, result.payload)
        self.assertIn('memory_changed_before_handoff', result.degraded)

    def test_new_transcription_marker_during_tokenization_is_not_silently_ignored(self) -> None:
        from evernook import Capture
        event = Event(Source('synthetic', 'session', 'tool', 'marker-change'), 'tool', 1, None, 'complete',
                      '合成工具材料：散步。')
        message_id = self.runtime.submit_capture(self.access, Capture(event, 'message', tool_name='synthetic-tool')).message.message_id
        runtime = self.runtime
        class AddingMarkerCounter(Counter):
            changed = False
            def count(self, text: str) -> int:
                if '合成工具材料' in text and not self.changed:
                    self.changed = True
                    with runtime._connection() as db:
                        db.execute("UPDATE captures SET manifest=json_set(manifest,'$.tool_name','evernook_asr_transcript_v1') WHERE message_id=?", (message_id,))
                return len(text)
        result = ContextBuilder(runtime, SemanticRecall(runtime), ContextPolicy(6000, 12000, 4000, 7, 8192), AddingMarkerCounter()).build(
            self.access, '散步', as_of='2026-11-14T01:00:00Z', mode='normal', timeout=20)
        self.assertNotIn(event.text, result.payload)
        self.assertIn('memory_changed_before_handoff', result.degraded)

    def test_lost_capture_is_an_unverified_tool_and_topic_snapshot_expires(self) -> None:
        from evernook.topic_views import TopicViewDefinition, TopicViews
        message_id = submit(self.runtime, None, self.access, self.material)['receipt']['message']['message_id']
        views = TopicViews(self.runtime)
        views.configure(self.access, 'topic-define', 'speech-topic', 0, TopicViewDefinition('合成', '合成核对', '散步'))
        self.assertEqual(views.refresh(self.access, 'speech-topic').state, 'valid')
        with self.runtime._connection() as db:
            db.execute('DELETE FROM attachments WHERE message_id=?', (message_id,))
            db.execute('DELETE FROM captures WHERE message_id=?', (message_id,))
        hit = asdict(self.runtime.search(self.access, '散步', mode='exact').hits[0])
        self.assertEqual(hit['transcription'], {'kind': 'tool_output', 'confirmation': 'unverified', 'metadata': 'unavailable'})
        self.assertEqual(views.read(self.access, 'speech-topic').state, 'needs_refresh')

    def test_reflection_rechecks_provenance_before_remote_egress_and_handoff(self) -> None:
        from evernook.reflection_evidence import EvidenceTools
        message_id = submit(self.runtime, None, self.access, self.material)['receipt']['message']['message_id']
        tools = EvidenceTools(self.runtime, SemanticRecall(self.runtime), self.access, maximum=8,
            chars=4096, candidates=16, check=lambda: 20.0)
        tools.search('散步')
        self.assertEqual(tools.evidence[0]['transcription']['confirmation'], 'unverified')
        from evernook.reflection_protocol import build_messages
        packet = json.loads(build_messages({'question': '散步', 'evidence': tools.evidence})[-1]['content'])
        annotation = next(iter(packet['evidence'].values()))['transcription']
        self.assertEqual(annotation['confirmation'], 'unverified')
        self.assertEqual(annotation['speaker'], 'user')
        self.assertNotIn('model', annotation)
        self.assertNotIn('provider', annotation)
        with self.runtime._connection() as db:
            db.execute('DELETE FROM attachments WHERE message_id=? AND attachment_id=?', (message_id, ATTACHMENT))
        with self.assertRaisesRegex(MemoryFailure, 'reflection_source_unavailable'):
            tools.authorize_remote()
        self.assertFalse(tools.verify(lambda: 20.0))
        self.assertEqual(tools.evidence, [])

    def test_ordinary_archive_preserves_raw_transcript_and_metadata_for_review(self) -> None:
        from evernook import restore_archive
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        message_id = submit(self.runtime, None, self.access, self.material)['receipt']['message']['message_id']
        self.runtime.export_archive(self.access, self.root / 'archive')
        archive = restore_archive(self.root / 'archive', self.root / 'review')
        self.assertEqual(archive.review_message(message_id).message.event.text, self.material.text)
        metadata = json.loads(archive.review_attachment(message_id, ATTACHMENT))
        self.assertEqual((metadata['confirmation'], metadata['model']), ('unverified', 'model-v1'))
        confirmation = self.runtime.submit(self.access, Event(Source('synthetic', 'session', 'main', 'archive-hide'),
            'user', 2, None, 'complete', '合成：停止召回转写。'))
        self.runtime.change(self.access, Change('archive-suppress-asr', message_id, 1, 'suppress', confirmation.message_id, 'synthetic'))
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replayed = replay_archive_controls(self.runtime, self.access, self.root / 'archive', self.root / 'replayed', checkpoint=checkpoint)
        self.assertEqual(replayed.review_message(message_id).state.status, 'suppressed')
        self.assertEqual(json.loads(replayed.review_attachment(message_id, ATTACHMENT))['confirmation'], 'unverified')

    def test_confirmed_purge_removes_metadata_and_prevents_replay(self) -> None:
        from evernook.purge import PurgeDecision, PurgeWorkflow
        message_id = submit(self.runtime, None, self.access, self.material)['receipt']['message']['message_id']
        workflow = PurgeWorkflow(self.runtime)
        preview = workflow.preview(self.access, 'purge-transcription', (message_id,))
        confirmation = self.runtime.submit(self.access, Event(Source('synthetic', 'session', 'main', 'purge-confirm'),
            'user', 2, None, 'complete', '合成用户确认删除该测试材料。'))
        workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation.message_id))
        self.assertIsNone(read(self.runtime, self.access, message_id))
        self.assertEqual(self.runtime.search(self.access, '散步', mode='exact').hits, ())
        with self.runtime._connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM attachments WHERE message_id=?', (message_id,)).fetchone())
        with self.assertRaisesRegex(MemoryFailure, 'source_purged'):
            submit(self.runtime, None, self.access, self.material)

    def test_incomplete_and_sensitive_never_retain_plaintext_in_ordinary_store(self) -> None:
        partial = replace(self.material, source=replace(self.material.source, event='partial'), completion='partial', text='synthetic-partial-asr-sentinel')
        result = submit(self.runtime, None, self.access, partial)
        self.assertFalse(result['receipt']['complete'])
        self.assertFalse(result['receipt']['message']['original_saved'])
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            submit(self.runtime, None, self.access, replace(self.material, sensitive=True, text='synthetic-secret-asr-sentinel'))
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            submit(self.runtime, None, self.access, replace(self.material, text='password=synthetic-asr-credential-168'))
        for path in self.config.parent.glob('*'):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(b'synthetic-partial-asr-sentinel', content)
                self.assertNotIn(b'synthetic-secret-asr-sentinel', content)
                self.assertNotIn(b'synthetic-asr-credential-168', content)

    def test_invalid_material_is_rejected(self) -> None:
        for value in (replace(self.material, confidence=float('nan')), replace(self.material, start_ms=9, end_ms=1),
                      replace(self.material, execution='automatic'), replace(self.material, provider='https://example.invalid'),
                      replace(self.material, model='C:/private/model'), replace(self.material, model='../private')):
            with self.subTest(value=value), self.assertRaises(MemoryFailure):
                submit(self.runtime, None, self.access, value)
        namespaced = replace(self.material, model='synthetic/voice-model')
        self.assertEqual(submit(self.runtime, None, self.access, namespaced)['storage'], 'ordinary')

    def test_unverified_speaker_label_cannot_become_user_statement(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan
        message_id = submit(self.runtime, None, self.access, self.material)['receipt']['message']['message_id']
        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (message_id,), processor_version='synthetic-asr-check')
        assert lease.token is not None
        span = EvidenceSpan(message_id, 1, 'original', 1, 0, len(self.material.text), self.material.text)
        draft = CandidateDraft(self.material.text, 'user_statement', (span,))
        with self.assertRaisesRegex(MemoryFailure, 'candidate_provenance_mismatch'):
            workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))

    def test_user_correction_is_separate_evidence_with_source_lineage(self) -> None:
        message_id = submit(self.runtime, None, self.access, self.material)['receipt']['message']['message_id']
        correction = self.runtime.submit(self.access, Event(Source('synthetic', 'session', 'main', 'voice-correction'),
            'user', 2, None, 'complete', '合成：我说的是喜欢散心。'))
        state = self.runtime.change(self.access, Change('correct-asr', message_id, 1, 'correct', correction.message_id, 'synthetic'))
        self.assertEqual(state.replacement_id, correction.message_id)
        self.assertIsNone(read(self.runtime, self.access, message_id))
        self.assertEqual(self.runtime.read(self.access, correction.message_id).event.role, 'user')


if __name__ == '__main__':
    unittest.main()
