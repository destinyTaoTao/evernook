"""Compatibility import for platform.windows_acl."""
import sys
from typing import TYPE_CHECKING

from .platform import windows_acl as _implementation

if TYPE_CHECKING:
    from .platform.windows_acl import (
        current_user_sid as current_user_sid,
        verify_dacl as verify_dacl,
        _current_user_only as _current_user_only,
    )
else:
    sys.modules[__name__] = _implementation
