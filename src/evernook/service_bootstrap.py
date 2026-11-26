"""Validate persisted vault bindings and supplied materials before attaching any inbox."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .missing_vault import attachment, read_state
from .models import MemoryFailure
from .paths import external
from .runtime import MemoryRuntime, _load
from .runtime_mode import RuntimeMode, runtime_mode
from .vault_retirement import active_registry


def inspect_vault_startup(runtime: MemoryRuntime, config: Path, *, protected_root: Path | None,
                       protected_keys: Path | None, protected_history_roots: tuple[Path, ...],
                       activate_owner: Path | None, activate_verified_inbox: bool,
                       activate_verified_multi: bool, activate_restored_without_vault: bool,
                       allow_legacy_shared: bool) -> tuple[RuntimeMode, bool, dict[str, Any] | None]:
    from .vault_single_binding import read as read_single_binding
    with runtime._connection(ownership_exclusive=True) as check:
        mode = runtime_mode(_load(config))
        multi_marker = mode == "multi"
        single_marker = mode == "single"
        binding_exists = (runtime.root / 'protected-multi-binding.json').exists()
        if (multi_marker and not binding_exists) or (binding_exists and not multi_marker and not activate_verified_multi):
            raise MemoryFailure('protected_multi_binding_mismatch')
        single_exists = (runtime.root / 'protected-single-binding.json').exists()
        if (single_marker and not single_exists) or (single_exists and not single_marker and not activate_verified_inbox):
            raise MemoryFailure('protected_single_binding_mismatch')
        if multi_marker and (protected_root is None or not protected_history_roots):
            raise MemoryFailure('protected_multi_binding_mismatch')
        if single_marker and (allow_legacy_shared or activate_verified_multi
                              or activate_restored_without_vault
                              or (activate_owner is not None and not activate_verified_inbox)):
            raise MemoryFailure('invalid_protected_service_config')
        if single_marker and (protected_root is None or protected_keys is None or protected_history_roots):
            raise MemoryFailure('protected_single_binding_mismatch')
        if single_marker and protected_root is not None and protected_keys is not None:
            bound = read_single_binding(runtime)
            if (bound is None or str(external(protected_root)) != bound['inbox_dir']
                    or str(external(protected_keys)) != bound['key_dir']):
                raise MemoryFailure('protected_single_binding_mismatch')
        if ((protected_history_roots and not (multi_marker or activate_verified_multi))
                or (multi_marker and (activate_owner is not None or allow_legacy_shared))):
            raise MemoryFailure('invalid_protected_service_config')
        missing_vault = read_state(runtime, check)
        attached_vault = attachment(runtime, check)
        if (len(active_registry(runtime, check)) > 1
                and not (multi_marker or activate_verified_multi or activate_restored_without_vault or missing_vault)):
            raise MemoryFailure('protected_cutover_requires_migration')
    if missing_vault and (protected_root is not None or allow_legacy_shared):
        raise MemoryFailure('protected_restore_required')
    if attached_vault is not None:
        if (multi_marker or activate_verified_multi or allow_legacy_shared or protected_root is None or protected_keys is None
                or str(external(protected_root)) != attached_vault['inbox_dir']
                or str(external(protected_keys)) != attached_vault['key_dir']):
            raise MemoryFailure('protected_attachment_mismatch')
    if missing_vault and not _load(config).get('service_mode') and not activate_restored_without_vault:
        raise MemoryFailure('protected_restore_required')
    return mode, missing_vault, attached_vault
