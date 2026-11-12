"""Service-owned bounded maintenance using the existing persistent policies."""
from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import threading
import time
from typing import Any

from .maintenance import MaintenanceBudget, NavigationMaintenance
from .models import Access, MemoryFailure
from .embedding import LocalMiniLM
from .semantic import SemanticRecall
from .runtime import MemoryRuntime
from .scheduler import ConsolidationScheduler


class ServiceMaintenance:
    def __init__(self, runtime: MemoryRuntime, access_path: Path, interval: float,
                 navigation_policies: tuple[str, ...], stopping: threading.Event, *, embedding_config: Path | None = None):
        if not math.isfinite(interval) or not 0.2 <= interval <= 3600:
            raise MemoryFailure("invalid_service_maintenance_interval")
        if len(navigation_policies) > 16 or len(set(navigation_policies)) != len(navigation_policies):
            raise MemoryFailure("invalid_service_navigation_policies")
        self.runtime, self.access_path = runtime, access_path
        access = runtime.access_from_file(access_path)
        for scope in ("recall", "maintain", "accept"):
            runtime._require_scope(access, scope, "service_maintenance")
        self.interval, self.policies, self.stopping = interval, navigation_policies, stopping
        self.semantic = SemanticRecall(runtime, LocalMiniLM.from_file(embedding_config)) if embedding_config is not None else None
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"enabled": True, "ticks": 0, "error": None, "skipped": None}
        self.thread = threading.Thread(target=self.run, name="evernook-service-maintenance")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def tick(self) -> dict[str, Any]:
        access = self.runtime.access_from_file(self.access_path)
        scheduler = ConsolidationScheduler(self.runtime)
        signal = _MaintenanceSignal(scheduler, access, self.stopping)
        reason = signal.reason()
        if reason:
            return {"skipped": reason}
        deadline = time.monotonic() + 1
        for _ in range(16):
            reason = signal.reason()
            if reason:
                return {"skipped": reason}
            if time.monotonic() >= deadline:
                break
            lexical = self.runtime.refresh_lexical(access, limit=1)
            if not lexical.pending_messages:
                break
        navigation = NavigationMaintenance(self.runtime)
        navigation_status: dict[str, Any] = {}
        for policy in self.policies:
            reason = signal.reason()
            if reason:
                return {"skipped": reason}
            completed = navigation.tick(access, policy, budget=MaintenanceBudget(16, 1, 0.5),
                cancel=self.stopping, foreground=signal)
            navigation_status[policy] = asdict(completed.status)
        reason = signal.reason()
        if reason:
            return {"skipped": reason}
        semantic = None
        if self.semantic is not None:
            try:
                semantic = asdict(self.semantic.refresh(access, limit=1, timeout=5, cancel=signal))
            except MemoryFailure as failure:
                reason = signal.reason()
                if reason:
                    return {"skipped": reason}
                if failure.code in {"unauthorized", "scope_forbidden", "incompatible_contract", "service_required"}:
                    raise
                semantic = {"state": "unavailable", "error_code": failure.code}
        tick = scheduler.trigger(access, reason="idle")
        return {"skipped": tick.skipped, "scheduler": asdict(scheduler.status(access)),
            "lexical": asdict(self.runtime.lexical_status(access)), "navigation": navigation_status, "semantic": semantic}

    def run(self) -> None:
        while not self.stopping.wait(self.interval):
            try:
                update = self.tick()
                update["error"] = None
            except MemoryFailure as error:
                update = {"error": error.code}
            except Exception:
                update = {"error": "service_maintenance_failed"}
            with self.lock:
                self.state.update(update)
                self.state["ticks"] += 1


class _MaintenanceSignal(threading.Event):
    def __init__(self, scheduler: ConsolidationScheduler, access: Access, stopping: threading.Event):
        super().__init__()
        self.scheduler, self.access, self.stopping = scheduler, access, stopping

    def reason(self) -> str | None:
        if self.stopping.is_set():
            return "stopping"
        # This is polled during inference and SQLite progress. Avoid full backlog/index counts here.
        self.scheduler.runtime._authorize(self.access)
        with self.scheduler.runtime._connection() as db:
            db.execute("BEGIN")
            self.scheduler._schema(db)
            if self.scheduler._settings(db).paused:
                return "paused"
            return "foreground" if self.scheduler._foreground_count(db) else None

    def is_set(self) -> bool:
        return self.reason() is not None
