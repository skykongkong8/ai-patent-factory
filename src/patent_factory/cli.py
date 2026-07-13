from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .adapters.base import TransportResponse
from .adapters.kipris import KIPRIS_HOST, KiprisAdapter
from .adapters.manual_web import ManualWebAdapter, sanitize_manual_records
from .database import connect_database, export_profile, ingest, utc_now
from .models import QueryEnvelope
from .paths import contained_input, contained_output, private_root
from .profile import MAX_DOCUMENT_BYTES, document_facts, folder_facts, interview_facts
from .provenance import digest, normalize
from .research import PlannedQuery, run_research
from .state import StateError

QUESTIONS = (
    ("name", "이름 또는 식별명을 입력하세요"),
    ("technical_domain", "주요 기술 분야를 입력하세요"),
    ("expertise", "전문 경험을 입력하세요"),
    ("project_summary", "해결하려는 기술 문제를 요약하세요"),
)


class CliError(Exception):
    pass


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="patent_factory", description="Local Korean invention-profile workflow")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="create privacy-safe local roots")
    initialize.add_argument("--documents", type=Path, default=Path("documents"))
    initialize.add_argument("--workspace", type=Path, default=Path("workspace"))

    profile = commands.add_parser("profile", help="ingest or interview for profile facts")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    for name in ("folder", "document"):
        command = profile_commands.add_parser(name, help=f"ingest one {name}")
        command.add_argument("source", type=Path)
        command.add_argument("--profile", type=Path, help="profile export (default: WORKSPACE_ROOT/profile.json)")
        command.add_argument("--database", type=Path, help="SQLite state (default: WORKSPACE_ROOT/profile.sqlite3)")
        command.add_argument("--documents-root", type=Path, default=Path("documents"))
        command.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    interview = profile_commands.add_parser("interview", help="run a real or scripted interview")
    interview.add_argument("--responses", type=str, help="JSON response file, or - for stdin")
    interview.add_argument("--profile", type=Path, help="profile export (default: WORKSPACE_ROOT/profile.json)")
    interview.add_argument("--database", type=Path, help="SQLite state (default: WORKSPACE_ROOT/profile.sqlite3)")
    interview.add_argument("--documents-root", type=Path, default=Path("documents"))
    interview.add_argument("--workspace-root", type=Path, default=Path("workspace"))

    research = commands.add_parser("research", help="run bounded fixture or manual research")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    for name in ("fixture", "manual"):
        command = research_commands.add_parser(name, help=f"run one {name} research operation")
        command.add_argument("source", type=Path)
        command.add_argument("--run", type=Path, required=True, help="private run directory under workspace root")
        command.add_argument("--run-id", required=True)
        command.add_argument("--query", required=True)
        command.add_argument("--idempotency-key")
        command.add_argument("--retrieved-at", help="fixed UTC timestamp for deterministic offline fixtures")
        command.add_argument("--byte-budget", type=int, default=1_000_000)
        command.add_argument("--result-budget", type=int, default=30)
        command.add_argument("--documents-root", type=Path, default=Path("documents"))
        command.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    manual = research_commands.choices["manual"]
    manual.add_argument("--allow-host", action="append", required=True)
    return parser


def _initialize(documents: Path, workspace: Path) -> dict[str, Any]:
    created = []
    for name, path in (("documents", documents), ("workspace", workspace)):
        existed = path.exists()
        private_root(path, f"{name} root", create=True)
        if not existed:
            created.append(name)
    return {"command": "init", "created": created, "status": "ready", "version": __version__}


def _responses(source: str | None, documents_root: Path) -> dict[str, Any]:
    if source:
        if source == "-":
            text = sys.stdin.read(MAX_DOCUMENT_BYTES + 1)
            if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
                raise CliError("interview responses too large")
        else:
            response_path = contained_input(Path(source), documents_root, "interview responses")
            if response_path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise CliError("interview responses too large")
            text = response_path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise CliError("interview responses must be a JSON object")
        return data
    if not sys.stdin.isatty():
        raise CliError("interactive interview requires a terminal; use --responses FILE or --responses -")
    data = {}
    for field, prompt in QUESTIONS:
        print(f"{prompt}: ", end="", file=sys.stderr, flush=True)
        data[field] = input()
    return data


