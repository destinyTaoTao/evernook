from __future__ import annotations

import io
import struct
import unittest
import zipfile

from evernook import MemoryFailure
from evernook.vault_stage_check import _bound_directory


class StageDirectoryBoundsTests(unittest.TestCase):
    def test_normal_directory_and_oversized_entry_count(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
            for index in range(11):
                archive.writestr(f'{index:02d}.enc', b'synthetic')
        _bound_directory(stream)
        raw = bytearray(stream.getvalue())
        raw[-14:-12] = struct.pack('<H', 1000)
        raw[-12:-10] = struct.pack('<H', 1000)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_backup'):
            _bound_directory(io.BytesIO(raw))
        for count in (67, 68):
            candidate = io.BytesIO()
            with zipfile.ZipFile(candidate, 'w', compression=zipfile.ZIP_STORED) as archive:
                for index in range(count):
                    archive.writestr(f'{index:02d}.enc', b'')
            if count == 67:
                _bound_directory(candidate)
            else:
                with self.assertRaisesRegex(MemoryFailure, 'invalid_backup'):
                    _bound_directory(candidate)

    def test_zip64_directory_bounds_are_checked_before_zip_parser(self) -> None:
        central = b'c' * 20
        pos = len(central)
        record = struct.pack('<4sQHHIIQQQQ', b'PK\x06\x06', 44, 45, 45, 0, 0, 11, 11, len(central), 0)
        locator = struct.pack('<4sLQL', b'PK\x06\x07', 0, pos, 1)
        end = struct.pack('<4s4H2LH', b'PK\x05\x06', 0, 0, 0xffff, 0xffff, 0xffffffff, 0xffffffff, 0)
        _bound_directory(io.BytesIO(central + record + locator + end))
        oversized = struct.pack('<4sQHHIIQQQQ', b'PK\x06\x06', 44, 45, 45, 0, 0, 68, 68, len(central), 0)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_backup'):
            _bound_directory(io.BytesIO(central + oversized + locator + end))


if __name__ == '__main__':
    unittest.main()
