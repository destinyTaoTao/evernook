"""Bounded retrieval intent, separate from the retained original user input."""
from __future__ import annotations

from dataclasses import dataclass

from .models import MemoryFailure
from .safety import credential_signal


@dataclass(frozen=True)
class QueryPlan:
    kind: str
    query: str
    original_chars: int
    represented_start: int | None
    represented_end: int | None
    limited: bool
    since: str | None = None
    until: str | None = None


def plan_query(original: str, intent: str | None = None) -> QueryPlan:
    if not isinstance(original, str) or not original.strip() or len(original) > 1048576:
        raise MemoryFailure("invalid_query")
    try:
        original.encode("utf-8", errors="strict")
    except UnicodeError:
        raise MemoryFailure("invalid_encoding") from None
    # Validate ALL original input before selecting a short intent. A safe intent cannot launder a secret.
    if credential_signal(original):
        raise MemoryFailure("protected_query")
    if intent is not None:
        if not isinstance(intent, str) or not intent.strip() or len(intent) > 256:
            raise MemoryFailure("invalid_query_intent")
        try:
            intent.encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_encoding") from None
        if credential_signal(intent):
            raise MemoryFailure("protected_query")
        return QueryPlan("explicit_intent", intent, len(original), None, None, False)
    if len(original) <= 256:
        return QueryPlan("original", original, len(original), 0, len(original), False)
    # A deterministic tail is only a fallback, not a semantic summary of everything omitted.
    end = len(original.rstrip())
    start = max(0, end - 256)
    tail = original[start:end]
    # Retain the final complete question/sentence when one fits; never consult an answer oracle.
    boundary = max(tail.rfind("。"), tail.rfind("\n"), tail.rfind("！"))
    if boundary >= 0 and tail[boundary + 1:].strip():
        start += boundary + 1
    return QueryPlan("bounded_tail", original[start:end], len(original), start, end, True)
