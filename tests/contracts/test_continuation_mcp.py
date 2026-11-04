from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters

from evernook import Event, EvidenceSpan, MemoryRuntime, Source, initialize, pair_client
from evernook.continuations import ContinuationDraft


class ContinuationMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_capture_client_saves_and_fresh_cross_entry_client_restores_but_recall_profile_cannot_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-continuation-mcp-') as directory:
            config, _ = initialize(Path(directory) / 'ordinary', client_id='owner', entry='owner')
            access_file = config.parent / 'mcp-access-host.json'
            access = pair_client(config, client_id='host', entry='dsh', access_path=access_file, scopes=('capture', 'recall'))
            runtime = MemoryRuntime(config)
            text = '合成任务：恢复工作；下一步运行测试。'
            receipt = runtime.submit(access, Event(Source('dsh', 'native', 'main', 'input'), 'user', 1, None, 'complete', text))
            span = EvidenceSpan(receipt.message_id, 1, 'original', 1, 0, len(text), text)
            draft = ContinuationDraft('work', 'main', 0, receipt.message_id, span, (), (span,), 'in_progress',
                (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
            args = ['-m', 'evernook.mcp_server', '--config', str(config), '--access', str(access_file)]
            parameters = StdioServerParameters(command=sys.executable, args=[*args, '--profile', 'trusted-host'])
            async with Client(parameters, read_timeout_seconds=20) as client:
                saved = await client.call_tool('evernook_v1_continuation_save', {'request_id': 'native-1', 'draft': asdict(draft)})
                self.assertFalse(saved.is_error, saved)
            other_file = config.parent / 'mcp-access-other.json'
            pair_client(config, client_id='other', entry='pi', access_path=other_file, scopes=('capture', 'recall'))
            other_args = list(args)
            other_args[other_args.index('--access') + 1] = str(other_file)
            async with Client(parameters.model_copy(update={'args': [*other_args, '--profile', 'trusted-host']}), read_timeout_seconds=20) as client:
                restored = await client.call_tool('evernook_v1_continuation_current', {'task_id': 'work', 'branch_id': 'main'})
                self.assertFalse(restored.is_error, restored)
                self.assertEqual(restored.structured_content, saved.structured_content)
            async with Client(parameters.model_copy(update={'args': other_args}), read_timeout_seconds=20) as client:
                denied = await client.call_tool('evernook_v1_continuation_save', {'request_id': 'forged', 'draft': asdict(draft)})
                self.assertTrue(denied.is_error)
                assert denied.structured_content is not None
                self.assertEqual(denied.structured_content['error']['code'], 'operation_not_allowed')


if __name__ == '__main__':
    unittest.main()
