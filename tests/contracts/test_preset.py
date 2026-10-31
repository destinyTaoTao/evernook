from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import (COMPANION_PRESET, HARNESS_DEFAULT_SCOPES, MemoryFailure, MemoryRuntime, Source, apply_preset,
                      initialize, pair_client, show_preset)
from evernook.consolidation import ConsolidationWorkflow
from evernook.scheduler import ConsolidationScheduler


class PresetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-preset-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, _ = initialize(self.directory / "data", client_id="owner", entry="owner")
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / "mcp-access-owner.json"
        pair_client(self.config, client_id="owner", entry="owner", access_path=self.owner_path)
        self.access = self.runtime.access_from_file(self.owner_path)
        self.harness_path = self.config.parent / "mcp-access-harness.json"
        self.harness = pair_client(self.config, client_id="harness", entry="harness",
                                   access_path=self.harness_path, scopes=HARNESS_DEFAULT_SCOPES)

    def test_apply_defaults_match_documented_recommendations_and_set_side_effects(self) -> None:
        view = apply_preset(self.access, self.runtime)
        self.assertEqual(view.name, "companion")
        self.assertEqual(view.values, COMPANION_PRESET)
        self.assertEqual(view.values["timezone"], "Asia/Shanghai")
        self.assertEqual(view.values["must_read_tokens"], 4000)
        self.assertEqual(view.values["session_tokens"], 16000)
        self.assertEqual(view.declared_budgets, (4000, 16000, 1000))
        self.assertEqual(view.overridden, ())
        self.assertTrue(view.applied)
        scheduler = ConsolidationScheduler(self.runtime).status(self.access)
        self.assertEqual((scheduler.max_sources_per_run, scheduler.max_runs_per_trigger, scheduler.paused), (8, 1, False))
        tier = self.runtime._require_scope(self.access, "maintain", "test_probe")  # auth side effect only
        from evernook.consolidation import ConsolidationWorkflow
        # Tier side effect: consolidation configure wrote review (visible via a manual-then-read cycle).
        workflow = ConsolidationWorkflow(self.runtime)
        workflow.configure(self.access, tier="auto")
        applied = apply_preset(self.access, self.runtime)
        self.assertEqual(applied.values["consolidation_tier"], "review")

    def test_overrides_win_and_are_validated(self) -> None:
        view = apply_preset(self.access, self.runtime, overrides={
            "consolidation_tier": "auto", "must_read_tokens": 2000, "timezone": "Asia/Shanghai"})
        self.assertEqual(view.overridden, ("consolidation_tier", "must_read_tokens", "timezone"))
        self.assertEqual(view.values["must_read_tokens"], 2000)
        self.assertEqual(view.values["session_tokens"], 16000)
        stored = show_preset(self.access, self.runtime)
        self.assertEqual(stored.values["must_read_tokens"], 2000)
        self.assertEqual(stored.values["consolidation_tier"], "auto")
        with self.assertRaisesRegex(MemoryFailure, "invalid_preset_override"):
            apply_preset(self.access, self.runtime, overrides={"mystery_key": 1})
        with self.assertRaisesRegex(MemoryFailure, "invalid_preset_override"):
            apply_preset(self.access, self.runtime, overrides={"recent_days": 0})
        with self.assertRaisesRegex(MemoryFailure, "invalid_preset_budgets"):
            apply_preset(self.access, self.runtime, overrides={"must_read_tokens": 30000})
        with self.assertRaisesRegex(MemoryFailure, "invalid_preset_override"):
            apply_preset(self.access, self.runtime, overrides={"session_tokens": "16000"})
        with self.assertRaisesRegex(MemoryFailure, "unknown_preset"):
            apply_preset(self.access, self.runtime, name="hardcore")
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            apply_preset(self.harness, self.runtime)
        view = show_preset(self.access, self.runtime)
        self.assertTrue(view.applied)

    def test_unapplied_show_reports_recommendations_only(self) -> None:
        view = show_preset(self.access, self.runtime)
        self.assertFalse(view.applied)
        self.assertEqual(view.values, COMPANION_PRESET)

    def test_cli_apply_and_show(self) -> None:
        apply_process = subprocess.run([sys.executable, "-m", "evernook.cli", "preset-apply", "--config", str(self.config),
                                        "--access", str(self.owner_path)],
            input=json.dumps({"overrides": {"idle_seconds": 600}}, ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=30, cwd=Path(__file__).resolve().parents[2])
        applied = json.loads(apply_process.stdout.decode("utf-8"))
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["result"]["values"]["idle_seconds"], 600)
        show_process = subprocess.run([sys.executable, "-m", "evernook.cli", "preset-show", "--config", str(self.config),
                                       "--access", str(self.owner_path)], input=b"{}", capture_output=True, timeout=30,
                                      cwd=Path(__file__).resolve().parents[2])
        shown = json.loads(show_process.stdout.decode("utf-8"))
        self.assertTrue(shown["ok"], shown)
        self.assertEqual(shown["result"]["overridden"], ["idle_seconds"])
        self.assertEqual(shown["result"]["declared_budgets"], [4000, 16000, 1000])


if __name__ == "__main__":
    unittest.main()
