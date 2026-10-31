"""Companion preset: recommended defaults, fully overridable. Recommended values, not verified performance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3

from .consolidation import CONSOLIDATION_TIERS, ConsolidationWorkflow
from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json
from .scheduler import ConsolidationScheduler


COMPANION_PRESET_VERSION = 1
# The originally proposed budget triple, preserved verbatim for traceability. deep=1000 predates
# the context contract's normal<=deep ordering; the recommended deep budget is 4x the session one.
COMPANION_DECLARED_BUDGETS = (4000, 16000, 1000)
# Recommended values only: none of these are validated performance or quality promises.
# declared_budgets preserves the originally proposed 4000/16000/1000 triple verbatim; deep=1000
# predates the context contract's normal<=deep ordering and cannot be applied directly, so the
# recommended deep budget derives from 4x the session budget until the user confirms otherwise.
COMPANION_PRESET: dict[str, object] = {
    "timezone": "Asia/Shanghai",
    "must_read_tokens": 4000,
    "session_tokens": 16000,
    "deep_tokens": 64000,
    "persona_tokens": 600,
    "profile_tokens": 400,
    "recent_days": 7,
    "recent_target_chars": 8192,
    "scheduler_max_sources_per_run": 8,
    "scheduler_max_runs_per_trigger": 1,
    "consolidation_tier": "review",
    "idle_seconds": 900,
}
_PRESET_INT_RANGES: dict[str, tuple[int, int]] = {
    "must_read_tokens": (128, 131072),
    "session_tokens": (128, 131072),
    "deep_tokens": (128, 131072),
    "persona_tokens": (0, 131072),
    "profile_tokens": (0, 131072),
    "recent_days": (1, 36500),
    "recent_target_chars": (1, 1048576),
    "scheduler_max_sources_per_run": (1, 16),
    "scheduler_max_runs_per_trigger": (1, 16),
    "idle_seconds": (0, 86400),
}


@dataclass(frozen=True)
class PresetView:
    name: str
    version: int
    applied: bool
    values: dict[str, object]
    declared_budgets: tuple[int, int, int]
    overridden: tuple[str, ...]


def _validate(effective: dict[str, object], overrides: dict[str, object]) -> None:
    for key, value in overrides.items():
        if key == "timezone":
            if not isinstance(value, str) or not value or len(value) > 64:
                raise MemoryFailure("invalid_preset_override")
            _identifier(value)
        elif key == "consolidation_tier":
            if not isinstance(value, str) or value not in CONSOLIDATION_TIERS:
                raise MemoryFailure("invalid_preset_override")
        elif key in _PRESET_INT_RANGES:
            low, high = _PRESET_INT_RANGES[key]
            if type(value) is not int or not low <= value <= high:
                raise MemoryFailure("invalid_preset_override")
        else:
            raise MemoryFailure("invalid_preset_override")
    pinned, normal, deep = (effective["must_read_tokens"], effective["session_tokens"], effective["deep_tokens"])
    assert type(pinned) is int and type(normal) is int and type(deep) is int
    if not 128 <= pinned <= normal <= deep:
        raise MemoryFailure("invalid_preset_budgets")


def apply_preset(access: Access, runtime: MemoryRuntime, *, name: str = "companion",
                 overrides: dict[str, object] | None = None) -> PresetView:
    """Trusted maintenance boundary: applies side-effect settings and stores the effective document."""
    runtime._require_scope(access, "maintain", "preset_apply")
    if name != "companion":
        raise MemoryFailure("unknown_preset")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise MemoryFailure("invalid_preset_override")
    unknown = set(overrides) - set(COMPANION_PRESET)
    if unknown:
        raise MemoryFailure("invalid_preset_override")
    effective = dict(COMPANION_PRESET)
    effective.update(overrides)
    _validate(effective, overrides)
    consolidation = ConsolidationWorkflow(runtime)
    consolidation.configure(access, tier=str(effective["consolidation_tier"]))
    scheduler_sources = effective["scheduler_max_sources_per_run"]
    scheduler_runs = effective["scheduler_max_runs_per_trigger"]
    assert type(scheduler_sources) is int and type(scheduler_runs) is int
    scheduler = ConsolidationScheduler(runtime, consolidation)
    scheduler.configure(access, max_sources_per_run=scheduler_sources, max_runs_per_trigger=scheduler_runs)
    now = datetime.now(timezone.utc).isoformat()
    with runtime._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS preset_settings_v1 (
            name TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL,
            declared TEXT NOT NULL, applied_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO preset_settings_v1 VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
            version=excluded.version, payload=excluded.payload, declared=excluded.declared,
            applied_at=excluded.applied_at""",
            (name, COMPANION_PRESET_VERSION, _json(effective), _json(list(COMPANION_DECLARED_BUDGETS)), now))
        db.execute("COMMIT")
    return PresetView(name, COMPANION_PRESET_VERSION, True, effective,
                      (4000, 16000, 1000), tuple(sorted(overrides)))


def show_preset(access: Access, runtime: MemoryRuntime, *, name: str = "companion") -> PresetView:
    runtime._require_scope(access, "maintain", "preset_show")
    if name != "companion":
        raise MemoryFailure("unknown_preset")
    with runtime._connection() as db:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='preset_settings_v1'").fetchone():
            return PresetView(name, COMPANION_PRESET_VERSION, False, dict(COMPANION_PRESET), COMPANION_DECLARED_BUDGETS, ())
        row = db.execute("SELECT version,payload,declared,applied_at FROM preset_settings_v1 WHERE name=?", (name,)).fetchone()
        if row is None:
            return PresetView(name, COMPANION_PRESET_VERSION, False, dict(COMPANION_PRESET), COMPANION_DECLARED_BUDGETS, ())
        declared = tuple(json.loads(row["declared"]))
        values = json.loads(row["payload"])
        overridden = tuple(sorted(key for key in COMPANION_PRESET if values.get(key) != COMPANION_PRESET[key]))
        return PresetView(name, int(row["version"]), True, values,
                          (int(declared[0]), int(declared[1]), int(declared[2])), overridden)
