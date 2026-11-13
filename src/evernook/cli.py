"""Trusted local maintenance entry point; payloads use UTF-8 stdin, never argv."""
from __future__ import annotations

import argparse
import getpass
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sys
import threading
from typing import Any, NoReturn

from .models import Access, Change, Event, ImportantMark, MemoryFailure, Source
from .runtime import MemoryRuntime, initialize, pair_client
from .embedding import load_embedding
from .semantic import SemanticRecall


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default diagnostic repeats user-controlled arguments.
        raise MemoryFailure("invalid_arguments")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MemoryFailure("invalid_arguments")
        result[key] = value
    return result


def _input() -> dict[str, Any]:
    # A 1 MiB text can expand sixfold when JSON-escaped. Bound the wire input too.
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise MemoryFailure("input_too_large")
    try:
        value: object = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique)
    except UnicodeError:
        raise MemoryFailure("invalid_encoding") from None
    except ValueError:
        raise MemoryFailure("invalid_arguments") from None
    if not isinstance(value, dict):
        raise MemoryFailure("invalid_arguments")
    return value


def _parser() -> Parser:
    parser = Parser(description="EverNook trusted local CLI v1 (Windows fixed NTFS)")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize a new private runtime directory")
    init.add_argument("--data-dir", type=Path, required=True)
    init.add_argument("--client-id", required=True)
    init.add_argument("--entry", required=True)
    init.add_argument("--allow-archive-review", action="store_true", help="Explicitly grant this new local owner archive/backup access")
    pair = commands.add_parser("pair", help="Pair a client using trusted local access")
    pair.add_argument("--config", type=Path, required=True)
    pair.add_argument("--client-id", required=True)
    pair.add_argument("--entry", required=True)
    pair.add_argument("--access", type=Path, required=True)
    pair.add_argument("--scopes", help="Comma-separated scope pack (e.g. recall,capture); omit for full trusted-local scopes")
    unlock = commands.add_parser("pair-unlock", help="Trusted local reset of the pairing lockout; audited")
    unlock.add_argument("--config", type=Path, required=True)
    lockout = commands.add_parser("lockout-configure", help="Configure the pairing failure threshold")
    lockout.add_argument("--config", type=Path, required=True)
    lockout.add_argument("--failure-limit", type=int, required=True)
    audit_config = commands.add_parser("audit-configure", help="Configure audit log retention in days")
    audit_config.add_argument("--config", type=Path, required=True)
    audit_config.add_argument("--retention-days", type=int, required=True)
    for name in ("vault-init", "vault-receive", "vault-pending", "vault-reconcile", "vault-show"):
        vault = commands.add_parser(name, help="Explicit protected inbox operation; no plaintext-read tool")
        vault.add_argument("--config", type=Path, required=True)
        vault.add_argument("--access", type=Path, required=True)
        vault.add_argument("--inbox-dir", type=Path, required=True)
        vault.add_argument("--key-dir", type=Path, required=True)
        if name in ("vault-reconcile", "vault-show"):
            vault.add_argument("--object-id", required=True)
        if name == "vault-show":
            vault.add_argument("--purpose", required=True)
    backup_init = commands.add_parser("backup-init", help="Initialize separate encryption and password-protected recovery material")
    backup_init.add_argument("--key-dir", type=Path, required=True)
    backup_create = commands.add_parser("backup-create", help="Create an authenticated encrypted snapshot; no scheduled activation")
    backup_create.add_argument("--config", type=Path, required=True)
    backup_create.add_argument("--access", type=Path, required=True)
    backup_create.add_argument("--key-dir", type=Path, required=True)
    backup_create.add_argument("--destination", type=Path, required=True)
    backup_restore = commands.add_parser("backup-restore", help="Decrypt into a new review-only ordinary archive")
    backup_restore.add_argument("--source", type=Path, required=True)
    backup_restore.add_argument("--destination", type=Path, required=True)
    backup_restore.add_argument("--recovery-file", type=Path, required=True)
    backup_retain = commands.add_parser("backup-retain", help="Plan/apply retention while preserving a locally verified recovery anchor")
    backup_retain.add_argument("--root", type=Path, required=True)
    backup_retain.add_argument("--key-dir", type=Path, required=True)
    backup_retain.add_argument("--recovery-verified-id", required=True)
    backup_retain.add_argument("--apply", action="store_true")
    for name, description in (
        ("remember", "Save one Event JSON object from UTF-8 stdin"),
        ("service-pair", "Pair a client through the service using an existing administrator"),
        ("service-pair-unlock", "Clear service pairing lockout using an existing administrator"),
        ("manage", "Trusted local UI read/preview/confirm; never a model tool"),
        ("host-recovery", "Trusted host review and read-only artifact receipts; never a model tool"),
        ("mark-important", "Trusted exact important-word mark using JSON stdin"),
        ("change", "Trusted source correction, suppression or revocation using JSON stdin"),
        ("search", "Search using a JSON object from UTF-8 stdin"),
        ("read", "Read ordinary evidence by ID"),
        ("capture-chunk", "Trusted ordered durable text chunk using JSON stdin"),
        ("read-stream", "Page committed stream text and completion using JSON stdin"),
        ("state", "Read evidence visibility and version"),
        ("status", "Report core and lexical processing status"),
        ("refresh-index", "Refresh the rebuildable lexical index"),
        ("refresh-index-window", "Trusted bounded lexical catch-up with one pending-ID window"),
        ("rebuild-index", "Rebuild the lexical index from retained evidence"),
        ("prepare-search-acceleration", "Trusted optional covering-index build; may temporarily block writers"),
        ("remove-search-acceleration", "Trusted removal of the optional search covering index"),
        ("prepare-lexical-id-acceleration", "Trusted optional FTS ID rebuild; requires a writer maintenance window"),
        ("remove-lexical-id-acceleration", "Trusted reverse rebuild of the optional FTS ID index"),
        ("prepare-search-checkpoint", "Trusted explicit lexical coverage checkpoint build; requires FTS ID acceleration"),
        ("remove-search-checkpoint", "Trusted removal of the optional coverage checkpoint and tracking triggers"),
        ("refresh-search-checkpoint", "Trusted bounded reconciliation of changed checkpoint IDs"),
        ("recall", "Fuse lexical and optional local semantic evidence using JSON stdin"),
        ("context", "Build the authorized host context through the independent service"),
        ("refresh-semantic", "Index at most 16 missing semantic chunks"),
        ("semantic-status", "Report semantic coverage for the configured model space"),
        ("fact-decide", "Trusted source-backed fact decision using JSON stdin; never a model tool"),
        ("fact-query", "Read current/historical/planned facts using JSON stdin"),
        ("candidate-list", "Read a completed candidate run for trusted review using JSON stdin"),
        ("extract", "Explicit experimental offline quote selection into candidates; no fact acceptance"),
        ("extract-preferences", "Explicit conservative offline preference drafts; no automatic acceptance"),
        ("episode-propose", "Trusted source-bound cross-session excerpt candidate; review only"),
        ("episode-read", "Read a source-validated episode candidate with provenance"),
        ("episode-configure", "Configure optional review-only episode candidates in background batches"),
        ("episode-status", "Read effective episode candidate policy"),
        ("observation-propose", "Trusted source-backed derived observation candidate"),
        ("observation-rebind", "Explicitly remap imported evidence to current raw IDs; review only"),
        ("observation-read", "Trusted current observation and provenance"),
        ("observation-decide", "Explicitly publish or withdraw an observation"),
        ("observation-split", "Atomically split a reviewed observation"),
        ("observation-related", "Maintenance deduplication independent of QA ranking"),
        ("observation-query", "Read currently published observations for a topic"),
        ("observation-configure", "Configure optional finite-rule review candidates"),
        ("observation-status", "Observation processing policy and progress"),
        ("observation-process", "Run a bounded observation maintenance batch"),
        ("observation-evidence", "Page through active source atoms including quarantined topics"),
        ("reflect", "Explicit temporary read-only memory reasoning"),
        ("reflect-configure", "Trusted opt-in to reflection freshness clocks"),
        ("topic-view-define", "Define or revise a user-selected memory topic"),
        ("topic-view-read", "Read only a currently usable topic snapshot"),
        ("topic-view-control", "Pause resume or delete a topic view"),
        ("topic-view-status", "Topic definitions jobs and freshness clocks"),
        ("topic-view-refresh", "Explicit bounded refresh of one topic view"),
        ("topic-view-tick", "One coalesced background refresh"),
        ("topic-view-edit", "Save an explicit user revision as raw evidence"),
        ("preference-clarify", "Trusted atomic preference dispute and profile pause"),
        ("preference-resolve", "Trusted user clarification as correction or real change"),
        ("preference-update-configure", "Narrow finite preference rules and explicitly admit their pinned identity"),
        ("preference-update-status", "Read saved automatic preference admission and historical receipts"),
        ("preference-update-process", "Trusted finite preference rules and gated publication"),
        ("maintenance-configure", "Configure explicit fixed-offset extractive navigation; no OS scheduler"),
        ("maintenance-pause", "Pause/resume navigation without pausing raw collection"),
        ("maintenance-tick", "Run bounded recoverable navigation work using JSON stdin"),
        ("maintenance-status", "Read navigation backlog and scan cursors"),
        ("navigation-read", "Read a current source-validated navigation bucket"),
        ("experience-decide", "Trusted version-bound experience review; never a model tool"),
        ("experience-read", "Read the current source-validated experience draft/state"),
        ("experience-from-navigation", "Create an extractive experience candidate from existing navigation"),
        ("purge-preview", "Preview an exact ordinary-source purge scope; does not delete evidence"),
        ("purge", "Trusted confirmed purge of the exact previewed scope; never a model tool"),
        ("persona-set", "Trusted user-confirmed persona block update; never a model tool"),
        ("persona-rollback", "Restore all persona blocks to a past snapshot as a traceable change"),
        ("persona-read", "Read current or as-of persona blocks"),
        ("persona-history", "Read one persona block's version history"),
        ("profile-decide", "Trusted profile tag lifecycle decision (propose/confirm/correct-like); never a model tool"),
        ("profile-read", "Read one profile tag's current versioned state"),
        ("profile-summary", "Aggregate current confirmed profile tags by category"),
        ("profile-history", "Read one profile tag's revision history"),
        ("audit-query", "Query the append-only caller audit log using JSON stdin"),
        ("consolidation-begin", "Claim a raw source range for explicit consolidation"),
        ("consolidation-complete", "Submit consolidation drafts into the review queue; profile tags propose only"),
        ("consolidation-queue", "Read the consolidation review queue"),
        ("consolidation-status", "Read one consolidation run status"),
        ("consolidation-fail", "Mark a consolidation run failed with an error code"),
        ("consolidation-configure", "Set consolidation tier and the low-risk merge whitelist"),
        ("scheduler-configure", "Configure the in-process consolidation scheduler"),
        ("scheduler-pause", "Pause or resume the consolidation scheduler"),
        ("scheduler-status", "Read scheduler settings and progress"),
        ("scheduler-session-end", "Capture-scope hook marking a session boundary"),
        ("scheduler-foreground", "Capture-scope expiring foreground activity for one owned session"),
        ("scheduler-trigger", "Run one bounded consolidation pass (extractive builtin provider)"),
        ("preset-apply", "Apply the companion preset of recommended defaults; overrides win"),
        ("preset-show", "Read the applied or recommended companion preset"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--access", type=Path, required=True)
        command.add_argument("--require-service", action="store_true")
        if name == "service-pair":
            command.add_argument("--client-id", required=True)
            command.add_argument("--entry", required=True)
            command.add_argument("--scopes")
        if name == "refresh-index-window":
            command.add_argument("--limit", type=int, default=5000)
            command.add_argument("--budget-seconds", type=float, default=120)
        if name == "refresh-search-checkpoint":
            command.add_argument("--limit", type=int, default=5000)
        if name in ("recall", "refresh-semantic", "semantic-status") or name.startswith('topic-view-'):
            command.add_argument("--embedding-config", type=Path)
        if name in ("extract", "extract-preferences"):
            command.add_argument("--extraction-config", type=Path, required=True)
        if name in ('reflect', 'reflect-configure'):
            command.add_argument('--reflection-config', type=Path)
            command.add_argument('--embedding-config', type=Path)
        if name in ("read", "state"):
            command.add_argument("--message-id", required=True)
    return parser


def _execute(args: argparse.Namespace, *, input_value: dict[str, Any] | None = None,
             service_runtime: MemoryRuntime | None = None, service_access: Access | None = None,
             service_cancel: threading.Event | None = None) -> object:
    def read_input() -> dict[str, Any]:
        return dict(input_value) if input_value is not None else _input()

    if args.command.startswith("vault-"):
        try:
            from .protected import ProtectedInbox, initialize_protected_inbox
        except ImportError:
            raise MemoryFailure("vault_dependency_missing") from None
        memory = MemoryRuntime(args.config)
        access = memory.access_from_file(args.access)
        if args.command == "vault-init":
            if not sys.stdin.isatty():
                raise MemoryFailure("interactive_vault_initialization_required")
            password = getpass.getpass("Local vault password: ").encode("utf-8")
            if password != getpass.getpass("Repeat vault password: ").encode("utf-8"):
                raise MemoryFailure("vault_password_mismatch")
            inbox = initialize_protected_inbox(memory, args.inbox_dir, args.key_dir, password)
            return {"inbox_id": inbox.inbox_id, "key_id": inbox.public["key_id"], "plaintext_read_enabled": False}
        inbox = ProtectedInbox(memory, args.inbox_dir, args.key_dir)
        if args.command == "vault-show":
            try:
                from .vault_local import show_local
            except ImportError:
                # tkinter is a stdlib component that some Python builds omit; the vault display
                # must fail with its own diagnostic instead of a generic operation failure.
                raise MemoryFailure("vault_display_unavailable") from None
            return show_local(inbox, access, args.object_id, args.purpose)
        if args.command == "vault-pending":
            return {"objects": [asdict(item) for item in inbox.pending(access)], "limit": 100}
        if args.command == "vault-reconcile":
            return inbox.reconcile(access, args.object_id)
        value = read_input()
        source = Source(**value.pop("source"))
        return inbox.submit(access, Event(source=source, **value))
    if args.command.startswith("backup-"):
        try:
            from .backup import create_backup, initialize_backup_keys, restore_backup, retain_backups
        except ImportError:
            raise MemoryFailure("backup_dependency_missing") from None
        if args.command == "backup-create":
            memory = MemoryRuntime(args.config)
            return create_backup(memory, memory.access_from_file(args.access), args.destination, args.key_dir)
        if args.command == "backup-retain":
            return retain_backups(args.root, args.key_dir, recovery_verified_id=args.recovery_verified_id, apply=args.apply)
        if not sys.stdin.isatty():
            raise MemoryFailure("interactive_recovery_required")
        password = getpass.getpass("Local recovery password: ").encode("utf-8")
        if args.command == "backup-init":
            if password != getpass.getpass("Repeat recovery password: ").encode("utf-8"):
                raise MemoryFailure("recovery_password_mismatch")
            return initialize_backup_keys(args.key_dir, password)
        return restore_backup(args.source, args.destination, args.recovery_file, password).status()
    if args.command == "init":
        config, access = initialize(args.data_dir, client_id=args.client_id, entry=args.entry,
                                    allow_archive_review=args.allow_archive_review)
        access_path = config.parent / "mcp-access-owner.json"
        pair_client(config, client_id=access.client_id, entry=args.entry, access_path=access_path)
        return {"config": str(config), "access_file": str(access_path)}
    if args.command == "pair":
        scopes = tuple(item.strip() for item in args.scopes.split(",")) if args.scopes else None
        pair_client(args.config, client_id=args.client_id, entry=args.entry, access_path=args.access, scopes=scopes)
        return {"access_file": str(args.access)}
    if args.command in ("pair-unlock", "lockout-configure", "audit-configure"):
        from .runtime import configure_audit, configure_lockout, unlock_pairing
        if args.command == "pair-unlock":
            return unlock_pairing(args.config)
        if args.command == "lockout-configure":
            return configure_lockout(args.config, failure_limit=args.failure_limit)
        return configure_audit(args.config, retention_days=args.retention_days)
    runtime = service_runtime if service_runtime is not None else MemoryRuntime(args.config)
    access = service_access if service_access is not None else runtime.access_from_file(args.access)
    if args.command == 'context':
        raise MemoryFailure('service_unavailable')
    if args.command in {'reflect', 'reflect-configure'}:
        from .reflection_model import from_file
        if args.reflection_config is None:
            raise MemoryFailure('reflection_model_unconfigured')
        backend = load_embedding(args.embedding_config) if args.embedding_config else None
        reflector = from_file(runtime, SemanticRecall(runtime, backend), args.reflection_config)
        value = read_input()
        if args.command == 'reflect-configure':
            if value:
                raise MemoryFailure('invalid_arguments')
            return reflector.configure(access)
        if 'topic_view_ids' in value:
            if not isinstance(value['topic_view_ids'], list):
                raise MemoryFailure('invalid_arguments')
            value['topic_view_ids'] = tuple(value['topic_view_ids'])
        return reflector.reflect(access, **value, cancel=service_cancel)
    if args.command == "manage":
        from .management import MemoryManagement
        management = MemoryManagement(runtime)
        value = read_input()
        action = value.pop("action", None)
        if action == "read":
            return management.read(access, **value)
        if action == "preview":
            return management.preview(access, **value)
        if action == "confirm":
            return management.confirm(access, **value)
        if action == "cancel":
            return management.cancel(access, **value)
        raise MemoryFailure("invalid_management_action")
    if args.command == "host-recovery":
        from .host_recovery import HostRecovery
        return HostRecovery(runtime).run(access, read_input())
    if args.command == "capture-chunk":
        value = read_input()
        event = value.pop("event")
        source = Source(**event.pop("source"))
        return runtime.append_stream(access, Event(source=source, **event), **value)
    if args.command == "read-stream":
        value = read_input()
        return runtime.read_stream(access, Source(**value.pop("source")), **value)
    if args.command == "mark-important":
        return runtime.mark_important(access, ImportantMark(**read_input()))
    if args.command == "change":
        return runtime.change(access, Change(**read_input()))
    if args.command == "candidate-list":
        from .candidates import CandidateWorkflow
        return {"candidates": [asdict(candidate) for candidate in CandidateWorkflow(runtime).candidates(access, **read_input())]}
    if args.command in ("purge-preview", "purge"):
        from .purge import PurgeDecision, PurgeWorkflow
        purge_workflow = PurgeWorkflow(runtime)
        value = read_input()
        if args.command == "purge":
            return purge_workflow.purge(access, PurgeDecision(**value))
        if not isinstance(value.get("source_ids"), list):
            raise MemoryFailure("invalid_arguments")
        value["source_ids"] = tuple(value["source_ids"])
        return purge_workflow.preview(access, **value)
    if args.command.startswith("experience-"):
        from .candidates import EvidenceSpan
        from .experience import ExperienceDecision, ExperienceDraft, ExperienceRegistry
        experiences = ExperienceRegistry(runtime)
        value = read_input()
        if args.command == "experience-read":
            return experiences.read(access, **value)
        if args.command == "experience-from-navigation":
            return experiences.propose_navigation(access, **value)
        draft = value.get("draft")
        if draft is not None:
            if not isinstance(draft, dict) or not isinstance(draft.get("sources"), list):
                raise MemoryFailure("invalid_arguments")
            draft["sources"] = tuple(EvidenceSpan(**span) for span in draft["sources"])
            value["draft"] = ExperienceDraft(**draft)
        return experiences.decide(access, ExperienceDecision(**value))
    if args.command.startswith("maintenance-") or args.command == "navigation-read":
        from .maintenance import MaintenanceBudget, NavigationMaintenance, NavigationPolicy
        maintenance = NavigationMaintenance(runtime)
        value = read_input()
        if args.command == "maintenance-configure":
            if "topics" in value:
                if not isinstance(value["topics"], list):
                    raise MemoryFailure("invalid_arguments")
                value["topics"] = tuple(value["topics"])
            return maintenance.configure(access, NavigationPolicy(**value))
        if args.command == "maintenance-pause":
            return maintenance.pause(access, **value)
        if args.command == "maintenance-status":
            return maintenance.status(access, **value)
        if args.command == "navigation-read":
            return maintenance.read(access, **value)
        value["budget"] = MaintenanceBudget(**value["budget"])
        return maintenance.tick(access, **value)
    if args.command.startswith('topic-view-'):
        from .topic_views import TopicViewDefinition, TopicViews
        from .topic_view_refresh import view_budget
        backend = load_embedding(args.embedding_config) if args.embedding_config else None
        topic_views = TopicViews(runtime, SemanticRecall(runtime, backend))
        value = read_input()
        if args.command == 'topic-view-status':
            if value:
                raise MemoryFailure('invalid_arguments')
            return topic_views.status(access)
        if args.command == 'topic-view-read':
            budget = view_budget(value.pop('timeout', 10), service_cancel)
            return topic_views.read(access, **value, check=budget)
        if args.command == 'topic-view-refresh':
            return topic_views.refresh(access, **value, cancel=service_cancel)
        if args.command == 'topic-view-tick':
            return topic_views.tick(access, **value, cancel=service_cancel)
        if args.command == 'topic-view-control':
            return topic_views.control(access, **value)
        if args.command == 'topic-view-edit':
            return topic_views.add_evidence(access, **value)
        definition = value.get('definition')
        if not isinstance(definition, dict):
            raise MemoryFailure('invalid_arguments')
        definition = dict(definition)
        for name in ('entries', 'sessions', 'source_ids', 'observation_keys'):
            if name in definition:
                if not isinstance(definition[name], list):
                    raise MemoryFailure('invalid_arguments')
                definition[name] = tuple(tuple(item) for item in definition[name]) if name == 'sessions' else tuple(definition[name])
        value['definition'] = TopicViewDefinition(**definition)
        return topic_views.configure(access, **value)
    if args.command.startswith('observation-'):
        from .candidates import EvidenceSpan
        from .observations import ObservationDraft, ObservationRegistry
        from .observation_processing import ObservationProcessing
        observations = ObservationRegistry(runtime)
        value = read_input()
        if args.command == 'observation-status':
            if value:
                raise MemoryFailure('invalid_arguments')
            return ObservationProcessing(runtime).status(access)
        if args.command == 'observation-configure':
            return ObservationProcessing(runtime).configure(access, **value)
        if args.command == 'observation-process':
            return ObservationProcessing(runtime).tick(access, **value, cancel=service_cancel)
        import time
        timeout = value.pop('timeout', 10)
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise MemoryFailure('invalid_observation_budget')
        deadline = time.monotonic() + float(timeout)
        def observation_check() -> float:
            if service_cancel is not None and service_cancel.is_set():
                raise MemoryFailure('observation_canceled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryFailure('observation_timeout')
            return remaining
        if args.command == 'observation-read':
            return observations.read(access, **value, check=observation_check)
        if args.command == 'observation-evidence':
            return ObservationProcessing(runtime).evidence(access, **value, check=observation_check)
        if args.command == 'observation-related':
            return observations.related(access, **value, check=observation_check)
        if args.command == 'observation-query':
            return observations.query(access, **value, check=observation_check)
        if args.command == 'observation-decide':
            return observations.decide(access, **value, check=observation_check)
        def observation_draft(data: Any) -> ObservationDraft:
            if not isinstance(data, dict):
                raise MemoryFailure('invalid_arguments')
            data = dict(data)
            for name in ('support', 'counterevidence'):
                if not isinstance(data.get(name), list):
                    raise MemoryFailure('invalid_arguments')
                data[name] = tuple(EvidenceSpan(**span) for span in data[name])
            return ObservationDraft(**data)
        if args.command == 'observation-split':
            if not isinstance(value.get('drafts'), list):
                raise MemoryFailure('invalid_arguments')
            value['drafts'] = tuple(observation_draft(draft) for draft in value['drafts'])
            return observations.split(access, **value, check=observation_check)
        value['draft'] = observation_draft(value.get('draft'))
        if args.command == 'observation-rebind':
            return observations.rebind(access, **value, check=observation_check)
        if 'parents' in value:
            if not isinstance(value['parents'], list):
                raise MemoryFailure('invalid_arguments')
            value['parents'] = tuple(value['parents'])
        if 'source_remap' in value:
            if not isinstance(value['source_remap'], list):
                raise MemoryFailure('invalid_arguments')
            value['source_remap'] = tuple(tuple(pair) for pair in value['source_remap'])
        return observations.propose(access, **value, check=observation_check)
    if args.command.startswith('episode-'):
        from .episodes import Episodes
        episodes = Episodes(runtime)
        value = read_input()
        if args.command == 'episode-status':
            if value:
                raise MemoryFailure('invalid_arguments')
            return {'enabled': episodes.enabled(access), 'publication': 'review_only'}
        if args.command == 'episode-configure':
            return episodes.configure(access, **value)
        if args.command == 'episode-read':
            return episodes.read(access, **value)
        for key in ('source_ids', 'entities'):
            if key in value:
                if not isinstance(value[key], list):
                    raise MemoryFailure('invalid_arguments')
                value[key] = tuple(value[key])
        return episodes.propose(access, **value, cancel=service_cancel)
    if args.command.startswith('preference-update-'):
        from .automatic_preferences import PreferenceUpdates
        preferences = PreferenceUpdates(runtime)
        value = read_input()
        if args.command == 'preference-update-status':
            return preferences.status(access, **value)
        if args.command == 'preference-update-configure':
            if 'categories' in value:
                if not isinstance(value['categories'], list):
                    raise MemoryFailure('invalid_arguments')
                value['categories'] = tuple(value['categories'])
            return preferences.configure(access, **value)
        if set(value) - {'source_ids', 'timeout'} or not isinstance(value.get('source_ids'), list):
            raise MemoryFailure('invalid_arguments')
        value['source_ids'] = tuple(value['source_ids'])
        return preferences.process(access, **value, cancel=service_cancel)
    if args.command in ("preference-clarify", "preference-resolve"):
        from .preference_clarifications import OpenClarification, PreferenceClarifications, ResolveClarification
        clarifications = PreferenceClarifications(runtime)
        if args.command == "preference-clarify":
            return clarifications.open(access, OpenClarification(**read_input()))
        return clarifications.resolve(access, ResolveClarification(**read_input()))
    if args.command in ("extract", "extract-preferences"):
        from .extraction import ExtractionProcessor, LocalQwen
        value = read_input()
        if set(value) - {"source_ids", "timeout"} or not isinstance(value.get("source_ids"), list):
            raise MemoryFailure("invalid_arguments")
        value["source_ids"] = tuple(value["source_ids"])
        if args.command == "extract-preferences":
            from .preferences import PreferenceProcessor
            return PreferenceProcessor(runtime, LocalQwen.from_file(args.extraction_config)).process(access, **value)
        return ExtractionProcessor(runtime, LocalQwen.from_file(args.extraction_config)).process(access, **value)
    if args.command.startswith("profile-"):
        from .candidates import EvidenceSpan
        from .profile import ProfileDecision, ProfileDraft, ProfileRegistry
        profiles = ProfileRegistry(runtime)
        value = read_input()
        if args.command == "profile-decide":
            draft = value.get("draft")
            if draft is not None:
                if not isinstance(draft, dict) or not isinstance(draft.get("sources"), list):
                    raise MemoryFailure("invalid_arguments")
                draft["sources"] = tuple(EvidenceSpan(**span) for span in draft["sources"])
                value["draft"] = ProfileDraft(**draft)
            return profiles.decide(access, ProfileDecision(**value))
        if args.command == "profile-read":
            return profiles.read(access, **value)
        if args.command == "profile-summary":
            if isinstance(value.get("categories"), list):
                value["categories"] = tuple(value["categories"])
            return profiles.summary(access, **value)
        return profiles.history(access, **value)
    if args.command.startswith("persona-"):
        from .persona import PersonaRegistry, PersonaRollback, PersonaSet
        personas = PersonaRegistry(runtime)
        value = read_input()
        if args.command == "persona-set":
            return personas.set(access, PersonaSet(**value))
        if args.command == "persona-rollback":
            return personas.rollback(access, PersonaRollback(**value))
        if args.command == "persona-history":
            return personas.history(access, **value)
        return personas.view(access, **value)
    if args.command in ("fact-decide", "fact-query"):
        from .facts import FactDecision, FactRegistry
        registry = FactRegistry(runtime)
        value = read_input()
        if args.command == "fact-query":
            return registry.query(access, **value)
        if "resolves" in value:
            if not isinstance(value["resolves"], list):
                raise MemoryFailure("invalid_arguments")
            value["resolves"] = tuple(value["resolves"])
        return registry.decide(access, FactDecision(**value))
    if args.command in ("recall", "refresh-semantic", "semantic-status"):
        backend = load_embedding(args.embedding_config) if args.embedding_config else None
        workflow = SemanticRecall(runtime, backend)
        if args.command == "recall":
            return workflow.recall(access, **read_input())
        if args.command == "refresh-semantic":
            return workflow.refresh(access)
        return workflow.status(access)
    if args.command == "remember":
        event = read_input()
        source = Source(**event.pop("source"))
        return runtime.submit(access, Event(source=source, **event))
    if args.command == "search":
        return runtime.search(access, **read_input())
    if args.command == "audit-query":
        return runtime.audit_query(access, **read_input())
    if args.command.startswith("consolidation-"):
        from .candidates import EvidenceSpan
        from .consolidation import ConsolidationDraft, ConsolidationWorkflow
        consolidation = ConsolidationWorkflow(runtime)
        value = read_input()
        if args.command == "consolidation-begin":
            if not isinstance(value.get("source_ids"), list):
                raise MemoryFailure("invalid_arguments")
            value["source_ids"] = tuple(value["source_ids"])
            return consolidation.begin(access, **value)
        if args.command == "consolidation-complete":
            if not isinstance(value.get("drafts"), list):
                raise MemoryFailure("invalid_arguments")
            drafts: list[ConsolidationDraft] = []
            for item in value["drafts"]:
                if not isinstance(item, dict) or not isinstance(item.get("sources"), list):
                    raise MemoryFailure("invalid_arguments")
                item["sources"] = tuple(EvidenceSpan(**span) for span in item["sources"])
                drafts.append(ConsolidationDraft(**item))
            value["drafts"] = tuple(drafts)
            return consolidation.complete(access, **value)
        if args.command == "consolidation-queue":
            return consolidation.queue(access, **value)
        if args.command == "consolidation-configure":
            if isinstance(value.get("kinds"), list):
                value["kinds"] = tuple(value["kinds"])
            if isinstance(value.get("profile_categories"), list):
                value["profile_categories"] = tuple(value["profile_categories"])
            return consolidation.configure(access, **value)
        if args.command == "consolidation-status":
            return consolidation.status(access, **value)
        return consolidation.fail(access, **value)
    if args.command.startswith("scheduler-"):
        from .scheduler import ConsolidationScheduler
        scheduler = ConsolidationScheduler(runtime)
        value = read_input()
        if args.command == "scheduler-foreground":
            return scheduler.foreground(access, **value)
        if args.command == "scheduler-configure":
            return scheduler.configure(access, **value)
        if args.command == "scheduler-pause":
            return scheduler.pause(access, **value)
        if args.command == "scheduler-status":
            return scheduler.status(access)
        if args.command == "scheduler-session-end":
            return scheduler.session_end(access, **value)
        return scheduler.trigger(access, **value)
    if args.command.startswith("preset-"):
        from .preset import apply_preset, show_preset
        value = read_input()
        if args.command == "preset-apply":
            return apply_preset(access, runtime, **value)
        return show_preset(access, runtime, **value)
    if args.command == "read":
        message = runtime.read(access, args.message_id)
        if message is None:
            raise MemoryFailure("message_not_found")
        return message
    if args.command == "state":
        return runtime.state(access, args.message_id)
    if args.command == "refresh-index":
        return runtime.refresh_lexical(access)
    if args.command == "refresh-index-window":
        return runtime.refresh_lexical_window(access, limit=args.limit, budget_seconds=args.budget_seconds)
    if args.command == "rebuild-index":
        return runtime.rebuild_lexical(access)
    if args.command in ("prepare-search-acceleration", "remove-search-acceleration"):
        return {"enabled": runtime.configure_lexical_acceleration(access,
            enabled=args.command == "prepare-search-acceleration")}
    if args.command in ("prepare-lexical-id-acceleration", "remove-lexical-id-acceleration"):
        return {"enabled": runtime.configure_lexical_id_acceleration(access,
            enabled=args.command == "prepare-lexical-id-acceleration")}
    if args.command in ("prepare-search-checkpoint", "remove-search-checkpoint"):
        return {"enabled": runtime.configure_lexical_checkpoint(access,
            enabled=args.command == "prepare-search-checkpoint")}
    if args.command == "refresh-search-checkpoint":
        return {"processed": runtime.refresh_lexical_checkpoint(access, limit=args.limit),
                "enabled": runtime.lexical_checkpoint_enabled(access)}
    return {"capabilities": runtime.handshake(access), "ledger": asdict(runtime.status(access)),
            "lexical": asdict(runtime.lexical_status(access)),
            "lexical_acceleration": runtime.lexical_acceleration_enabled(access),
            "lexical_id_acceleration": runtime.lexical_id_acceleration_enabled(access),
            "lexical_checkpoint": runtime.lexical_checkpoint_enabled(access), "consumer_running": False}


def main() -> None:
    try:
        args = _parser().parse_args()
        from .service import CLI_OPERATIONS, ENDPOINT, ServiceClient, client_access
        from .runtime import _load
        required = getattr(args, "require_service", False)
        configured = hasattr(args, "config") and bool(_load(args.config).get("service_mode"))
        if configured:
            required = True
        if required and args.command not in CLI_OPERATIONS and args.command not in ("service-pair", "service-pair-unlock"):
            raise MemoryFailure("service_unavailable")
        if args.command == "service-pair":
            access = client_access(args.config, args.access)
            result = ServiceClient(args.config).call(access, "pair-client", {"client_id": args.client_id,
                "entry": args.entry, "scopes": args.scopes.split(",") if args.scopes else None})
        elif args.command == "service-pair-unlock":
            access = client_access(args.config, args.access)
            result = ServiceClient(args.config).call(access, "pair-unlock", {})
        elif args.command in CLI_OPERATIONS and (required or (args.config.parent / ENDPOINT).exists()):
            access = client_access(args.config, args.access)
            value = {} if args.command == "status" else (
                {"message_id": args.message_id} if args.command in ("read", "state") else _input())
            result = ServiceClient(args.config).call(access, "cli",
                {"command": args.command, "input": value}, timeout=130 if args.command == "reflect" else 30)
        else:
            result = _execute(args)
        from .serialization import result_value
        result = result_value(result)
        payload: dict[str, object] = {"contract_version": 1, "ok": True, "result": result}
        code = 0
    except MemoryFailure as error:
        payload = {"contract_version": 1, "ok": False, "error": {"code": error.code}}
        code = 1
    except (KeyError, TypeError, ValueError):
        payload = {"contract_version": 1, "ok": False, "error": {"code": "invalid_arguments"}}
        code = 1
    except Exception:
        payload = {"contract_version": 1, "ok": False, "error": {"code": "local_operation_failed"}}
        code = 1
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
