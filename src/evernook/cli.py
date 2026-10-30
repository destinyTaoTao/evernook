"""Trusted local maintenance entry point; payloads use UTF-8 stdin, never argv."""
from __future__ import annotations

import argparse
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
    pair = commands.add_parser("pair", help="Pair a client using trusted local access")
    pair.add_argument("--config", type=Path, required=True)
    pair.add_argument("--client-id", required=True)
    pair.add_argument("--entry", required=True)
    pair.add_argument("--access", type=Path, required=True)
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
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--access", type=Path, required=True)
        if name in ("recall", "refresh-semantic", "semantic-status"):
            command.add_argument("--embedding-config", type=Path)
        if name in ("read", "state"):
            command.add_argument("--message-id", required=True)
    return parser


def _execute(args: argparse.Namespace) -> object:
    if args.command == "init":
        config, access = initialize(args.data_dir, client_id=args.client_id, entry=args.entry)
        access_path = config.parent / "mcp-access-owner.json"
        pair_client(config, client_id=access.client_id, entry=args.entry, access_path=access_path)
        return {"config": str(config), "access_file": str(access_path)}
    if args.command == "pair":
        pair_client(args.config, client_id=args.client_id, entry=args.entry, access_path=args.access)
        return {"access_file": str(args.access)}
    runtime = MemoryRuntime(args.config)
    access = runtime.access_from_file(args.access)
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
