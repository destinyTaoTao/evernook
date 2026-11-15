"""Publish encrypted vault packages through held, birth-private files."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Callable

from .models import MemoryFailure
from .secure_ntfs import PrivateDirectory, PrivateTree


def publish_package(package: PrivateDirectory, destination: Path, expected: dict[str, str],
                    check: Callable[[], None]) -> None:
    """Write the authenticated header last; incomplete output is never success.

    No overwrite or path-based ACL mutation occurs. A crash may leave a
    private partial destination, which is reported as outcome_unknown and
    must not be treated as a usable backup.
    """
    with PrivateTree(destination.parent) as parent:
        target = parent.mkdir(destination.name)
        # Files stay held until the entire publication has been verified.
        for name in sorted(expected, key=lambda item: (item == 'backup.json', item)):
            check()
            source = package.open_file(name)
            try:
                output = target.create_file(name)
                digest = hashlib.sha256()
                while chunk := source.stream.read(1024 * 1024):
                    check()
                    output.stream.write(chunk)
                    digest.update(chunk)
                if not hmac.compare_digest(digest.hexdigest(), expected[name]):
                    raise MemoryFailure('vault_backup_package_changed')
                output.stream.flush()
                os.fsync(output.stream.fileno())
            finally:
                source.release()
        package.verify()
        target.verify()
        check()
