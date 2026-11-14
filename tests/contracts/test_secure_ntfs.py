"""Synthetic Windows-only checks for the experimental NTFS primitive."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import struct
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

    def test_existing_child_junction_is_not_followed(self):
        target = self.root / 'target'
        target.mkdir()
        junction = self.root / 'existing'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(target)],
                                capture_output=True)
        if result.returncode:
            self.skipTest('junction creation unavailable')
        try:
            with PrivateTree(self.root) as root:
                with self.assertRaises(MemoryFailure):
                    root.mkdir('existing')
            self.assertEqual(list(target.iterdir()), [])
        finally:
            junction.rmdir()

    def test_held_ancestor_cannot_be_replaced_by_another_process(self):
        parent = self.root / 'parent'
        parent.mkdir()
        anchor = parent / 'anchor'
        anchor.mkdir()
        with PrivateTree(anchor) as root:
            result = subprocess.run([sys.executable, '-c',
                'import os,sys; os.rename(sys.argv[1],sys.argv[2])',
                str(parent), str(self.root / 'stolen')], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            root.mkdir('created')
        self.assertTrue((anchor / 'created').is_dir())

    def test_managed_child_blocks_planting_rename_and_replacement(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        with PrivateTree(self.root) as root:
            child = root.mkdir('child')
            # Write-data-class and delete-class opens are refused, so no other
            # process can plant children, rename or replace a managed child.
            for access in (0x40000000, 0x2, 0x4, 0x10000):
                handle = kernel.CreateFileW(str(self.root / 'child'), access, 7, None, 3, 0x02200000, None)
                self.assertEqual(handle, wintypes.HANDLE(-1).value)
            child.mkdir('created')
        self.assertTrue((self.root / 'child' / 'created').is_dir())

    def test_concurrent_write_attempts_cannot_plant_or_replace_managed_child(self):
        # Isolate a potentially stuck kernel call and its handles from the test runner.
        if os.environ.get('EVERNOOK_NTFS_RACE_CHILD') != '1':
            environment = dict(os.environ, EVERNOOK_NTFS_RACE_CHILD='1')
            result = subprocess.run([sys.executable, '-m', 'unittest',
                'test_secure_ntfs.SecureNtfsTests.test_concurrent_write_attempts_cannot_plant_or_replace_managed_child'],
                env=environment, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            return
        target = self.root / 'target'
        target.mkdir()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        kernel.DeviceIoControl.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        substitute = ('\\??\\' + str(target)).encode('utf-16-le')
        display = str(target).encode('utf-16-le')
        body = (struct.pack('<HHHH', 0, len(substitute), len(substitute) + 2, len(display))
                + substitute + b'\0\0' + display + b'\0\0')
        setup = ctypes.create_string_buffer(struct.pack('<IHH', 0xA0000003, len(body), 0) + body)
        remove = ctypes.create_string_buffer(struct.pack('<IHH', 0xA0000003, 0, 0))
        for index in range(12):
            with self.subTest(index=index), PrivateTree(self.root) as root:
                child = root.mkdir(f'race-{index}')
                path = self.root / f'race-{index}'
                stop = threading.Event()
                detected: list[str] = []
                def attacker():
                    returned = wintypes.DWORD()
                    while not stop.is_set():
                        # Write-data opens are refused; the only in-place mutation
                        # left is a write-attributes junction conversion, whose
                        # detection is asserted below. Neither may plant content.
                        handle = kernel.CreateFileW(str(path), 0x40000000, 7, None, 3, 0x02200000, None)
                        if handle != wintypes.HANDLE(-1).value:
                            detected.append('write-open-accepted')
                            kernel.CloseHandle(wintypes.HANDLE(int(handle)))
                            return
                        handle = kernel.CreateFileW(str(path), 0x100, 7, None, 3, 0x02200000, None)
                        if handle != wintypes.HANDLE(-1).value:
                            kernel.DeviceIoControl(handle, 0x900A4, setup, len(setup) - 1, None, 0,
                                                   ctypes.byref(returned), None)
                            kernel.DeviceIoControl(handle, 0x900AC, remove, len(remove) - 1, None, 0,
                                                   ctypes.byref(returned), None)
                            kernel.CloseHandle(handle)
                        stop.wait(0.001)
                worker = threading.Thread(target=attacker, daemon=True)
                worker.start()
                try:
                    for step in range(20):
                        child.mkdir(f'created-{step}')
                        # Transient write-attributes conversions may or may not
                        # be visible here; they are asserted statically below.
                        # Content must never land in the redirected target.
                        self.assertFalse(list(target.iterdir()))
                finally:
                    stop.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertFalse(detected, detected)
                self.assertFalse(list(target.iterdir()))

    def test_in_place_junction_conversion_is_detected_and_never_redirects(self):
        target = self.root / 'target'
        target.mkdir()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        kernel.DeviceIoControl.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        with PrivateTree(self.root) as root:
            child = root.mkdir('child')
            handle = kernel.CreateFileW(str(self.root / 'child'), 0x100, 7, None, 3, 0x02200000, None)
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
            installed = False
            try:
                substitute = ('\\??\\' + str(target)).encode('utf-16-le')
                display = str(target).encode('utf-16-le')
                body = (struct.pack('<HHHH', 0, len(substitute), len(substitute) + 2, len(display))
                        + substitute + b'\0\0' + display + b'\0\0')
                data = struct.pack('<IHH', 0xA0000003, len(body), 0) + body
                buffer, returned = ctypes.create_string_buffer(data), wintypes.DWORD()
                self.assertTrue(kernel.DeviceIoControl(handle, 0x900A4, buffer, len(data), None, 0,
                                                       ctypes.byref(returned), None), ctypes.get_last_error())
                installed = True
                # Creation through the held handle refuses while the object is
                # a reparse point: OBJ_DONT_REPARSE turns the conversion into a
                # detected abort instead of a redirected write.
                with self.assertRaises(MemoryFailure):
                    child.mkdir('probe')
                self.assertFalse((target / 'probe').exists())
                # The conversion is detected on the held identity and fails closed.
                with self.assertRaises(MemoryFailure):
                    root.verify()
            finally:
                try:
                    if installed:
                        data = struct.pack('<IHH', 0xA0000003, 0, 0)
                        buffer, returned = ctypes.create_string_buffer(data), wintypes.DWORD()
                        self.assertTrue(kernel.DeviceIoControl(handle, 0x900AC, buffer, len(data), None, 0,
                                                               ctypes.byref(returned), None), ctypes.get_last_error())
                finally:
                    kernel.CloseHandle(handle)
            root.verify()
            child.mkdir('probe')
            self.assertTrue((self.root / 'child' / 'probe').is_dir())

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
