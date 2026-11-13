"""Trusted local management boundary. Never register this as an MCP/model tool."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import sqlite3
import time
from typing import Any
from uuid import uuid4

from .candidates import EvidenceSpan
from .models import Access, Change, Event, MemoryFailure, Source
from .profile import ProfileDecision, ProfileRegistry
from .runtime import MemoryRuntime, _identifier, _json, _RECALLABLE, _time
from .safety import credential_signal


class MemoryManagement:
    """The trusted host must establish the actual user click before calling confirm."""

    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime
        self.profiles = ProfileRegistry(runtime)

    def _authorize(self, access: Access) -> Any:
        identity = self.runtime._require_scope(access, "maintain", "memory_management")
        if not {"recall", "accept"}.issubset(identity.scopes):
            raise MemoryFailure("permission_denied")
        return identity

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        # Only keyed fingerprints and IDs: previews/receipts must not retain a second
        # copy of source text after that source is purged.
        db.execute("""CREATE TABLE IF NOT EXISTS management_previews_v1 (
            id TEXT PRIMARY KEY, client_id TEXT NOT NULL, fingerprint BLOB NOT NULL,
            snapshot TEXT NOT NULL, expires REAL NOT NULL, outcome TEXT)""")

    @staticmethod
    def _page(offset: int, limit: int) -> None:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise MemoryFailure("invalid_page")

    @staticmethod
    def _profile(record: Any) -> dict[str, Any]:
        value: dict[str, Any] = asdict(record)
        for source in value["draft"]["sources"]:
            source.pop("expected_text", None)
        return value

    @staticmethod
    def _source(db: sqlite3.Connection, message_id: str) -> dict[str, Any]:
        _identifier(message_id)
        state = MemoryRuntime._state(db, message_id)
        row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (message_id,)).fetchone()
        message = MemoryRuntime._stored(row)
        if state.status != "active" or message.event.role not in {"user", "assistant", "tool"}:
            raise MemoryFailure("source_unavailable")
        return {"message_id": message_id, "state_version": state.version,
                "content_version": message.receipt.content_version, "source": asdict(message.event.source),
                "role": message.event.role, "occurred_at": message.event.occurred_at,
                "received_at": message.receipt.received_at, "completion": message.event.completion,
                "representation": message.receipt.representation, "text": message.event.text}

    @staticmethod
    def _matches_sources(db: sqlite3.Connection, ids: list[str], entry: str | None,
                         session_id: str | None, start: float | None, end: float | None) -> bool:
        if entry is None and session_id is None and start is None and end is None:
            return True
        for message_id in dict.fromkeys(ids):
            if db.execute(f"""SELECT 1 FROM messages WHERE id=? AND {_RECALLABLE}
                AND (? IS NULL OR json_extract(event,'$.source.entry')=?)
                AND (? IS NULL OR json_extract(event,'$.source.session')=?)
                AND (? IS NULL OR occurred>=?) AND (? IS NULL OR occurred<?)""",
                (message_id, entry, entry, session_id, session_id, start, start, end, end)).fetchone():
                return True
        return False

    def _network_graph(self, db: sqlite3.Connection, offset: int, limit: int, query: str,
                       category: str | None, entry: str | None, session_id: str | None,
                       start: float | None, end: float | None) -> dict[str, Any]:
        """Bounded pages from each layer, evaluated in one authorized read snapshot."""
        from .facts import FactRegistry
        from .observations import ObservationRegistry

        nodes: list[dict[str, Any]] = []
        has_more = False
        def rows(table: str, sql: str) -> list[Any]:
            nonlocal has_more
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                return []
            found = db.execute(sql, (limit + 1, offset)).fetchall()
            has_more = has_more or len(found) > limit
            return found[:limit]

        def add(kind: str, identifier: str, label: str, revision: int, state: str,
                node_category: str | None, edges: list[dict[str, str]], inference: bool = False) -> None:
            ids = [edge['source_id'] for edge in edges]
            if ((query and query not in label) or (category is not None and category != node_category)
                    or not self._matches_sources(db, ids, entry, session_id, start, end)):
                return
            nodes.append({'kind': kind, 'id': identifier, 'label': label, 'revision': revision,
                          'state': state, 'inference': inference, 'edges': edges})

        for row in rows('profile_events_v1', """SELECT e.tag_id FROM profile_events_v1 e
            WHERE e.revision=(SELECT max(n.revision) FROM profile_events_v1 n WHERE n.tag_id=e.tag_id)
            AND json_extract(e.record,'$.state')='confirmed' ORDER BY e.tag_id LIMIT ? OFFSET ?"""):
            profile_view = self.profiles._view(db, row[0])
            if profile_view.record is not None and not profile_view.error_code:
                profile = profile_view.record
                add('profile', profile.tag_id, profile.draft.label, profile.revision, profile.state, profile.draft.category,
                    [{'source_id': span.message_id, 'kind': 'evidence'} for span in profile.draft.sources])
        for row in rows('fact_heads_v1', 'SELECT subject,predicate FROM fact_heads_v1 ORDER BY subject,predicate LIMIT ? OFFSET ?'):
            fact_view = FactRegistry._query_snapshot(db, row[0], row[1])
            fact = fact_view.fact
            if fact is not None:
                add('fact', fact.fact_id, fact.text, fact.revision, fact_view.state, fact.category,
                    [{'source_id': span.message_id, 'kind': 'evidence'} for span in fact.sources])
        now = datetime.now(timezone.utc).timestamp()
        for row in rows('observation_events_v1', """SELECT e.record FROM observation_events_v1 e
            WHERE NOT EXISTS (SELECT 1 FROM observation_events_v1 n WHERE n.observation_id=e.observation_id AND n.revision>e.revision)
            AND e.state IN ('published','conflicted') ORDER BY e.observation_id LIMIT ? OFFSET ?"""):
            observed = ObservationRegistry._record(row[0])
            if ((observed.draft.valid_from is not None and _time(observed.draft.valid_from) > now)
                    or (observed.draft.valid_until is not None and _time(observed.draft.valid_until) <= now)):
                continue
            observation_view = ObservationRegistry._view(db, observed)
            if observation_view.conclusion is not None:
                add('observation', observed.observation_id, observation_view.conclusion, observed.revision, observation_view.state, observed.draft.category,
                    [{'source_id': context['message_id'], 'kind': context['stance']} for context in observation_view.source_contexts],
                    observed.draft.inference)
        return {'nodes': nodes, 'next_offset': offset + limit if has_more else None}

    def read(self, access: Access, *, view: str, offset: int = 0, limit: int = 25,
             session_id: str | None = None, query: str = "", message_id: str | None = None,
             tag_id: str | None = None, cursor: int = 0, category: str | None = None,
             entry: str | None = None, since: str | None = None, until: str | None = None) -> dict[str, Any]:
        self._authorize(access)
        self._page(offset, limit)
        if session_id is not None:
            _identifier(session_id)
        if category is not None:
            _identifier(category)
        if entry is not None:
            _identifier(entry)
        if view not in {"timeline", "network_profiles", "network_facts", "network_observations", "network_graph"} and any(value is not None for value in (entry, since, until)):
            raise MemoryFailure("invalid_arguments")
        start, end = _time(since) if since is not None else None, _time(until) if until is not None else None
        if start is not None and end is not None and start >= end:
            raise MemoryFailure("invalid_time_range")
        if not isinstance(query, str) or len(query) > 256 or credential_signal(query):
            raise MemoryFailure("invalid_query")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            if view == "network_graph":
                return self._network_graph(db, offset, limit, query, category, entry, session_id, start, end)
            if view == "source":
                if message_id is None or type(cursor) is not int or cursor < 0:
                    raise MemoryFailure("invalid_arguments")
                item = self._source(db, message_id)
                text = item.pop("text")
                if cursor > len(text):
                    raise MemoryFailure("invalid_page")
                end = min(len(text), cursor + 2048)
                return {**item, "text": text[cursor:end], "start": cursor, "end": end,
                        "total": len(text), "next_cursor": end if end < len(text) else None}
            if view == "history":
                if tag_id is None:
                    raise MemoryFailure("invalid_arguments")
                _identifier(tag_id)
                if not self.profiles._latest(db, tag_id):
                    return {"items": [], "next_offset": None}
                current = self.profiles._view(db, tag_id)
                if current.error_code:
                    raise MemoryFailure("source_unavailable")
                rows = db.execute("SELECT record FROM profile_events_v1 WHERE tag_id=? ORDER BY revision DESC LIMIT ? OFFSET ?",
                                  (tag_id, limit + 1, offset)).fetchall()
                items = []
                for row in rows[:limit]:
                    record = self.profiles._record(row[0])
                    try:
                        self.profiles._sources(db, record.draft)
                        if record.confirmation_id is not None and self.profiles._confirmation(db, record.confirmation_id) != record.confirmation_version:
                            continue
                    except MemoryFailure:
                        continue
                    items.append(self._profile(record))
                return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}
            if view in {"profiles", "network_profiles", "interests", "drafts", "suppressed", "rejected"}:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_events_v1'").fetchone():
                    return {"items": [], "next_offset": None}
                state = {"profiles": "confirmed", "network_profiles": "confirmed", "interests": "confirmed", "drafts": "draft", "suppressed": "suppressed", "rejected": "rejected"}[view]
                rows = db.execute("""SELECT e.tag_id FROM profile_events_v1 e JOIN
                    (SELECT tag_id,max(revision) revision FROM profile_events_v1 GROUP BY tag_id) h
                    ON e.tag_id=h.tag_id AND e.revision=h.revision
                    WHERE json_extract(e.record,'$.state')=?
                    AND (?<>'interests' OR json_extract(e.record,'$.draft.category') IN ('interest','preference'))
                    AND (? IS NULL OR json_extract(e.record,'$.draft.category')=?)
                    AND (?='' OR instr(json_extract(e.record,'$.draft.label'),?)>0)
                    ORDER BY json_extract(e.record,'$.recorded_at') DESC,e.tag_id
                    LIMIT ? OFFSET ?""", (state, view, category, category, query, query, limit + 1, offset)).fetchall()
                items = []
                for row in rows[:limit]:
                    profile_view = self.profiles._view(db, row[0])
                    if profile_view.record is not None and not profile_view.error_code:
                        if view == "network_profiles" and not self._matches_sources(db,
                                [span.message_id for span in profile_view.record.draft.sources], entry, session_id, start, end):
                            continue
                        if state == 'draft' and self.profiles._rejection(db, profile_view.record.draft) is not None:
                            continue
                        if state == 'rejected':
                            if not db.execute("SELECT 1 FROM profile_approvals_v1 WHERE tag_id=? AND action='reject' AND reviewed_revision=?",
                                              (profile_view.record.tag_id, profile_view.record.revision - 1)).fetchone():
                                continue
                        items.append(self._profile(profile_view.record))
                return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}
            if view == "timeline":
                rows = db.execute(f"""SELECT event,receipt FROM messages WHERE {_RECALLABLE}
                    AND json_extract(event,'$.role') IN ('user','assistant','tool')
                    AND json_extract(event,'$.source.branch')<>'management-confirmation'
                    AND (? IS NULL OR json_extract(event,'$.source.entry')=?)
                    AND (? IS NULL OR json_extract(event,'$.source.session')=?)
                    AND (? IS NULL OR occurred>=?) AND (? IS NULL OR occurred<?)
                    AND (?='' OR instr(json_extract(event,'$.text'),?)>0)
                    ORDER BY received DESC,id DESC LIMIT ? OFFSET ?""",
                    (entry, entry, session_id, session_id, start, start, end, end, query, query, limit + 1, offset)).fetchall()
                items = []
                for row in rows[:limit]:
                    message = self.runtime._stored(row)
                    item = self._source(db, message.receipt.message_id)
                    text = item.pop("text")
                    items.append({**item, "excerpt": text[:240], "total": len(text)})
                return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}
            if view == "network_facts":
                # Page by temporal slot, not historical decision: filtering revisions first
                # could revive a superseded fact when its successor is unavailable.
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fact_heads_v1'").fetchone():
                    return {"items": [], "next_offset": None}
                from .facts import FactRegistry
                rows = db.execute("SELECT subject,predicate FROM fact_heads_v1 ORDER BY subject,predicate LIMIT ? OFFSET ?",
                                  (limit + 1, offset)).fetchall()
                items = []
                for row in rows[:limit]:
                    fact_view = FactRegistry._query_snapshot(db, row[0], row[1])
                    fact = fact_view.fact
                    if fact is None:
                        continue
                    if ((query and query not in fact.text) or (category is not None and category != fact.category)
                            or not self._matches_sources(db, [span.message_id for span in fact.sources], entry, session_id, start, end)):
                        continue
                    items.append({"fact_id": fact.fact_id, "subject_id": fact.subject_id,
                                  "predicate": fact.predicate, "revision": fact.revision,
                                  "state": fact_view.state, "text": fact.text,
                                  "sources": list(dict.fromkeys(span.message_id for span in fact.sources))})
                return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}
            if view == "network_observations":
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_events_v1'").fetchone():
                    return {"items": [], "next_offset": None}
                from .observations import ObservationRegistry
                rows = db.execute("""SELECT e.record FROM observation_events_v1 e
                    WHERE NOT EXISTS (SELECT 1 FROM observation_events_v1 newer
                        WHERE newer.observation_id=e.observation_id AND newer.revision>e.revision)
                    AND e.state IN ('published','conflicted')
                    ORDER BY e.observation_id LIMIT ? OFFSET ?""", (limit + 1, offset)).fetchall()
                items = []
                now = datetime.now(timezone.utc).timestamp()
                for row in rows[:limit]:
                    observed = ObservationRegistry._record(row[0])
                    if ((observed.draft.valid_from is not None and _time(observed.draft.valid_from) > now)
                            or (observed.draft.valid_until is not None and _time(observed.draft.valid_until) <= now)):
                        continue
                    observation = ObservationRegistry._view(db, observed)
                    if observation.conclusion is None:
                        continue
                    contexts = observation.source_contexts
                    if ((query and query not in observation.conclusion) or (category is not None and category != observed.draft.category)
                            or not self._matches_sources(db, [c['message_id'] for c in contexts], entry, session_id, start, end)):
                        continue
                    items.append({"observation_id": observed.observation_id, "revision": observed.revision,
                                  "state": observation.state, "conclusion": observation.conclusion,
                                  "inference": observed.draft.inference,
                                  "support": list(dict.fromkeys(c['message_id'] for c in contexts if c['stance'] == 'support')),
                                  "counterevidence": list(dict.fromkeys(c['message_id'] for c in contexts if c['stance'] == 'counterevidence'))})
                return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}
            if view == "status":
                count = db.execute("SELECT count(*) FROM messages").fetchone()[0]
                return {"messages": count, "preview": True, "daily_admitted": False,
                        "ordinary_memory_encrypted": False, "configuration_schema_version": 1,
                        "supported_actions": sorted(_OPERATIONS)}
        raise MemoryFailure("invalid_management_view")

    @staticmethod
    def _operation(operation: dict[str, Any]) -> None:
        if not isinstance(operation, dict) or not isinstance(operation.get("kind"), str):
            raise MemoryFailure("invalid_management_operation")
        kind = operation["kind"]
        if kind not in _OPERATIONS:
            raise MemoryFailure("invalid_management_operation")
        target = "message_id" if kind.startswith("source_") else "tag_id"
        extra = {"text"} if kind == "source_correct" else {"label"} if kind == "profile_revise" else set()
        if set(operation) != {"kind", target} | extra:
            raise MemoryFailure("invalid_management_operation")
        _identifier(operation[target])
        for key in extra:
            text = operation[key]
            bound = 16384 if key == "text" else 256
            if not isinstance(text, str) or not text.strip() or len(text) > bound or credential_signal(text):
                raise MemoryFailure("invalid_management_text")
            try:
                text.encode("utf-8", errors="strict")
            except UnicodeError:
                raise MemoryFailure("invalid_encoding") from None

    def _snapshot(self, db: sqlite3.Connection, operation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        kind = operation["kind"]
        snapshot: dict[str, Any] = {"sources": []}
        details: dict[str, Any] = {"sources": []}
        confirmation_source: str | None = None
        if kind.startswith("source_"):
            source_ids = [operation["message_id"]]
            snapshot["dependencies"] = self._dependencies(db, operation["message_id"])
            details["affected"] = []
            for dependency in snapshot["dependencies"]:
                item = dict(dependency)
                if dependency['kind'] == 'profile':
                    profile_view = self.profiles._view(db, dependency['id'])
                    if profile_view.record is not None and not profile_view.error_code:
                        item['label'] = profile_view.record.draft.label
                elif dependency['kind'] == 'observation':
                    from .observations import ObservationRegistry
                    observed = ObservationRegistry._latest(db, dependency['id'])
                    if observed is not None and observed.revision == dependency['revision']:
                        observation_view = ObservationRegistry._view(db, observed)
                        if observation_view.conclusion is not None:
                            item['label'] = observation_view.conclusion
                details['affected'].append(item)
        else:
            record = self.profiles._latest(db, operation["tag_id"])
            if record is None:
                raise MemoryFailure("profile_not_found")
            view = self.profiles._view(db, record.tag_id)
            if view.error_code:
                raise MemoryFailure("source_unavailable")
            allowed = {"profile_confirm": {"draft"}, "profile_reject": {"draft"},
                       "profile_suppress": {"confirmed"}, "profile_revise": {"confirmed", "draft"}, "profile_reopen": {"rejected"}}
            if record.state not in allowed[kind]:
                raise MemoryFailure("invalid_profile_transition")
            if record.draft.processor_version.startswith("preference-clarification-v1:"):
                raise MemoryFailure("preference_clarification_required")
            snapshot["profile"] = [record.tag_id, record.revision]
            confirmation_source = record.confirmation_id
            source_ids = sorted({s.message_id for s in record.draft.sources}
                                | ({record.confirmation_id} if record.confirmation_id else set()))
            details["previous"] = self._profile(record)
        for source_id in source_ids:
            item = self._source(db, source_id)
            if kind.startswith("source_") and item["representation"] != "original":
                raise MemoryFailure("protected_source_unsupported")
            snapshot["sources"].append([source_id, item["state_version"], item["content_version"]])
            if source_id == confirmation_source:
                continue
            text = item.pop("text")
            details["sources"].append({**item, "excerpt": text[:2048], "total": len(text)})
        return snapshot, details

    @staticmethod
    def _dependencies(db: sqlite3.Connection, message_id: str) -> list[dict[str, Any]]:
        affected: list[dict[str, Any]] = []
        for table, source_column in (("important", "message_id"), ("source_context", "source_id")):
            rows = db.execute(f"SELECT id FROM {table} WHERE {source_column}=? ORDER BY id LIMIT 1001",
                              (message_id,)).fetchall()
            affected.extend({"kind": table, "id": row[0]} for row in rows)
        for table, id_column, path in (("profile_events_v1", "tag_id", "$.draft.sources"),
                                       ("fact_decisions_v1", "id", "$.sources"),
                                       ("experience_events_v1", "experience_id", "$.draft.sources")):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            rows = db.execute(f"""SELECT DISTINCT t.{id_column},t.revision FROM {table} t,
                json_each(t.record,?) s WHERE json_extract(s.value,'$.message_id')=?
                OR json_extract(t.record,'$.confirmation_id')=? ORDER BY 1,2 LIMIT 1001""",
                              (path, message_id, message_id)).fetchall()
            if len(rows) > 1000:
                raise MemoryFailure("management_scope_too_large")
            affected.extend({"kind": table.split("_")[0], "id": row[0], "revision": row[1]} for row in rows)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_events_v1'").fetchone():
            rows = db.execute("""SELECT t.observation_id,t.revision FROM observation_events_v1 t
                WHERE json_extract(t.record,'$.confirmation_id')=?
                   OR EXISTS (SELECT 1 FROM json_each(t.record,'$.draft.support') s
                              WHERE json_extract(s.value,'$.message_id')=?)
                   OR EXISTS (SELECT 1 FROM json_each(t.record,'$.draft.counterevidence') s
                              WHERE json_extract(s.value,'$.message_id')=?)
                ORDER BY 1,2 LIMIT 1001""", (message_id, message_id, message_id)).fetchall()
            if len(rows) > 1000:
                raise MemoryFailure("management_scope_too_large")
            affected.extend({"kind": "observation", "id": row[0], "revision": row[1]} for row in rows)
        if len(affected) > 1000:
            raise MemoryFailure("management_scope_too_large")
        return affected

    def preview(self, access: Access, operation: dict[str, Any]) -> dict[str, Any]:
        identity = self._authorize(access)
        self._operation(operation)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            # Expired unconsumed previews contain no evidence and need no permanent retention.
            db.execute("DELETE FROM management_previews_v1 WHERE outcome IS NULL AND expires<?", (time.time(),))
            if db.execute("SELECT count(*) FROM management_previews_v1 WHERE outcome IS NULL AND client_id=?", (identity.client_id,)).fetchone()[0] >= 64:
                raise MemoryFailure("management_queue_full")
            snapshot, details = self._snapshot(db, operation)
            proposal_id, expires = str(uuid4()), time.time() + 300
            fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(operation).encode(), hashlib.sha256)
            db.execute("INSERT INTO management_previews_v1 VALUES (?,?,?,?,?,NULL)",
                       (proposal_id, identity.client_id, fingerprint, _json(snapshot), expires))
            db.execute("COMMIT")
        return {"proposal_id": proposal_id, "expires_at": expires, "operation": operation, **details,
                "residuals": ["originals_retained", "previous_host_context_not_retracted", "topic_views_may_invalidate"]}

    def confirm(self, access: Access, proposal_id: str, operation: dict[str, Any]) -> dict[str, Any]:
        identity = self._authorize(access)
        _identifier(proposal_id)
        self._operation(operation)
        fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(operation).encode(), hashlib.sha256)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            row = db.execute("SELECT * FROM management_previews_v1 WHERE id=?", (proposal_id,)).fetchone()
            if row is None or row["client_id"] != identity.client_id or not hmac.compare_digest(bytes(row["fingerprint"]), fingerprint):
                raise MemoryFailure("management_preview_mismatch")
            if row["outcome"]:
                previous_outcome = dict(json.loads(row["outcome"]))
                if previous_outcome.get('state') == 'canceled':
                    raise MemoryFailure('management_preview_canceled')
                return previous_outcome
            if time.time() > row["expires"]:
                raise MemoryFailure("management_preview_expired")
            snapshot, _ = self._snapshot(db, operation)
            if _json(snapshot) != row["snapshot"]:
                raise MemoryFailure("management_preview_stale")

            def evidence(text: str, suffix: str) -> str:
                branch = "management-confirmation" if suffix == ":confirm" or operation['kind'] == 'source_suppress' else "management-input"
                event = Event(Source(identity.entry, "memory-management", branch, proposal_id + suffix),
                              "user", 0, datetime.now(timezone.utc).isoformat(), "complete", text)
                self.runtime._validate_event(event)
                digest = hmac.digest(self.runtime._fingerprint_key, _json(asdict(event)).encode(), hashlib.sha256)
                return self.runtime._persist_event(db, event, event, digest).message_id

            kind = operation["kind"]
            target: str = operation["message_id"] if kind.startswith("source_") else operation["tag_id"]
            if kind.startswith("source_"):
                text = operation.get("text", "管理页面确认停止召回来源 " + target)
                confirmation_id = evidence(text, ":source")
                change = Change(proposal_id, target, snapshot["sources"][0][1],
                                "correct" if kind == "source_correct" else "suppress", confirmation_id,
                                "Trusted management page confirmed " + proposal_id)
                result = self.runtime._change(db, change)
                revision, state = result.version, result.status
            else:
                record = self.profiles._latest(db, target)
                assert record is not None
                if kind == "profile_revise":
                    label = operation["label"]
                    source_id = evidence(label, ":replacement")
                    draft = replace(record.draft, label=label, confidence=1.0,
                                    sources=(EvidenceSpan(source_id, 1, "original", 1, 0, len(label), label),),
                                    processor_version="trusted-management-v1")
                    record = self.profiles._decide(db, ProfileDecision(proposal_id + ":revise", target,
                                                                     record.revision, "revise", draft))
                confirmation_id = evidence("管理页面确认 " + kind + " " + target, ":confirm")
                action = "confirm" if kind == "profile_revise" else kind.removeprefix("profile_")
                result_profile = self.profiles._decide(db, ProfileDecision(proposal_id, target, record.revision,
                                                                          action, confirmation_id=confirmation_id))
                revision, state = result_profile.revision, result_profile.state
            outcome = {"proposal_id": proposal_id, "action": kind, "target_id": target,
                       "revision": revision, "state": state, "confirmation_id": confirmation_id}
            db.execute("UPDATE management_previews_v1 SET outcome=? WHERE id=?", (_json(outcome), proposal_id))
            db.execute("COMMIT")
        return outcome

    def cancel(self, access: Access, proposal_id: str) -> dict[str, Any]:
        identity = self._authorize(access)
        _identifier(proposal_id)
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            row = db.execute('SELECT * FROM management_previews_v1 WHERE id=?', (proposal_id,)).fetchone()
            if row is None or row['client_id'] != identity.client_id:
                raise MemoryFailure('management_preview_mismatch')
            if row['outcome']:
                return dict(json.loads(row['outcome']))
            outcome = {'proposal_id': proposal_id, 'state': 'canceled'}
            db.execute('UPDATE management_previews_v1 SET outcome=? WHERE id=?', (_json(outcome), proposal_id))
            db.execute('COMMIT')
            return outcome


_OPERATIONS = {"source_suppress", "source_correct", "profile_confirm", "profile_reject", "profile_suppress", "profile_revise", "profile_reopen"}
