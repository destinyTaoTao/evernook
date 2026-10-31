"""Authenticated local ordinary-text API; no network listener or model inference."""
from .models import (
    Access, ArchiveView, AttachmentInfo, AttachmentInput, AuditPage, AuditRecord, Capture, CaptureReceipt,
    Change, ChangeRecord, ContextReceipt, DerivedContext, Event,
    ImportantItem, ImportantMark, ImportantPage, MemoryFailure, MessageState, Page,
    Receipt, RuntimeStatus, Source, StoredMessage, SearchSpan, SearchHit, SearchPage, LexicalStatus,
)
from .runtime import HARNESS_DEFAULT_SCOPES, SCOPES, MemoryRuntime, configure_audit, configure_lockout, initialize, pair_client, unlock_pairing
from .archive import ArchiveStatus, CaptureReview, EvidenceArchive, ImportantReview, restore_archive, verify_archive
from .candidates import CandidateDraft, CandidateLease, CandidateRun, CandidateWorkflow, EvidenceSpan, ExtractionSource, FactCandidate
from .embedding import EmbeddingSpace, LocalMiniLM
from .semantic import RecallHit, RecallPage, SemanticRecall, SemanticStatus
from .purge import PurgeDecision, PurgePreview, PurgeResult, PurgeWorkflow
from .persona import PERSONA_BLOCKS, PersonaBlockRecord, PersonaHistoryPage, PersonaRegistry, PersonaRollback, PersonaRollbackResult, PersonaSet, PersonaView
from .profile import PROFILE_CATEGORIES, PROFILE_TAXONOMY_VERSION, ProfileDecision, ProfileDraft, ProfileHistoryPage, ProfileRecord, ProfileRegistry, ProfileSummary, ProfileView

__all__ = [
    "Access", "ArchiveView", "Change", "ChangeRecord", "ContextReceipt", "DerivedContext",
    "AttachmentInfo", "AttachmentInput", "Capture", "CaptureReceipt",
    "Event", "ImportantItem", "ImportantMark", "ImportantPage", "MemoryFailure", "MemoryRuntime",
    "MessageState", "Page", "Receipt", "RuntimeStatus", "Source", "StoredMessage", "initialize", "pair_client",
    "HARNESS_DEFAULT_SCOPES", "SCOPES", "AuditPage", "AuditRecord", "configure_audit", "configure_lockout", "unlock_pairing",
    "SearchSpan", "SearchHit", "SearchPage", "LexicalStatus",
    "ArchiveStatus", "CaptureReview", "EvidenceArchive", "ImportantReview", "restore_archive", "verify_archive",
    "CandidateDraft", "CandidateLease", "CandidateRun", "CandidateWorkflow", "EvidenceSpan", "ExtractionSource", "FactCandidate",
    "EmbeddingSpace", "LocalMiniLM", "RecallHit", "RecallPage", "SemanticRecall", "SemanticStatus",
    "PurgeDecision", "PurgePreview", "PurgeResult", "PurgeWorkflow",
    "PERSONA_BLOCKS", "PersonaBlockRecord", "PersonaHistoryPage", "PersonaRegistry", "PersonaRollback", "PersonaRollbackResult", "PersonaSet", "PersonaView",
    "PROFILE_CATEGORIES", "PROFILE_TAXONOMY_VERSION", "ProfileDecision", "ProfileDraft", "ProfileHistoryPage", "ProfileRecord", "ProfileRegistry", "ProfileSummary", "ProfileView",
]
