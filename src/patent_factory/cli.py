from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .database import connect_database, export_profile, ingest
from .paths import contained_input, contained_output, private_root
from .profile import MAX_DOCUMENT_BYTES, document_facts, folder_facts, interview_facts

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "init":
            emit(_initialize(args.documents, args.workspace))
            return 0
        payload, code = _profile(args)
        emit(payload)
        return code
    except (CliError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        emit({"error": str(exc), "status": "error"})
        return 2
