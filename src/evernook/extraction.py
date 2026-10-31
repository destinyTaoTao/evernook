"""Bounded offline extractive candidate processing. Never accepts model assertions as facts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import subprocess
import threading
import time
from typing import Protocol

from .candidates import CandidateDraft, CandidateRun, CandidateWorkflow, EvidenceSpan, ExtractionSource
from .models import Access, MemoryFailure
from .paths import external
from .runtime import MemoryRuntime, _json, _load, _unique_object
from .safety import credential_signal


class ExtractorBackend(Protocol):
    processor_version: str

    def select(self, sources: tuple[ExtractionSource, ...], *, timeout: float,
               cancel: threading.Event | None) -> object: ...


@dataclass(frozen=True)
class ExtractionReport:
    run: CandidateRun
    error_code: str | None
    automatic_acceptance: bool = False


_FILES = {
    "config.json": "8a04114ba59cc42b47d804d35d1d5c61d746ae4634f41f796768c6e302d39b9e",
    "generation_config.json": "9e9e031ae8bca36eefcdcdd0b35d83c61baa01e31d68d5f5a961c9ca1a4b95bf",
    "tokenizer.json": "e7a95fce95bf5b0946d0ddb3f9d7caa030b7e850bbe92b0edb26bcf563e9f3d5",
    "tokenizer_config.json": "7c5194eecfd95a4dcbd6b4b502af785d2541fd65a388d1cae7ad43693c67b802",
    "special_tokens_map.json": "76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd",
    "added_tokens.json": "c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680",
    "merges.txt": "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
    "vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    "onnx/model_q4.onnx": "d43d836fc5e240df9013733ccd214972c5d21bd9ec47e574e4f1e359cf90aed0",
}


def _remaining(deadline: float, cancel: threading.Event | None) -> float:
    if cancel is not None and cancel.is_set():
        raise MemoryFailure("extraction_canceled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MemoryFailure("extraction_timeout")
    return remaining


def _admit(sources: tuple[ExtractionSource, ...]) -> None:
    if not isinstance(sources, tuple) or not 1 <= len(sources) <= 16 or any(not isinstance(item, ExtractionSource) for item in sources):
        raise MemoryFailure("extraction_input_not_admitted")
    if (sum(len(item.message.event.text) for item in sources) > 16384
            or any(not item.message.receipt.original_saved or item.message.event.completion != "complete"
                   or credential_signal(item.message.event.text) for item in sources)):
        raise MemoryFailure("extraction_input_not_admitted")


class LocalQwen:
    """Pinned R0 candidate, experimental and NOT quality-admitted. No automatic downloader."""
    processor_version: str

    def __init__(self, *, node: Path, transformers_dir: Path, model_dir: Path):
        if not node.is_absolute() or not transformers_dir.is_absolute():
            raise MemoryFailure("invalid_extraction_config")
        self.node, self.transformers_dir, self.model_dir = node.resolve(), transformers_dir.resolve(), external(model_dir)
        try:
            self._worker_digest = hashlib.sha256(Path(__file__).with_name("extraction_worker.mjs").read_bytes()).hexdigest()
        except OSError:
            raise MemoryFailure("extraction_unavailable") from None
        self.processor_version = "qwen3-0.6b-extractive-v1:" + hashlib.sha256(
            (_json(_FILES) + ":transformers4.2.0:ort1.24.3:" + self._worker_digest).encode()).hexdigest()

    @classmethod
    def from_file(cls, path: Path) -> LocalQwen:
        value = _load(external(path))
        if (set(value) != {"schema_version", "profile", "node", "transformers_dir", "model_dir"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["profile"] != "qwen3-0.6b-experimental-extractive-v1"
                or any(not isinstance(value[key], str) for key in ("node", "transformers_dir", "model_dir"))):
            raise MemoryFailure("invalid_extraction_config")
        return cls(node=Path(value["node"]), transformers_dir=Path(value["transformers_dir"]), model_dir=Path(value["model_dir"]))

    def select(self, sources: tuple[ExtractionSource, ...], *, timeout: float, cancel: threading.Event | None) -> object:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise MemoryFailure("invalid_extraction_request")
        _admit(sources)
        deadline = time.monotonic() + timeout
        process: subprocess.Popen[bytes] | None = None
        try:
            for name, expected in _FILES.items():
                _remaining(deadline, cancel)
                path = (self.model_dir / name).resolve()
                if not path.is_relative_to(self.model_dir):
                    raise MemoryFailure("extraction_integrity_mismatch")
                with path.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                        raise MemoryFailure("extraction_integrity_mismatch")
            payload = _json({"model_dir": str(self.model_dir), "transformers_dir": str(self.transformers_dir),
                "sources": [{"role": item.message.event.role, "text": item.message.event.text} for item in sources]}).encode()
            _remaining(deadline, cancel)
            if hashlib.sha256(Path(__file__).with_name("extraction_worker.mjs").read_bytes()).hexdigest() != self._worker_digest:
                raise MemoryFailure("extraction_integrity_mismatch")
            process = subprocess.Popen([str(self.node), str(Path(__file__).with_name("extraction_worker.mjs"))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            pending_input: bytes | None = payload
            while True:
                try:
                    output, _ = process.communicate(input=pending_input, timeout=min(0.1, _remaining(deadline, cancel)))
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
            if len(output) > 1048576:
                raise MemoryFailure("extraction_unavailable")
            value = json.loads(output.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
            if process.returncode:
                raise MemoryFailure("extraction_unavailable")
            if (not isinstance(value, dict) or set(value) != {"selection_json", "network_attempts"}
                    or type(value["network_attempts"]) is not int or value["network_attempts"] != 0):
                raise MemoryFailure("extraction_invalid_output")
            return self.parse_selection(value["selection_json"])
        except (OSError, ValueError, TypeError):
            raise MemoryFailure("extraction_unavailable") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    @staticmethod
    def parse_selection(value: object) -> object:
        if not isinstance(value, str) or len(value) > 65536:
            raise MemoryFailure("extraction_invalid_output")
        text = value.strip()
        # Accept a single whole JSON code fence; no prose repair, partial JSON or duplicate keys.
        for prefix in ("```json\n", "```\n"):
            if text.startswith(prefix) and text.endswith("\n```"):
                text = text[len(prefix):-4]
                break
        try:
            result: object = json.loads(text, object_pairs_hook=_unique_object)
            return result
        except (ValueError, UnicodeError):
            raise MemoryFailure("extraction_invalid_output") from None


class ExtractionProcessor:
    def __init__(self, runtime: MemoryRuntime, backend: ExtractorBackend):
        self.runtime, self.workflow, self.backend = runtime, CandidateWorkflow(runtime), backend

    @staticmethod
    def _drafts(selection: object, sources: tuple[ExtractionSource, ...]) -> tuple[CandidateDraft, ...]:
        if not isinstance(selection, dict) or set(selection) != {"selections"} or not isinstance(selection["selections"], list) or len(selection["selections"]) > 32:
            raise MemoryFailure("extraction_invalid_output")
        drafts = []
        for item in selection["selections"]:
            if (not isinstance(item, dict) or set(item) != {"evidence", "provenance", "disputed"}
                    or not isinstance(item["evidence"], list) or not 1 <= len(item["evidence"]) <= 16
                    or type(item["disputed"]) is not bool or not isinstance(item["provenance"], str)):
                raise MemoryFailure("extraction_invalid_output")
            spans: list[EvidenceSpan] = []
            for selected in item["evidence"]:
                if (not isinstance(selected, dict) or set(selected) != {"source", "quote"}
                        or type(selected["source"]) is not int or not 0 <= selected["source"] < len(sources)
                        or not isinstance(selected["quote"], str) or not selected["quote"] or len(selected["quote"]) > 4096):
                    raise MemoryFailure("extraction_invalid_output")
                source = sources[selected["source"]]
                text, quote = source.message.event.text, selected["quote"]
                start = text.find(quote)
                if start < 0 or text.find(quote, start + 1) >= 0:
                    raise MemoryFailure("extraction_quote_not_unique")
                spans.append(EvidenceSpan(source.message.receipt.message_id, source.state_version,
                    source.message.receipt.representation, source.message.receipt.content_version, start, start + len(quote), quote))
            # Only source words can become candidate text. No generated summary can invent a fact here.
            draft = CandidateDraft("\n".join(span.expected_text for span in spans), item["provenance"], tuple(spans), item["disputed"])
            CandidateWorkflow._validate(draft, sources)
            drafts.append(draft)
        return tuple(drafts)

    def process(self, access: Access, source_ids: tuple[str, ...], *, timeout: float = 120,
                cancel: threading.Event | None = None) -> ExtractionReport:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise MemoryFailure("invalid_extraction_request")
        deadline = time.monotonic() + timeout
        lease = self.workflow.begin(access, source_ids, processor_version=self.backend.processor_version,
                                    lease_seconds=math.ceil(timeout) + 30)
        if lease.token is None:
            return ExtractionReport(lease.status, None)
        try:
            _remaining(deadline, cancel)
            _admit(lease.sources)
            selected = self.backend.select(lease.sources, timeout=_remaining(deadline, cancel), cancel=cancel)
            drafts = self._drafts(selected, lease.sources)
            _remaining(deadline, cancel)
            completed = self.workflow.complete(access, lease.status.run_id, lease.token, drafts)
            return ExtractionReport(completed, None)
        except Exception as error:
            code = error.code if isinstance(error, MemoryFailure) else "extractor_failed"
            # Model-shaped candidate validation failures are bad output, not a crashed extractor.
            code = {"invalid_candidate": "extraction_invalid_output"}.get(code, code)
            safe = code if code in {"extraction_canceled", "extraction_timeout", "extraction_invalid_output",
                "extraction_quote_not_unique", "extraction_integrity_mismatch", "extraction_unavailable",
                "extraction_input_not_admitted", "stale_source", "candidate_provenance_mismatch", "protected_candidate"} else "extractor_failed"
            durable = {"extraction_canceled": "canceled", "extraction_timeout": "timed_out",
                "extraction_invalid_output": "invalid_output", "extraction_quote_not_unique": "invalid_output",
                "candidate_provenance_mismatch": "invalid_output", "extraction_input_not_admitted": "input_not_admitted",
                "protected_candidate": "input_not_admitted", "extraction_integrity_mismatch": "dependency_unavailable",
                "extraction_unavailable": "dependency_unavailable"}.get(safe, "extractor_failed")
            try:
                status = self.workflow.fail(access, lease.status.run_id, lease.token, error_code=durable)
            except MemoryFailure:
                status = self.workflow.status(access, lease.status.run_id)
            return ExtractionReport(status, safe)
