"""Trusted local maintenance entry point; payloads use UTF-8 stdin, never argv."""
from __future__ import annotations

import argparse
import getpass
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sys
from typing import Any, NoReturn

from .models import Event, MemoryFailure, Source
from .runtime import MemoryRuntime, initialize, pair_client
from .embedding import LocalMiniLM
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
        ("search", "Search using a JSON object from UTF-8 stdin"),
        ("read", "Read ordinary evidence by ID"),
        ("state", "Read evidence visibility and version"),
        ("status", "Report core and lexical processing status"),
        ("refresh-index", "Refresh the rebuildable lexical index"),
        ("rebuild-index", "Rebuild the lexical index from retained evidence"),
        ("recall", "Fuse lexical and optional local semantic evidence using JSON stdin"),
        ("refresh-semantic", "Index at most 16 missing semantic chunks"),
        ("semantic-status", "Report semantic coverage for the configured model space"),
        ("fact-decide", "Trusted source-backed fact decision using JSON stdin; never a model tool"),
        ("fact-query", "Read current/historical/planned facts using JSON stdin"),
        ("extract", "Explicit experimental offline quote selection into candidates; no fact acceptance"),
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
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--access", type=Path, required=True)
        if name in ("recall", "refresh-semantic", "semantic-status"):
            command.add_argument("--embedding-config", type=Path)
        if name == "extract":
            command.add_argument("--extraction-config", type=Path, required=True)
        if name in ("read", "state"):
            command.add_argument("--message-id", required=True)
    return parser


def _execute(args: argparse.Namespace) -> object:
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
        value = _input()
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
        pair_client(args.config, client_id=args.client_id, entry=args.entry, access_path=args.access)
        return {"access_file": str(args.access)}
    runtime = MemoryRuntime(args.config)
    access = runtime.access_from_file(args.access)
    if args.command in ("purge-preview", "purge"):
        from .purge import PurgeDecision, PurgeWorkflow
        purge_workflow = PurgeWorkflow(runtime)
        value = _input()
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
        value = _input()
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
        value = _input()
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
    if args.command == "extract":
        from .extraction import ExtractionProcessor, LocalQwen
        value = _input()
        if set(value) - {"source_ids", "timeout"} or not isinstance(value.get("source_ids"), list):
            raise MemoryFailure("invalid_arguments")
        value["source_ids"] = tuple(value["source_ids"])
        return ExtractionProcessor(runtime, LocalQwen.from_file(args.extraction_config)).process(access, **value)
    if args.command.startswith("profile-"):
        from .candidates import EvidenceSpan
        from .profile import ProfileDecision, ProfileDraft, ProfileRegistry
        profiles = ProfileRegistry(runtime)
        value = _input()
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
        value = _input()
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
        value = _input()
        if args.command == "fact-query":
            return registry.query(access, **value)
        if "resolves" in value:
            if not isinstance(value["resolves"], list):
                raise MemoryFailure("invalid_arguments")
            value["resolves"] = tuple(value["resolves"])
        return registry.decide(access, FactDecision(**value))
    if args.command in ("recall", "refresh-semantic", "semantic-status"):
        backend = LocalMiniLM.from_file(args.embedding_config) if args.embedding_config else None
        workflow = SemanticRecall(runtime, backend)
        if args.command == "recall":
            return workflow.recall(access, **_input())
        if args.command == "refresh-semantic":
            return workflow.refresh(access)
        return workflow.status(access)
    if args.command == "remember":
        event = _input()
        source = Source(**event.pop("source"))
        return runtime.submit(access, Event(source=source, **event))
    if args.command == "search":
        return runtime.search(access, **_input())
    if args.command == "read":
        message = runtime.read(access, args.message_id)
        if message is None:
            raise MemoryFailure("message_not_found")
        return message
    if args.command == "state":
        return runtime.state(access, args.message_id)
    if args.command == "refresh-index":
        return runtime.refresh_lexical(access)
    if args.command == "rebuild-index":
        return runtime.rebuild_lexical(access)
    return {"capabilities": runtime.handshake(access), "ledger": asdict(runtime.status(access)),
            "lexical": asdict(runtime.lexical_status(access)), "consumer_running": False}


def main() -> None:
    try:
        result = _execute(_parser().parse_args())
        if is_dataclass(result) and not isinstance(result, type):
            result = asdict(result)
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
