"""Compatibility import for platform.secure_ntfs."""
import sys
from typing import TYPE_CHECKING

from .platform import secure_ntfs as _implementation

if TYPE_CHECKING:
    from .platform.secure_ntfs import (
        HeldFile as HeldFile,
        PrivateDirectory as PrivateDirectory,
        PrivateTree as PrivateTree,
        _shared_native as _shared_native,
    )
else:
    sys.modules[__name__] = _implementation
