"""Conservative offline preference drafts; labels never constitute quality admission."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re
import threading
import time

from .candidates import CandidateDraft, ExtractionSource
from .extraction import ExtractionProcessor, ExtractionReport, ExtractorBackend
from .models import Access
from .runtime import MemoryRuntime


@dataclass(frozen=True)
class PreferenceReport(ExtractionReport):
    elapsed_seconds: float = 0
    quality_admission: str = "pending"


def _category(text: str) -> str | None:
    patterns = (
        ("response_style", r"我(?:偏好|喜欢|希望)(?:(?:简短|详细|分点|分段|表格|列表)(?:的)?(?:回答|回复|答复)|(?:回答|回复|答复)(?:简短|详细|分点|分段|用表格|用列表|先给结论再分点))[。.!！]?"),
        ("technical_tool", r"我(?:常用|习惯用|偏好用)\s*(?i:Python|Java|JavaScript|TypeScript|Rust|Go|C\+\+|C#|Kotlin|Spring|Git|VS Code|Visual Studio Code|IntelliJ IDEA|Vim|Emacs|PostgreSQL|MySQL|SQLite|Docker|Linux|Windows)(?:\s*[0-9]+(?:\.[0-9]+){0,2})?[。.!！]?"),
        ("general_interest", r"我(?:喜欢|爱好)(?:观鸟|摄影|绘画|书法|阅读|园艺|徒步|跑步|游泳|音乐|围棋|象棋|烘焙|天文)[。.!！]?"),
    )
    for category, pattern in patterns:
        if re.fullmatch(pattern, text.strip()):
            return category
    return None


class PreferenceProcessor(ExtractionProcessor):
    """Reuse leases/commit/recovery, retaining only complete, recognized user statements."""
    def __init__(self, runtime: MemoryRuntime, backend: ExtractorBackend):
        super().__init__(runtime, backend)
        self._version = "preference-v1:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + ":" + hashlib.sha256(
            backend.processor_version.encode("utf-8")).hexdigest()

    @property
    def processor_version(self) -> str:
        return self._version

    @staticmethod
    def _drafts(selection: object, sources: tuple[ExtractionSource, ...]) -> tuple[CandidateDraft, ...]:
        drafts = ExtractionProcessor._drafts(selection, sources)
        originals = {item.message.receipt.message_id: item.message.event.text for item in sources}
        result = []
        for draft in drafts:
            if draft.provenance != "user_statement" or draft.disputed or len(draft.sources) != 1:
                continue
            span, = draft.sources
            # A selected substring must not discard quote/negation/high-risk context.
            if span.start != 0 or draft.text != originals[span.message_id]:
                continue
            category = _category(draft.text)
            if category is not None:
                result.append(replace(draft, preference_category=category))
        return tuple(result)

    def process(self, access: Access, source_ids: tuple[str, ...], *, timeout: float = 120,
                cancel: threading.Event | None = None) -> PreferenceReport:
        start = time.monotonic()
        report = super().process(access, source_ids, timeout=timeout, cancel=cancel)
        return PreferenceReport(report.run, report.error_code, False, time.monotonic() - start)
