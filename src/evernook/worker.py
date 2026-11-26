"""Compatibility import for platform.worker."""
import sys
from typing import TYPE_CHECKING

from .platform import worker as _implementation

if TYPE_CHECKING:
    from .platform.worker import (
        _ExtendedLimits as _ExtendedLimits,
        protect_parent_lifetime as protect_parent_lifetime,
        main as main,
    )
elif __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
