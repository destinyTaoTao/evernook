from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import threading
import tempfile
import unittest

from evernook import Change, Event, MemoryRuntime, Source, initialize
from evernook.episodes import Episodes
from evernook.episodes import episode_draft
from evernook.models import MemoryFailure
from evernook.consolidation import ConsolidationWorkflow
from evernook.scheduler import ConsolidationScheduler


class EpisodeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-episodes-')
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / 'runtime', client_id='synthetic', entry='test')
        self.runtime = MemoryRuntime(self.config)
        self.episodes = Episodes(self.runtime)

    def save(self, text: str, session: str, *, role: str = 'user') -> str:
        return self.runtime.submit(self.access, Event(Source('test', session, 'main', session + role), role, 1,
            '2021-01-01T00:00:00Z', 'complete', text)).message_id

    def test_cross_session_excerpts_keep_roles_times_entities_and_review_authority(self) -> None:
        first = self.save('合成：在星湖镇认识了安禾。', 'before')
        second = self.save('合成：我们聊过安禾教修车的经历。', 'later', role='assistant')
        ConsolidationWorkflow(self.runtime).configure(self.access, tier='auto')
        candidate = self.episodes.propose(self.access, (first, second), entities=('安禾',))
        self.assertEqual(candidate.state, 'pending_review')
        self.assertEqual(candidate.independent_sources, 2)
        self.assertEqual({source['role'] for source in candidate.source_contexts}, {'user', 'assistant'})
        self.assertEqual(candidate.authority, 'review_candidate')
        self.assertEqual(len(candidate.entity_mentions), 2)
        self.assertTrue(all(source['learned_at'] and source['occurred_at'] for source in candidate.source_contexts))
        repeated = self.episodes.propose(self.access, (first, first, second), entities=('安禾',))
        self.assertEqual(repeated.candidate_id, candidate.candidate_id)
        self.assertEqual(repeated.independent_sources, 2)

    def test_suppression_blocks_all_candidate_text_and_rebuild_uses_new_source_identity(self) -> None:
        first = self.save('合成：在星湖镇认识了安禾。', 'before')
        second = self.save('合成：后来安禾教修车。', 'later')
        candidate = self.episodes.propose(self.access, (first, second), entities=('安禾',))
        correction = self.save('合成：更正，认识安禾的地点是银叶桥。', 'correction')
        self.runtime.change(self.access, Change('episode-correct', first, 1, 'correct', correction, 'synthetic'))
        unavailable = self.episodes.read(self.access, candidate.candidate_id)
        self.assertEqual(unavailable.state, 'unavailable')
        self.assertIsNone(unavailable.summary)
        self.assertFalse(unavailable.source_contexts)
        rebuilt = self.episodes.propose(self.access, (correction, second), entities=('安禾',))
        self.assertNotEqual(rebuilt.candidate_id, candidate.candidate_id)
        self.assertIn('银叶桥', rebuilt.summary or '')

    def test_literal_entity_annotations_cannot_invent_names(self) -> None:
        first = self.save('合成：在星湖镇认识了安禾。', 'before')
        second = self.save('合成：后来教修车。', 'later')
        with self.assertRaisesRegex(MemoryFailure, 'episode_entity_not_grounded'):
            self.episodes.propose(self.access, (first, second), entities=('虚构人物',))

    def test_explicitly_enabled_background_batches_create_review_only_episodes(self) -> None:
        self.assertFalse(self.episodes.enabled(self.access))
        self.episodes.configure(self.access, enabled=True)
        self.save('合成：在星湖镇认识了安禾。', 'before')
        self.save('合成：后来安禾教修车。', 'later')
        scheduler = ConsolidationScheduler(self.runtime)
        scheduler.configure(self.access, min_pending=1, idle_seconds=0, max_trigger_seconds=5)
        tick = scheduler.trigger(self.access, reason='idle')
        self.assertEqual(tick.processed, 2)
        candidates = [row for row in ConsolidationWorkflow(self.runtime).queue(self.access).rows if row.kind == 'episode']
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].state, 'pending_review')

    def test_generic_source_bound_candidate_cannot_forge_episode_summary(self) -> None:
        first = self.save('合成：在星湖镇认识了安禾。', 'before')
        second = self.save('合成：后来安禾教修车。', 'later')
        workflow = ConsolidationWorkflow(self.runtime)
        lease = workflow.begin(self.access, (first, second), processor_version='synthetic-forged')
        draft = episode_draft(lease.sources)
        assert lease.token is not None
        forged = replace(draft, detail=draft.detail.replace('星湖镇', '虚构镇'))
        result = workflow.complete(self.access, lease.status.run_id, lease.token, (forged,))
        with self.assertRaisesRegex(MemoryFailure, 'invalid_episode'):
            self.episodes.read(self.access, result.drafts[0].candidate_id)
        from evernook.management import MemoryManagement
        self.assertEqual(MemoryManagement(self.runtime).read(self.access, view='network_graph')['nodes'], [])

    def test_precanceled_request_cannot_create_a_candidate(self) -> None:
        first = self.save('合成：在星湖镇认识了安禾。', 'before')
        second = self.save('合成：后来安禾教修车。', 'later')
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(MemoryFailure, 'episode_canceled'):
            self.episodes.propose(self.access, (first, second), cancel=cancel)
        self.assertFalse(ConsolidationWorkflow(self.runtime).queue(self.access).rows)


if __name__ == '__main__':
    unittest.main()