def _profile(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    documents_root = private_root(args.documents_root, "documents root")
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    database_argument = args.database or args.workspace_root / "profile.sqlite3"
    profile_argument = args.profile or args.workspace_root / "profile.json"
    database_path = contained_output(database_argument, workspace_root, "database output")
    profile_path = contained_output(profile_argument, workspace_root, "profile output")
    if database_path == profile_path:
        raise CliError("database and profile outputs must be distinct")
    if args.profile_command == "folder":
        incoming = folder_facts(contained_input(args.source, documents_root, "folder input", directory=True))
        input_mode = "folder"
    elif args.profile_command == "document":
        incoming = document_facts(contained_input(args.source, documents_root, "document input"), root=documents_root)
        input_mode = "document"
    else:
        incoming = interview_facts(_responses(args.responses, documents_root))
        input_mode = "interview"
    with connect_database(database_path) as connection:
        result = ingest(connection, input_mode, incoming)
        export_profile(connection, profile_path)
    if result.conflicts:
        return ({
            "batch_id": result.batch_id,
            "command": "profile",
            "conflicts": list(result.conflicts),
            "input_count": result.input_count,
            "input_mode": input_mode,
            "profile_revision": result.profile_revision,
            "status": "conflict_resolution_required",
        }, 3)
    return ({
        "batch_id": result.batch_id,
        "changes": result.changes,
        "command": "profile",
        "fact_count": result.fact_count,
        "input_count": result.input_count,
        "input_mode": input_mode,
        "profile_revision": result.profile_revision,
        "status": result.status,
    }, 0)


def _bounded_bytes(path: Path, byte_budget: int, label: str) -> bytes:
    if not 1 <= byte_budget <= 10_000_000:
        raise CliError(f"{label} byte budget must be between 1 and 10000000")
    if path.stat().st_size > byte_budget:
        raise CliError(f"{label} exceeds byte budget")
    payload = path.read_bytes()
    if len(payload) > byte_budget:
        raise CliError(f"{label} exceeds byte budget")
    return payload


def _research(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = utc_now()
    documents_root = private_root(args.documents_root, "documents root")
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    run_root = contained_input(args.run, workspace_root, "research run", directory=True)
    database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "research database")
    source = contained_input(args.source, documents_root, "research source")

    if args.research_command == "fixture":
        body = _bounded_bytes(source, args.byte_budget, "KIPRIS fixture")

        def fixture_transport(url: str, timeout: float, byte_budget: int) -> TransportResponse:
            del url, timeout, byte_budget
            return TransportResponse(200, {"Content-Type": "application/xml"}, body)

        adapter = KiprisAdapter(
            "fixture-only", transport=fixture_transport, credential_required=False,
        )
        envelope = QueryEnvelope(
            run_id=normalize(args.run_id), adapter="kipris", adapter_version="plus-xml-v1",
            capability="word_search", allowed_scheme="https", allowed_host=KIPRIS_HOST,
            deadline_seconds=10, page=1, page_cap=5, result_budget=args.result_budget,
            byte_budget=args.byte_budget, retry_budget=0, retry_ownership="research_runner",
            query_projection={"word": normalize(args.query), "year": 0, "patent": True, "utility": True},
        )
        planned = PlannedQuery(envelope, args.query, args.query, "origin", 0)
    else:
        encoded = _bounded_bytes(source, args.byte_budget, "manual import")
        imported = json.loads(encoded.decode("utf-8"))
        if not isinstance(imported, dict) or not isinstance(imported.get("records"), list):
            raise CliError("manual import must be an object with a records list")
        allowed_hosts = tuple(dict.fromkeys(normalize(host).casefold() for host in args.allow_host))
        if not allowed_hosts or any(not host for host in allowed_hosts):
            raise CliError("manual import requires a non-empty host allowlist")
        sanitized_records = sanitize_manual_records(imported["records"], allowed_hosts)
        adapter = ManualWebAdapter(allowed_hosts)
        envelope = QueryEnvelope(
            run_id=normalize(args.run_id), adapter="manual_web", adapter_version="import-v1",
            capability="import", allowed_scheme="https", allowed_host=allowed_hosts[0],
            deadline_seconds=10, page=1, page_cap=1, result_budget=args.result_budget,
            byte_budget=args.byte_budget, retry_budget=0, retry_ownership="research_runner",
            query_projection={"content_type": "application/json", "records": sanitized_records},
        )
        planned = PlannedQuery(envelope, args.query, args.query, "manual_import", 0)

    envelope.validate()
    idempotency_key = args.idempotency_key or "research-" + digest({
        "request_fingerprint": envelope.request_fingerprint,
        "source_mode": args.research_command,
    })[:20]
    with connect_database(database_path) as connection:
        result = run_research(
            connection,
            run_root=run_root,
            run_id=envelope.run_id,
            adapter=adapter,
            query=planned,
            idempotency_key=idempotency_key,
            retrieved_at=args.retrieved_at,
        )
    payload = result.as_dict()
    payload.update({"started_at": started_at, "ended_at": utc_now()})
    return payload, 0 if payload["status"] == "complete" else 4


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "init":
            emit(_initialize(args.documents, args.workspace))
            return 0
        payload, code = _research(args) if args.command == "research" else _profile(args)
        emit(payload)
        return code
    except (CliError, OSError, StateError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        emit({"error": str(exc), "status": "error"})
        return 2
