from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from evernook import Change, Event, EvidenceSpan, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.management import MemoryManagement
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry


class ManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-management-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="owner", entry="owner")
        self.runtime = MemoryRuntime(self.config)
        self.ui = MemoryManagement(self.runtime)
        self.profiles = ProfileRegistry(self.runtime)
        self.seq = 0

    def source(self, text: str = "合成：喜欢观鸟🙂") -> EvidenceSpan:
        self.seq += 1
        receipt = self.runtime.submit(self.access, Event(Source("owner", "synthetic-session", "main", str(self.seq)),
                                                        "user", self.seq, None, "complete", text))
        return EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)

    def draft(self, tag_id: str = "birding") -> str:
        span = self.source()
        self.profiles.decide(self.access, ProfileDecision("draft-" + tag_id, tag_id, 0, "propose",
                              ProfileDraft("interest", "喜欢观鸟", 0.8, (span,))))
        return span.message_id

    def apply(self, operation: dict[str, str]) -> dict[str, object]:
        preview = self.ui.preview(self.access, operation)
        return self.ui.confirm(self.access, preview["proposal_id"], operation)

    def test_persona_projection_is_trusted_versioned_and_does_not_revive_hidden_source(self) -> None:
        from evernook.persona import PersonaRegistry, PersonaSet
        source = self.source('合成：请使用温和语气。')
        other = self.source('合成：请称呼我小夏。')
        persona = PersonaRegistry(self.runtime)
        persona.set(self.access, PersonaSet('persona-tone', 'tone', '合成：温和', source.message_id, 'synthetic-private-reason', 0))
        persona.set(self.access, PersonaSet('persona-address', 'address', '合成：小夏', other.message_id, 'synthetic-private-reason', 0))
        first = self.ui.read_versioned(self.access, view='persona', limit=1)
        self.assertEqual(len(first['value']['items']), 1)
        self.assertIsNotNone(first['value']['next_offset'])
        page = self.ui.read(self.access, view='persona', query='温和')
        self.assertEqual(len(page['items']), 1)
        item = page['items'][0]
        self.assertEqual((item['block'], item['revision'], item['evidence_id']), ('tone', 1, source.message_id))
        self.assertNotIn('synthetic-private-reason', json.dumps(page, ensure_ascii=False))
        for invalid in ({'category': 'interest'}, {'session_id': 's'}, {'message_id': source.message_id}, {'cursor': 1}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(MemoryFailure, 'invalid_arguments'):
                self.ui.read(self.access, view='persona', **invalid)
        ordinary = pair_client(self.config, client_id='ordinary-persona', entry='ordinary-persona',
            access_path=self.config.parent / 'mcp-access-ordinary-persona.json', scopes=('capture', 'recall'))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.ui.read(ordinary, view='persona')
        old = self.ui.fingerprint(self.access, view='persona', query='温和')['token']
        self.runtime.change(self.access, Change('hide-persona-source', source.message_id, 1, 'suppress', other.message_id, 'synthetic'))
        current = self.ui.read(self.access, view='persona', query='温和')['items'][0]
        self.assertEqual(current['text'], '合成：温和')
        self.assertIsNone(current['evidence_id'])
        self.assertNotEqual(old, self.ui.fingerprint(self.access, view='persona', query='温和')['token'])

    def test_persona_preview_confirm_stale_cancel_clear_and_authority(self) -> None:
        from evernook.persona import PersonaRegistry
        operation = {'kind': 'persona_set', 'block': 'tone', 'text': '合成：温和语气'}
        preview = self.ui.preview(self.access, operation)
        stale = self.ui.preview(self.access, {**operation, 'text': '合成：活泼语气'})
        with self.runtime._connection() as db:
            stored = db.execute('SELECT snapshot FROM management_previews_v1 WHERE id=?', (preview['proposal_id'],)).fetchone()[0]
        self.assertNotIn(operation['text'], stored)
        with self.assertRaisesRegex(MemoryFailure, 'management_preview_mismatch'):
            self.ui.confirm(self.access, preview['proposal_id'], {**operation, 'text': '合成：擅自改变'})
        outcome = self.ui.confirm(self.access, preview['proposal_id'], operation)
        self.assertEqual(outcome['revision'], 1)
        self.assertEqual(self.ui.confirm(self.access, preview['proposal_id'], operation), outcome)
        self.assertEqual(PersonaRegistry(self.runtime).view(self.access).blocks['tone'].text, operation['text'])
        original = self.runtime.read(self.access, outcome['confirmation_id'])
        self.assertEqual(original.event.text, operation['text'])
        self.assertEqual(original.event.role, 'user')
        with self.assertRaisesRegex(MemoryFailure, 'management_preview_stale'):
            self.ui.confirm(self.access, stale['proposal_id'], {**operation, 'text': '合成：活泼语气'})
        cancel = self.ui.preview(self.access, operation)
        self.ui.cancel(self.access, cancel['proposal_id'])
        with self.assertRaisesRegex(MemoryFailure, 'management_preview_canceled'):
            self.ui.confirm(self.access, cancel['proposal_id'], operation)
        for invalid in ({**operation, 'block': 'unknown'}, {**operation, 'text': 'password=synthetic-secret-123'},
                        {**operation, 'text': '   '},
                        {**operation, 'approved': True}):
            with self.subTest(invalid=invalid), self.assertRaises(MemoryFailure):
                self.ui.preview(self.access, invalid)
        ordinary = pair_client(self.config, client_id='persona-writer', entry='persona-writer',
            access_path=self.config.parent / 'mcp-access-persona-writer.json', scopes=('capture', 'recall'))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.ui.preview(ordinary, operation)
        clearing = {**operation, 'text': ''}
        clear_preview = self.ui.preview(self.access, clearing)
        cleared = self.ui.confirm(self.access, clear_preview['proposal_id'], clearing)
        self.assertEqual(cleared['revision'], 2)
        self.assertEqual(PersonaRegistry(self.runtime).view(self.access).blocks['tone'].text, '')
        self.assertTrue(self.runtime.read(self.access, cleared['confirmation_id']).event.text)

    def test_persona_confirmation_rolls_back_evidence_and_revision_on_failure(self) -> None:
        from evernook.persona import PersonaRegistry
        operation = {'kind': 'persona_set', 'block': 'tone', 'text': '合成：原子提交'}
        preview = self.ui.preview(self.access, operation)
        before = self.runtime.status(self.access).messages
        real_set = PersonaRegistry._set
        def fail_after_write(registry: object, db: object, request: object) -> object:
            real_set(registry, db, request)
            raise RuntimeError('synthetic interrupted commit')
        with patch.object(PersonaRegistry, '_set', autospec=True, side_effect=fail_after_write):
            with self.assertRaisesRegex(RuntimeError, 'synthetic interrupted commit'):
                self.ui.confirm(self.access, preview['proposal_id'], operation)
        self.assertEqual(self.runtime.status(self.access).messages, before)
        self.assertIsNone(PersonaRegistry(self.runtime).view(self.access).blocks['tone'])
        outcome = self.ui.confirm(self.access, preview['proposal_id'], operation)
        self.assertEqual(outcome['revision'], 1)
        self.assertEqual(self.runtime.status(self.access).messages, before + 1)

    def test_concurrent_persona_confirmations_cannot_both_write_same_revision(self) -> None:
        operations = [{'kind': 'persona_set', 'block': 'tone', 'text': f'合成语气{index}'} for index in (1, 2)]
        proposals = [self.ui.preview(self.access, operation)['proposal_id'] for operation in operations]
        barrier = threading.Barrier(2)
        def confirm(index: int) -> str:
            barrier.wait(timeout=10)
            try:
                result = self.ui.confirm(self.access, proposals[index], operations[index])
                return 'saved-' + str(result['revision'])
            except MemoryFailure as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(confirm, (0, 1)))
        self.assertCountEqual(outcomes, ['saved-1', 'management_preview_stale'])

    def test_persona_confirmation_rechecks_permissions_after_initial_authorization(self) -> None:
        from evernook.persona import PersonaRegistry
        path = self.config.parent / 'mcp-access-persona-editor.json'
        editor = pair_client(self.config, client_id='persona-editor', entry='persona-editor', access_path=path,
                             scopes=('recall', 'maintain', 'accept'))
        operation = {'kind': 'persona_set', 'block': 'tone', 'text': '合成撤权测试'}
        preview = self.ui.preview(editor, operation)
        original_authorize = self.ui._authorize
        def revoke_after_authorize(access: object) -> object:
            identity = original_authorize(access)
            pair_client(self.config, client_id='persona-editor', entry='persona-editor', access_path=path, scopes=('recall',))
            return identity
        with patch.object(self.ui, '_authorize', side_effect=revoke_after_authorize):
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                self.ui.confirm(editor, preview['proposal_id'], operation)
        self.assertIsNone(PersonaRegistry(self.runtime).view(self.access).blocks['tone'])
        pair_client(self.config, client_id='persona-editor', entry='persona-editor', access_path=path,
                    scopes=('recall', 'maintain', 'accept'))
        self.assertEqual(self.ui.confirm(editor, preview['proposal_id'], operation)['revision'], 1)

    def test_persona_preview_and_cancel_recheck_permissions_under_write_lock(self) -> None:
        path = self.config.parent / 'mcp-access-persona-editor.json'
        editor = pair_client(self.config, client_id='persona-editor', entry='persona-editor', access_path=path,
                             scopes=('recall', 'maintain', 'accept'))
        operation = {'kind': 'persona_set', 'block': 'tone', 'text': '合成撤权测试'}
        existing = self.ui.preview(editor, operation)
        original_authorize = self.ui._authorize
        def revoke_after_authorize(access: object) -> object:
            identity = original_authorize(access)
            pair_client(self.config, client_id='persona-editor', entry='persona-editor', access_path=path, scopes=('recall',))
            return identity
        for action in ('preview', 'cancel'):
            with self.subTest(action=action), patch.object(self.ui, '_authorize', side_effect=revoke_after_authorize):
                with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                    if action == 'preview':
                        self.ui.preview(editor, operation)
                    else:
                        self.ui.cancel(editor, existing['proposal_id'])
            pair_client(self.config, client_id='persona-editor', entry='persona-editor', access_path=path,
                        scopes=('recall', 'maintain', 'accept'))
        self.assertEqual(self.ui.confirm(editor, existing['proposal_id'], operation)['revision'], 1)

    def test_topic_settings_projection_tracks_versions_and_never_returns_snapshot_text(self) -> None:
        from evernook.topic_views import TopicViewDefinition, TopicViews
        views = TopicViews(self.runtime)
        definition = TopicViewDefinition('合成专题', '合成用途', '合成问题', entries=('owner',))
        views.configure(self.access, 'topic-create', 'topic-synthetic', 0, definition)
        first = self.ui.read_versioned(self.access, view='topic_settings')
        item = first['value']['items'][0]
        self.assertEqual((item['view_id'], item['revision'], item['state']), ('topic-synthetic', 1, 'pending'))
        self.assertEqual(item['definition']['entries'], ('owner',))
        self.assertNotIn('data', item)
        self.assertEqual(self.ui.fingerprint(self.access, view='topic_settings')['token'], first['token'])
        self.source('合成问题：隐藏的专题证据正文')
        views.refresh(self.access, 'topic-synthetic', timeout=10)
        current = self.ui.read_versioned(self.access, view='topic_settings')
        self.assertEqual(current['value']['items'][0]['state'], 'valid')
        self.assertNotIn('隐藏的专题证据正文', json.dumps(current['value'], ensure_ascii=False))
        self.assertNotEqual(current['token'], first['token'])
        self.source('合成：使专题快照失效')
        invalidated = self.ui.read_versioned(self.access, view='topic_settings')
        self.assertEqual(invalidated['value']['items'][0]['state'], 'needs_refresh')
        self.assertNotEqual(invalidated['token'], current['token'])
        views.control(self.access, 'topic-pause', 'topic-synthetic', 1, action='pause')
        paused = self.ui.read_versioned(self.access, view='topic_settings')
        self.assertNotEqual(first['token'], paused['token'])
        self.assertEqual(paused['value']['items'][0]['state'], 'paused')
        with self.assertRaisesRegex(MemoryFailure, 'topic_view_revision_conflict'):
            views.control(self.access, 'topic-stale', 'topic-synthetic', 1, action='resume')
        ordinary = pair_client(self.config, client_id='ordinary-topic', entry='ordinary-topic',
            access_path=self.config.parent / 'mcp-access-ordinary-topic.json', scopes=('capture', 'recall'))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.ui.read(ordinary, view='topic_settings')
        views.control(self.access, 'topic-delete', 'topic-synthetic', 2, action='delete')
        self.assertEqual(self.ui.read(self.access, view='topic_settings')['items'], [])

    def test_profile_review_revision_suppression_and_original_retention(self) -> None:
        source_id = self.draft()
        self.assertEqual(len(self.ui.read(self.access, view="drafts")["items"]), 1)
        self.apply({"kind": "profile_confirm", "tag_id": "birding"})
        self.assertEqual(len(self.ui.read(self.access, view="network_profiles", entry="owner", session_id="synthetic-session")["items"]), 1)
        self.assertEqual(self.ui.read(self.access, view="network_profiles", entry="other")["items"], [])
        self.assertEqual(self.ui.read(self.access, view="network_profiles", since="2020-01-01T00:00:00Z")["items"], [])
        items = self.ui.read(self.access, view="profiles")["items"]
        self.assertEqual(items[0]["state"], "confirmed")
        self.assertNotIn("expected_text", items[0]["draft"]["sources"][0])
        self.apply({"kind": "profile_revise", "tag_id": "birding", "label": "喜欢拍摄鸟类"})
        record = self.profiles.read(self.access, "birding").record
        assert record is not None
        self.assertEqual(record.draft.label, "喜欢拍摄鸟类")
        self.apply({"kind": "profile_suppress", "tag_id": "birding"})
        self.assertEqual(self.ui.read(self.access, view="profiles")["items"], [])
        self.assertIsNotNone(self.runtime.read(self.access, source_id))
        self.assertGreater(len(self.ui.read(self.access, view="history", tag_id="birding")["items"]), 2)

    def test_source_preview_stale_dependency_and_confirm_replay(self) -> None:
        source_id = self.draft()
        operation = {"kind": "source_suppress", "message_id": source_id}
        preview = self.ui.preview(self.access, operation)
        self.assertEqual(preview["affected"][0]["id"], "birding")
        self.apply({"kind": "profile_confirm", "tag_id": "birding"})
        with self.assertRaisesRegex(MemoryFailure, "management_preview_stale"):
            self.ui.confirm(self.access, preview["proposal_id"], operation)
        preview = self.ui.preview(self.access, operation)
        first = self.ui.confirm(self.access, preview["proposal_id"], operation)
        restarted = MemoryManagement(MemoryRuntime(self.config))
        self.assertEqual(restarted.confirm(self.access, preview["proposal_id"], operation), first)
        self.assertIsNone(self.runtime.read(self.access, source_id))
        self.assertEqual(self.ui.read(self.access, view="profiles")["items"], [])
        with self.assertRaisesRegex(MemoryFailure, "source_unavailable"):
            self.ui.read(self.access, view="source", message_id=source_id)

    def test_atomic_failure_leaves_no_confirmation_or_change(self) -> None:
        source_id = self.source().message_id
        operation = {"kind": "source_suppress", "message_id": source_id}
        preview = self.ui.preview(self.access, operation)
        before = self.runtime.status(self.access).messages
        with self.runtime._connection() as db:
            db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON changes BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(MemoryFailure):
            self.ui.confirm(self.access, preview["proposal_id"], operation)
        self.assertEqual(self.runtime.status(self.access).messages, before)
        self.assertEqual(self.runtime.state(self.access, source_id).status, "active")
        with self.runtime._connection() as db:
            db.execute("DROP TRIGGER synthetic_failure")
        self.assertEqual(self.ui.confirm(self.access, preview["proposal_id"], operation)["state"], "suppressed")

    def test_projection_token_tracks_visibility_without_exposing_content_or_audit_noise(self) -> None:
        source_id = self.draft()
        self.apply({'kind': 'profile_confirm', 'tag_id': 'birding'})
        arguments = {'view': 'network_graph', 'entry': 'owner', 'session_id': 'synthetic-session'}
        first = self.ui.read_versioned(self.access, **arguments)
        self.assertTrue(first['value']['nodes'])
        self.assertEqual(len(first['token']), 64)
        for _ in range(2):
            token = self.ui.fingerprint(self.access, **arguments)
            self.assertEqual(token, {'token': first['token']})
            self.assertNotIn('观鸟', json.dumps(token, ensure_ascii=False))
        other = self.ui.fingerprint(self.access, view='network_graph', entry='other')
        self.assertNotEqual(other['token'], first['token'])
        source = self.ui.read_versioned(self.access, view='source', message_id=source_id)
        self.apply({'kind': 'source_suppress', 'message_id': source_id})
        self.assertNotEqual(self.ui.fingerprint(self.access, **arguments)['token'], first['token'])
        with self.assertRaisesRegex(MemoryFailure, 'source_unavailable'):
            self.ui.fingerprint(self.access, view='source', message_id=source_id)
        self.assertIn('观鸟', source['value']['text'])
        self.assertNotEqual(MemoryManagement(MemoryRuntime(self.config)).fingerprint(self.access, **arguments)['token'],
                            self.ui.fingerprint(self.access, **arguments)['token'])
        ordinary_path = self.config.parent / 'mcp-access-ordinary.json'
        pair_client(self.config, client_id='ordinary', entry='ordinary', access_path=ordinary_path, scopes=('capture', 'recall'))
        ordinary = self.runtime.access_from_file(ordinary_path)
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.ui.fingerprint(ordinary, **arguments)

    def test_projection_token_remains_bound_to_the_snapshot_during_a_racing_change(self) -> None:
        source_id = self.draft()
        self.apply({'kind': 'profile_confirm', 'tag_id': 'birding'})
        original_read = self.ui.read
        def racing_read(*args: object, **kwargs: object) -> dict[str, object]:
            value = original_read(*args, **kwargs)
            self.apply({'kind': 'source_suppress', 'message_id': source_id})
            return value
        with patch.object(self.ui, 'read', side_effect=racing_read):
            snapshot = self.ui.read_versioned(self.access, view='network_graph')
        self.assertTrue(snapshot['value']['nodes'])
        self.assertNotEqual(self.ui.fingerprint(self.access, view='network_graph')['token'], snapshot['token'])

    def test_batch_fingerprints_are_ordered_bounded_and_fail_closed(self) -> None:
        span = self.source()
        requests = [{'view': 'timeline', 'limit': 24}, {'view': 'source', 'message_id': span.message_id}]
        expected = [self.ui.fingerprint(self.access, **request)['token'] for request in requests]
        self.assertEqual(self.ui.fingerprints(self.access, requests), {'tokens': expected})
        for invalid in ([], requests * 2, [{'view': 'source', 'message_id': span.message_id, 'action': 'confirm'}], ['bad']):
            with self.assertRaisesRegex(MemoryFailure, 'invalid_arguments'):
                self.ui.fingerprints(self.access, invalid)
        self.apply({'kind': 'source_suppress', 'message_id': span.message_id})
        with self.assertRaisesRegex(MemoryFailure, 'source_unavailable'):
            self.ui.fingerprints(self.access, requests)

    def test_permissions_expiry_payload_binding_and_cross_client(self) -> None:
        status = self.ui.read(self.access, view="status")
        self.assertEqual(status["configuration_schema_version"], 1)
        self.assertFalse(status["daily_admitted"])
        self.assertNotIn("token", json.dumps(status))
        ordinary_path = self.config.parent / "mcp-access-ordinary.json"
        pair_client(self.config, client_id="ordinary", entry="dsh", access_path=ordinary_path, scopes=("capture", "recall"))
        ordinary = self.runtime.access_from_file(ordinary_path)
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.ui.read(ordinary, view="status")
        source_id = self.source().message_id
        operation = {"kind": "source_correct", "message_id": source_id, "text": "合成：修正后的原话"}
        preview = self.ui.preview(self.access, operation)
        with self.assertRaisesRegex(MemoryFailure, "management_preview_mismatch"):
            self.ui.confirm(self.access, preview["proposal_id"], {**operation, "text": "另一个值"})
        other_path = self.config.parent / "mcp-access-other.json"
        pair_client(self.config, client_id="other", entry="other", access_path=other_path, scopes=("recall", "maintain", "accept"))
        with self.assertRaisesRegex(MemoryFailure, "management_preview_mismatch"):
            self.ui.confirm(self.runtime.access_from_file(other_path), preview["proposal_id"], operation)
        with self.runtime._connection() as db:
            db.execute("UPDATE management_previews_v1 SET expires=0 WHERE id=?", (preview["proposal_id"],))
        with self.assertRaisesRegex(MemoryFailure, "management_preview_expired"):
            self.ui.confirm(self.access, preview["proposal_id"], operation)
        self.apply(operation)
        self.assertEqual(self.runtime.state(self.access, source_id).status, "corrected")
        self.assertIsNone(self.runtime.read(self.access, source_id))

    def test_source_unicode_pagination_timeline_and_real_cli(self) -> None:
        text = "合成🙂" * 900
        source_id = self.source(text).message_id
        page = self.ui.read(self.access, view="source", message_id=source_id)
        tail = self.ui.read(self.access, view="source", message_id=source_id, cursor=page["next_cursor"])
        self.assertEqual(page["text"] + tail["text"], text)
        self.assertEqual(self.ui.read(self.access, view="timeline", session_id="other")["items"], [])
        self.assertEqual(self.ui.read(self.access, view="timeline", query="🙂")["items"][0]["message_id"], source_id)
        path = self.config.parent / "mcp-access-cli-ui.json"
        pair_client(self.config, client_id="cli-ui", entry="ui", access_path=path, scopes=("recall", "maintain", "accept"))
        result = subprocess.run([sys.executable, "-m", "evernook.cli", "manage", "--config", str(self.config),
                                 "--access", str(path)], input=b'{"action":"read","view":"status"}',
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(json.loads(result.stdout)["result"]["daily_admitted"])
        filtered = subprocess.run([sys.executable, "-m", "evernook.cli", "manage", "--config", str(self.config),
                                   "--access", str(path)],
                                  input=b'{"action":"read","view":"timeline","entry":"owner","session_id":"synthetic-session"}',
                                  capture_output=True, timeout=30)
        self.assertEqual(filtered.returncode, 0, filtered.stdout)
        self.assertEqual(json.loads(filtered.stdout)["result"]["items"][0]["message_id"], source_id)
        with self.assertRaisesRegex(MemoryFailure, "invalid_management_operation"):
            self.ui.preview(self.access, {"kind": "purge", "message_id": source_id})

    def test_timeline_filters_entry_session_and_occurrence_time(self) -> None:
        def submit(session: str, event_id: str, occurred: str | None) -> str:
            return self.runtime.submit(self.access, Event(Source("owner", session, "main", event_id),
                "user", 1, occurred, "complete", "合成筛选记录")).message_id

        first = submit("session-a", "one", "2022-01-01T00:00:00Z")
        submit("session-b", "two", "2023-01-01T00:00:00Z")
        unknown = submit("session-a", "three", None)
        def ids(**kwargs: object) -> set[str]:
            return {item["message_id"] for item in self.ui.read(self.access, view="timeline", **kwargs)["items"]}
        self.assertEqual(ids(entry="owner", session_id="session-a"), {first, unknown})
        self.assertEqual(ids(entry="other"), set())
        self.assertEqual(ids(since="2022-01-01T08:00:00+08:00", until="2023-01-01T00:00:00Z"), {first})
        page = self.ui.read(self.access, view="timeline", entry="owner", session_id="session-a", limit=1)
        self.assertIsNotNone(page["next_offset"])
        tail = self.ui.read(self.access, view="timeline", entry="owner", session_id="session-a", limit=1, offset=page["next_offset"])
        self.assertEqual({page["items"][0]["message_id"], tail["items"][0]["message_id"]}, {first, unknown})
        with self.assertRaisesRegex(MemoryFailure, "invalid_time_range"):
            ids(since="2023-01-01T00:00:00Z", until="2023-01-01T00:00:00Z")
        self.apply({"kind": "source_suppress", "message_id": first})
        self.assertEqual(ids(since="2022-01-01T00:00:00Z", until="2023-01-01T00:00:00Z"), set())

    def test_timeline_and_cross_layer_graph_use_half_open_local_midnight_bounds(self) -> None:
        cases = (
            ('before', '2023-12-31T15:59:59Z'),
            ('start', '2023-12-31T16:00:00Z'),
            ('inside', '2024-01-01T15:59:59Z'),
            ('end', '2024-01-01T16:00:00Z'),
            ('unknown', None),
        )
        ids: dict[str, str] = {}
        for index, (name, occurred) in enumerate(cases, 1):
            text = '合成边界：' + name
            receipt = self.runtime.submit(self.access, Event(Source('owner', 'boundary-session', 'main', name),
                'user', index, occurred, 'complete', text))
            ids[name] = receipt.message_id
            span = EvidenceSpan(receipt.message_id, 1, 'original', 1, 0, len(text), text)
            self.profiles.decide(self.access, ProfileDecision('propose-' + name, 'tag-' + name, 0, 'propose',
                ProfileDraft('interest', text, 0.8, (span,))))
            self.apply({'kind': 'profile_confirm', 'tag_id': 'tag-' + name})
        filters = {'entry': 'owner', 'session_id': 'boundary-session',
                   'since': '2024-01-01T00:00:00+08:00', 'until': '2024-01-02T00:00:00+08:00'}
        timeline = self.ui.read(self.access, view='timeline', **filters)
        self.assertEqual({item['message_id'] for item in timeline['items']}, {ids['start'], ids['inside']})
        graph = self.ui.read(self.access, view='network_graph', **filters)
        self.assertEqual({node['label'] for node in graph['nodes']}, {'合成边界：start', '合成边界：inside'})
        self.assertEqual(self.ui.read(self.access, view='timeline', **{**filters, 'session_id': 'other'})['items'], [])
        self.assertEqual(self.ui.read(self.access, view='network_graph', **{**filters, 'entry': 'other'})['nodes'], [])
        self.assertIn(ids['unknown'], {item['message_id'] for item in self.ui.read(self.access, view='timeline')['items']})

    def test_graph_links_shared_source_and_withholds_all_dependents_when_suppressed(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow
        from evernook.facts import FactDecision, FactRegistry
        from evernook.observations import ObservationDraft, ObservationRegistry

        span = self.source("合成：我喜欢观鸟。")
        self.profiles.decide(self.access, ProfileDecision("graph-profile", "graph-tag", 0, "propose",
            ProfileDraft("interest", "喜欢观鸟", 0.8, (span,))))
        self.apply({"kind": "profile_confirm", "tag_id": "graph-tag"})
        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (span.message_id,), processor_version="graph-test-v1")
        assert lease.token is not None
        workflow.complete(self.access, lease.status.run_id, lease.token,
                          (CandidateDraft(span.expected_text, "user_statement", (span,)),))
        candidate = workflow.candidates(self.access, lease.status.run_id)[0]
        FactRegistry(self.runtime).decide(self.access, FactDecision("graph-fact", "person", "interest", candidate.candidate_id,
            span.message_id, 0, "assert", "2020-01-01T00:00:00Z"))
        registry = ObservationRegistry(self.runtime)
        observation = registry.propose(self.access, "graph-observation", ObservationDraft("person:interest",
            "合成：观察到观鸟偏好。", (span,), (), "experience", None, None, "graph-test-v1", "graph-test-v1"))
        registry.decide(self.access, "graph-publish", observation.observation_id, observation.revision,
                        action="publish", confirmation_id=span.message_id)
        result = self.ui.read(self.access, view="network_graph", entry="owner", session_id="synthetic-session")
        self.assertEqual({node["kind"] for node in result["nodes"]}, {"profile", "fact", "observation"})
        self.assertTrue(all(span.message_id in {edge["source_id"] for edge in node["edges"]} for node in result["nodes"]))
        self.assertNotIn("expected_text", json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.ui.read(self.access, view="network_graph", entry="other")["nodes"], [])
        ordinary_path = self.config.parent / "mcp-access-graph-ordinary.json"
        pair_client(self.config, client_id="graph-ordinary", entry="graph-ordinary", access_path=ordinary_path, scopes=("capture", "recall"))
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.ui.read(self.runtime.access_from_file(ordinary_path), view="network_graph")
        self.apply({"kind": "source_suppress", "message_id": span.message_id})
        self.assertEqual(self.ui.read(self.access, view="network_graph")["nodes"], [])

    def test_graph_pagination_retains_sparse_pages(self) -> None:
        for tag in ("alpha", "beta"):
            self.draft(tag)
            self.apply({"kind": "profile_confirm", "tag_id": tag})
        first = self.ui.read(self.access, view="network_graph", limit=1, query="missing")
        self.assertEqual(first["nodes"], [])
        self.assertEqual(first["next_offset"], 1)
        second = self.ui.read(self.access, view="network_graph", limit=1, offset=first["next_offset"])
        self.assertEqual([node["id"] for node in second["nodes"]], ["beta"])
        self.assertIsNone(second["next_offset"])

    def test_source_preview_includes_observation_evidence_and_detects_new_revision(self) -> None:
        from dataclasses import replace
        from evernook.observations import ObservationDraft, ObservationRegistry

        support, counter, confirmation = self.source("合成支持"), self.source("合成反证"), self.source("合成确认")
        registry = ObservationRegistry(self.runtime)
        draft = ObservationDraft("test:preview", "合成观察", (support,), (counter,), "experience", None, None,
                                 "synthetic-v1", "synthetic-v1")
        candidate = registry.propose(self.access, "preview-obs", draft)
        published = registry.decide(self.access, "preview-publish", candidate.observation_id, candidate.revision,
                                    action="publish", confirmation_id=confirmation.message_id)
        for span in (support, counter, confirmation):
            preview = self.ui.preview(self.access, {"kind": "source_suppress", "message_id": span.message_id})
            self.assertIn(("observation", published.observation_id, published.revision),
                          {(item["kind"], item["id"], item.get("revision")) for item in preview["affected"]})
        operation = {"kind": "source_suppress", "message_id": support.message_id}
        preview = self.ui.preview(self.access, operation)
        registry.propose(self.access, "preview-change", replace(draft, conclusion="合成新观察"),
                         expected_revision=published.revision, operation="change", observation_id=published.observation_id)
        with self.assertRaisesRegex(MemoryFailure, "management_preview_stale"):
            self.ui.confirm(self.access, preview["proposal_id"], operation)

    def test_canceled_preview_stays_canceled_after_restart_and_cannot_confirm(self) -> None:
        source_id = self.source().message_id
        operation = {'kind': 'source_suppress', 'message_id': source_id}
        preview = self.ui.preview(self.access, operation)
        canceled = self.ui.cancel(self.access, preview['proposal_id'])
        restarted = MemoryManagement(MemoryRuntime(self.config))
        self.assertEqual(restarted.cancel(self.access, preview['proposal_id']), canceled)
        with self.assertRaisesRegex(MemoryFailure, 'management_preview_canceled'):
            restarted.confirm(self.access, preview['proposal_id'], operation)
        self.assertIsNotNone(self.runtime.read(self.access, source_id))
        preview = self.ui.preview(self.access, operation)
        committed = self.ui.confirm(self.access, preview['proposal_id'], operation)
        self.assertEqual(self.ui.cancel(self.access, preview['proposal_id']), committed)


if __name__ == "__main__":
    unittest.main()
