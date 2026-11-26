"""Interpret the v1 on-disk mode flags without changing the upgrade boundary."""
from collections.abc import Mapping
from typing import Literal

from .models import MemoryFailure


RuntimeMode = Literal["embedded", "ordinary", "single", "multi"]
_REQUIRED = {"schema_version", "data_dir", "busy_timeout_ms"}
_MARKERS = {"service_mode", "restored_missing_vault", "protected_single_binding", "protected_multi_binding"}


def runtime_mode(config: Mapping[str, object]) -> RuntimeMode:
    keys = config.keys()
    if not _REQUIRED <= keys or keys - _REQUIRED - _MARKERS:
        raise MemoryFailure("unsupported_config")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise MemoryFailure("unsupported_config")
    markers = keys & _MARKERS
    if not markers:
        return "embedded"
    if "service_mode" not in markers or any(config[key] is not True for key in markers):
        raise MemoryFailure("unsupported_config")
    if "protected_single_binding" in markers:
        if "protected_multi_binding" in markers:
            raise MemoryFailure("unsupported_config")
        return "single"
    return "multi" if "protected_multi_binding" in markers else "ordinary"
