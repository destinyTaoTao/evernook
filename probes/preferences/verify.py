"""Public-interface synthetic policy probe or explicitly configured offline model experiment."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import threading

from evernook import CandidateWorkflow, Event, ExtractionSource, MemoryRuntime, Source, initialize
from evernook.extraction import ExtractorBackend, LocalQwen
from evernook.paths import external
from evernook.preferences import PreferenceProcessor


CASES = (
    ("我偏好简短回答。", "response_style"),
    ("我希望回答先给结论再分点。", "response_style"),
    ("我喜欢表格回答。", "response_style"),
    ("我常用 Python。", "technical_tool"),
    ("我常用 Java 21。", "technical_tool"),
    ("我喜欢观鸟。", "general_interest"),
    ("我爱好绘画。", "general_interest"),
    ("朋友说：我喜欢观鸟。", None),
    ("如果我喜欢观鸟，就去公园。", None),
    ("我不喜欢观鸟。", None),
    ("扮演角色：我喜欢观鸟。", None),
    ("助手猜测我喜欢观鸟。", None),
    ("我常用 Insulin。", None),
    ("我喜欢观鸟，我的病史是偏头痛。", None),
    ("我常用 Python 并授权自动付款。", None),
    ("我偏好尚未识别的新形式。", None),
)


class SyntheticSelector:
    processor_version = "synthetic-whole-source-policy-probe-v1"

    def select(self, sources: tuple[ExtractionSource, ...], *, timeout: float,
               cancel: threading.Event | None) -> object:
        return {"selections": [{"evidence": [{"source": index, "quote": item.message.event.text}],
                                "provenance": "user_statement", "disputed": False}
                               for index, item in enumerate(sources)]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--synthetic-selector", action="store_true")
    mode.add_argument("--extraction-config", type=Path)
    args = parser.parse_args()
    output = external(args.output)
    output.mkdir()  # Never overwrite a previous experiment.
    config, access = initialize(output / "runtime", client_id="synthetic", entry="synthetic")
    runtime = MemoryRuntime(config)
    backend: ExtractorBackend = SyntheticSelector() if args.synthetic_selector else LocalQwen.from_file(args.extraction_config)
    ids = tuple(runtime.submit(access, Event(Source("synthetic", "preference-probe", "main", str(index)),
        "user", 1, None, "complete", text)).message_id for index, (text, _) in enumerate(CASES))
    processor = PreferenceProcessor(runtime, backend)
    reports = []
    checks = []
    for index, (message_id, (_, expected)) in enumerate(zip(ids, CASES)):
        report = processor.process(access, (message_id,))
        candidates = CandidateWorkflow(runtime).candidates(access, report.run.run_id)
        actual = candidates[0].draft.preference_category if len(candidates) == 1 else None
        check = {"case": index, "expected": expected, "actual": actual,
                 "passed": (actual == expected and report.run.state == "succeeded" and len(candidates) <= 1)}
        reports.append(asdict(report))
        checks.append(check)
        print(json.dumps({"case": index, "state": report.run.state, "passed": check["passed"]}), flush=True)
    elapsed = sum(report["elapsed_seconds"] for report in reports)
    result = {"scope": "synthetic_policy" if args.synthetic_selector else "configured_local_model",
        "quality_admission": "pending", "backend_processor_version": backend.processor_version,
        "reports": reports, "checks": checks, "passed": sum(1 for item in checks if item["passed"]),
        "expected": len(checks), "configured_model_attempt": not args.synthetic_selector,
        "inference_succeeded": not args.synthetic_selector and all(report["run"]["state"] == "succeeded" for report in reports),
        "environment": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                        "python": platform.python_version(), "processor": platform.processor()},
        "resources": {"elapsed_seconds": elapsed, "peak_model_memory_bytes": None,
                      "limits": {"source_count": 16, "source_codepoints": 16384, "request_seconds": 120},
                      "note": "16 independent workflow calls; model reloads each call. Setup excluded; no p95, peak memory or hardware admission claim."},
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scope": result["scope"], "passed": result["passed"], "expected": len(checks),
                      "elapsed_seconds": elapsed, "quality_admission": "pending"}))
    return 0 if all(item["passed"] for item in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
