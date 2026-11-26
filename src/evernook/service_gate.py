"""Compatibility import for platform.service_gate."""
import sys
from typing import TYPE_CHECKING

from .platform import service_gate as _implementation

if TYPE_CHECKING:
    from .platform.service_gate import (
        ownership_gate as ownership_gate,
    )
else:
    sys.modules[__name__] = _implementation
