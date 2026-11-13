from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import PropertyMock, patch

from evernook import Change, Event, MemoryRuntime, Source, initialize
from evernook.candidates import EvidenceSpan
from evernook.models import MemoryFailure
from evernook.observations import ObservationDraft, ObservationRegistry
from evernook.observation_processing import ObservationProcessing


class ObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-observations-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / 'runtime', client_id='owner', entry='owner', allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.registry = ObservationRegistry(self.runtime)
        self.sequence = 0

    def source(self, text: str, *, role: str = 'user', occurred: str = '2020-01-01T00:00:00Z') -> EvidenceSpan:
        self.sequence += 1
        receipt = self.runtime.submit(self.access, Event(Source('owner', 'session-' + str(self.sequence), 'main', '1'),
            role, 1, occurred, 'complete', text))
        return EvidenceSpan(receipt.message_id, 1, 'original', 1, 0, len(text), text)

    def draft(self, support: tuple[EvidenceSpan, ...], counters: tuple[EvidenceSpan, ...] = ()) -> ObservationDraft:
        return ObservationDraft('user:wetland', '合成观察：用户多次提到湿地经历；不推断频率。', support, counters,
            'experience', '2020-01-01T00:00:00Z', None, 'synthetic-processor-v1', 'synthetic-policy-v1')

    def test_management_network_requires_published_live_evidence(self) -> None:
        from evernook.management import MemoryManagement
        from evernook import pair_client

        ui = MemoryManagement(self.runtime)
        support = self.source('合成：第一次观察。')
        counter = self.source('合成：另一种说法。', occurred='2021-01-01T00:00:00Z')
        candidate = self.registry.propose(self.access, 'network-candidate', self.draft((support,), (counter,)))
        self.assertEqual(ui.read(self.access, view='network_observations')['items'], [])
        confirmation = self.source('合成：确认发布观察。')
        self.registry.decide(self.access, 'network-publish', candidate.observation_id, candidate.revision,
                             action='publish', confirmation_id=confirmation.message_id)
        item = ui.read(self.access, view='network_observations')['items'][0]
        self.assertEqual(item['support'], [support.message_id])
        self.assertEqual(item['counterevidence'], [counter.message_id])
        self.assertTrue(item['inference'])
        self.assertEqual(len(ui.read(self.access, view='network_observations', entry='owner', session_id='session-1',
                                     since='2019-01-01T00:00:00Z')['items']), 1)
        self.assertEqual(ui.read(self.access, view='network_observations', entry='other', session_id='session-1')['items'], [])
        self.assertEqual(ui.read(self.access, view='network_observations', session_id='session-1',
                                 since='2021-01-01T00:00:00Z')['items'], [])
        self.assertNotIn('expected_text', json.dumps(item, ensure_ascii=False))
        self.runtime.change(self.access, Change('network-hide', counter.message_id, 1, 'suppress', confirmation.message_id, 'synthetic'))
        self.assertEqual(ui.read(self.access, view='network_observations')['items'], [])
        path = self.config.parent / 'mcp-access-network.json'
        pair_client(self.config, client_id='network', entry='network', access_path=path, scopes=('capture', 'recall'))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            ui.read(self.runtime.access_from_file(path), view='network_observations')

    def test_management_network_omits_expired_even_with_filters(self) -> None:
        from evernook.management import MemoryManagement

        support = self.source('合成：已经过期的观察。')
        draft = replace(self.draft((support,)), valid_until='2021-01-01T00:00:00Z')
        candidate = self.registry.propose(self.access, 'expired-candidate', draft)
        confirmation = self.source('合成：确认发布。')
        self.registry.decide(self.access, 'expired-publish', candidate.observation_id, candidate.revision,
                             action='publish', confirmation_id=confirmation.message_id)
        ui = MemoryManagement(self.runtime)
        self.assertEqual(ui.read(self.access, view='network_observations')['items'], [])
        self.assertEqual(ui.read(self.access, view='network_observations', session_id='session-1')['items'], [])

    def test_provenance_deduplicates_raw_events_and_requires_explicit_publication(self) -> None:
        first = self.source('合成：我们在湿地见过白鹭。')
        second = self.source('合成：后来又聊到湿地。', role='assistant')
        draft = self.draft((first, first, second))
        candidate = self.registry.propose(self.access, 'candidate-one', draft)
        self.assertEqual(candidate.state, 'candidate')
        self.assertEqual(candidate.independent_support, 1)
        self.assertEqual({row['role'] for row in candidate.source_contexts}, {'user', 'assistant'})
        repeated = self.registry.propose(self.access, 'candidate-one', draft)
        self.assertEqual(repeated.revision, candidate.revision)
        approval = self.source('合成：确认发布这条派生观察。')
        published = self.registry.decide(self.access, 'publish-one', candidate.observation_id, candidate.revision,
            action='publish', confirmation_id=approval.message_id)
        self.assertEqual(published.state, 'published')
        self.assertTrue(published.inference)

    def test_merge_must_keep_counterevidence_and_never_silently_choose_a_side(self) -> None:
        first = self.source('合成：我喜欢湿地观鸟。')
        negative = self.source('合成：现在不再喜欢湿地观鸟。')
        original = self.registry.propose(self.access, 'initial', self.draft((first,), (negative,)))
        more = self.source('合成：还提过白鹭。')
        with self.assertRaisesRegex(MemoryFailure, 'observation_evidence_loss'):
            self.registry.propose(self.access, 'omit-counter', self.draft((first, more)),
                observation_id=original.observation_id, expected_revision=original.revision, operation='merge')
        merged = self.registry.propose(self.access, 'keep-counter', self.draft((first, more), (negative,)),
            observation_id=original.observation_id, expected_revision=original.revision, operation='merge')
        self.assertEqual(merged.independent_counterevidence, 1)

    def test_control_invalidates_entire_conclusion_before_background_refresh(self) -> None:
        first = self.source('合成：在湿地看白鹭。')
        candidate = self.registry.propose(self.access, 'initial', self.draft((first,)))
        confirmation = self.source('合成：确认发布观察。')
        published = self.registry.decide(self.access, 'publish', candidate.observation_id, 1,
            action='publish', confirmation_id=confirmation.message_id)
        self.runtime.change(self.access, Change('hide', first.message_id, 1, 'suppress', confirmation.message_id, 'synthetic'))
        unavailable = self.registry.read(self.access, published.observation_id)
        self.assertEqual(unavailable.state, 'unavailable')
        self.assertIsNone(unavailable.conclusion)
        self.assertFalse(unavailable.source_contexts)

    def test_span_forgery_and_derived_ids_cannot_become_independent_evidence(self) -> None:
        first = self.source('合成：在湿地看白鹭。')
        with self.assertRaises(MemoryFailure):
            self.registry.propose(self.access, 'forged', self.draft((replace(first, expected_text='编造的原话'),)))
        candidate = self.registry.propose(self.access, 'original', self.draft((first,)))
        with self.assertRaises(MemoryFailure):
            self.registry.propose(self.access, 'derived', self.draft((replace(first, message_id=candidate.observation_id),)))

    def test_counterevidence_cannot_be_relabelled_as_support_during_merge(self) -> None:
        first, counter = self.source('合成：喜欢湿地。'), self.source('合成：不喜欢湿地。')
        original = self.registry.propose(self.access, 'original', self.draft((first,), (counter,)))
        with self.assertRaisesRegex(MemoryFailure, 'observation_evidence_loss'):
            self.registry.propose(self.access, 'reverse-stance', self.draft((first, counter)),
                observation_id=original.observation_id, expected_revision=1, operation='merge')

    def test_split_withdraws_parent_atomically_and_retains_sources_in_children(self) -> None:
        first, second = self.source('合成：湿地见白鹭。'), self.source('合成：桥边认识安禾。')
        original = self.registry.propose(self.access, 'parent', self.draft((first, second)))
        confirmation = self.source('合成：确认拆分两个主题。')
        children = self.registry.split(self.access, 'split', original.observation_id, 1,
            (self.draft((first,)), replace(self.draft((second,)), topic_key='user:bridge')),
            confirmation_id=confirmation.message_id)
        self.assertEqual(len(children), 2)
        self.assertEqual(self.registry.read(self.access, original.observation_id).state, 'withdrawn')
        repeated = self.registry.split(self.access, 'split', original.observation_id, 1,
            (self.draft((first,)), replace(self.draft((second,)), topic_key='user:bridge')),
            confirmation_id=confirmation.message_id)
        self.assertEqual([child.observation_id for child in repeated], [child.observation_id for child in children])

    def test_change_and_correction_are_distinct_versions_with_old_raw_evidence_retained(self) -> None:
        first = self.source('合成：以前住在星湖镇。')
        original = self.registry.propose(self.access, 'old', self.draft((first,)))
        newer = self.source('合成：现在搬到银叶桥。')
        changed = self.registry.propose(self.access, 'change', replace(self.draft((newer,)), valid_from='2022-01-01T00:00:00Z'),
            observation_id=original.observation_id, expected_revision=1, operation='change')
        corrected = self.registry.propose(self.access, 'correction', self.draft((newer,)),
            observation_id=original.observation_id, expected_revision=changed.revision, operation='correction')
        assert corrected.record is not None
        self.assertEqual(corrected.record.operation, 'correction')
        self.assertEqual(self.runtime.state(self.access, first.message_id).status, 'active')
        with self.runtime._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM observation_events_v1').fetchone()[0], 3)

    def test_commit_budget_failure_rolls_back_publication(self) -> None:
        first = self.source('合成：在湿地看鸟。')
        original = self.registry.propose(self.access, 'original', self.draft((first,)))
        confirmation = self.source('合成：确认发布。')
        def canceled() -> float:
            raise MemoryFailure('observation_canceled')
        with self.assertRaisesRegex(MemoryFailure, 'observation_canceled'):
            self.registry.decide(self.access, 'cancel', original.observation_id, 1,
                action='publish', confirmation_id=confirmation.message_id, check=canceled)
        self.assertEqual(self.registry.read(self.access, original.observation_id).revision, 1)

    def test_worker_updates_one_slot_with_support_and_denial_without_publication_or_replay_votes(self) -> None:
        worker = ObservationProcessing(self.runtime)
        self.assertFalse(worker.status(self.access)['enabled'])
        worker.configure(self.access, enabled=True)
        self.source('我喜欢摄影。')
        one = worker.tick(self.access, limit=1, timeout=5)
        self.assertEqual(one['candidates'], 1)
        self.source('我不喜欢摄影。')
        two = worker.tick(self.access, limit=1, timeout=5)
        self.assertEqual(two['observation_ids'], one['observation_ids'])
        view = self.registry.read(self.access, one['observation_ids'][0])
        self.assertEqual((view.independent_support, view.independent_counterevidence), (1, 1))
        self.assertEqual(view.state, 'candidate')
        self.assertFalse(self.registry.query(self.access, view.record.draft.topic_key if view.record else '').rows)
        self.assertEqual(worker.tick(self.access, limit=1)['state'], 'idle')
        self.assertEqual(self.registry.read(self.access, view.observation_id).revision, 2)

    def test_worker_crash_rolls_back_progress_and_retries_same_lease_without_duplicate_observation(self) -> None:
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        self.source('我喜欢摄影。')
        with patch.object(worker.registry, '_append', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
                worker.tick(self.access, limit=1, timeout=5)
        completed = worker.tick(self.access, limit=1, timeout=5)
        self.assertEqual(completed['processed'], 1)
        with self.runtime._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM observation_events_v1').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT attempt FROM candidate_runs_v1').fetchone()[0], 2)
        self.assertEqual(worker.tick(self.access, limit=1)['state'], 'idle')

    def test_worker_precancel_does_not_create_progress_or_candidates(self) -> None:
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        self.source('我喜欢摄影。')
        canceled = threading.Event()
        canceled.set()
        with self.assertRaisesRegex(MemoryFailure, 'observation_canceled'):
            worker.tick(self.access, cancel=canceled)
        self.assertEqual(worker.status(self.access)['completed_runs'], 0)

    def test_process_death_after_durable_lease_resumes_once_after_expiration(self) -> None:
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        first = self.source('我喜欢摄影。')
        access_path = self.config.parent / 'mcp-access-worker.json'
        access_path.write_text(json.dumps(asdict(self.access)), encoding='utf-8')
        code = ('import os,sys; from pathlib import Path; from evernook import MemoryRuntime; '
            'from evernook.observation_processing import ObservationProcessing; '
            'r=MemoryRuntime(Path(sys.argv[1])); a=r.access_from_file(Path(sys.argv[2])); w=ObservationProcessing(r); '
            'w.workflow.begin(a,(sys.argv[3],),processor_version=w.processor,lease_seconds=1); os._exit(71)')
        dead = subprocess.run([sys.executable, '-B', '-c', code, str(self.config), str(access_path), first.message_id],
            capture_output=True, timeout=15)
        self.assertEqual(dead.returncode, 71)
        time.sleep(1.05)
        receipt = worker.tick(self.access, limit=1, timeout=5)
        self.assertEqual(receipt['processed'], 1)
        self.assertEqual(worker.tick(self.access, limit=1)['state'], 'idle')
        with self.runtime._connection() as db:
            self.assertEqual(db.execute('SELECT attempt FROM candidate_runs_v1').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM observation_events_v1').fetchone()[0], 1)

    def test_processor_change_reprocesses_originals_without_duplicate_independent_votes_or_publication(self) -> None:
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        self.source('我喜欢摄影。')
        first = worker.tick(self.access, limit=1, timeout=5)
        with patch.object(ObservationProcessing, 'processor', new_callable=PropertyMock, return_value='observation-rules-v1:synthetic-next'):
            second = worker.tick(self.access, limit=1, timeout=5)
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual(first['observation_ids'], second['observation_ids'])
        view = self.registry.read(self.access, first['observation_ids'][0])
        self.assertEqual(view.independent_support, 1)
        self.assertEqual(view.state, 'candidate')

    def test_purge_of_one_member_rebuilds_surviving_aggregation_without_blocking_later_work(self) -> None:
        from evernook.purge import PurgeDecision, PurgeWorkflow
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        first = self.source('我喜欢摄影。')
        worker.tick(self.access, limit=1, timeout=5)
        self.source('我不喜欢摄影。')
        worker.tick(self.access, limit=1, timeout=5)
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, 'purge-member', (first.message_id,))
        confirmation = self.source('合成：确认删除第一个来源。')
        purge.purge(self.access, PurgeDecision(preview.request_id, confirmation.message_id))
        self.source('我喜欢阅读。')
        for _ in range(4):
            worker.tick(self.access, limit=1, timeout=5)
        from evernook.automatic_preferences import PreferenceUpdates
        key, _ = PreferenceUpdates._key_value('我喜欢阅读。', 'general_interest')
        self.assertEqual(len(self.registry.related(self.access, key).rows), 1)

    def test_candidate_budget_splits_sources_without_dropping_later_clauses(self) -> None:
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        self.source('我喜欢摄影。' * 17)
        self.source('我喜欢摄影。' * 17)
        first = worker.tick(self.access, timeout=5)
        second = worker.tick(self.access, timeout=5)
        self.assertEqual((first['processed'], second['processed']), (1, 1))
        self.assertEqual((first['extracted'], second['extracted']), (17, 17))
        view = self.registry.read(self.access, first['observation_ids'][0])
        self.assertEqual(view.independent_support, 2)

    def test_overflow_quarantines_topic_and_continues_other_slots_without_truncating_counterevidence(self) -> None:
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        for _ in range(64):
            self.source('我喜欢摄影。')
        self.source('我不喜欢摄影。')
        self.source('我喜欢阅读。')
        receipts = [worker.tick(self.access, limit=16, timeout=10) for _ in range(5)]
        self.assertTrue(any(receipt.get('blocked_topics') for receipt in receipts))
        from evernook.automatic_preferences import PreferenceUpdates
        key, _ = PreferenceUpdates._key_value('我喜欢摄影。', 'general_interest')
        blocked, = self.registry.related(self.access, key).rows
        self.assertEqual(blocked.state, 'needs_review')
        self.assertIsNone(blocked.conclusion)
        evidence = worker.evidence(self.access, key, limit=32, offset=64)
        self.assertEqual(evidence['rows'][0]['stance'], 'counterevidence')
        other, _ = PreferenceUpdates._key_value('我喜欢阅读。', 'general_interest')
        self.assertEqual(len(self.registry.related(self.access, other).rows), 1)

    def test_overflow_split_must_include_the_late_counterevidence_not_only_old_head(self) -> None:
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        self.source('我喜欢摄影。' * 32)
        self.source('我喜欢摄影。' * 32)
        first = worker.tick(self.access, limit=1, timeout=5)
        worker.tick(self.access, limit=1, timeout=5)
        before = self.registry.read(self.access, first['observation_ids'][0])
        assert before.record is not None
        negative = self.source('我不喜欢摄影。')
        worker.tick(self.access, limit=1, timeout=5)
        parent = self.registry.read(self.access, before.observation_id)
        support = before.record.draft.support
        confirmation = self.source('合成：审查全部依据后确认拆分。')
        children = (replace(before.record.draft, topic_key='photo:one', support=support[:32]),
            replace(before.record.draft, topic_key='photo:two', support=support[32:]))
        with self.assertRaisesRegex(MemoryFailure, 'observation_evidence_loss'):
            self.registry.split(self.access, 'incomplete-overflow-split', parent.observation_id, parent.revision,
                children, confirmation_id=confirmation.message_id)
        complete = (replace(children[0], counterevidence=(negative,)), children[1])
        result = self.registry.split(self.access, 'complete-overflow-split', parent.observation_id, parent.revision,
            complete, confirmation_id=confirmation.message_id)
        self.assertEqual(sum(child.independent_counterevidence for child in result), 1)

    def test_independent_slot_lookup_is_not_affected_by_question_answer_rrf(self) -> None:
        first = self.source('合成：在湿地看鸟。')
        exact = self.registry.propose(self.access, 'slot', self.draft((first,)))
        for index in range(20):
            self.registry.propose(self.access, 'noise-' + str(index), replace(self.draft((first,)), topic_key='noise:' + str(index)))
        with patch('evernook.semantic.SemanticRecall.recall', side_effect=AssertionError('QA RRF must not run')):
            page = self.registry.related(self.access, 'user:wetland', query='湿地', limit=1)
        self.assertEqual(page.rows[0].observation_id, exact.observation_id)

    def test_source_purged_observation_body_and_maintenance_atoms_are_physically_removed(self) -> None:
        from evernook.purge import PurgeDecision, PurgeWorkflow
        worker = ObservationProcessing(self.runtime)
        worker.configure(self.access, enabled=True)
        target = self.source('我喜欢摄影。')
        completed = worker.tick(self.access, limit=1, timeout=5)
        observation_id = completed['observation_ids'][0]
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, 'purge-observation', (target.message_id,))
        confirmation = self.source('合成：确认删除这一条来源及派生记录。')
        purge.purge(self.access, PurgeDecision(preview.request_id, confirmation.message_id))
        with self.assertRaisesRegex(MemoryFailure, 'observation_not_found'):
            self.registry.read(self.access, observation_id)
        with self.runtime._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM observation_events_v1').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM observation_atoms_v1').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM observation_requests_v1').fetchone()[0], 0)

    def test_source_suppressed_before_publication_cannot_publish_stale_candidate(self) -> None:
        first = self.source('合成：湿地观鸟。')
        candidate = self.registry.propose(self.access, 'propose', self.draft((first,)))
        confirmation = self.source('合成：确认不再使用这一条来源。')
        self.runtime.change(self.access, Change('suppress-before-publish', first.message_id, 1, 'suppress', confirmation.message_id, 'synthetic'))
        with self.assertRaisesRegex(MemoryFailure, 'stale_observation_source'):
            self.registry.decide(self.access, 'publish-stale', candidate.observation_id, 1,
                action='publish', confirmation_id=confirmation.message_id)

    def test_explicit_import_rebinds_every_source_and_does_not_inherit_publication(self) -> None:
        first = self.source('合成：湿地观鸟。')
        original = replace(first, message_id='imported-old-id')
        imported = self.registry.rebind(self.access, 'remap', self.draft((original,)), {'imported-old-id': first.message_id})
        self.assertEqual(imported.state, 'candidate')
        assert imported.record is not None
        self.assertEqual(imported.record.source_remap, (('imported-old-id', first.message_id),))
        self.assertEqual(imported.record.draft.support[0].message_id, first.message_id)
        self.assertEqual(imported.record.draft.policy_version, 'rebind-review-v1')
        with self.assertRaisesRegex(MemoryFailure, 'invalid_observation_remap'):
            self.registry.rebind(self.access, 'incomplete', self.draft((original,)), {})

    def test_withdrawal_advances_control_head_and_old_snapshot_cannot_publish_it_back(self) -> None:
        from evernook.archive import capture_control_checkpoint, export_archive, replay_archive_controls
        from evernook.runtime_restore import publish_replayed_runtime
        first = self.source('合成：湿地观鸟。')
        candidate = self.registry.propose(self.access, 'propose', self.draft((first,)))
        confirmation = self.source('合成：确认发布。')
        published = self.registry.decide(self.access, 'publish', candidate.observation_id, 1,
            action='publish', confirmation_id=confirmation.message_id)
        old = self.root / 'old'
        export_archive(self.runtime, self.access, old)
        before = capture_control_checkpoint(self.runtime, self.access)
        withdrawal = self.source('合成：确认撤回这条观察。')
        self.registry.decide(self.access, 'withdraw', candidate.observation_id, published.revision,
            action='withdraw', confirmation_id=withdrawal.message_id)
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        self.assertNotEqual(before.control_head, checkpoint.control_head)
        replayed = replay_archive_controls(self.runtime, self.access, old, self.root / 'replayed', checkpoint=checkpoint)
        with self.assertRaisesRegex(MemoryFailure, 'archive_evidence_incomplete'):
            publish_replayed_runtime(self.runtime, self.access, replayed.root, self.root / 'restored',
                checkpoint=checkpoint, client_id='restored', entry='test')


if __name__ == '__main__':
    unittest.main()
