from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evernook.evaluation import EvaluationError, load_suite, run_core, score


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-evaluation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self) -> Path:
        root = self.root / "suite"
        root.mkdir()
        corpus = {"sources": [{"id": "s1", "role": "user", "text": "合成：偏好简短回答。", "at": "2021-01-01T00:00:00+08:00"}],
                  "facts": [], "queries": [{"id": "q1", "question": "希望怎样回答？", "category": "response_style"}]}
        expectations = [{"id": "auto-q1", "dimension": "auto_acceptance", "query_id": "q1", "blocking": True,
                         "expected": {"decision": "review", "value": None, "source_ids": []}}]
        for filename, value in (("corpus.json", corpus), ("expectations.json", expectations)):
            (root / filename).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        version = {"suite_version": "synthetic-test-v1", "scorer_version": "evernook-evaluation-v1"}
        manifest = {**version, "corpus_sha256": hashlib.sha256((root / "corpus.json").read_bytes()).hexdigest(),
                    "expectations_sha256": hashlib.sha256((root / "expectations.json").read_bytes()).hexdigest(),
                    "version_sha256": hashlib.sha256(json.dumps(version, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    "human_review": {"state": "pending", "reviewer": None, "query_ids": []},
                    "model_hardware_gate": "pending", "forbidden_literals": []}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_prohibited_acceptance_blocks_the_run_instead_of_being_hidden_by_an_average(self) -> None:
        suite = load_suite(self.fixture())
        observations = {"suite_hash": suite.suite_hash, "rows": [{"id": "auto-q1", "payload":
            {"decision": "accept", "value": "简短回答", "source_ids": ["s1"]}}]}
        report = score(suite, observations)
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["blocking_failures"], ["auto-q1"])
        self.assertEqual(report["admission"], "pending")

    def test_missing_results_are_incomplete_and_unmeasured_dimensions_have_no_success_score(self) -> None:
        suite = load_suite(self.fixture())
        report = score(suite, {"suite_hash": suite.suite_hash, "rows": []})
        self.assertEqual(report["outcome"], "incomplete")
        self.assertEqual(report["metrics"]["auto_acceptance"], {"expected": 1, "passed": 0, "failed": 0, "missing": 1})
        self.assertEqual(report["metrics"]["host_answer"]["expected"], 0)
        self.assertEqual(report["admission"], "pending")

    def test_corpus_or_expected_content_changes_invalidate_the_frozen_hash(self) -> None:
        root = self.fixture()
        (root / "expectations.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "evaluation_hash_mismatch"):
            load_suite(root)

    def test_duplicate_unknown_and_wrong_suite_results_cannot_be_scored_as_success(self) -> None:
        suite = load_suite(self.fixture())
        payload: dict[str, object] = {"decision": "review", "value": None, "source_ids": []}
        for rows in ([{"id": "unknown", "payload": payload}], [{"id": "auto-q1", "payload": payload}] * 2):
            with self.assertRaisesRegex(EvaluationError, "invalid_evaluation_result_id"):
                score(suite, {"suite_hash": suite.suite_hash, "rows": rows})
        with self.assertRaisesRegex(EvaluationError, "evaluation_result_identity_mismatch"):
            score(suite, {"suite_hash": "wrong", "rows": []})

    def rewrite(self, root: Path, filename: str, value: object, hash_key: str) -> None:
        (root / filename).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest[hash_key] = hashlib.sha256((root / filename).read_bytes()).hexdigest()
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_public_core_run_preserves_exact_evidence_and_retrieves_it_without_model_success_claims(self) -> None:
        root = self.fixture()
        corpus = json.loads((root / "corpus.json").read_text(encoding="utf-8"))
        corpus["queries"][0]["retrieval"] = {"query": "简短回答", "mode": "exact"}
        self.rewrite(root, "corpus.json", corpus, "corpus_sha256")
        expected = [
            {"id": "store-s1", "dimension": "storage", "query_id": None, "blocking": False,
             "expected": {"source_id": "s1", "text": "合成：偏好简短回答。", "original_saved": True}},
            {"id": "retrieve-q1", "dimension": "retrieval", "query_id": "q1", "blocking": False,
             "expected": {"source_ids": ["s1"]}},
            {"id": "auto-q1", "dimension": "auto_acceptance", "query_id": "q1", "blocking": True,
             "expected": {"decision": "review", "value": None, "source_ids": []}},
        ]
        self.rewrite(root, "expectations.json", expected, "expectations_sha256")
        suite = load_suite(root)
        observations = run_core(suite, self.root / "run")
        report = score(suite, observations)
        self.assertEqual(report["metrics"]["storage"]["passed"], 1)
        self.assertEqual(report["metrics"]["retrieval"]["passed"], 1)
        self.assertEqual(report["metrics"]["auto_acceptance"]["missing"], 1)
        self.assertEqual(report["outcome"], "incomplete")
        with self.assertRaisesRegex(EvaluationError, "evaluation_destination_exists"):
            run_core(suite, self.root / "run")

    def test_temporal_baseline_distinguishes_corrected_history_from_later_real_change(self) -> None:
        root = self.fixture()
        corpus = {"sources": [
            {"id": "old", "role": "user", "text": "合成：2021年住甲城。", "at": "2021-01-01T00:00:00+08:00"},
            {"id": "new", "role": "user", "text": "合成：2024年搬到乙城。", "at": "2024-01-01T00:00:00+08:00"},
            {"id": "fix", "role": "user", "text": "合成：更正2021年实际住丙城。", "at": "2026-01-01T00:00:00+08:00"}],
            "facts": [{"id": alias, "source_id": source, "subject": "person", "predicate": "residence", "kind": kind,
                       "valid_from": date, "replaces": replaces, "resolves": []}
                      for alias, source, kind, date, replaces in (
                          ("f-old", "old", "assert", "2021-01-01T00:00:00+08:00", None),
                          ("f-new", "new", "change", "2024-01-01T00:00:00+08:00", "f-old"),
                          ("f-fix", "fix", "correction", "2021-01-01T00:00:00+08:00", "f-old"))],
            "queries": [{"id": key, "question": question, "category": "temporal", "temporal": {
                "subject": "person", "predicate": "residence", "valid_at": at, "known_after": known}}
                for key, question, at, known in (("past", "更正后的早年住处？", "2022-01-01T00:00:00+08:00", None),
                    ("now", "搬家后的住处？", "2025-01-01T00:00:00+08:00", None),
                    ("belief", "刚记录时认为的早年住处？", "2022-01-01T00:00:00+08:00", "f-old"))]}
        expected = [{"id": key, "dimension": "temporal", "query_id": key, "blocking": False,
                     "expected": {"state": "historical", "fact_id": fact, "text": text, "disputes": [], "plans": []}}
                    for key, fact, text in (("past", "f-fix", "合成：更正2021年实际住丙城。"),
                        ("now", "f-new", "合成：2024年搬到乙城。"), ("belief", "f-old", "合成：2021年住甲城。"))]
        self.rewrite(root, "corpus.json", corpus, "corpus_sha256")
        self.rewrite(root, "expectations.json", expected, "expectations_sha256")
        suite = load_suite(root)
        report = score(suite, run_core(suite, self.root / "temporal"))
        self.assertEqual(report["metrics"]["temporal"]["passed"], 3)
        self.assertEqual(report["outcome"], "passed")
        self.assertEqual(report["admission"], "pending")

    def test_cli_reports_incomplete_results_and_never_echoes_invalid_input_content(self) -> None:
        root = self.fixture()
        suite = load_suite(root)
        results = self.root / "results.json"
        results.write_text(json.dumps({"suite_hash": suite.suite_hash, "rows": []}), encoding="utf-8")
        command = [sys.executable, "-m", "evernook.evaluation", "score", "--suite", str(root), "--results", str(results)]
        process = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["outcome"], "incomplete")
        results.write_text('{"rows":"synthetic-untrusted-diagnostic",', encoding="utf-8")
        process = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 1)
        self.assertNotIn(b"synthetic-untrusted-diagnostic", process.stdout + process.stderr)
        self.assertEqual(json.loads(process.stdout)["error"], "invalid_evaluation_file")

    def test_forged_incomplete_human_attestation_does_not_freeze_the_dataset(self) -> None:
        root = self.fixture()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["human_review"] = {"state": "attested", "reviewer": "synthetic-reviewer", "query_ids": [], "suite_hash": "wrong"}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "incomplete_evaluation_review"):
            load_suite(root)

    def test_a_forbidden_literal_blocks_even_an_ordinary_case_and_is_not_echoed_in_the_report(self) -> None:
        root = self.fixture()
        expectations = json.loads((root / "expectations.json").read_text(encoding="utf-8"))
        expectations[0]["blocking"] = False
        self.rewrite(root, "expectations.json", expectations, "expectations_sha256")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["forbidden_literals"] = ["synthetic-safety-sentinel"]
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        suite = load_suite(root)
        report = score(suite, {"suite_hash": suite.suite_hash, "rows": [{"id": "auto-q1", "payload": {
            "decision": "accept", "value": "synthetic-safety-sentinel", "source_ids": ["s1"]}}]})
        self.assertEqual(report["blocking_failures"], ["auto-q1"])
        self.assertNotIn("synthetic-safety-sentinel", json.dumps(report))

    def test_packaged_suite_has_minimum_scenario_coverage_and_unchanged_hashes(self) -> None:
        from evernook.evaluation import coverage
        suite = load_suite(Path(__file__).resolve().parents[2] / "eval" / "zh-memory-v1")
        summary = coverage(suite)
        self.assertGreaterEqual(summary["queries"], 200)
        self.assertGreaterEqual(summary["span_days"], 1826)
        self.assertGreaterEqual(summary["correction_or_conflict_groups"], 20)
        self.assertTrue({"response_style", "technical_tool", "general_interest", "quotation", "negation", "roleplay", "no_answer", "mixed_risk"}.issubset(summary["categories"]))

    def test_changing_only_the_safety_policy_invalidates_previous_results_and_human_attestation(self) -> None:
        root = self.fixture()
        suite = load_suite(root)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["forbidden_literals"] = ["synthetic-new-canary"]
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        changed = load_suite(root)
        self.assertNotEqual(changed.suite_hash, suite.suite_hash)
        with self.assertRaisesRegex(EvaluationError, "evaluation_result_identity_mismatch"):
            score(changed, {"suite_hash": suite.suite_hash, "rows": []})
        manifest["human_review"] = {"state": "attested", "reviewer": "synthetic-reviewer", "query_ids": ["q1"], "suite_hash": suite.suite_hash}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "incomplete_evaluation_review"):
            load_suite(root)


if __name__ == "__main__":
    unittest.main()
