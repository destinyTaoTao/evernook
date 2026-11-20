from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest


PROBE = Path(__file__).resolve().parents[2] / "probes" / "admission" / "matrix_report.py"
POLICY = PROBE.with_name("policy.json")


class ScaleMatrixReportTests(unittest.TestCase):
    def test_manifest_only_after_interruption_reports_missing_samples(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-scale-matrix-report-") as directory:
            root = Path(directory)
            policy_raw = POLICY.read_bytes()
            (root / "manifest.json").write_text(json.dumps({
                "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
            }), encoding="utf-8")

            report = self.report(root)

        self.assertFalse(report["matrix_finished"])
        self.assertFalse(report["selected_attempts_finished"])
        self.assertEqual(report["admission"], "blocked")
        self.assertTrue(all(cell["missing_samples"] == cell["expected_samples"] for cell in report["cells"]))
        self.assertTrue(all(not cell["complete_memory_p95_gate"] for cell in report["cells"]))

    def test_completed_filtered_window_does_not_mark_matrix_finished(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-scale-matrix-report-") as directory:
            root = Path(directory)
            policy_raw = POLICY.read_bytes()
            scale = json.loads(policy_raw)["scale"]
            warm_count, fresh_count = scale["warm_samples_per_cell"], scale["fresh_process_samples_per_cell"]
            (root / "manifest.json").write_text(json.dumps({
                "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
                "selected_conditions": ["idle"],
                "selected_queries": ["recent"],
            }), encoding="utf-8")
            sample = {
                "lexical_ms": 25, "context_ms": 100, "recall_phases": None,
                "lexical": {"index": {"source_messages": 100000}},
                "context": {"complete": True, "degraded": []},
            }
            attempts = [("idle-recent-warm-0.json", warm_count)]
            attempts.extend((f"idle-recent-fresh_process-{index}.json", 1) for index in range(fresh_count))
            for result, count in attempts:
                (root / result).write_text(json.dumps({"samples": [sample] * count}), encoding="utf-8")
            (root / "report.json").write_text(json.dumps({
                "results": [{"result": result, "returncode": 0, "valid_workload": True} for result, _ in attempts],
                "traffic": [],
            }), encoding="utf-8")

            report = self.report(root)

        self.assertFalse(report["matrix_finished"])
        self.assertTrue(report["selected_attempts_finished"])
        recent = next(cell for cell in report["cells"] if cell["condition"] == "idle"
                      and cell["query"] == "recent" and cell["cache"] == "warm")
        self.assertEqual(recent["valid_complete_contexts"], warm_count)
        self.assertTrue(recent["complete_memory_p95_gate"])
        self.assertEqual(sum(cell["observed_samples"] for cell in report["cells"]), warm_count + fresh_count)
        self.assertEqual(report["admission"], "blocked")

    def test_final_receipt_with_missing_attempts_keeps_matrix_unfinished(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-scale-matrix-report-") as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps({
                "policy_sha256": hashlib.sha256(POLICY.read_bytes()).hexdigest(),
            }), encoding="utf-8")
            (root / "report.json").write_text(json.dumps({"results": [], "traffic": []}), encoding="utf-8")

            report = self.report(root)

        self.assertFalse(report["selected_attempts_finished"])
        self.assertFalse(report["matrix_finished"])

    def report(self, root: Path) -> dict[str, Any]:
        output = root / "summary.json"
        run = subprocess.run([sys.executable, "-B", str(PROBE), "--matrix", str(root), "--output", str(output)],
                             capture_output=True, check=False, timeout=20)
        self.assertEqual(run.returncode, 2, run.stderr.decode(errors="replace"))
        report: dict[str, Any] = json.loads(output.read_bytes())
        return report
