"""Open-LLM-VTuber reference adapter: text-turn capture over the paired default scopes, gaps disclosed."""
from __future__ import annotations

from pathlib import Path

from ..context import ContextBundle, ContextPolicy, ContextBuilder
from ..models import Access, CaptureReceipt, Event, MemoryFailure, StoredMessage
from ..runtime import HARNESS_DEFAULT_SCOPES, MemoryRuntime, pair_client
from ..semantic import SemanticRecall
from .surface import AdapterGap, AdapterSurface


class OLVReferenceAdapter(AdapterSurface):
    """Text-path reference for Open-LLM-VTuber (v2 memory still early). Voice, vision, Live2D and
    tool streams stay disclosed gaps; the whole assistant turn is buffered before publication."""

    name = "olv-reference-v1"

    def __init__(self, config_path: Path):
        self.runtime = MemoryRuntime(config_path)
        self.access_path = self.runtime.root / "mcp-access-olv.json"
        self._access: Access | None = None
        self._entry: str | None = None
        self.recall = SemanticRecall(self.runtime)

    def pair(self, client_id: str, entry: str) -> Access:
        self._access = pair_client(self.runtime.root / "runtime.json", client_id=client_id, entry=entry,
                                   access_path=self.access_path, scopes=HARNESS_DEFAULT_SCOPES)
        self._entry = entry
        return self._access

    def _require_access(self) -> Access:
        if self._access is None:
            raise MemoryFailure("unauthorized")
        return self._access

    def covered_events(self) -> tuple[str, ...]:
        return ("text_user_turn", "text_assistant_turn", "whole_turn_buffered_publish")

    def gaps(self) -> tuple[AdapterGap, ...]:
        return (
            AdapterGap("voice_asr_transcript", "ASR transcripts are host-owned until the voice capture contract exists"),
            AdapterGap("asr_confidence", "no confidence field in the text capture contract yet"),
            AdapterGap("vision_frame", "multimodal understanding is out of scope for the memory layer"),
            AdapterGap("live2d_interaction", "Live2D interaction events are shell-internal"),
            AdapterGap("tool_execution_stream", "tool streams follow the dsh/pi native contracts, not yet OLV"),
        )

    def capture(self, event: Event) -> CaptureReceipt:
        # Host events arrive under shell-local source names; the adapter maps them onto the
        # paired entry namespace before submitting, exactly as a real shell adapter would.
        from dataclasses import replace
        from ..models import Capture
        access = self._require_access()
        assert self._entry is not None
        mapped = event if event.source.entry == self._entry else replace(
            event, source=replace(event.source, entry=self._entry))
        return self.runtime.submit_capture(access, Capture(mapped, "message"))

    def read_back(self, message_id: str) -> StoredMessage | None:
        return self.runtime.read(self._require_access(), message_id)

    def inject(self, query: str, policy: ContextPolicy, counter: object) -> ContextBundle:
        builder = ContextBuilder(self.runtime, self.recall, policy, counter)  # type: ignore[arg-type]
        return builder.build(self._require_access(), query, as_of="2026-10-30T12:00:00+08:00", mode="normal", timeout=20)

    def attempt_out_of_scope(self) -> str:
        try:
            self.runtime.refresh_lexical(self._require_access())
        except MemoryFailure as failure:
            return failure.code
        return "ok"

    def access(self) -> Access:
        return self._require_access()
