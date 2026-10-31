"""Adapter surface for the conformance suite. Shells implement this; the suite checks the contract."""
from __future__ import annotations

from dataclasses import dataclass

from ..context import ContextBundle, ContextPolicy
from ..models import Access, CaptureReceipt, Event, StoredMessage
from ..runtime import HARNESS_DEFAULT_SCOPES


@dataclass(frozen=True)
class AdapterGap:
    event_kind: str
    reason: str


class AdapterSurface:
    """Minimal external behaviour every shell adapter must expose to the conformance suite."""

    name: str

    def pair(self, client_id: str, entry: str) -> Access:
        """Pair an independent identity with the harness default scopes and return its access."""
        raise NotImplementedError

    def covered_events(self) -> tuple[str, ...]:
        raise NotImplementedError

    def gaps(self) -> tuple[AdapterGap, ...]:
        raise NotImplementedError

    def capture(self, event: Event) -> CaptureReceipt:
        raise NotImplementedError

    def read_back(self, message_id: str) -> StoredMessage | None:
        raise NotImplementedError

    def inject(self, query: str, policy: ContextPolicy, counter: object) -> ContextBundle:
        raise NotImplementedError

    def attempt_out_of_scope(self) -> str:
        """Run one maintain-scope operation with the paired identity and return the failure code."""
        raise NotImplementedError

    def access(self) -> Access:
        """The paired access used for capture, so the suite can verify scope discipline."""
        raise NotImplementedError


def harness_default_scopes() -> tuple[str, ...]:
    return HARNESS_DEFAULT_SCOPES
