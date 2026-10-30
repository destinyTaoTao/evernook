"""Caller-visible values for the version 1 ordinary-text contract."""
from dataclasses import dataclass, field


class MemoryFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Access:
    client_id: str
    token: str = field(repr=False)
    contract_version: int = 1


@dataclass(frozen=True)
class Source:
    entry: str
    session: str
    branch: str
    event: str


@dataclass(frozen=True)
class Event:
    source: Source
    role: str
    sequence: int
    occurred_at: str | None
    completion: str
    text: str = field(repr=False)
    sensitive: bool = False
    protected_source: bool = False


@dataclass(frozen=True)
class Receipt:
    message_id: str
    received_at: str
    status: str
    representation: str
    original_saved: bool
    content_version: int = 1


@dataclass(frozen=True)
class StoredMessage:
    event: Event
    receipt: Receipt


@dataclass(frozen=True)
class Page:
    messages: tuple[StoredMessage, ...]
    next_offset: int | None


@dataclass(frozen=True)
class RuntimeStatus:
    messages: int
    pending_jobs: int
    backup_enabled: bool = False
    vault_enabled: bool = False
    important_items: int = 0


@dataclass(frozen=True)
class ImportantMark:
    message_id: str
    representation: str
    content_version: int
    scope: str
    start: int | None
    end: int | None
    expected_text: str = field(repr=False)


@dataclass(frozen=True)
class ImportantItem:
    important_id: str
    message_id: str
    representation: str
    content_version: int
    scope: str
    start: int
    end: int
    created_at: str


@dataclass(frozen=True)
class ImportantPage:
    item: ImportantItem
    text: str = field(repr=False)
    start: int
    end: int
    remaining: int
    next_cursor: int | None


@dataclass(frozen=True)
class Change:
    request_id: str
    target_id: str
    expected_version: int
    action: str
    evidence_id: str
    reason: str = field(repr=False)


@dataclass(frozen=True)
class MessageState:
    message_id: str
    version: int
    status: str
    replacement_id: str | None = None
    change_id: str | None = None


@dataclass(frozen=True)
class ChangeRecord:
    request: Change
    state: MessageState
    recorded_at: str

    @property
    def reason(self) -> str:
        return self.request.reason


@dataclass(frozen=True)
class ArchiveView:
    message: StoredMessage
    state: MessageState
    changes: tuple[ChangeRecord, ...]
    archive_only: bool = True


@dataclass(frozen=True)
class DerivedContext:
    result_id: str
    source_id: str
    source_version: int
    text: str = field(repr=False)
    protected: bool = False


@dataclass(frozen=True)
class ContextReceipt:
    result_id: str
    source_id: str
    source_version: int
    committed_at: str


@dataclass(frozen=True)
class AttachmentInput:
    attachment_id: str
    label: str
    media_type: str
    chunks: tuple[bytes, ...] | None = field(repr=False)
    expected_size: int | None = None
    expected_sha256: str | None = None
    complete: bool = True
    protected: bool = False


@dataclass(frozen=True)
class Capture:
    event: Event
    kind: str
    input_id: str | None = None
    tool_name: str | None = None
    attachments: tuple[AttachmentInput, ...] = ()
    provided: bool = True
    text_chunks: tuple[str, ...] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AttachmentInfo:
    attachment_id: str
    label: str
    media_type: str
    size: int | None
    sha256: str | None
    saved: bool
    gap: str | None


@dataclass(frozen=True)
class CaptureReceipt:
    message: Receipt
    kind: str
    input_id: str | None
    tool_name: str | None
    attachments: tuple[AttachmentInfo, ...]
    gaps: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class SearchSpan:
    start: int
    end: int
    text: str = field(repr=False)


@dataclass(frozen=True)
class SearchHit:
    message_id: str
    source: Source
    role: str
    sequence: int
    occurred_at: str | None
    received_at: str
    completion: str
    representation: str
    content_version: int
    state_version: int
    spans: tuple[SearchSpan, ...]


@dataclass(frozen=True)
class LexicalStatus:
    state: str
    source_messages: int
    indexed_messages: int
    pending_messages: int
    source_watermark: str | None
    indexed_watermark: str | None
    normalization_version: int = 1


@dataclass(frozen=True)
class SearchPage:
    hits: tuple[SearchHit, ...]
    next_offset: int | None
    index: LexicalStatus
    used_index: bool
    fallback_reason: str | None
