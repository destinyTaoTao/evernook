from __future__ import annotations

import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import evernook
from evernook.evaluation import EvaluationError
from evernook.learning_evaluation import ARMS, load_suite, score
from probes.admission.learning import implementation_identity


SUITE = Path(__file__).resolve().parents[2] / "eval" / "zh-learning-v1"


class LearningEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = load_suite(SUITE)
        self.configuration = {"model": "none", "processor": "test-only", "budget": {"candidate_k": 10},
                              "policy_sha256": self.suite.manifest["policy_sha256"], "implementation": "test"}

    def observations(self) -> dict[str, object]:
        return {"suite_hash": self.suite.suite_hash, "configuration": self.configuration, "rows": []}

    def row(self, payload: dict[str, object]) -> dict[str, object]:
        return {"case_id": "old-initial", "arm": "raw", "payload": payload,
                "measurements": {"elapsed_ms": 1.0, "private_bytes": None, "remaining_children": None}}

    def test_empty_run_keeps_every_arm_and_phase_missing(self) -> None:
        report = score(self.suite, self.observations())
        self.assertEqual(report["outcome"], "incomplete")
        self.assertEqual(report["admission"], "pending")
        for arm in ARMS:
            self.assertEqual(report["arms"][arm]["retrieval"]["missing"], len(self.suite.cases))
            self.assertIsNone(report["latency"][arm]["p95_ms"])

    def test_retrieval_partial_recall_is_separate_from_full_case_pass(self) -> None:
        row = {**self.row({"retrieval": {"source_ids": ["trip-a"]}}), "case_id": "multi-initial"}
        observations = {**self.observations(), "rows": [row]}
        report = score(self.suite, observations)
        self.assertEqual(report["arms"]["raw"]["retrieval"]["failed"], 1)
        self.assertEqual(report["candidate_recall_at_10"]["raw"], 0.5)

    def test_duplicate_unknown_and_wrong_identity_cannot_inflate_metrics(self) -> None:
        for ids in (["old", "old"], ["unknown"]):
            with self.assertRaises(EvaluationError):
                score(self.suite, {**self.observations(), "rows": [self.row({"retrieval": {"source_ids": ids}})]})
        row = self.row({})
        for rows in ([row, row], [{**row, "arm": "imaginary"}], [{**row, "case_id": "missing"}]):
            with self.assertRaises(EvaluationError):
                score(self.suite, {**self.observations(), "rows": rows})
        with self.assertRaises(EvaluationError):
            score(self.suite, {**self.observations(), "suite_hash": "other"})

    def test_valid_source_id_with_wrong_quote_or_version_fails_evidence(self) -> None:
        source = self.suite.sources["old"]
        citation = {"source_id": "old", "start": 0, "end": len(source["text"]), "quote": source["text"],
                    "state_version": 1, "content_version": 1}
        for change in ({"quote": "invented"}, {"state_version": 2}, {"start": True}, {"end": 0}):
            report = score(self.suite, {**self.observations(), "rows": [self.row({"evidence": {"citations": [{**citation, **change}]}})]})
            self.assertEqual(report["arms"]["raw"]["evidence"]["failed"], 1)
        report = score(self.suite, {**self.observations(), "rows": [self.row({"evidence": {"citations": [citation]}})]})
        self.assertEqual(report["arms"]["raw"]["evidence"]["passed"], 1)

    def test_legal_but_irrelevant_quote_does_not_satisfy_required_evidence(self) -> None:
        citation = {"source_id": "old", "start": 0, "end": 4, "quote": self.suite.sources["old"]["text"][:4],
                    "state_version": 1, "content_version": 1}
        report = score(self.suite, {**self.observations(), "rows": [self.row({"evidence": {"citations": [citation]}})]})
        self.assertEqual(report["arms"]["raw"]["evidence"]["failed"], 1)

    def test_missing_measurements_are_not_zeros_and_invalid_values_rejected(self) -> None:
        report = score(self.suite, {**self.observations(), "rows": [self.row({})]})
        self.assertIsNone(report["resources"]["peak_private_bytes"])
        self.assertIsNone(report["resources"]["remaining_children"])
        for value in (-1, float("nan"), True):
            row = {**self.row({}), "measurements": {"elapsed_ms": value}}
            with self.assertRaises(EvaluationError):
                score(self.suite, {**self.observations(), "rows": [row]})

    def test_configuration_is_bound_and_unexpected_payload_not_ignored(self) -> None:
        bad = copy.deepcopy(self.configuration)
        bad["policy_sha256"] = "other"
        with self.assertRaises(EvaluationError):
            score(self.suite, {**self.observations(), "configuration": bad})
        with self.assertRaises(EvaluationError):
            score(self.suite, {**self.observations(), "rows": [self.row({"bogus": {}})]})

    def test_all_oracle_positive_controls_pass_but_never_grant_admission(self) -> None:
        rows = []
        for arm in ARMS:
            for case_id, case in self.suite.cases.items():
                expected = copy.deepcopy(case["expected"])
                citations = []
                for source_id, quote in expected["evidence"]["required_quotes"].items():
                    text = self.suite.sources[source_id]["text"]
                    start = text.index(quote)
                    citations.append({"source_id": source_id, "start": start, "end": start + len(quote), "quote": quote,
                                      "state_version": 1, "content_version": 1})
                expected["evidence"] = {"citations": citations}
                expected["retrieval"] = {"source_ids": expected["retrieval"]["required"]}
                expected["host_answer"]["citations"] = copy.deepcopy(citations)
                rows.append({"case_id": case_id, "arm": arm, "payload": expected, "measurements": {}})
        report = score(self.suite, {**self.observations(), "rows": rows})
        self.assertEqual(report["outcome"], "passed")
        self.assertEqual(report["totals"]["missing"], 0)
        self.assertEqual(report["admission"], "pending")

    def test_tampering_either_asset_or_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "eval" / "zh-learning-v1"
            shutil.copytree(SUITE, target)
            policy = root / "probes" / "admission" / "policy.json"
            policy.parent.mkdir(parents=True)
            policy.write_bytes((SUITE.parents[1] / "probes" / "admission" / "policy.json").read_bytes())
            for filename in ("corpus.json", "expectations.json"):
                path = target / filename
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(EvaluationError, "learning_hash_mismatch"):
                    load_suite(target)
                path.write_bytes(original)
            policy.write_bytes(b"{}")
            with self.assertRaisesRegex(EvaluationError, "learning_policy_mismatch"):
                load_suite(target)

    def test_source_control_and_false_auto_accept_are_scored_as_failures(self) -> None:
        payload = {"retrieval": {"source_ids": ["suppressed"]},
                   "automatic": {"decision": "accept", "value": "photography", "source_ids": ["suppressed"]}}
        row = {**self.row(payload), "case_id": "control-continued"}
        report = score(self.suite, {**self.observations(), "rows": [row]})
        self.assertEqual(report["arms"]["raw"]["retrieval"]["failed"], 1)
        self.assertEqual(report["automatic_updates"]["raw"]["false_update_rate"], 1)
        self.assertEqual(report["categories"]["raw"]["source_suppression"]["retrieval"]["failed"], 1)

    def test_forbidden_literal_is_checked_even_if_an_oracle_were_erroneously_permissive(self) -> None:
        suite = copy.deepcopy(self.suite)
        payload = {"decision": "answer", "value": suite.manifest["forbidden_literals"][0], "source_ids": []}
        suite.cases["old-initial"]["expected"]["host_answer"] = payload
        report = score(suite, {**self.observations(), "rows": [self.row({"host_answer": payload})]})
        self.assertEqual(report["arms"]["raw"]["host_answer"]["failed"], 1)

    def test_host_requires_its_own_actual_answer_and_verified_citations(self) -> None:
        expected = copy.deepcopy(self.suite.cases["old-initial"]["expected"]["host_answer"])
        for payload in (expected, {**expected, "citations": []}, {**expected, "answer_text": "另加没有来源的事实", "citations": []}):
            report = score(self.suite, {**self.observations(), "rows": [self.row({"host_answer": payload})]})
            self.assertEqual(report["arms"]["raw"]["host_answer"]["failed"], 1)

    def test_full_history_fixture_crosses_the_old_fixed_candidate_boundary(self) -> None:
        case = self.suite.cases["position-end"]
        available = case["available_sources"]
        self.assertGreater(len(available), 10000)
        additions = next(step["add"] for step in self.suite.steps if step["scenario"] == "position" and step["phase"] == "initial")
        self.assertGreater(additions.index("position-last"), 10000)

    def test_recall_reports_coverage_for_selective_submissions(self) -> None:
        report = score(self.suite, {**self.observations(), "rows": [self.row({"retrieval": {"source_ids": ["old"]}})]})
        self.assertEqual(report["candidate_recall_at_10"]["raw"], 1)
        self.assertEqual(report["retrieval_coverage"]["raw"]["observed"], 1)
        self.assertLess(report["retrieval_coverage"]["raw"]["fraction"], .1)

    def test_revocation_and_physical_purge_are_distinct_fixture_actions(self) -> None:
        actions = {control["action"] for step in self.suite.steps for control in step["controls"]}
        self.assertIn("revoke", actions)
        self.assertIn("purge", actions)
        self.assertNotIn("delete", actions)

    def test_temporal_fixture_explicitly_supports_continuous_residence(self) -> None:
        self.assertIn("一直", self.suite.sources["home-old"]["text"])

    def test_collector_identity_follows_the_imported_package_not_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "__init__.py"
            package.write_text("# synthetic installed package", encoding="utf-8")
            with patch.object(evernook, "__file__", str(package)):
                first = implementation_identity()
                self.assertEqual(first["package_root"], str(root.resolve()))
                package.write_text("# another installed version", encoding="utf-8")
                self.assertNotEqual(first["sha256"], implementation_identity()["sha256"])


if __name__ == "__main__":
    unittest.main()
