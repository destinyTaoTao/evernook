from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from evernook import Event, Source, Capture, Change, MemoryFailure, MemoryRuntime, initialize, pair_client
from evernook.host_recovery import HostRecovery


class HostRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='evernook-recovery-control-')
        self.addCleanup(temp.cleanup)
        config, self.owner = initialize(Path(temp.name) / 'memory', client_id='owner', entry='owner')
        self.config = config
        self.runtime = MemoryRuntime(config)
        self.control = HostRecovery(self.runtime)
        result = self.runtime.submit_capture(self.owner, Capture(Event(Source('owner', 'synthetic', 'main', 'result'),
            'tool', 1, None, 'complete', '合成完整结果'), 'message'))
        self.message_id = result.message.message_id
        self.review = {'action': 'preview', 'fingerprint': 'a' * 64, 'references': [[self.message_id, 1, 1, True]]}

    def test_confirmation_and_completion_are_persistent_and_idempotent(self) -> None:
        first = self.control.run(self.owner, self.review)
        second = self.control.run(self.owner, self.review)
        self.assertEqual(first['target_session'], second['target_session'])
        confirm = {'action': 'confirm', 'proposal_id': first['proposal_id'], 'fingerprint': 'a' * 64}
        pending = self.control.run(self.owner, confirm)
        self.assertEqual(pending['state'], 'confirmed')
        restarted = HostRecovery(MemoryRuntime(self.config))
        self.assertEqual(restarted.run(self.owner, confirm), pending)
        complete = {'action': 'complete', 'operation_id': pending['operation_id']}
        done = restarted.run(self.owner, complete)
        self.assertEqual(done['state'], 'complete')
        self.assertEqual(restarted.run(self.owner, complete), done)
        another = {'action': 'confirm', 'proposal_id': second['proposal_id'], 'fingerprint': 'a' * 64}
        self.assertEqual(restarted.run(self.owner, another), done)

    def test_canceled_expired_changed_or_ordinary_identity_cannot_confirm(self) -> None:
        draft = self.control.run(self.owner, self.review)
        self.control.run(self.owner, {'action': 'cancel', 'proposal_id': draft['proposal_id']})
        with self.assertRaisesRegex(MemoryFailure, 'recovery_canceled'):
            self.control.run(self.owner, {'action': 'confirm', 'proposal_id': draft['proposal_id'], 'fingerprint': 'a' * 64})
        with patch('evernook.host_recovery.time.time', return_value=1):
            expired = self.control.run(self.owner, self.review)
        with self.assertRaisesRegex(MemoryFailure, 'recovery_expired'):
            self.control.run(self.owner, {'action': 'confirm', 'proposal_id': expired['proposal_id'], 'fingerprint': 'a' * 64})
        current = self.control.run(self.owner, self.review)
        confirmation = self.runtime.submit(self.owner, Event(Source('owner', 'synthetic', 'main', 'suppress'),
            'user', 2, None, 'complete', '合成确认停止召回'))
        self.runtime.change(self.owner, Change('suppress-recovery-source', self.message_id, 1, 'suppress', confirmation.message_id, 'synthetic'))
        with self.assertRaisesRegex(MemoryFailure, 'recovery_stale'):
            self.control.run(self.owner, {'action': 'confirm', 'proposal_id': current['proposal_id'], 'fingerprint': 'a' * 64})
        path = self.config.parent / 'mcp-access-ordinary.json'
        ordinary = pair_client(self.config, client_id='ordinary', entry='dsh', access_path=path, scopes=('recall', 'capture'))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.control.run(ordinary, self.review)

    def test_other_actor_and_mismatching_review_cannot_reuse_confirmation(self) -> None:
        preview = self.control.run(self.owner, self.review)
        with self.assertRaisesRegex(MemoryFailure, 'recovery_review_mismatch'):
            self.control.run(self.owner, {'action': 'confirm', 'proposal_id': preview['proposal_id'], 'fingerprint': 'b' * 64})
        other = pair_client(self.config, client_id='other-owner', entry='other-owner',
            access_path=self.config.parent / 'mcp-access-other.json', scopes=('recall', 'maintain', 'accept'))
        with self.assertRaisesRegex(MemoryFailure, 'recovery_not_found'):
            self.control.run(other, {'action': 'confirm', 'proposal_id': preview['proposal_id'], 'fingerprint': 'a' * 64})

    def test_preview_capacity_is_bounded_and_cancel_releases_capacity(self) -> None:
        previews = [self.control.run(self.owner, self.review) for _ in range(64)]
        with self.assertRaisesRegex(MemoryFailure, 'recovery_queue_full'):
            self.control.run(self.owner, self.review)
        self.control.run(self.owner, {'action': 'cancel', 'proposal_id': previews[0]['proposal_id']})
        self.assertTrue(self.control.run(self.owner, self.review)['read_only'])
