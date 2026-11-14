from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evernook import MemoryFailure
from evernook.paths import external, provision_private_directory
from evernook.vault_stage_publication import publish_guard


class VaultStagePublicationTests(unittest.TestCase):
    def test_handle_blocks_replace_and_write_then_publishes_same_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-stage-publication-') as temporary:
            root = Path(temporary) / 'private'
            provision_private_directory(root)
            root = external(root)
            item = root / 'bundle'
            item.write_bytes(b'synthetic-proof')
            identity = item.stat()
            with publish_guard(item, identity, root, root.stat(), lambda: None) as (stream, publish):
                self.assertEqual(stream.read(), b'synthetic-proof')
                with self.assertRaises(OSError):
                    item.write_bytes(b'modified')
                with self.assertRaises(OSError):
                    item.rename(root / 'replacement')
                with self.assertRaises(OSError):
                    root.rename(root.with_name('replaced-parent'))
                with self.assertRaises(OSError):
                    root.parent.rename(root.parent.with_name(root.parent.name + '-moved'))
                child = subprocess.run([sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b"changed")',
                                        str(item)], capture_output=True, timeout=15)
                self.assertNotEqual(child.returncode, 0)
                publish(root / 'published-🐈-🐾')
            self.assertEqual((root / 'published-🐈-🐾').read_bytes(), b'synthetic-proof')

    def test_replaced_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-stage-publication-') as temporary:
            root = Path(temporary) / 'private'
            provision_private_directory(root)
            root = external(root)
            item = root / 'bundle'
            item.write_bytes(b'synthetic-proof')
            identity = item.stat()
            item.rename(root / 'original')
            item.write_bytes(b'synthetic-proof')
            with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unsafe'):
                with publish_guard(item, identity, root, root.stat(), lambda: None):
                    pass

    def test_replaced_destination_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-stage-publication-') as temporary:
            root = external(Path(temporary))
            source = root / 'source'
            provision_private_directory(source)
            item = source / 'bundle'
            item.write_bytes(b'synthetic-proof')
            destination = root / 'target'
            provision_private_directory(destination)
            parent_identity = destination.stat()
            destination.rename(root / 'original-target')
            provision_private_directory(destination)
            with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unsafe'):
                with publish_guard(item, item.stat(), destination, parent_identity, lambda: None):
                    pass


if __name__ == '__main__':
    unittest.main()
