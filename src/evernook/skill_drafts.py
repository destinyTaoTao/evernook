"""Deterministic proposals for review from an original tool result and its linked input."""
from __future__ import annotations

import hmac
import sqlite3

from .candidates import EvidenceSpan
from .experience import ExperienceDecision, ExperienceDraft, ExperienceRecord, ExperienceRegistry
from .models import Access, CaptureReceipt, MemoryFailure, StoredMessage
from .runtime import MemoryRuntime, _MEMORY_EVIDENCE, _RECALLABLE, _identifier, _json


_RULE = "tool-outcome-review-v1"
_EXCERPT_CHARS = 512
_LESSONS = {
    "failed": "The tool reported a failure. Review the recorded outcome and verify the prerequisites before considering a retry.",
    "canceled": "The tool was reported canceled. Check whether it produced partial effects before considering a retry.",
}


class SkillDraftProposer:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    def propose(self, access: Access, *, tool_result_id: str) -> ExperienceRecord | None:
        """Explicit trusted proposal. Return None when the captured result is ineligible."""
        identity = self.runtime._authorize(access)
        required = {"recall", "accept"}
        allowed = required.issubset(identity.scopes)
        self.runtime._audit(identity, "skill_draft_propose", "", "ok" if allowed else "denied")
        if not allowed:
            raise MemoryFailure("permission_denied")
        _identifier(tool_result_id)
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            proposal = self._proposal(db, tool_result_id)
        if proposal is None:
            return None
        registry = ExperienceRegistry(self.runtime)
        registry._validate_decision(proposal)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Pairing updates take the same writer lock as this decision.
            if not required.issubset(self.runtime._authorize(access).scopes):
                raise MemoryFailure("permission_denied")
            record = registry._write_decision(db, proposal)
            db.execute("COMMIT")
            return record

    def _proposal(self, db: sqlite3.Connection, tool_result_id: str) -> ExperienceDecision | None:
        """Build the decision in the caller's read snapshot, without writing any state."""
        original = self._original(db, tool_result_id)
        if original is None:
            return None
        result, result_span, capture = original
        lesson = _LESSONS.get(result.event.completion)
        # Capture records incomplete_output for a saved failure or cancellation. Any other gap is disqualifying.
        if (result.event.role != "tool" or lesson is None or capture is None or capture.kind != "tool_result"
                or capture.complete or capture.gaps != ("incomplete_output",) or not capture.tool_name
                or capture.input_id is None or capture.input_id == tool_result_id):
            return None
        linked = self._original(db, capture.input_id)
        if linked is None:
            return None
        user_input, input_span, input_capture = linked
        if (user_input.event.role != "user" or user_input.event.completion != "complete"
                or user_input.event.sequence >= result.event.sequence):
            return None
        if input_capture is not None and (input_capture.kind != "message" or not input_capture.complete
                or input_capture.input_id is not None or input_capture.tool_name is not None):
            return None
        upstream, downstream = user_input.event.source, result.event.source
        if (upstream.entry, upstream.session, upstream.branch) != (downstream.entry, downstream.session, downstream.branch):
            return None

        key = hmac.digest(self.runtime._fingerprint_key, _json([_RULE, tool_result_id]).encode(), "sha256").hex()
        draft = ExperienceDraft("skill_draft", lesson,
            "This proposal is for review of the linked input and recorded tool outcome.",
            (input_span, result_span), processor_version=_RULE)
        return ExperienceDecision(f"skill-draft-propose:{key}", f"skill-draft:{key}", 0, "propose", draft)

    def _original(self, db: sqlite3.Connection, message_id: str) -> tuple[StoredMessage, EvidenceSpan, CaptureReceipt | None] | None:
        row = db.execute(f"""SELECT messages.event,messages.receipt,c.input_id,c.manifest,
            COALESCE(v.version,1) AS state_version FROM messages
            LEFT JOIN captures c ON c.message_id=messages.id LEFT JOIN visibility v ON v.message_id=messages.id
            WHERE messages.id=? AND {_RECALLABLE} AND {_MEMORY_EVIDENCE}""", (message_id,)).fetchone()
        if row is None:
            return None
        message = MemoryRuntime._stored(row)
        text = message.event.text[:_EXCERPT_CHARS]
        if (message.event.sensitive or message.event.protected_source or not message.receipt.original_saved
                or message.receipt.representation != "original" or not text.strip()):
            return None
        capture = None
        if row["manifest"] is not None:
            manifest = MemoryRuntime._capture_receipt(row["manifest"])
            if manifest.input_id != row["input_id"] or manifest.message != message.receipt:
                return None
            capture = self.runtime._check_capture(db, manifest)
        span = EvidenceSpan(message_id, int(row["state_version"]), "original",
            message.receipt.content_version, 0, len(text), text)
        return message, span, capture
