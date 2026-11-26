"""Compatibility import for platform.paths."""
import sys
from typing import TYPE_CHECKING

from .platform import paths as _implementation

if TYPE_CHECKING:
    from .platform.paths import (
        external as external,
        provision_private_directory as provision_private_directory,
        verify_private_directory as verify_private_directory,
    )
else:
    sys.modules[__name__] = _implementation
