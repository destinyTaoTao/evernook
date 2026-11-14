"""Synthetic Windows-only checks for the experimental NTFS primitive."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from evernook.models import MemoryFailure
from evernook.paths import external, verify_private_directory
from evernook.secure_ntfs import PrivateTree, _shared_native


@unittest.skipUnless(sys.platform == 'win32', 'Windows NTFS primitive')
class SecureNtfsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evernook-ntfs-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_nested_creation_has_private_acl_and_holds_handles(self):
        with PrivateTree(self.root) as root:
            directory = root.mkdir('private-🐈')
            with directory.file('sample.bin') as stream:
                stream.write(b'synthetic')
                stream.flush()
                result = subprocess.run([sys.executable, '-c',
                    'import os,sys; os.rename(sys.argv[1],sys.argv[2])',
                    str(self.root / 'private-🐈'), str(self.root / 'moved')],
                    capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                result = subprocess.run([sys.executable, '-c',
                    "import sys; open(sys.argv[1], 'wb').write(b'overwrite')",
                    str(self.root / 'private-🐈' / 'sample.bin')], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            verify_private_directory(external(self.root / 'private-🐈'),
                extra_files=(external(self.root / 'private-🐈' / 'sample.bin'),))
        self.assertEqual((self.root / 'private-🐈' / 'sample.bin').read_bytes(), b'synthetic')

    def test_existing_names_are_refused_without_modification(self):
        (self.root / 'existing').mkdir()
        (self.root / 'sample').write_bytes(b'unchanged')
        with PrivateTree(self.root) as root:
            with self.assertRaises(MemoryFailure):
                root.mkdir('existing')
            with self.assertRaises(MemoryFailure):
                with root.file('sample'):
                    self.fail('existing file opened')
        self.assertEqual((self.root / 'sample').read_bytes(), b'unchanged')

    def test_existing_open_file_is_not_modified(self):
        path = self.root / 'held'
        path.write_bytes(b'unchanged')
        with path.open('rb'):
            with PrivateTree(self.root) as root:
                with self.assertRaises(MemoryFailure):
                    with root.file('held'):
                        self.fail('existing file opened')
                root.mkdir('new')
        self.assertEqual(path.read_bytes(), b'unchanged')

    def test_invalid_names_are_rejected(self):
        with PrivateTree(self.root) as root:
            for name in ('', '.', '..', 'a/b', 'a\\b', 'a:b', 'tail.', 'tail ',
                         'CON.txt', 'x' * 256, '\ud800'):
                with self.subTest(name=repr(name)), self.assertRaises(MemoryFailure):
                    root.mkdir(name)

    def test_replaced_parent_is_rejected(self):
        parent = self.root / 'parent'
        parent.mkdir()
        tree = PrivateTree(parent)
        parent.rename(self.root / 'original')
        parent.mkdir()
        with self.assertRaises(MemoryFailure):
            with tree as root:
                root.mkdir('must-not-exist')
        self.assertFalse((parent / 'must-not-exist').exists())

    def test_junction_parent_is_rejected(self):
        target = self.root / 'target'
        target.mkdir()
        junction = self.root / 'junction'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(target)],
                                capture_output=True)
        if result.returncode:
            self.skipTest('junction creation unavailable')
        try:
            with self.assertRaises(MemoryFailure):
                with PrivateTree(junction) as root:
                    root.mkdir('must-not-exist')
            self.assertFalse((target / 'must-not-exist').exists())
        finally:
            junction.rmdir()

    def test_acl_validation_failure_releases_handle_and_writes_no_data(self):
        native = _shared_native()
        with PrivateTree(self.root) as root:
            with patch.object(native, 'verify_acl', side_effect=MemoryFailure('synthetic_failure')):
                with self.assertRaises(MemoryFailure):
                    with root.file('empty') as stream:
                        stream.write(b'must-not-be-written')
        self.assertEqual((self.root / 'empty').read_bytes(), b'')
        (self.root / 'empty').rename(self.root / 'released')

    def test_wrong_thread_and_closed_tree_fail_closed(self):
        failures = []
        tree = PrivateTree(self.root)
        with tree as root:
            def other_thread():
                try:
                    root.mkdir('wrong-thread')
                except MemoryFailure as error:
                    failures.append(str(error))
            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join()
        self.assertEqual(len(failures), 1)
        with self.assertRaises(MemoryFailure):
            root.mkdir('closed')
        self.assertFalse((self.root / 'wrong-thread').exists())

    def test_process_exit_releases_handles_and_preserves_acl(self):
        script = ("import os,sys; from pathlib import Path; "
                  "from evernook.secure_ntfs import PrivateTree; "
                  "tree=PrivateTree(Path(sys.argv[1])); root=tree.__enter__(); "
                  "root.mkdir('crash'); os._exit(23)")
        result = subprocess.run([sys.executable, '-c', script, str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 23, result.stderr.decode(errors='replace'))
        verify_private_directory(external(self.root / 'crash'))
        (self.root / 'crash').rename(self.root / 'released')


if __name__ == '__main__':
    unittest.main()
