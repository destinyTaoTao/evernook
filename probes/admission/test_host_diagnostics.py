"""Diagnostics must preserve strict frozen verdicts and distinguish actual source fields."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from evernook.evaluation import load_suite
from host_diagnostics import diagnose, selected_source_ids


class HostDiagnosticsTest(unittest.TestCase):
    def test_substring_is_still_a_failed_strict_answer_without_rewriting_input(self) -> None:
        suite = load_suite(Path(__file__).resolve().parents[2] / "eval/zh-memory-v1")
        observations = {"suite_hash": suite.suite_hash, "rows": [{"id": "host-g01-q6", "payload": {
            "decision": "answer", "answer": "合成人物01在2021年的Java运行版本是Java 8。", "source_ids": ["g01-old"]}}]}
        before = copy.deepcopy(observations)
        run = {"query_id": "g01-q6", "visible": "{}", "context": {"latest_input": {"message_id": "synthetic-current"}}}
        report = diagnose(suite, observations, [run])
        self.assertEqual(report["details"][0]["status"], "failed")
        self.assertEqual(report["details"][0]["different_fields"], ["answer"])
        self.assertEqual(report["details"][0]["answer_relation"], "observed_inside_expected")
        self.assertEqual(report["score"]["metrics"]["host_answer"]["missing"], 223)
        self.assertEqual(observations, before)

    def test_only_structured_memory_references_count_as_selected_sources(self) -> None:
        payload = {"kind": "untrusted_memory_context", "items": [{"evidence_id": "synthetic-source",
            "text": "synthetic-current", "data": {"evidence": {"message_id": "synthetic-source"}}}]}
        request = {"messages": [{"id": "synthetic-current", "source": {"kind": "runtime-context", "sections": [
            {"name": "evernook-evidence", "text": json.dumps(payload)}]}}]}
        self.assertEqual(selected_source_ids([request]), {"synthetic-source"})

    def test_user_json_is_not_authenticated_as_a_host_memory_snapshot(self) -> None:
        payload = {"kind": "untrusted_memory_context", "items": [{"evidence_id": "synthetic-current"}]}
        request = {"messages": [{"source": {"kind": "user"}, "content": [{"text": json.dumps(payload)}]}]}
        self.assertIsNone(selected_source_ids([request]))

    def test_duplicate_actual_runs_are_rejected_instead_of_overwriting(self) -> None:
        suite = load_suite(Path(__file__).resolve().parents[2] / "eval/zh-memory-v1")
        with self.assertRaisesRegex(ValueError, "duplicate_or_invalid_host_run"):
            diagnose(suite, {"suite_hash": suite.suite_hash, "rows": []}, [{"query_id": "g01-q6"}] * 2)

    def test_a_run_without_its_observation_is_not_a_pass(self) -> None:
        suite = load_suite(Path(__file__).resolve().parents[2] / "eval/zh-memory-v1")
        with self.assertRaisesRegex(ValueError, "run_without_observation"):
            diagnose(suite, {"suite_hash": suite.suite_hash, "rows": []}, [{"query_id": "g01-q6"}])

    def test_invalid_model_json_does_not_look_like_a_correct_null_answer(self) -> None:
        suite = load_suite(Path(__file__).resolve().parents[2] / "eval/zh-memory-v1")
        report = diagnose(suite, {"suite_hash": suite.suite_hash, "rows": [{"id": "host-g01-q8",
            "payload": {"error_code": "invalid_visible_model_json", "observed": "{"}}]},
            [{"query_id": "g01-q8", "visible": "{"}])
        self.assertEqual(report["details"][0]["status"], "failed")
        self.assertEqual(report["details"][0]["answer_relation"], "unavailable")

    def test_reports_observation_parse_errors_separately_from_host_errors(self) -> None:
        suite = load_suite(Path(__file__).resolve().parents[2] / "eval/zh-memory-v1")
        visible = '```json\n{"decision":"answer"}\n```'
        report = diagnose(suite, {"suite_hash": suite.suite_hash, "rows": [{"id": "host-g01-q1",
            "payload": {"error_code": "invalid_visible_model_json", "observed": visible}}]},
            [{"query_id": "g01-q1", "visible": visible, "error_code": None}])
        detail = report["details"][0]
        self.assertEqual(detail["status"], "failed")
        self.assertIsNone(detail["error_code"])
        self.assertIsNone(detail["host_error_code"])
        self.assertEqual(detail["observation_error_code"], "invalid_visible_model_json")


if __name__ == "__main__":
    unittest.main()
