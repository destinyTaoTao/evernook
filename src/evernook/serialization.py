"""JSON-compatible result values shared by local CLI and authenticated service."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import math
from typing import Any


def result_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return result_value(asdict(value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError('non_string_result_key')
        return {key: result_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [result_value(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise TypeError('unsupported_result_value')
