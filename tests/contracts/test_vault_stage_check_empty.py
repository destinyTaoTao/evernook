from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from evernook import MemoryRuntime, initialize, pair_client
from evernook.backup import create_backup, initialize_backup_keys
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.protected import initialize_protected_inbox
from evernook.service import ServiceClient
from evernook.vault_multi_proof import prepare
from evernook.vault_multi_restore import restore_stage
from evernook.vault_stage_check import check_stage


class EmptyStageTests(unittest.TestCase):
    def test_empty_registered_set_can_be_staged_and_rechecked(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-empty-stage-') as temporary:
            root = Path(temporary)
            config, owner = initialize(root / 'ordinary', client_id='owner', entry='owner', allow_archive_review=True)
            owner_file = config.parent / 'mcp-access-owner.json'
            pair_client(config, client_id='owner', entry='owner', access_path=owner_file)
            runtime = MemoryRuntime(config)
            passwords = (b'synthetic empty vault password one', b'synthetic empty vault password two')
            first = initialize_protected_inbox(runtime, root / 'inbox-a', root / 'keys-a', passwords[0])
            second = initialize_protected_inbox(runtime, root / 'inbox-b', root / 'keys-b', passwords[1])
            first.register(owner); second.register(owner)
            prepare(runtime, owner, ((first, passwords[0]), (second, passwords[1])), inventory_statement='STOPPED_AND_INVENTORIED')
            processes: list[subprocess.Popen[bytes]] = []
            def start(command: list[str], endpoint: Path) -> None:
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                processes.append(process)
                deadline = time.monotonic() + 30
                while not endpoint.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        assert process.stderr is not None
                        self.fail(process.stderr.read().decode(errors='replace') if process.poll() is not None else 'timeout')
                    time.sleep(.05)
            try:
                start([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                       '--activate-owner', str(owner_file), '--activate-verified-multi',
                       '--protected-inbox', str(first.root), '--protected-keys', str(first.key_root),
                       '--protected-history-inbox', str(second.root), '--protected-history-keys', str(second.key_root)],
                      config.parent / 'memory-service.json')
                keys = initialize_backup_keys(root / 'backup-keys', b'synthetic empty backup password')
                create_backup(MemoryRuntime(config, service_owner=True), owner, root / 'ordinary-backup', root / 'backup-keys')
                ServiceClient(config).protected_backup(owner, root / 'vault-backup', root / 'backup-keys')
                processes[-1].terminate(); processes[-1].wait(timeout=15)
                recovered = root / 'recovered'
                with OfflineRecovery(root / 'ordinary-backup', recovered, Path(keys.recovery_file), b'synthetic empty backup password') as review:
                    review.publish(ACKNOWLEDGEMENT)
                recovered_config, recovered_owner_file = recovered / 'runtime.json', recovered / 'mcp-access-owner.json'
                recovered_owner = MemoryRuntime(recovered_config).access_from_file(recovered_owner_file)
                start([sys.executable, '-B', '-m', 'evernook.service', '--config', str(recovered_config),
                       '--activate-owner', str(recovered_owner_file), '--activate-restored-without-vault'],
                      recovered / 'memory-service.json')
                processes[-1].terminate(); processes[-1].wait(timeout=15)
                by_id = {first.inbox_id: passwords[0], second.inbox_id: passwords[1]}
                stage = root / 'staged'
                restore_stage(recovered_config, recovered_owner, root / 'vault-backup', stage, Path(keys.recovery_file),
                              b'synthetic empty backup password', lambda _slot, inbox_id: by_id[inbox_id])
                result = check_stage(recovered_config, recovered_owner, stage, Path(keys.recovery_file), b'synthetic empty backup password')
                self.assertEqual(result['inboxes'], 2)
                self.assertEqual(result['protected_objects'], 0)
                self.assertFalse(result['attachment_authorized'])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=20)


if __name__ == '__main__':
    unittest.main()
