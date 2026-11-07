from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from evernook import Event, EvidenceSpan, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.management import MemoryManagement
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry


class ProfileRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-rejection-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / 'data', client_id='owner', entry='owner', allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.profiles = ProfileRegistry(self.runtime)
        self.ui = MemoryManagement(self.runtime)
        self.sequence = 0

    def source(self, text: str = '合成：长期兴趣是观鸟') -> EvidenceSpan:
        self.sequence += 1
        receipt = self.runtime.submit(self.access, Event(Source('owner', 'synthetic', 'main', str(self.sequence)),
                                                        'user', self.sequence, None, 'complete', text))
        return EvidenceSpan(receipt.message_id, 1, 'original', 1, 0, len(text), text)

    def propose(self, tag: str, draft: ProfileDraft) -> None:
        self.profiles.decide(self.access, ProfileDecision('propose-' + tag, tag, 0, 'propose', draft))

    def click(self, kind: str, tag: str) -> None:
        operation = {'kind': kind, 'tag_id': tag}
        preview = self.ui.preview(self.access, operation)
        self.ui.confirm(self.access, preview['proposal_id'], operation)

    def test_new_processor_and_tag_inherit_rejection_without_suppressing_original(self) -> None:
        span = self.source()
        draft = ProfileDraft('interest', '观鸟', 0.8, (span,), 'synthetic-model-v1')
        self.propose('old', draft)
        self.click('profile_reject', 'old')
        restarted = ProfileRegistry(MemoryRuntime(self.config))
        result = restarted.decide(self.access, ProfileDecision('new-run', 'new', 0, 'propose',
                                   replace(draft, processor_version='synthetic-model-v2', confidence=0.99)))
        self.assertEqual(result.state, 'rejected')
        self.assertEqual(self.ui.read(self.access, view='drafts')['items'], [])
        self.assertIsNotNone(self.runtime.read(self.access, span.message_id))

    def test_old_pending_candidate_cannot_be_confirmed_after_parallel_rejection(self) -> None:
        draft = ProfileDraft('interest', '观鸟', 0.8, (self.source(),))
        self.propose('first', draft)
        self.propose('inflight', draft)
        self.click('profile_reject', 'first')
        self.assertEqual(self.ui.read(self.access, view='drafts')['items'], [])
        confirmation = self.source('合成：旧预览点击')
        with self.assertRaisesRegex(MemoryFailure, 'profile_previously_rejected'):
            self.profiles.decide(self.access, ProfileDecision('late-confirm', 'inflight', 1, 'confirm',
                                                             confirmation_id=confirmation.message_id))
        self.assertEqual(self.profiles.read(self.access, 'inflight').state, 'draft')

    def test_new_evidence_and_explicit_reopen_are_separate_review_opportunities(self) -> None:
        draft = ProfileDraft('interest', '观鸟', 0.8, (self.source(),))
        self.propose('original', draft)
        self.click('profile_reject', 'original')
        self.propose('inherited', replace(draft, processor_version='synthetic-v2'))
        with self.assertRaisesRegex(MemoryFailure, 'profile_reopen_origin_required'):
            self.click('profile_reopen', 'inherited')
        new_source = self.source()
        self.propose('fresh', replace(draft, sources=(new_source,)))
        self.assertEqual(len(self.ui.read(self.access, view='drafts')['items']), 1)
        with self.assertRaisesRegex(MemoryFailure, 'profile_reopen_required'):
            self.profiles.decide(self.access, ProfileDecision('implicit-reopen', 'original', 2, 'revise', draft))
        self.click('profile_reopen', 'original')
        self.assertEqual(self.profiles.read(self.access, 'original').state, 'draft')
        self.assertEqual(self.ui.read(self.access, view='rejected')['items'], [])
        with self.assertRaisesRegex(MemoryFailure, 'profile_reopen_origin_required'):
            self.click('profile_reopen', 'inherited')
        self.click('profile_confirm', 'original')
        self.assertEqual(self.profiles.read(self.access, 'original').state, 'confirmed')

    def test_replayed_archive_retains_rejection_against_a_new_processor(self) -> None:
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        from evernook.runtime_restore import publish_replayed_runtime

        draft = ProfileDraft('interest', '观鸟', 0.8, (self.source(),))
        self.propose('original', draft)
        root = self.config.parent.parent
        self.runtime.export_archive(self.access, root / 'before-reject')
        self.click('profile_reject', 'original')
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, root / 'before-reject', root / 'review', checkpoint=checkpoint)
        config, owner = publish_replayed_runtime(self.runtime, self.access, root / 'review', root / 'restored',
                                                checkpoint=checkpoint, client_id='restored', entry='owner')
        profiles = ProfileRegistry(MemoryRuntime(config))
        result = profiles.decide(owner, ProfileDecision('new-after-restore', 'new', 0, 'propose',
                                 replace(draft, processor_version='synthetic-v3')))
        self.assertEqual(result.state, 'rejected')


if __name__ == '__main__':
    unittest.main()
