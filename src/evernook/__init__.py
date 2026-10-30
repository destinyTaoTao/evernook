"""Authenticated local ordinary-text API; no network listener or model inference."""
from .models import (
    Access, ArchiveView, AttachmentInfo, AttachmentInput, Capture, CaptureReceipt,
    Change, ChangeRecord, ContextReceipt, DerivedContext, Event,
    ImportantItem, ImportantMark, ImportantPage, MemoryFailure, MessageState, Page,
    Receipt, RuntimeStatus, Source, StoredMessage, SearchSpan, SearchHit, SearchPage, LexicalStatus,
)
from .runtime import MemoryRuntime, initialize, pair_client
from .archive import ArchiveStatus, CaptureReview, EvidenceArchive, ImportantReview, restore_archive, verify_archive
from .candidates import CandidateDraft, CandidateLease, CandidateRun, CandidateWorkflow, EvidenceSpan, ExtractionSource, FactCandidate
from .embedding import EmbeddingSpace, LocalMiniLM
from .semantic import RecallHit, RecallPage, SemanticRecall, SemanticStatus

__all__ = [
    "Access", "ArchiveView", "Change", "ChangeRecord", "ContextReceipt", "DerivedContext",
    "AttachmentInfo", "AttachmentInput", "Capture", "CaptureReceipt",
    "Event", "ImportantItem", "ImportantMark", "ImportantPage", "MemoryFailure", "MemoryRuntime",
    "MessageState", "Page", "Receipt", "RuntimeStatus", "Source", "StoredMessage", "initialize", "pair_client",
    "SearchSpan", "SearchHit", "SearchPage", "LexicalStatus",
    "ArchiveStatus", "CaptureReview", "EvidenceArchive", "ImportantReview", "restore_archive", "verify_archive",
    "CandidateDraft", "CandidateLease", "CandidateRun", "CandidateWorkflow", "EvidenceSpan", "ExtractionSource", "FactCandidate",
    "EmbeddingSpace", "LocalMiniLM", "RecallHit", "RecallPage", "SemanticRecall", "SemanticStatus",
]
