from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Change, Event, MemoryRuntime, Source, initialize
from evernook.models import MemoryFailure
from evernook.topic_views import TopicViewDefinition, TopicViews


class TopicViewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-topic-views-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / 'runtime', client_id='owner', entry='owner', allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.views = TopicViews(self.runtime)
        self.number = 0

    def save(self, text: str, *, session: str = 'history') -> str:
        self.number += 1
        return self.runtime.submit(self.access, Event(Source('owner', session, 'main', str(self.number)), 'user',
            self.number, '2020-01-01T00:00:00Z', 'complete', text)).message_id

    def definition(self) -> TopicViewDefinition:
        return TopicViewDefinition('湿地经历', '为后续聊天提供可回源背景', '湿地')

    def test_definition_refresh_and_read_are_shared_and_source_backed(self) -> None:
        source = self.save('合成：我们在湿地见过白鹭。')
        configured = self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.assertEqual(configured.state, 'pending')
        refreshed = self.views.refresh(self.access, 'wetland', timeout=10)
        self.assertEqual(refreshed.state, 'valid')
        self.assertIsNotNone(refreshed.data)
        assert refreshed.data is not None
        self.assertEqual(refreshed.data['raw_excerpts'][0]['message_id'], source)
        other = TopicViews(MemoryRuntime(self.config)).read(self.access, 'wetland')
        self.assertEqual(other.snapshot_revision, refreshed.snapshot_revision)
        self.assertEqual(other.data, refreshed.data)

    def test_management_graph_only_exposes_current_topic_with_authorized_evidence(self) -> None:
        from evernook.management import MemoryManagement
        source = self.save('合成：我们在湿地见过白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        ui = MemoryManagement(self.runtime, self.views)
        page = ui.read_versioned(self.access, view='network_graph', entry='owner', session_id='history')
        topics = [node for node in page['value']['nodes'] if node['kind'] == 'topic']
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]['label'], '湿地经历')
        self.assertEqual(topics[0]['edges'], [{'source_id': source, 'kind': 'evidence'}])
        self.assertFalse(ui.read(self.access, view='network_graph', entry='other')['nodes'])
        self.assertFalse(ui.read(self.access, view='network_graph', since='2021-01-01T00:00:00Z')['nodes'])
        with self.runtime._connection() as db:
            expiry = db.execute("SELECT expires_at FROM topic_view_snapshots_v1 WHERE view_id='wetland'").fetchone()[0]
            db.execute("UPDATE topic_view_snapshots_v1 SET expires_at='2000-01-01T00:00:00Z' WHERE view_id='wetland'")
        self.assertFalse(ui.read(self.access, view='network_graph')['nodes'])
        with self.runtime._connection() as db:
            db.execute("UPDATE topic_view_snapshots_v1 SET expires_at=? WHERE view_id='wetland'", (expiry,))
        processor = self.views.processor
        self.views._processor_identity = 'changed-processor'
        self.assertFalse(ui.read(self.access, view='network_graph')['nodes'])
        self.views._processor_identity = processor
        confirmation = self.save('合成：停止旧来源。')
        self.assertFalse(ui.read(self.access, view='network_graph')['nodes'])
        self.assertNotEqual(ui.fingerprint(self.access, view='network_graph', entry='owner', session_id='history')['token'], page['token'])
        self.views.refresh(self.access, 'wetland', timeout=10)
        self.runtime.change(self.access, Change('hide-topic-source', source, 1, 'suppress', confirmation, 'synthetic'))
        self.assertFalse(ui.read(self.access, view='network_graph')['nodes'])
        self.views.control(self.access, 'pause-topic', 'wetland', 1, action='pause')
        self.assertFalse(ui.read(self.access, view='network_graph')['nodes'])

    def test_management_service_graph_uses_service_embedding_identity(self) -> None:
        from evernook import SemanticRecall
        from evernook.service import dispatch
        from tests.contracts.test_semantic import SyntheticEmbedding
        backend = SyntheticEmbedding()
        self.views = TopicViews(self.runtime, SemanticRecall(self.runtime, backend))
        self.save('合成：我们在湿地见过白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        request = {'version': 1, 'access': asdict(self.access), 'operation': 'cli',
            'arguments': {'command': 'manage', 'input': {'action': 'read', 'view': 'network_graph'}}}
        with patch('evernook.cli.load_embedding', return_value=backend) as load:
            result = dispatch(self.runtime, request, embedding_config=self.root / 'trusted-embedding.json')
        load.assert_called_once_with(self.root / 'trusted-embedding.json')
        assert isinstance(result, dict)
        self.assertTrue(any(node['kind'] == 'topic' for node in result['nodes']))
        batch = {'version': 1, 'access': asdict(self.access), 'operation': 'cli',
            'arguments': {'command': 'manage', 'input': {'action': 'fingerprints', 'requests': [{'view': 'network_graph'}]}}}
        with patch('evernook.cli.load_embedding', return_value=backend):
            token = dispatch(self.runtime, batch, embedding_config=self.root / 'trusted-embedding.json')
        assert isinstance(token, dict)
        self.assertEqual(len(token['tokens'][0]), 64)
        without = dispatch(self.runtime, request)
        assert isinstance(without, dict)
        self.assertFalse(any(node['kind'] == 'topic' for node in without['nodes']))

    def test_new_evidence_and_retraction_are_distinct_clocks_and_old_body_is_never_read(self) -> None:
        source = self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        before = self.views.status(self.access)['clock']
        confirmation = self.save('合成：后来湿地见苍鹭。')
        after = self.views.status(self.access)['clock']
        self.assertGreater(after['evidence'], before['evidence'])
        stale = self.views.read(self.access, 'wetland')
        self.assertEqual(stale.state, 'needs_refresh')
        self.assertIsNone(stale.data)
        self.views.refresh(self.access, 'wetland', timeout=10)
        self.runtime.change(self.access, Change('hide', source, 1, 'suppress', confirmation, 'synthetic'))
        retracted = self.views.status(self.access)['clock']
        self.assertGreater(retracted['control'], after['control'])
        self.assertIsNone(self.views.read(self.access, 'wetland').data)

    def test_many_new_messages_coalesce_to_one_refresh_job(self) -> None:
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        for index in range(20):
            self.save('合成湿地消息' + str(index))
        status = self.views.status(self.access)
        self.assertEqual(status['definitions'], 1)
        self.assertLessEqual(status['stored_jobs'], 1)
        tick = self.views.tick(self.access, timeout=10)
        self.assertEqual(tick['refreshed'], 1)
        self.assertEqual(self.views.tick(self.access, timeout=10)['refreshed'], 0)

    def test_pause_revision_and_delete_have_explicit_states_without_erasing_originals(self) -> None:
        source = self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        paused = self.views.control(self.access, 'pause', 'wetland', 1, action='pause')
        self.assertEqual(paused.state, 'paused')
        self.assertEqual(self.views.tick(self.access)['refreshed'], 0)
        resumed = self.views.control(self.access, 'resume', 'wetland', 2, action='resume')
        self.assertEqual(resumed.state, 'needs_refresh')
        self.views.refresh(self.access, 'wetland', timeout=10)
        deleted = self.views.control(self.access, 'delete', 'wetland', 3, action='delete')
        self.assertEqual(deleted.state, 'deleted')
        self.assertIsNone(deleted.data)
        self.assertEqual(self.runtime.state(self.access, source).status, 'active')
        with self.runtime._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM topic_view_snapshots_v1').fetchone()[0], 0)

    def test_configuration_is_idempotent_and_policy_edits_invalidate_old_content(self) -> None:
        self.save('合成：湿地见白鹭。')
        first = self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        repeated = self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.assertEqual(first.definition_revision, repeated.definition_revision)
        self.views.refresh(self.access, 'wetland', timeout=10)
        changed = self.views.configure(self.access, 'revise', 'wetland', 1,
            replace(self.definition(), policy_version='topic-view-v2'))
        self.assertEqual(changed.state, 'needs_refresh')
        self.assertIsNone(changed.data)
        with self.assertRaisesRegex(MemoryFailure, 'topic_view_revision_conflict'):
            self.views.configure(self.access, 'stale-update', 'wetland', 1, self.definition())

    def test_explicit_user_edit_is_atomic_raw_evidence_and_does_not_change_core_persona(self) -> None:
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        edited = self.views.add_evidence(self.access, 'edit', 'wetland', 1, '合成：这个专题只指桥北湿地。')
        source_id = edited['source_receipt']['message_id']
        stored = self.runtime.read(self.access, source_id)
        assert stored is not None
        self.assertEqual(stored.event.role, 'user')
        self.assertEqual(stored.event.text, '合成：这个专题只指桥北湿地。')
        self.assertEqual(edited['view'].definition_revision, 2)
        repeated = self.views.add_evidence(self.access, 'edit', 'wetland', 1, '合成：这个专题只指桥北湿地。')
        self.assertTrue(repeated['replayed'])
        self.assertEqual(self.runtime.status(self.access).messages, 1)
        with self.runtime._connection() as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='persona_versions_v1'").fetchone())

    def test_clock_failure_rolls_back_raw_receipt_and_definition_failure_rolls_back_edit(self) -> None:
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        with self.runtime._connection() as db:
            db.execute("CREATE TRIGGER synthetic_clock_failure BEFORE UPDATE ON topic_view_clock_v1 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(MemoryFailure):
            self.save('合成：不能确认保存的湿地消息。')
        self.assertEqual(self.runtime.status(self.access).messages, 0)
        with self.runtime._connection() as db:
            db.execute('DROP TRIGGER synthetic_clock_failure')
            db.execute("CREATE TRIGGER synthetic_view_failure BEFORE INSERT ON topic_view_definitions_v1 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(MemoryFailure):
            self.views.add_evidence(self.access, 'edit-failed', 'wetland', 1, '合成：专题用户编辑也要整体回滚。')
        self.assertEqual(self.runtime.status(self.access).messages, 0)

    def test_source_change_during_refresh_does_not_commit_stale_body(self) -> None:
        target = self.save('合成：旧湿地话语。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        from evernook.topic_view_refresh import build
        def changed(*args: Any, **kwargs: Any) -> Any:
            result = build(*args, **kwargs)
            confirmation = self.save('合成：停止旧来源。')
            self.runtime.change(self.access, Change('during-refresh', target, 1, 'suppress', confirmation, 'synthetic'))
            return result
        with patch('evernook.topic_view_refresh.build', side_effect=changed):
            result = self.views.refresh(self.access, 'wetland', timeout=10)
        self.assertIsNone(result.data)
        with self.runtime._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM topic_view_snapshots_v1').fetchone()[0], 0)

    def test_precancel_and_expired_snapshot_never_return_old_body(self) -> None:
        self.save('合成：湿地旧话语。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        canceled = threading.Event()
        canceled.set()
        with self.assertRaisesRegex(MemoryFailure, 'topic_view_canceled'):
            self.views.refresh(self.access, 'wetland', cancel=canceled)
        self.views.refresh(self.access, 'wetland', timeout=10)
        with self.runtime._connection() as db:
            db.execute("UPDATE topic_view_snapshots_v1 SET expires_at='2000-01-01T00:00:00Z'")
        self.assertEqual(self.views.read(self.access, 'wetland').state, 'expired')
        self.assertIsNone(self.views.read(self.access, 'wetland').data)

    def test_only_exact_single_current_input_insertion_can_be_excluded_from_freshness(self) -> None:
        self.save('合成：湿地旧话语。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        current = self.save('合成：当前请求。')
        self.assertEqual(self.views.read(self.access, 'wetland').state, 'needs_refresh')
        self.assertEqual(self.views.read(self.access, 'wetland', current_input_id=current).state, 'valid')
        self.save('合成：另一条新证据。')
        self.assertEqual(self.views.read(self.access, 'wetland', current_input_id=current).state, 'needs_refresh')

    def test_normal_context_uses_bounded_current_view_and_honors_source_control(self) -> None:
        from evernook.context import ContextBuilder, ContextPolicy
        from test_context import SyntheticCounter
        source = self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        policy = ContextPolicy(10000, 20000, 0, 7, 8192, topic_view_ids=('wetland',), topic_view_tokens=7000)
        counter = SyntheticCounter()
        current = self.save('合成：新一轮聊这个专题。')
        builder = ContextBuilder(self.runtime, self.views.recall, policy, counter)
        import json
        bundle = builder.build(self.access, '湿地', as_of='2026-11-13T00:00:00Z', mode='normal', timeout=10, current_input_id=current)
        data = json.loads(bundle.payload)
        self.assertTrue(any(item['kind'] == 'topic_view' for item in data['items']))
        self.assertLessEqual(bundle.used_tokens, 10000)
        self.runtime.change(self.access, Change('hide-context', source, 1, 'suppress', current, 'synthetic'))
        after = builder.build(self.access, '湿地', as_of='2026-11-13T00:00:00Z', mode='normal', timeout=10, current_input_id=current)
        self.assertFalse(any(item['kind'] == 'topic_view' for item in json.loads(after.payload)['items']))

    def test_tokenizer_time_source_change_cannot_leave_view_or_duplicate_raw_body_in_payload(self) -> None:
        from evernook.context import ContextBuilder, ContextPolicy
        from test_context import SyntheticCounter
        source = self.save('合成：湿地旧正文。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        confirmation = self.save('合成：当前会话请求。')
        runtime, access = self.runtime, self.access
        class ChangedCounter(SyntheticCounter):
            changed = False
            def count(self, text: str) -> int:
                if 'untrusted_memory_context' in text and not self.changed:
                    self.changed = True
                    runtime.change(access, Change('hide-during-count', source, 1, 'suppress', confirmation, 'synthetic'))
                return super().count(text)
        builder = ContextBuilder(self.runtime, self.views.recall,
            ContextPolicy(10000, 20000, 0, 7, 8192, topic_view_ids=('wetland',), topic_view_tokens=7000), ChangedCounter())
        bundle = builder.build(self.access, '湿地', as_of='2026-11-13T00:00:00Z', mode='normal', timeout=10,
            current_input_id=confirmation)
        self.assertEqual(bundle.payload, '')
        self.assertEqual(bundle.evidence_count, 0)
        self.assertIn('memory_changed_before_handoff', bundle.degraded)

    def test_tokenizer_time_source_change_is_blocked_without_configured_views(self) -> None:
        from evernook.context import ContextBuilder, ContextPolicy
        from test_context import SyntheticCounter
        source = self.save('合成：湿地旧正文。')
        confirmation = self.save('合成：当前会话请求。')
        runtime, access = self.runtime, self.access
        class ChangedCounter(SyntheticCounter):
            changed = False
            def count(self, text: str) -> int:
                if 'untrusted_memory_context' in text and not self.changed:
                    self.changed = True
                    runtime.change(access, Change('hide-no-view', source, 1, 'suppress', confirmation, 'synthetic'))
                return super().count(text)
        builder = ContextBuilder(self.runtime, self.views.recall, ContextPolicy(10000, 20000, 0, 7, 8192), ChangedCounter())
        result = builder.build(self.access, '湿地', as_of='2026-11-13T00:00:00Z', mode='normal', timeout=10, current_input_id=confirmation)
        self.assertEqual(result.payload, '')
        self.assertIn('memory_changed_before_handoff', result.degraded)

    def test_current_view_is_not_presented_as_historical_knowledge(self) -> None:
        from evernook.context import ContextBuilder, ContextPolicy
        from test_context import SyntheticCounter
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        builder = ContextBuilder(self.runtime, self.views.recall,
            ContextPolicy(10000, 20000, 0, 7, 8192, topic_view_ids=('wetland',), topic_view_tokens=7000), SyntheticCounter())
        result = builder.build(self.access, '', as_of='2026-11-13T00:00:00Z', known_at='2020-01-01T00:00:00Z', mode='normal', timeout=10)
        self.assertFalse(any(item['kind'] == 'topic_view' for item in json.loads(result.payload)['items']))
        self.assertTrue(any(item.reason == 'topic_view_history_unavailable' for item in result.omitted))

    def test_fresh_replayed_snapshot_preserves_clock_and_old_definition_cannot_return(self) -> None:
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        from evernook.runtime_restore import publish_replayed_runtime
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        self.runtime.export_archive(self.access, self.root / 'old')
        self.views.control(self.access, 'pause', 'wetland', 1, action='pause')
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / 'old', self.root / 'old-replayed', checkpoint=checkpoint)
        with self.assertRaisesRegex(MemoryFailure, 'archive_evidence_incomplete'):
            publish_replayed_runtime(self.runtime, self.access, self.root / 'old-replayed', self.root / 'bad',
                checkpoint=checkpoint, client_id='restored', entry='test')
        self.runtime.export_archive(self.access, self.root / 'fresh')
        replay_archive_controls(self.runtime, self.access, self.root / 'fresh', self.root / 'fresh-replayed', checkpoint=checkpoint)
        config, access = publish_replayed_runtime(self.runtime, self.access, self.root / 'fresh-replayed', self.root / 'good',
            checkpoint=checkpoint, client_id='restored', entry='test')
        self.assertEqual(TopicViews(MemoryRuntime(config)).read(access, 'wetland').state, 'paused')

    def test_purge_removes_all_snapshot_bodies_and_refresh_does_not_reuse_old_revision(self) -> None:
        from evernook.purge import PurgeDecision, PurgeWorkflow
        source = self.save('合成：湿地旧正文。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        old = self.views.refresh(self.access, 'wetland', timeout=10)
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, 'purge-view-source', (source,))
        confirmation = self.save('合成：确认删除这一条来源。')
        purge.purge(self.access, PurgeDecision(preview.request_id, confirmation))
        self.assertIsNone(self.views.read(self.access, 'wetland').data)
        with self.runtime._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM topic_view_snapshots_v1').fetchone()[0], 0)
        new = self.views.refresh(self.access, 'wetland', timeout=10)
        self.assertGreater(new.snapshot_revision, old.snapshot_revision)
        self.assertEqual(new.state, 'empty')

    def test_active_restore_installs_only_known_clock_hooks_before_new_capture(self) -> None:
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        from evernook.runtime_restore import publish_replayed_runtime
        import sqlite3
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        self.runtime.export_archive(self.access, self.root / 'snapshot')
        with closing(sqlite3.connect((self.root / 'snapshot' / 'evidence.sqlite3').as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 0)
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / 'snapshot', self.root / 'review', checkpoint=checkpoint)
        config, access = publish_replayed_runtime(self.runtime, self.access, self.root / 'review', self.root / 'active',
            checkpoint=checkpoint, client_id='restored', entry='test')
        restored = MemoryRuntime(config)
        self.assertEqual(TopicViews(restored).read(access, 'wetland').state, 'valid')
        restored.submit(access, Event(Source('test', 'fresh', 'main', '1'), 'user', 1, None, 'complete', '合成：新证据。'))
        self.assertEqual(TopicViews(restored).read(access, 'wetland').state, 'needs_refresh')

    def test_request_ids_are_separated_between_control_and_user_edit(self) -> None:
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.control(self.access, 'cross-operation', 'wetland', 1, action='pause')
        with self.assertRaisesRegex(MemoryFailure, 'topic_view_request_conflict'):
            self.views.add_evidence(self.access, 'cross-operation', 'wetland', 1, 'pause')
        self.assertEqual(self.runtime.status(self.access).messages, 0)

    def test_explicit_edit_is_cited_as_user_metadata_outside_historical_selector_scope(self) -> None:
        source = self.save('合成：旧湿地话语。')
        scoped = replace(self.definition(), source_ids=(source,), sessions=(('owner', 'history'),),
            since='2019-01-01T00:00:00Z', until='2021-01-01T00:00:00Z')
        self.views.configure(self.access, 'define', 'wetland', 0, scoped)
        self.views.add_evidence(self.access, 'edit-scope', 'wetland', 1, '合成：修订专题为桥南湿地。')
        refreshed = self.views.refresh(self.access, 'wetland', timeout=10)
        assert refreshed.data is not None
        self.assertTrue(any(item['quote'] == '合成：修订专题为桥南湿地。'
            and item.get('scope_exception') == 'explicit_user_view_edit' for item in refreshed.data['citations']))

    def test_transitive_rule_code_change_invalidates_existing_snapshot_identity(self) -> None:
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        original_read = Path.read_bytes
        def changed(path: Path) -> bytes:
            value = original_read(path)
            return value + b'\n# synthetic implementation change' if path.name == 'preference_statements.py' else value
        with patch.object(Path, 'read_bytes', new=changed):
            upgraded = TopicViews(self.runtime)
            self.assertNotEqual(upgraded.processor, self.views.processor)
            self.assertEqual(upgraded.read(self.access, 'wetland').state, 'needs_refresh')

    def test_maintenance_without_recall_cannot_read_body_through_idempotent_mutation(self) -> None:
        from evernook import pair_client
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        limited = pair_client(self.config, client_id='no-read', entry='no-read',
            access_path=self.config.parent / 'mcp-access-no-read.json', scopes=('maintain', 'accept', 'capture'))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.views.configure(limited, 'define', 'wetland', 0, self.definition())
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.views.control(limited, 'pause-no-read', 'wetland', 1, action='pause')

    def test_host_system_metadata_does_not_invalidate_view_or_replace_current_input_marker(self) -> None:
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        current = self.save('合成：当前用户请求。')
        self.runtime.submit(self.access, Event(Source('owner', 'metadata', 'main', '1'), 'system', 1,
            None, 'complete', '合成：宿主准备下一轮的系统元数据。'))
        self.assertEqual(self.views.read(self.access, 'wetland', current_input_id=current).state, 'valid')
        self.assertEqual(self.views.read(self.access, 'wetland').state, 'needs_refresh')

    def test_projection_and_partial_records_never_become_view_evidence(self) -> None:
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        self.views.refresh(self.access, 'wetland', timeout=10)
        for index, event in enumerate((Event(Source('owner', 'projection', 'main', '1'), 'user', 1, None, 'complete',
                '合成：明确标为敏感的原始内容。', sensitive=True),
                Event(Source('owner', 'partial', 'main', '1'), 'assistant', 1, None, 'partial', '合成未完成回复'))):
            with self.subTest(index=index):
                self.runtime.submit(self.access, event)
                self.assertEqual(self.views.read(self.access, 'wetland').state, 'valid')

    def test_abrupt_worker_death_leaves_retryable_lease_and_does_not_duplicate_snapshot(self) -> None:
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        access_path = self.config.parent / 'mcp-access-worker.json'
        access_path.write_text(json.dumps(asdict(self.access)), encoding='utf-8')
        code = ("import os,sys; from pathlib import Path; from unittest.mock import patch; from evernook import MemoryRuntime; "
            "from evernook.topic_views import TopicViews; r=MemoryRuntime(Path(sys.argv[1])); a=r.access_from_file(Path(sys.argv[2])); "
            "p=patch('evernook.topic_view_refresh.build',side_effect=lambda *args,**kwargs: os._exit(71)); p.start(); "
            "TopicViews(r).refresh(a,'wetland',timeout=1)")
        dead = subprocess.run([sys.executable, '-B', '-c', code, str(self.config), str(access_path)], capture_output=True, timeout=15)
        self.assertEqual(dead.returncode, 71)
        time.sleep(6.1)
        result = self.views.tick(self.access, timeout=10)
        self.assertEqual(result['refreshed'], 1)
        with self.runtime._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM topic_view_snapshots_v1').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT attempt FROM topic_view_jobs_v1').fetchone()[0], 2)
        self.assertEqual(self.views.tick(self.access)['refreshed'], 0)

    def test_export_rejects_unknown_trigger_instead_of_allowing_arbitrary_sql(self) -> None:
        self.views.configure(self.access, 'define', 'wetland', 0, self.definition())
        with self.runtime._connection() as db:
            db.execute('CREATE TRIGGER foreign_sql AFTER INSERT ON messages BEGIN SELECT 1; END')
        with self.assertRaisesRegex(MemoryFailure, 'archive_integrity_mismatch'):
            self.runtime.export_archive(self.access, self.root / 'bad-export')

    def test_refresh_interval_coalesces_frequent_changes_but_explicit_refresh_is_available(self) -> None:
        self.save('合成：湿地见白鹭。')
        self.views.configure(self.access, 'define', 'wetland', 0, replace(self.definition(), refresh_interval_seconds=60))
        self.assertEqual(self.views.tick(self.access, timeout=10)['refreshed'], 1)
        for index in range(10):
            self.save('合成：湿地新消息' + str(index))
        self.assertEqual(self.views.read(self.access, 'wetland').state, 'needs_refresh')
        self.assertEqual(self.views.tick(self.access, timeout=10)['refreshed'], 0)
        self.assertEqual(self.views.refresh(self.access, 'wetland', timeout=10).state, 'valid')


if __name__ == '__main__':
    unittest.main()
