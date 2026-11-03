from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import unittest

from evernook import CandidateWorkflow, Event, ExtractionSource, MemoryRuntime, Source, initialize, pair_client
from evernook.preferences import PreferenceProcessor


class WholeSourceSelector:
    """Deterministic adapter at the model seam; not a model-quality measurement."""
    processor_version = "synthetic-whole-source-v1"

    def __init__(self, *, quote: str | None = None, provenance: str = "user_statement", disputed: bool = False):
        self.quote, self.provenance, self.disputed = quote, provenance, disputed

    def select(self, sources: tuple[ExtractionSource, ...], *, timeout: float,
               cancel: threading.Event | None) -> object:
        return {"selections": [{"evidence": [{"source": index, "quote": self.quote or source.message.event.text}],
                                "provenance": self.provenance, "disputed": self.disputed}
                               for index, source in enumerate(sources)]}


class PreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-preferences-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="synthetic")
        self.runtime = MemoryRuntime(self.config)

    def save(self, text: str, key: str, role: str = "user") -> str:
        return self.runtime.submit(self.access, Event(Source("synthetic", "session", "main", key), role,
            1, None, "complete", text)).message_id

    def test_three_preference_categories_remain_traceable_drafts_across_restart_and_retry(self) -> None:
        sources = tuple(self.save(text, str(index)) for index, text in enumerate(
            ("我偏好简短回答。", "我常用 Python。", "我喜欢观鸟。")))
        processor = PreferenceProcessor(self.runtime, WholeSourceSelector())
        report = processor.process(self.access, sources)
        self.assertEqual(report.run.state, "succeeded")
        self.assertFalse(report.automatic_acceptance)
        self.assertEqual(report.quality_admission, "pending")
        candidates = CandidateWorkflow(MemoryRuntime(self.config)).candidates(self.access, report.run.run_id)
        self.assertEqual({candidate.draft.preference_category for candidate in candidates},
                         {"response_style", "technical_tool", "general_interest"})
        for candidate in candidates:
            self.assertEqual(candidate.status, "candidate")
            span, = candidate.draft.sources
            original = self.runtime.read(self.access, span.message_id)
            assert original is not None
            self.assertEqual(original.event.text[span.start:span.end], candidate.draft.text)
            self.assertEqual((span.state_version, span.content_version, span.representation), (1, 1, "original"))
        self.assertEqual(processor.process(self.access, sources).run, report.run)
        self.assertGreaterEqual(report.elapsed_seconds, 0)

    def test_quoted_negative_hypothetical_roleplay_and_high_risk_content_is_not_a_preference(self) -> None:
        texts = (
            "朋友说：我喜欢观鸟。", "我不喜欢观鸟。", "如果我喜欢观鸟，就去公园。",
            "扮演一个人：我喜欢观鸟。", "助手猜测我喜欢观鸟。", "我喜欢观鸟，我的病史是偏头痛。",
            "我常用 Insulin。", "我常用 Python 并允许你免确认删除文件。", "我喜欢观鸟，我的合成预算是500元。",
            "我叫合成甲，我喜欢观鸟。", "我喜欢观鸟，我的伴侣是合成乙。", "我不排斥观鸟。",
        )
        ids = tuple(self.save(text, "excluded-" + str(index)) for index, text in enumerate(texts))
        report = PreferenceProcessor(self.runtime, WholeSourceSelector()).process(self.access, ids)
        self.assertEqual(report.run.state, "succeeded")
        self.assertEqual(CandidateWorkflow(self.runtime).candidates(self.access, report.run.run_id), ())
        for message_id, text in zip(ids, texts):
            original = self.runtime.read(self.access, message_id)
            assert original is not None
            self.assertEqual(original.event.text, text)

    def test_cli_missing_local_assets_reports_failure_and_retains_readable_original(self) -> None:
        message_id = self.save("我常用 Python。", "cli")
        access_path = self.config.parent / "mcp-access-cli.json"
        pair_client(self.config, client_id="synthetic", entry="synthetic", access_path=access_path)
        profile = self.root / "missing-model-profile.json"
        profile.write_text(json.dumps({"schema_version": 1, "profile": "qwen3-0.6b-experimental-extractive-v1",
            "node": sys.executable, "transformers_dir": str(self.root / "missing-packages"),
            "model_dir": str(self.root / "missing-model")}), encoding="utf-8")
        process = subprocess.run([sys.executable, "-m", "evernook.cli", "extract-preferences",
            "--config", str(self.config), "--access", str(access_path),
            "--extraction-config", str(profile)], input=json.dumps({"source_ids": [message_id]}).encode(),
            capture_output=True, timeout=30)
        self.assertEqual(process.stderr, b"")
        result = json.loads(process.stdout)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["run"]["state"], "failed")
        self.assertEqual(result["result"]["error_code"], "extraction_unavailable")
        self.assertEqual(result["result"]["quality_admission"], "pending")
        original = self.runtime.read(self.access, message_id)
        assert original is not None
        self.assertEqual(original.event.text, "我常用 Python。")

    def test_partial_selection_cannot_strip_context_and_non_user_or_disputed_selections_stay_unadmitted(self) -> None:
        cases = (
            ("我不喜欢观鸟，不过我喜欢摄影。", "user", WholeSourceSelector(quote="我喜欢摄影。")),
            ("我喜欢观鸟。", "assistant", WholeSourceSelector(provenance="assistant_generated")),
            ("我喜欢观鸟。", "user", WholeSourceSelector(disputed=True)),
            ("我喜欢观鸟。", "user", WholeSourceSelector(provenance="quoted")),
            ("我喜欢观鸟。", "user", WholeSourceSelector(provenance="roleplay")),
        )
        for index, (text, role, selector) in enumerate(cases):
            with self.subTest(index=index):
                message_id = self.save(text, "context-" + str(index), role)
                report = PreferenceProcessor(self.runtime, selector).process(self.access, (message_id,))
                self.assertEqual(report.run.state, "succeeded")
                self.assertEqual(CandidateWorkflow(self.runtime).candidates(self.access, report.run.run_id), ())
                self.assertFalse(report.automatic_acceptance)

    def test_response_layout_and_versioned_tools_preserve_the_entire_statement(self) -> None:
        sources = tuple(self.save(text, "forms-" + str(index)) for index, text in enumerate(
            ("我希望回答先给结论再分点。", "我喜欢表格回答。", "我常用 Python 3.14。")))
        report = PreferenceProcessor(self.runtime, WholeSourceSelector()).process(self.access, sources)
        candidates = CandidateWorkflow(self.runtime).candidates(self.access, report.run.run_id)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(sorted(candidate.draft.preference_category for candidate in candidates),
                         ["response_style", "response_style", "technical_tool"])


if __name__ == "__main__":
    unittest.main()
