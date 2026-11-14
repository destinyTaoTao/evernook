from __future__ import annotations

from dataclasses import replace
import json
import threading
from typing import Any
import unittest
from unittest.mock import Mock

from evernook import Access, Capture, Event, MemoryFailure, Source
from evernook.models import AttachmentInput
from evernook.private_capture_receipt import capture


class PrivateCaptureReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.access = Access('synthetic', 'token')
        self.event = Event(Source('synthetic', 'synthetic-session', 'main', '1'), 'user', 0, None, 'complete',
                           'synthetic-private-text-222', sensitive=True)

    def test_unsupported_material_is_never_transmitted_or_echoed(self) -> None:
        attachment = AttachmentInput('a', 'synthetic-private-label-222', 'application/octet-stream',
                                     (b'synthetic-private-bytes-222',))
        request = Capture(self.event, 'tool_call', input_id='x', tool_name='tool', attachments=(attachment,),
                          provided=False, text_chunks=('synthetic-private-stream-222',))
        result = capture(self.client, self.access, request)
        self.assertEqual(result['status'], 'not_submitted')
        self.assertEqual(result['gaps'], ['private_attachments_unsupported', 'private_stream_unsupported',
                                         'entry_not_provided', 'private_tool_metadata_unsupported'])
        self.assertNotIn('synthetic-private', json.dumps(result))
        self.client.call.assert_not_called()

    def test_incomplete_and_precanceled_never_send(self) -> None:
        canceled = threading.Event()
        canceled.set()
        result = capture(self.client, self.access, Capture(self.event, 'message'), cancel=canceled)
        self.assertEqual(result['gaps'], ['canceled_before_submission'])
        result = capture(self.client, self.access, Capture(replace(self.event, completion='canceled'), 'message'))
        self.assertEqual(result['gaps'], ['incomplete_output'])
        self.client.call.assert_not_called()

    def test_explicit_sensitive_marker_is_required(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, 'protected_explicit_mark_required'):
            capture(self.client, self.access, Capture(replace(self.event, sensitive=False), 'message'))
        self.client.call.assert_not_called()

    def test_saved_receipt_is_validated_and_only_safe_fields_returned(self) -> None:
        self.client.call.return_value = {'state': 'protected_linked', 'encrypted_original_saved': True,
            'inbox_id': '00000000-0000-4000-8000-000000000001', 'object_id': '00000000-0000-4000-8000-000000000002',
            'key_id': 'a' * 64, 'version': 1,
            'message': {'message_id': '00000000-0000-4000-8000-000000000003', 'original_saved': False,
                        'representation': 'projection', 'status': 'projection_only', 'content_version': 1,
                        'received_at': '2030-01-01T00:00:00+00:00', 'unexpected': 'synthetic-private-never-return'}}
        result = capture(self.client, self.access, Capture(self.event, 'message'))
        self.assertEqual(result['status'], 'saved')
        self.assertTrue(result['protected_original_saved'])
        self.assertNotIn('synthetic-private', json.dumps(result))
        self.assertEqual(self.client.call.call_args.args[1], 'protected-receive')

    def test_receipt_missing_status_or_invalid_version_never_reports_saved(self) -> None:
        base: dict[str, Any] = {'state': 'protected_linked', 'encrypted_original_saved': True,
                'inbox_id': '00000000-0000-4000-8000-000000000001', 'object_id': '00000000-0000-4000-8000-000000000002',
                'key_id': 'a' * 64, 'version': 1,
                'message': {'message_id': '00000000-0000-4000-8000-000000000003', 'original_saved': False,
                            'representation': 'projection', 'status': 'projection_only', 'content_version': 1,
                            'received_at': '2030-01-01T00:00:00+00:00'}}
        for change in ({'status': None}, {'status': 'committed'}, {'content_version': True}, {'received_at': 'invalid'}):
            self.client.call.return_value = {**base, 'message': {**base['message'], **change}}
            self.assertEqual(capture(self.client, self.access, Capture(self.event, 'message'))['status'], 'outcome_unknown')

    def test_send_failure_cancel_and_malformed_response_are_unknown(self) -> None:
        for failure in (MemoryFailure('service_canceled'), RuntimeError('synthetic-secret-in-exception')):
            self.client.call.side_effect = failure
            result = capture(self.client, self.access, Capture(self.event, 'message'))
            self.assertEqual(result['status'], 'outcome_unknown')
            self.assertIsNone(result['protected_original_saved'])
            self.assertNotIn('synthetic', json.dumps(result))
        self.client.call.side_effect = None
        self.client.call.return_value = {'state': 'protected_linked', 'text': 'synthetic-private'}
        result = capture(self.client, self.access, Capture(self.event, 'message'))
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertNotIn('synthetic-private', json.dumps(result))
