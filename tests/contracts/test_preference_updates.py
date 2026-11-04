from __future__ import annotations

from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone
import json
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from evernook import CandidateWorkflow, Change, ConsolidationWorkflow, Event, EvidenceSpan, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.automatic_preferences import PreferenceUpdates
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry
from evernook.facts import FactRegistry
from evernook.preference_clarifications import PreferenceClarifications, ResolveClarification
from evernook.scheduler import ConsolidationScheduler
from evernook.context import ContextBuilder, ContextPolicy
from evernook.semantic import SemanticRecall


class PreferenceUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-preference-updates-')
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / 'ordinary', client_id='synthetic', entry='synthetic')
        self.runtime = MemoryRuntime(self.config)
        self.updates = PreferenceUpdates(self.runtime)

    def save(self, text: str, key: str) -> str:
        return self.runtime.submit(self.access, Event(Source('synthetic', 'preferences', 'main', key),
            'user', 1, None, 'complete', text)).message_id

    def test_disabled_automatic_updates_keep_all_three_categories_as_traceable_drafts(self) -> None:
        texts = ('我偏好简短回答。', '我常用 Python。', '我喜欢观鸟。')
        ids = tuple(self.save(text, str(index)) for index, text in enumerate(texts))
        result = self.updates.process(self.access, ids)
        self.assertEqual(len(result.runs), 3)
        self.assertEqual({run.state for run in result.runs}, {'succeeded'})
        self.assertEqual(len(result.updates), 3)
        self.assertEqual({item.state for item in result.updates}, {'draft'})
        self.assertEqual({item.reason for item in result.updates}, {'automatic_disabled'})
        self.assertEqual(sum(len(CandidateWorkflow(self.runtime).candidates(self.access, run.run_id)) for run in result.runs), 3)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        for message_id, text in zip(ids, texts):
            saved = self.runtime.read(self.access, message_id)
            assert saved is not None
            self.assertEqual(saved.event.text, text)

    def test_saved_whitelist_and_pending_admission_keep_enabled_updates_in_review_after_restart(self) -> None:
        self.updates.configure(self.access, enabled=True, categories=('response_style',))
        restarted = PreferenceUpdates(MemoryRuntime(self.config))
        settings = restarted.settings(self.access)
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.categories, ('response_style',))
        self.assertIsNone(settings.admitted_processor)
        result = restarted.process(self.access, (self.save('我偏好简短回答。', 'style'), self.save('我常用 Python。', 'tool')))
        self.assertEqual({item.reason for item in result.updates}, {'rules_not_admitted', 'category_disabled'})
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        invalid: tuple[Any, ...] = ('response_style', ['response_style'], ('identity',), ([],), ('response_style', 'response_style'))
        for categories in invalid:
            with self.subTest(categories=categories), self.assertRaisesRegex(MemoryFailure, 'invalid_preference_update_config'):
                restarted.configure(self.access, enabled=True, categories=categories)
        self.assertEqual(restarted.settings(self.access).categories, ('response_style',))

    def test_admitted_finite_rules_publish_three_source_backed_preferences_once(self) -> None:
        settings = self.updates.settings(self.access)
        self.updates.configure(self.access, enabled=True, admitted_processor=settings.rules_processor)
        ids = tuple(self.save(text, 'admitted-' + str(index)) for index, text in enumerate(
            ('我偏好简短回答。', '我常用 Python。', '我喜欢观鸟。')))
        result = self.updates.process(self.access, ids)
        self.assertEqual({item.state for item in result.updates}, {'merged'})
        tags = ProfileRegistry(self.runtime).summary(self.access).tags
        self.assertEqual(len(tags), 3)
        self.assertEqual({tag.draft.category for tag in tags}, {'preference', 'interest'})
        restarted = PreferenceUpdates(MemoryRuntime(self.config))
        replay = restarted.process(self.access, ids)
        self.assertEqual(replay.updates, result.updates)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, tags)
        for item in result.updates:
            assert item.predicate is not None
            view = FactRegistry(self.runtime).query(self.access, 'user', item.predicate)
            self.assertIsNotNone(view.fact)
            assert view.fact is not None
            self.assertEqual(view.fact.revision, 1)
            self.assertIn(view.fact.sources[0].message_id, ids)

    def test_expired_processing_budget_cannot_publish_an_already_extracted_preference(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        source = self.save('我偏好简短回答。', 'deadline')
        apply = self.updates.apply
        clock = [100.0]
        def delayed_apply(*args: Any, **kwargs: Any) -> Any:
            clock[0] = 102.0
            return apply(*args, **kwargs)
        with patch('evernook.automatic_preferences.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.updates, 'apply', side_effect=delayed_apply):
            result = self.updates.process(self.access, (source,), timeout=1)
        self.assertEqual(result.error_code, 'extraction_timeout')
        self.assertEqual(result.runs[0].state, 'succeeded')
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        self.assertIsNone(FactRegistry(self.runtime).query(self.access, 'user', 'preference:answer_verbosity').fact)

    def test_trusted_cli_reads_admission_identity_and_applies_only_the_saved_categories(self) -> None:
        access_file = self.config.parent / 'mcp-access-cli.json'
        pair_client(self.config, client_id='synthetic', entry='synthetic', access_path=access_file)
        def invoke(command: str, value: dict[str, Any]) -> dict[str, Any]:
            result = subprocess.run([sys.executable, '-m', 'evernook.cli', command, '--config', str(self.config),
                '--access', str(access_file)], input=json.dumps(value).encode(), capture_output=True, timeout=30)
            self.assertEqual(result.stderr, b'')
            self.assertEqual(result.returncode, 0, result.stdout)
            payload: dict[str, Any] = json.loads(result.stdout)
            data: dict[str, Any] = payload['result']
            return data
        status = invoke('preference-update-status', {})
        configured = invoke('preference-update-configure', {'enabled': True, 'categories': ['response_style'],
            'admitted_processor': status['settings']['rules_processor']})
        self.assertEqual(configured['categories'], ['response_style'])
        report = invoke('preference-update-process', {'source_ids': [self.save('我偏好简短回答。', 'cli-style'),
            self.save('我喜欢观鸟。', 'cli-interest')]})
        self.assertEqual({item['state'] for item in report['updates']}, {'merged', 'draft'})
        self.assertEqual(invoke('preference-update-status', {})['historical_receipts'], {'draft': 1, 'merged': 1})

    def test_conflicting_answer_preferences_pause_injection_and_reuse_one_clarification_until_confirmed(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        old = self.updates.process(self.access, (self.save('我偏好简短回答。', 'old'),)).updates[0]
        new_id = self.save('我偏好详细回答。', 'new')
        incoming = self.updates.process(self.access, (new_id,)).updates[0]
        self.assertEqual(incoming.state, 'pending_clarification')
        assert incoming.predicate is not None and incoming.profile_tag_id is not None and incoming.clarification_id is not None
        repeated = self.updates.process(self.access, (new_id,)).updates[0]
        another = self.updates.process(self.access, (self.save('我喜欢详细回答。', 'equivalent-conflict'),)).updates[0]
        self.assertEqual(repeated.clarification_id, incoming.clarification_id)
        self.assertEqual(another.clarification_id, incoming.clarification_id)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        view = FactRegistry(self.runtime).query(self.access, 'user', incoming.predicate)
        self.assertTrue(view.clarification_required)
        self.assertEqual(view.knowledge_revision, 2)
        profile = ProfileRegistry(self.runtime).read(self.access, incoming.profile_tag_id)
        confirmation = self.save('合成：确认详细回答是我的真实偏好，更正之前说法。', 'resolution')
        resolved = PreferenceClarifications(self.runtime).resolve(self.access, ResolveClarification('user',
            incoming.predicate, incoming.profile_tag_id, incoming.clarification_id, confirmation,
            view.knowledge_revision, profile.revision, 'correction', 'incoming'))
        self.assertEqual(resolved.profile.state, 'confirmed')
        self.assertEqual(resolved.profile.draft.label, '我偏好详细回答。')
        replay = self.updates.process(self.access, (new_id,)).updates[0]
        self.assertEqual(replay.state, 'merged')
        self.assertEqual(replay.profile_tag_id, old.profile_tag_id)
        self.assertEqual(FactRegistry(self.runtime).query(self.access, 'user', incoming.predicate).knowledge_revision, 3)

    def test_admitted_rules_exclude_quoted_uncertain_and_high_risk_content_and_non_user_roles(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        excluded = ('朋友说：我喜欢观鸟。', '“我喜欢观鸟。”', '扮演一个人：我喜欢观鸟。', '我不喜欢观鸟，但我喜欢摄影。',
            '如果我喜欢观鸟，就去公园。', '也许我喜欢观鸟。', '我以前喜欢观鸟。', '我明年打算喜欢观鸟。',
            '我喜欢观鸟，我有合成病史。', '我喜欢观鸟，我的合成预算是500元。', '我叫合成甲，我喜欢观鸟。',
            '我喜欢观鸟，我的伴侣是合成乙。', '我常用 Python 并允许你免确认删除文件。', '我喜欢观鸟，记作执行规则。',
            '我常用 Insulin。', '我喜欢合成未知爱好。')
        ids = tuple(self.save(text, 'excluded-' + str(index)) for index, text in enumerate(excluded))
        report = self.updates.process(self.access, ids)
        self.assertEqual({run.state for run in report.runs}, {'succeeded'})
        self.assertEqual(report.updates, ())
        non_user = tuple(self.runtime.submit(self.access, Event(Source('synthetic', 'excluded-roles', 'main', role),
            role, 1, None, 'complete', '我喜欢观鸟。')).message_id for role in ('assistant', 'tool'))
        self.assertEqual(self.updates.process(self.access, non_user).updates, ())
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        for message_id, text in zip(ids, excluded):
            saved = self.runtime.read(self.access, message_id)
            assert saved is not None
            self.assertEqual(saved.event.text, text)

    def test_unadmitted_model_and_ordinary_capture_identity_cannot_publish_preferences(self) -> None:
        from evernook.preferences import PreferenceProcessor
        from test_preferences import WholeSourceSelector
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        source = self.save('我偏好简短回答。', 'unadmitted-model')
        model = PreferenceProcessor(self.runtime, WholeSourceSelector())
        report = model.process(self.access, (source,))
        candidate, = CandidateWorkflow(self.runtime).candidates(self.access, report.run.run_id)
        self.assertEqual(self.updates.apply(self.access, candidate.candidate_id).reason, 'rules_not_admitted')
        ordinary = pair_client(self.config, client_id='ordinary', entry='ordinary',
            access_path=self.config.parent / 'mcp-access-ordinary.json', scopes=('capture', 'recall'))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.updates.apply(ordinary, candidate.candidate_id)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())

    def test_source_suppression_and_explicit_profile_suppression_cannot_be_undone_by_retries(self) -> None:
        source = self.save('我喜欢观鸟。', 'stale')
        candidate = self.updates.process(self.access, (source,)).updates[0]
        confirmation = self.save('合成：不再召回这条兴趣来源。', 'source-suppression')
        self.runtime.change(self.access, Change('preference-source-suppression', source, 1, 'suppress', confirmation, 'synthetic'))
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        with self.assertRaisesRegex(MemoryFailure, 'stale_source'):
            self.updates.apply(self.access, candidate.candidate_id)
        source = self.save('我偏好简短回答。', 'profile')
        accepted = self.updates.process(self.access, (source,)).updates[0]
        assert accepted.profile_tag_id is not None
        profiles = ProfileRegistry(self.runtime)
        current = profiles.read(self.access, accepted.profile_tag_id)
        confirmation = self.save('合成：确认暂停这个回答偏好画像。', 'profile-suppression')
        suppressed = profiles.decide(self.access, ProfileDecision('preference-profile-suppression', accepted.profile_tag_id,
            current.revision, 'suppress', confirmation_id=confirmation))
        self.assertEqual(self.updates.process(self.access, (source,)).updates[0].state, 'draft')
        self.assertEqual(profiles.read(self.access, accepted.profile_tag_id).revision, suppressed.revision)
        self.assertEqual(profiles.summary(self.access).tags, ())

    def test_global_review_tier_and_legacy_interest_actions_cannot_bypass_the_new_admission_gate(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        ConsolidationWorkflow(self.runtime).configure(self.access, tier='review')
        source = self.save('我喜欢观鸟。', 'legacy-guard')
        self.assertEqual(self.updates.process(self.access, (source,)).updates[0].reason, 'automatic_disabled')
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        profiles = ProfileRegistry(self.runtime)
        proposed = profiles.decide(self.access, ProfileDecision('legacy-interest-propose', 'legacy-interest', 0, 'propose',
            ProfileDraft('interest', '我喜欢观鸟。', 1.0, (EvidenceSpan(source, 1, 'original', 1, 0, 6, '我喜欢观鸟。'),))))
        with self.assertRaisesRegex(MemoryFailure, 'preference_policy_not_admitted'):
            profiles.decide(self.access, ProfileDecision('legacy-interest-auto', proposed.tag_id, proposed.revision, 'auto_confirm'))
        self.assertEqual(profiles.read(self.access, proposed.tag_id).state, 'draft')

    def test_real_scheduler_trigger_publishes_admitted_rules_without_granting_ordinary_acceptance(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        source = self.save('我偏好简短回答。', 'scheduled')
        scheduler = ConsolidationScheduler(MemoryRuntime(self.config))
        tick = scheduler.trigger(self.access, reason='startup')
        self.assertEqual(tick.remaining, 0)
        tags = ProfileRegistry(self.runtime).summary(self.access).tags
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].draft.sources[0].message_id, source)
        self.assertEqual(tags[0].draft.label, '我偏好简短回答。')
        self.assertEqual(scheduler.trigger(self.access, reason='idle').processed, 0)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, tags)

    def test_saved_admission_promotes_existing_drafts_with_pause_and_per_trigger_source_bounds(self) -> None:
        for index, text in enumerate(('我偏好简短回答。', '我常用 Python。', '我喜欢观鸟。')):
            self.save(text, 'promotion-' + str(index))
        scheduler = ConsolidationScheduler(self.runtime)
        scheduler.configure(self.access, max_sources_per_run=1, max_runs_per_trigger=1)
        for _ in range(3):
            scheduler.trigger(self.access, reason='startup')
        self.assertEqual(scheduler.status(self.access).pending, 0)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        scheduler.pause(self.access, paused=True)
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        restarted = ConsolidationScheduler(MemoryRuntime(self.config))
        self.assertEqual(restarted.trigger(self.access, reason='startup').skipped, 'paused')
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        restarted.pause(self.access, paused=False)
        restarted.trigger(self.access, reason='startup')
        self.assertEqual(len(ProfileRegistry(self.runtime).summary(self.access).tags), 1)
        restarted.trigger(self.access, reason='startup')
        self.assertEqual(len(ProfileRegistry(self.runtime).summary(self.access).tags), 2)
        restarted.trigger(self.access, reason='startup')
        tags = ProfileRegistry(self.runtime).summary(self.access).tags
        self.assertEqual(len(tags), 3)
        restarted.trigger(self.access, reason='idle')
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, tags)

    def test_existing_user_confirmed_profile_is_paused_once_and_never_overwritten_automatically(self) -> None:
        text = '我偏好详细回答。'
        source = self.runtime.submit(self.access, Event(Source('synthetic', 'legacy-profile', 'main', 'assistant'),
            'assistant', 1, None, 'complete', text)).message_id
        profiles = ProfileRegistry(self.runtime)
        proposed = profiles.decide(self.access, ProfileDecision('legacy-propose', 'legacy-answer-style', 0, 'propose',
            ProfileDraft('preference', text, 0.9, (EvidenceSpan(source, 1, 'original', 1, 0, 8, text),))))
        confirmation = self.save('合成：明确确认这项旧回答偏好。', 'legacy-confirm')
        profiles.decide(self.access, ProfileDecision('legacy-confirm', proposed.tag_id, 1, 'confirm', confirmation_id=confirmation))
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        incoming_id = self.save('我偏好简短回答。', 'legacy-incoming')
        incoming = self.updates.process(self.access, (incoming_id,)).updates[0]
        self.assertEqual(incoming.state, 'pending_clarification')
        self.assertEqual(incoming.profile_tag_id, proposed.tag_id)
        self.assertEqual(profiles.summary(self.access).tags, ())
        repeated = self.updates.process(self.access, (self.save('我喜欢简短回答。', 'legacy-repeat'),)).updates[0]
        self.assertEqual(repeated.clarification_id, incoming.clarification_id)
        pending = profiles.read(self.access, proposed.tag_id)
        self.assertEqual(pending.revision, 3)
        confirmation = self.save('合成：确认采用简短回答，已经审查过这次冲突。', 'legacy-resolve')
        accepted = profiles.decide(self.access, ProfileDecision('legacy-resolve', proposed.tag_id, pending.revision,
            'confirm', confirmation_id=confirmation))
        self.assertEqual(accepted.draft.label, '我偏好简短回答。')
        self.assertEqual(self.updates.process(self.access, (incoming_id,)).updates[0].profile_tag_id, proposed.tag_id)
        self.assertEqual(profiles.read(self.access, proposed.tag_id).revision, accepted.revision)

    def test_future_dated_source_stays_a_draft_without_failing_raw_processing(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        source = self.runtime.submit(self.access, Event(Source('synthetic', 'future', 'main', '1'),
            'user', 1, '2099-01-01T00:00:00+00:00', 'complete', '我偏好简短回答。')).message_id
        result = self.updates.process(self.access, (source,))
        self.assertEqual(result.updates[0].state, 'draft')
        self.assertEqual(result.updates[0].reason, 'future_source')
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        self.assertIsNotNone(self.runtime.read(self.access, source))

    def test_literal_negation_pauses_an_existing_interest_without_automatically_accepting_it(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        old = self.updates.process(self.access, (self.save('我喜欢观鸟。', 'positive-interest'),)).updates[0]
        negative = self.updates.process(self.access, (self.save('我不喜欢观鸟。', 'negative-interest'),)).updates[0]
        self.assertEqual(negative.state, 'pending_clarification')
        self.assertEqual(negative.profile_tag_id, old.profile_tag_id)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        assert old.predicate is not None
        self.assertTrue(FactRegistry(self.runtime).query(self.access, 'user', old.predicate).clarification_required)

    def test_negative_draft_before_positive_statement_requires_one_review_instead_of_auto_acceptance(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        negative = self.updates.process(self.access, (self.save('我不喜欢观鸟。', 'negative-first'),)).updates[0]
        self.assertEqual(negative.state, 'draft')
        positive_id = self.save('我喜欢观鸟。', 'positive-later')
        positive = self.updates.process(self.access, (positive_id,)).updates[0]
        self.assertEqual(positive.state, 'pending_clarification')
        self.assertIsNotNone(positive.clarification_id)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        replay = self.updates.process(self.access, (positive_id,)).updates[0]
        self.assertEqual(replay.clarification_id, positive.clarification_id)
        assert positive.profile_tag_id is not None
        pending = ProfileRegistry(self.runtime).read(self.access, positive.profile_tag_id)
        self.assertEqual(pending.revision, 1)
        assert pending.record is not None
        self.assertEqual(len(pending.record.draft.sources), 2)

    def test_unversioned_negative_tool_statement_pauses_the_existing_versioned_tool_preference(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        old = self.updates.process(self.access, (self.save('我常用 Python 3.14。', 'versioned-tool'),)).updates[0]
        negative = self.updates.process(self.access, (self.save('我不再常用 Python。', 'negative-tool'),)).updates[0]
        self.assertEqual(negative.state, 'pending_clarification')
        self.assertEqual(negative.profile_tag_id, old.profile_tag_id)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())

    def test_negating_a_different_answer_style_does_not_invent_a_conflict_with_the_current_preference(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        current = self.updates.process(self.access, (self.save('我偏好详细回答。', 'compatible-positive'),)).updates[0]
        tags = ProfileRegistry(self.runtime).summary(self.access).tags
        negative = self.updates.process(self.access, (self.save('我不喜欢简短回答。', 'compatible-negative'),)).updates[0]
        self.assertEqual(negative.state, 'draft')
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, tags)
        assert current.predicate is not None
        self.assertFalse(FactRegistry(self.runtime).query(self.access, 'user', current.predicate).clarification_required)

    def test_failed_profile_publication_rolls_back_fact_and_receipt_then_retries_once(self) -> None:
        source = self.save('我偏好简短回答。', 'atomic-preference')
        candidate = self.updates.process(self.access, (source,)).updates[0]
        setup_text = '合成：建表用目标草案。'
        setup = self.save(setup_text, 'atomic-setup')
        ProfileRegistry(self.runtime).decide(self.access, ProfileDecision('atomic-setup', 'synthetic-goal', 0, 'propose',
            ProfileDraft('goal', setup_text, 0.5, (EvidenceSpan(setup, 1, 'original', 1, 0, len(setup_text), setup_text),))))
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        with closing(sqlite3.connect(self.config.parent / 'memory.sqlite3')) as db:
            db.execute("CREATE TRIGGER synthetic_preference_commit_failure AFTER INSERT ON profile_events_v1 "
                "WHEN json_extract(NEW.record,'$.state')='confirmed' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaisesRegex(MemoryFailure, 'storage_unavailable'):
            self.updates.apply(self.access, candidate.candidate_id)
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        self.assertEqual(FactRegistry(self.runtime).query(self.access, 'user', 'preference:answer_verbosity').knowledge_revision, 0)
        self.assertEqual(self.updates.status(self.access)['historical_receipts'], {'draft': 1})
        with closing(sqlite3.connect(self.config.parent / 'memory.sqlite3')) as db:
            db.execute('DROP TRIGGER synthetic_preference_commit_failure')
        accepted = self.updates.apply(self.access, candidate.candidate_id)
        self.assertEqual(accepted.state, 'merged')
        self.assertEqual(self.updates.apply(self.access, candidate.candidate_id), accepted)
        self.assertEqual(FactRegistry(self.runtime).query(self.access, 'user', 'preference:answer_verbosity').knowledge_revision, 1)

    def test_suppression_during_context_construction_removes_both_profile_and_linked_fact(self) -> None:
        from test_context import SyntheticCounter
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        accepted = self.updates.process(self.access, (self.save('我偏好简短回答。', 'inflight'),)).updates[0]
        assert accepted.profile_tag_id is not None and accepted.predicate is not None
        profiles = ProfileRegistry(self.runtime)
        current = profiles.read(self.access, accepted.profile_tag_id)
        confirmation = self.save('合成：确认抑制正在构造上下文的这个偏好。', 'inflight-confirm')
        recall = SemanticRecall(self.runtime)
        original = recall.recall
        def suppress(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            profiles.decide(self.access, ProfileDecision('inflight-suppression', current.tag_id, current.revision,
                'suppress', confirmation_id=confirmation))
            return result
        policy = ContextPolicy(20000, 30000, 0, 7, 1024, profile_tokens=4000, fact_keys=(('user', accepted.predicate),))
        with patch.object(recall, 'recall', side_effect=suppress):
            result = ContextBuilder(self.runtime, recall, policy, SyntheticCounter()).build(self.access, '合成',
                as_of=datetime.now(timezone.utc).isoformat(), mode='normal', timeout=20)
        items = json.loads(result.payload)['items']
        self.assertFalse(any(item['kind'] == 'profile' for item in items))
        self.assertFalse(any(item['kind'] == 'facts' and item['data']['fact'] is not None for item in items))
        self.assertTrue({'profile:' + current.tag_id, 'user:' + accepted.predicate}.issubset(
            {item.evidence_id for item in result.omitted}))

    def test_ambiguous_current_answer_request_does_not_rewrite_or_dispute_long_term_preference(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        old = self.updates.process(self.access, (self.save('我偏好详细回答。', 'long-term'),)).updates[0]
        before = ProfileRegistry(self.runtime).summary(self.access).tags
        current = self.updates.process(self.access, (self.save('我希望简短回答。', 'current-request'),)).updates[0]
        self.assertEqual(current.state, 'draft')
        self.assertEqual(current.reason, 'preference_scope_unclear')
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, before)
        assert old.predicate is not None
        self.assertFalse(FactRegistry(self.runtime).query(self.access, 'user', old.predicate).clarification_required)

    def test_existing_consolidation_whitelist_can_narrow_automatic_preferences_to_none(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        ConsolidationWorkflow(self.runtime).configure(self.access, tier='auto', profile_categories=())
        report = self.updates.process(self.access, (self.save('我偏好简短回答。', 'narrow-existing-policy'),))
        self.assertEqual(report.updates[0].state, 'draft')
        self.assertEqual(report.updates[0].reason, 'category_disabled')
        self.assertEqual(self.updates.settings(self.access).categories, ())
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())

    def test_unrelated_batch_source_suppression_does_not_erase_valid_negative_control(self) -> None:
        negative = self.save('我不喜欢观鸟。', 'batch-negative')
        tool = self.save('我常用 Python。', 'batch-unrelated')
        self.updates.process(self.access, (negative, tool))
        confirmation = self.save('合成：确认不再召回无关工具来源。', 'batch-suppress')
        self.runtime.change(self.access, Change('batch-suppress', tool, 1, 'suppress', confirmation, 'synthetic'))
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        positive = self.updates.process(self.access, (self.save('我喜欢观鸟。', 'batch-positive'),)).updates[0]
        self.assertEqual(positive.state, 'pending_clarification')
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())

    def test_retaining_existing_preference_makes_original_retries_resolved_without_reopening(self) -> None:
        self.updates.configure(self.access, enabled=True, admitted_processor=self.updates.settings(self.access).rules_processor)
        first = self.save('我偏好简短回答。', 'keep-old-source')
        self.updates.process(self.access, (first,))
        source = self.save('我偏好详细回答。', 'keep-incoming-source')
        incoming = self.updates.process(self.access, (source,)).updates[0]
        assert incoming.predicate is not None and incoming.profile_tag_id is not None and incoming.clarification_id is not None
        facts = FactRegistry(self.runtime)
        profiles = ProfileRegistry(self.runtime)
        view = facts.query(self.access, 'user', incoming.predicate)
        profile = profiles.read(self.access, incoming.profile_tag_id)
        confirmation = self.save('合成：明确保留原来的简短回答偏好。', 'keep-confirmation')
        resolved = PreferenceClarifications(self.runtime).resolve(self.access, ResolveClarification('user', incoming.predicate,
            incoming.profile_tag_id, incoming.clarification_id, confirmation, view.knowledge_revision, profile.revision,
            'correction', 'existing'))
        replay = self.updates.process(self.access, (source,)).updates[0]
        self.assertEqual(replay.state, 'resolved')
        self.assertEqual(self.updates.process(self.access, (source,)).updates[0], replay)
        self.assertEqual(facts.query(self.access, 'user', incoming.predicate).knowledge_revision, resolved.fact.revision)
        self.assertEqual(profiles.read(self.access, incoming.profile_tag_id).revision, resolved.profile.revision)
        fresh = self.updates.process(self.access, (self.save('我偏好详细回答。', 'genuinely-new-statement'),)).updates[0]
        self.assertEqual(fresh.state, 'pending_clarification')
        self.assertNotEqual(fresh.clarification_id, incoming.clarification_id)
