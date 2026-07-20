from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__
from .audit import run_audit_retrieval, run_audit_scoring
from .adapters.base import TransportResponse
from .adapters.google_patents import SERPAPI_HOST, GooglePatentsAdapter, serpapi_account
from .adapters.kipris import KIPRIS_HOST, KiprisAdapter
from .adapters.manual_web import (
    ManualWebAdapter, WEB_SOURCE_TAGS, normalize_web_rows, sanitize_manual_records,
)
from .config import load_evaluation_config, load_similarity_config
from .database import (
    connect_database, export_profile, ingest, profile_conflict_snapshot, resolve_profile_conflicts,
    resolve_run_id, utc_now,
)
from .decisions import inspect_gate, resolve_gate
from .evaluation import run_shortlist
from .ideation import DomainPivotRequiredError, run_ideation
from .lint import audit_advisories, shortlist_advisories
from .models import QueryEnvelope, RunState
from .paths import contained_input, contained_output, owner_only_file, private_contained_directory, private_root
from .profile import MAX_DOCUMENT_BYTES, document_facts, folder_facts, interview_facts
from .privacy import assert_canaries_absent, credential_canaries, delete_run, environment_secret
from .provenance import digest, normalize, strict_json_loads
from .research import (
    CredentialRequiredError, PlannedQuery, ResearchBudget, plan_keyword_queries,
    run_research, run_research_batch,
)
from .report import publish_report
from .review import run_review
from .runs import prepare_run_profile, start_run
from .scaffold import (
    count_todos, evidence_binding_table, scaffold_audit_query_input,
    scaffold_candidate_input, scaffold_report_input, scaffold_shortlist_input,
)
from .sharing import SensitiveDisclosureRequiredError, share_report
from .state import ALLOWED_TRANSITIONS, GATE_STATE_SET, StateError, StateStore
from .validation import validate_and_complete

QUESTIONS = (
    ("name", "이름 또는 식별명을 입력하세요"),
    ("technical_domain", "주요 기술 분야를 입력하세요"),
    ("expertise", "전문 경험을 입력하세요"),
    ("project_summary", "해결하려는 기술 문제를 요약하세요"),
)


class CliError(Exception):
    pass


class InvalidArgumentsError(CliError):
    code = "invalid_arguments"

    def __init__(self) -> None:
        super().__init__("invalid_arguments")


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise InvalidArgumentsError()


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _command_name(args: argparse.Namespace | None) -> str:
    if args is None:
        return "unknown"
    command = getattr(args, "command", "unknown")
    nested = getattr(args, f"{command}_command", None)
    return f"{command}.{nested}" if nested else command


def _failure_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(error, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(error, UnicodeError):
        return "invalid_unicode"
    if isinstance(error, CliError):
        return "cli_error"
    if isinstance(error, OSError):
        return "io_error"
    if isinstance(error, ValueError):
        return "invalid_input"
    return "runtime_error"


def _redacted_error(error: BaseException) -> str:
    message = str(error)
    for secret in credential_canaries():
        if secret in message:
            message = message.replace(secret, "[REDACTED]")
    return message


def _cli_result(
    payload: dict[str, Any], *, args: argparse.Namespace | None,
    started_at: str, ended_at: str, failure_code: str | None,
) -> dict[str, Any]:
    result = dict(payload)
    result["schema_version"] = "cli-result-v1"
    result["envelope_version"] = "cli-envelope-v1"
    result.setdefault("command", _command_name(args))
    run_id = getattr(args, "run_id", None) if args is not None else None
    if run_id is not None:
        result.setdefault("run_id", str(run_id))
    result.setdefault("started_at", started_at)
    result.setdefault("ended_at", ended_at)
    result.setdefault("prior_state", None)
    result.setdefault("next_state", None)
    result.setdefault("artifact_ids", [])
    result.setdefault("event_ids", list(result.get("transition_event_ids", [])))
    if "adapter_status" in result:
        result.setdefault("adapter_summary", result["adapter_status"])
    result.setdefault("failure_code", failure_code)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="patent_factory", description="Local Korean invention-profile workflow")
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
    conflict_inspect = profile_commands.add_parser("conflict-inspect", help="inspect one exact profile conflict batch")
    conflict_inspect.add_argument("--batch-id", required=True)
    conflict_inspect.add_argument("--database", type=Path, help="authoritative profile SQLite database")
    conflict_inspect.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    conflict_decide = profile_commands.add_parser("conflict-decide", help="decide one exact profile conflict batch")
    conflict_decide.add_argument("--batch-id", required=True)
    conflict_decide.add_argument("--input", type=Path, required=True)
    conflict_decide.add_argument("--profile", type=Path, help="profile export")
    conflict_decide.add_argument("--database", type=Path, help="authoritative profile SQLite database")
    conflict_decide.add_argument("--byte-budget", type=int, default=2_000_000)
    conflict_decide.add_argument("--workspace-root", type=Path, default=Path("workspace"))

    run = commands.add_parser("run", help="bootstrap and inspect private workflow runs")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    start = run_commands.add_parser("start", help="bind an authoritative profile and enter research_ready")
    start.add_argument("--run", type=Path, required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--profile", type=Path, help="current profile export")
    start.add_argument("--profile-database", type=Path, help="authoritative profile SQLite database")
    start.add_argument("--byte-budget", type=int, default=2_000_000)
    start.add_argument("--workspace-root", type=Path, default=Path("workspace"))

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
    serpapi = research_commands.add_parser(
        "serpapi", help="run one LIVE Google Patents search via SerpApi (opt-in network egress)",
    )
    serpapi.add_argument("--run", type=Path, required=True, help="private run directory under workspace root")
    serpapi.add_argument("--run-id", required=True)
    serpapi.add_argument("--query", required=True)
    serpapi.add_argument("--country", default="", help="comma-separated country codes, e.g. KR")
    serpapi.add_argument("--num", type=int, help="results per page (10-100)")
    serpapi.add_argument("--page", type=int, default=1)
    serpapi.add_argument("--result-budget", type=int, default=10)
    serpapi.add_argument("--byte-budget", type=int, default=1_000_000)
    serpapi.add_argument(
        "--min-quota", type=int, default=1,
        help="stop and emit a manual-import template at or below this many searches left",
    )
    serpapi.add_argument("--decision-id", help="credential gate decision id")
    serpapi.add_argument(
        "--idempotency-key",
        help="explicit key replays the stored result for this exact request, including "
             "stored failures; without it a failed attempt is retried under a fresh key",
    )
    serpapi.add_argument("--retrieved-at", help="fixed UTC timestamp for deterministic runs")
    serpapi.add_argument("--documents-root", type=Path, default=Path("documents"))
    serpapi.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    serpapi.add_argument("--fixture-response", type=Path, help=argparse.SUPPRESS)
    serpapi.add_argument("--fixture-account", type=Path, help=argparse.SUPPRESS)
    kipris_live = research_commands.add_parser(
        "kipris", help="run credentialed live KIPRIS keyword research (KIPRIS_PLUS_API_KEY)",
    )
    kipris_live.add_argument("--run", type=Path, required=True, help="private run directory under workspace root")
    kipris_live.add_argument("--run-id", required=True)
    kipris_live.add_argument("--query", required=True, help="origin query term")
    for expansion in (
        "korean-synonym", "english-synonym", "discovered-term",
        "classification", "applicant", "inventor",
    ):
        kipris_live.add_argument(
            f"--{expansion}", action="append", default=None,
            help=f"repeatable {expansion.replace('-', ' ')} expansion term",
        )
    kipris_live.add_argument("--max-depth", type=int, default=1)
    kipris_live.add_argument("--max-calls", type=int, default=12)
    kipris_live.add_argument("--result-budget", type=int, default=30)
    kipris_live.add_argument("--byte-budget", type=int, default=1_000_000)
    kipris_live.add_argument("--page-cap", type=int, default=5)
    kipris_live.add_argument("--retry-budget", type=int, default=0)
    kipris_live.add_argument("--decision-id", help="current credential approval for this exact batch")
    kipris_live.add_argument("--idempotency-key")
    kipris_live.add_argument("--retrieved-at", help="fixed UTC timestamp for deterministic tests")
    kipris_live.add_argument("--documents-root", type=Path, default=Path("documents"))
    kipris_live.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    normalize_web = research_commands.add_parser(
        "normalize-web",
        help="normalize agent-gathered public web metadata into a manual-import file (offline)",
    )
    normalize_web.add_argument("source", type=Path, help="web-rows-v1 JSON under the documents root")
    normalize_web.add_argument("--out", type=Path, required=True, help="manual-import JSON written under the documents root")
    normalize_web.add_argument("--allow-host", action="append", required=True)
    normalize_web.add_argument("--source-type", choices=sorted(WEB_SOURCE_TAGS), default="web")
    normalize_web.add_argument("--byte-budget", type=int, default=1_000_000)
    normalize_web.add_argument("--documents-root", type=Path, default=Path("documents"))
    normalize_web.add_argument("--workspace-root", type=Path, default=Path("workspace"))

    ideate = commands.add_parser("ideate", help="validate and persist structured candidate proposals")
    ideate.add_argument("--run", type=Path, required=True, help="private run directory under workspace root")
    ideate.add_argument("--run-id", required=True)
    ideate.add_argument("--profile", type=Path, required=True, help="current profile export under workspace root")
    ideate.add_argument("--profile-database", type=Path, required=True, help="authoritative profile SQLite database")
    ideate.add_argument("--input", type=Path, required=True, help="candidate-input-v1 JSON under workspace root")
    ideate.add_argument("--byte-budget", type=int, default=2_000_000)
    ideate.add_argument("--decision-id", help="current domain-pivot approval for this exact input")
    ideate.add_argument("--workspace-root", type=Path, default=Path("workspace"))

    shortlist = commands.add_parser("shortlist", help="persist finalists or explicit insufficient evidence")
    shortlist.add_argument("--run", type=Path, required=True, help="private run directory under workspace root")
    shortlist.add_argument("--run-id", required=True)
    shortlist.add_argument("--input", type=Path, required=True, help="shortlist-input-v1 JSON under workspace root")
    shortlist.add_argument("--byte-budget", type=int, default=2_000_000)
    shortlist.add_argument("--workspace-root", type=Path, default=Path("workspace"))

    audit = commands.add_parser("audit", help="retrieve and score finalist-specific KIPRIS corpora")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    retrieve = audit_commands.add_parser("retrieve", help="run fixture or credentialed live KIPRIS audit queries")
    retrieve.add_argument("--run", type=Path, required=True)
    retrieve.add_argument("--run-id", required=True)
    retrieve.add_argument("--query-input", type=Path, required=True)
    retrieve.add_argument("--fixture-manifest", type=Path, help="deterministic fixture manifest (omit with --live)")
    retrieve.add_argument("--live", action="store_true", help="use the credentialed live KIPRIS adapter instead of fixtures")
    retrieve.add_argument("--byte-budget", type=int, default=2_000_000)
    retrieve.add_argument("--decision-id", help="current credential approval for this exact audit request")
    retrieve.add_argument("--retrieved-at", help="fixed UTC timestamp for deterministic offline fixtures")
    retrieve.add_argument("--documents-root", type=Path, default=Path("documents"))
    retrieve.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    score = audit_commands.add_parser("score", help="score one frozen reviewed feature-map set")
    score.add_argument("--run", type=Path, required=True)
    score.add_argument("--run-id", required=True)
    score.add_argument("--feature-input", type=Path, required=True)
    score.add_argument("--byte-budget", type=int, default=2_000_000)
    score.add_argument("--workspace-root", type=Path, default=Path("workspace"))

    scaffold_command = commands.add_parser(
        "scaffold", help="emit a hash-bound draft request input for agent completion",
    )
    scaffold_commands = scaffold_command.add_subparsers(dest="scaffold_command", required=True)
    for name in ("candidate", "shortlist", "audit-query", "report"):
        command = scaffold_commands.add_parser(name, help=f"draft a {name} request input")
        command.add_argument("--out", type=Path, required=True, help="draft JSON written under the workspace root")
        command.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    for name in ("candidate", "shortlist", "audit-query"):
        command = scaffold_commands.choices[name]
        command.add_argument("--run", type=Path, required=True)
        command.add_argument("--run-id", required=True)
    scaffold_commands.choices["candidate"].add_argument(
        "--profile-database", type=Path, help="authoritative profile SQLite (default WORKSPACE_ROOT/profile.sqlite3)",
    )
    scaffold_commands.choices["candidate"].add_argument("--count", type=int, default=3)
    scaffold_commands.choices["report"].add_argument(
        "--profile-database", type=Path, help="authoritative profile SQLite (default WORKSPACE_ROOT/profile.sqlite3)",
    )
    scaffold_commands.choices["report"].add_argument("--language", choices=("en", "ko"), default="en")

    gate = commands.add_parser("gate", help="inspect or decide one exact current gate")
    gate_commands = gate.add_subparsers(dest="gate_command", required=True)
    for name in ("inspect", "decide"):
        command = gate_commands.add_parser(name)
        command.add_argument("--run", type=Path, required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--gate-id", required=True)
        command.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    gate_commands.choices["decide"].add_argument("--input", type=Path, required=True)
    gate_commands.choices["decide"].add_argument("--byte-budget", type=int, default=2_000_000)

    for name, help_text in (
        ("draft", "render the private Korean Markdown report"),
        ("review", "persist an independent hash-bound report review"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run", type=Path, required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--byte-budget", type=int, default=2_000_000)
        command.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    validate = commands.add_parser("validate", help="deterministically validate and complete the private report")
    validate.add_argument("--run", type=Path, required=True)
    validate.add_argument("--run-id")
    validate.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    share = commands.add_parser("share", help="guard and publish an external report share")
    share.add_argument("--run", type=Path, required=True)
    share.add_argument("--run-id", required=True)
    share.add_argument("--input", type=Path, required=True)
    share.add_argument("--decision-id")
    share.add_argument("--byte-budget", type=int, default=2_000_000)
    share.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    deletion = commands.add_parser("delete-run", help="delete one contained private run without following links")
    deletion.add_argument("--run", type=Path, required=True)
    deletion.add_argument("--workspace-root", type=Path, default=Path("workspace"))
    return parser


_REQUESTS_README = """# requests/

Versioned `*-input-v1`/`v2` request files live here. Do not hand-copy hashes:
generate a pre-bound draft with the scaffold verbs and fill in the TODO(agent)
prose —

    python3 -m patent_factory scaffold candidate   --run RUN --run-id ID --out workspace/requests/candidate-input-v1.json
    python3 -m patent_factory scaffold shortlist   --run RUN --run-id ID --out workspace/requests/shortlist-input-v1.json
    python3 -m patent_factory scaffold audit-query --run RUN --run-id ID --out workspace/requests/audit-query-input-v1.json
    python3 -m patent_factory scaffold report      --language en --out workspace/requests/report-input-v2.json

Field notes for every template are in workspace/README.md.
"""


def _initialize(documents: Path, workspace: Path) -> dict[str, Any]:
    created = []
    for name, path in (("documents", documents), ("workspace", workspace)):
        existed = path.exists()
        private_root(path, f"{name} root", create=True)
        if not existed:
            created.append(name)
        requests_directory = Path.cwd() / path / "requests"
        if not requests_directory.exists():
            requests_directory.mkdir(mode=0o700)
            created.append(f"{name}/requests")
    readme = Path.cwd() / workspace / "requests" / "README.md"
    if not readme.exists():
        readme.write_text(_REQUESTS_README, encoding="utf-8")
        readme.chmod(0o600)
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
        data = strict_json_loads(text)
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


def _profile_conflict(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    database_path = contained_output(args.database or args.workspace_root / "profile.sqlite3", workspace_root, "profile database")
    batch_id = normalize(args.batch_id)
    with connect_database(database_path) as connection:
        if args.profile_command == "conflict-inspect":
            return ({"command": "profile.conflict-inspect", **profile_conflict_snapshot(connection, batch_id)}, 0)
        input_path = contained_input(args.input, workspace_root, "profile conflict decision")
        decision_input = _json_object(input_path, args.byte_budget, "profile conflict decision")
        if decision_input.get("batch_id") != batch_id:
            raise CliError("profile conflict batch id does not match --batch-id")
        canaries = credential_canaries()
        assert_canaries_absent(decision_input, canaries, boundary="profile_conflict_decision")
        result = resolve_profile_conflicts(connection, decision_input)
        profile_path = contained_output(args.profile or args.workspace_root / "profile.json", workspace_root, "profile output")
        export_profile(connection, profile_path)
        return ({"command": "profile.conflict-decide", **result}, 0 if result["status"] == "profile_ready" else 3)


def _run_start(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    profile_path = contained_input(
        args.profile or args.workspace_root / "profile.json", workspace_root, "run profile export",
    )
    profile_database_path = contained_input(
        args.profile_database or args.workspace_root / "profile.sqlite3",
        workspace_root, "run profile database",
    )
    profile = _json_object(profile_path, args.byte_budget, "run profile export")
    database_candidate = (Path.cwd() / args.run / "factory.sqlite3").resolve(strict=False)
    if database_candidate == profile_database_path:
        raise CliError("run database must be distinct from the authoritative profile database")
    with connect_database(profile_database_path) as profile_connection:
        prepared_profile = prepare_run_profile(profile_connection, profile)
        run_root = private_contained_directory(args.run, workspace_root, "run root", create=True)
        database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "run database")
        with connect_database(database_path) as connection:
            result = start_run(
                connection, profile_connection=profile_connection, run_root=run_root,
                run_id=args.run_id, profile=profile, prepared_profile=prepared_profile,
            )
    return result.as_dict(), 0


def _delete_run_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_root = private_root(args.workspace_root, "workspace root")
    run_root = contained_input(args.run, workspace_root, "delete run", directory=True)
    report = delete_run(run_root, workspace_root)
    payload = {
        "command": "delete-run", "failures": list(report.failures),
        "removed": list(report.removed), "root": report.root,
        "status": "deleted" if report.complete else "partial_failure",
    }
    return payload, 0 if report.complete else 11


def _bounded_bytes(path: Path, byte_budget: int, label: str) -> bytes:
    if not 1 <= byte_budget <= 10_000_000:
        raise CliError(f"{label} byte budget must be between 1 and 10000000")
    if path.stat().st_size > byte_budget:
        raise CliError(f"{label} exceeds byte budget")
    payload = path.read_bytes()
    if len(payload) > byte_budget:
        raise CliError(f"{label} exceeds byte budget")
    return payload


def _credential_gate_payload(
    command: str, error: CredentialRequiredError, run_id: str,
) -> dict[str, Any]:
    gate = error.gate
    return {
        "command": command,
        "credential_name": gate.approval_scope.get("credential_name"),
        "gate_id": gate.gate_id,
        "next_state": "credential_required",
        "run_id": run_id,
        "status": "credential_required",
        "subject_revision_hash": gate.subject_revision_hash,
    }


def _research_kipris(
    args: argparse.Namespace, *, started_at: str, run_root: Path, database_path: Path,
) -> tuple[dict[str, Any], int]:
    budget = ResearchBudget(
        max_depth=args.max_depth, max_calls=args.max_calls,
        per_adapter_results=args.result_budget, retry_budget=args.retry_budget,
        page_cap=args.page_cap, byte_budget=args.byte_budget,
    )
    budget.validate()
    planned = plan_keyword_queries(
        run_id=normalize(args.run_id), origin_query=args.query,
        korean_synonyms=tuple(args.korean_synonym or ()),
        english_synonyms=tuple(args.english_synonym or ()),
        discovered_terms=tuple(args.discovered_term or ()),
        classifications=tuple(args.classification or ()),
        applicants=tuple(args.applicant or ()),
        inventors=tuple(args.inventor or ()),
        budget=budget,
    )
    service_key = environment_secret("KIPRIS_PLUS_API_KEY") or ""
    adapter = KiprisAdapter(service_key, credential_required=True)
    idempotency_key = args.idempotency_key or "research-kipris-" + digest({
        "fingerprints": [query.envelope.request_fingerprint for query in planned],
    })[:20]
    with connect_database(database_path) as connection:
        try:
            result = run_research_batch(
                connection,
                run_root=run_root,
                run_id=normalize(args.run_id),
                adapter=adapter,
                queries=planned,
                idempotency_key=idempotency_key,
                retrieved_at=args.retrieved_at,
                credential_decision_id=args.decision_id,
            )
        except CredentialRequiredError as error:
            return _credential_gate_payload("research", error, normalize(args.run_id)), 5
    payload = result.as_dict()
    payload.update({"started_at": started_at, "ended_at": utc_now()})
    return payload, 0 if payload["status"] == "complete" else 4


def _research_normalize_web(
    args: argparse.Namespace, *, started_at: str, documents_root: Path,
) -> tuple[dict[str, Any], int]:
    source = contained_input(args.source, documents_root, "web rows source")
    payload = _json_object(source, args.byte_budget, "web rows source")
    if (
        set(payload) != {"rows", "schema_version"}
        or payload["schema_version"] != "web-rows-v1"
        or not isinstance(payload["rows"], list)
    ):
        raise CliError("web rows source must be web-rows-v1 with a rows list")
    allowed_hosts = tuple(dict.fromkeys(normalize(host).casefold() for host in args.allow_host))
    if not allowed_hosts or any(not host for host in allowed_hosts):
        raise CliError("normalize-web requires a non-empty host allowlist")
    secret = environment_secret("KIPRIS_PLUS_API_KEY")
    assert_canaries_absent(payload, (secret,) if secret else (), boundary="web_rows")
    records = normalize_web_rows(payload["rows"], allowed_hosts, args.source_type)
    out_path = _prepare_contained_output(args.out, documents_root, "manual import output")
    out_path.write_text(
        json.dumps({"records": records}, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    out_path.chmod(0o600)
    return ({
        "command": "research",
        "ended_at": utc_now(),
        "output_path": str(args.out),
        "record_count": len(records),
        "records": [{
            "content_hash": item["content_hash"], "excerpt_hashes": item["excerpt_hashes"],
            "identifier": item["identifier"],
        } for item in records],
        "source_type": args.source_type,
        "started_at": started_at,
        "status": "normalized",
    }, 0)


def _research(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.research_command == "serpapi":
        return _research_serpapi(args)
    started_at = utc_now()
    documents_root = private_root(args.documents_root, "documents root")
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    if args.research_command == "normalize-web":
        return _research_normalize_web(args, started_at=started_at, documents_root=documents_root)
    run_root = contained_input(args.run, workspace_root, "research run", directory=True)
    database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "research database")
    if args.research_command == "kipris":
        return _research_kipris(
            args, started_at=started_at, run_root=run_root, database_path=database_path,
        )
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
        imported = strict_json_loads(encoded)
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


def _json_fixture_transport(body: bytes) -> Callable[[str, float, int], TransportResponse]:
    """Deterministic offline transport for tests: replay a fixture body, ignore the URL."""

    def transport(url: str, timeout: float, byte_budget: int) -> TransportResponse:
        del url, timeout, byte_budget
        return TransportResponse(200, {"Content-Type": "application/json"}, body)

    return transport


def _serpapi_stored_execution(
    connection: Any, run_id: str, idempotency_key: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT result_json FROM research_operations WHERE run_id=? AND idempotency_key=?",
        (run_id, idempotency_key),
    ).fetchone()
    return json.loads(row["result_json"]) if row is not None else None


def _serpapi_idempotency_key(
    connection: Any, run_id: str, base_key: str,
) -> tuple[str, dict[str, Any] | None]:
    """Reuse a key only when it replays this invocation's stored success.

    Any other prior use of the candidate key — a stored failure, or an attempt
    bound to a credential decision under the ``:credential:`` suffix — advances
    to a fresh retry key, because run_research derives its transition and
    decision bindings from the plain key and would otherwise replay or reject.
    """

    candidate = base_key
    attempt = 1
    while True:
        rows = connection.execute(
            "SELECT idempotency_key,result_json FROM research_operations "
            "WHERE run_id=? AND (idempotency_key=? OR idempotency_key LIKE ?)",
            (run_id, candidate, f"{candidate}:credential:%"),
        ).fetchall()
        if not rows:
            return candidate, None
        exact = next(
            (json.loads(row["result_json"]) for row in rows if row["idempotency_key"] == candidate),
            None,
        )
        if exact is not None and exact.get("status") == "success":
            return candidate, exact
        attempt += 1
        candidate = f"{base_key}-r{attempt}"


def _serpapi_decision_operation(connection: Any, run_id: str, decision_id: str) -> str:
    """Validate a credential decision locally, before any network egress."""

    row = connection.execute(
        "SELECT suspended_operation, stale FROM gate_decisions WHERE decision_id=? AND run_id=?",
        (decision_id, run_id),
    ).fetchone()
    if row is None:
        raise CliError("credential decision is unavailable")
    if row["stale"]:
        raise CliError("credential decision does not match the current request")
    operation = row["suspended_operation"] or ""
    if not operation.startswith("research.execute:"):
        raise CliError("credential decision does not belong to a research request")
    return operation


def _serpapi_decision_key(
    connection: Any, run_id: str, decision_id: str, operation: str,
) -> tuple[str, dict[str, Any] | None]:
    """Resume a credential decision with the exact key the decision is bound to."""

    key = operation[len("research.execute:"):]
    return key, _serpapi_stored_execution(connection, run_id, f"{key}:credential:{decision_id}")


def _serpapi_quota_exhausted(
    args: argparse.Namespace, documents_root: Path, account: dict[str, Any], started_at: str,
    *, state: StateStore, run_id: str, envelope: QueryEnvelope,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    private_contained_directory(
        args.documents_root / "requests", documents_root, "manual web template directory", create=True,
    )
    template_relative = (args.documents_root / "requests" / "manual-web-template.json").as_posix()
    template_path = contained_output(
        args.documents_root / "requests" / "manual-web-template.json",
        documents_root, "manual web template",
    )
    template = {"records": [{
        "canonical_url": "https://patents.google.com/patent/REPLACE_WITH_PUBLICATION/en",
        "identifier": "REPLACE_WITH_PUBLICATION",
        "title": "REPLACE_WITH_TITLE",
        "content_hash": "0" * 64,
        "language": "en",
        "provenance": "google_patents_manual",
        "excerpt_hashes": [],
        "interpretations": [],
        "limitations": ["User-supplied metadata; not a patentability conclusion."],
    }]}
    rendered = json.dumps(template, ensure_ascii=False, indent=2) + "\n"
    # A template the user already edited is never overwritten; an unreadable or
    # differently encoded file counts as edited.
    preserved = False
    if template_path.exists():
        if template_path.stat().st_size > 1_000_000:
            preserved = True
        else:
            try:
                preserved = template_path.read_text(encoding="utf-8") != rendered
            except (OSError, UnicodeError):
                preserved = True
    if not preserved:
        template_path.write_text(rendered, encoding="utf-8")
        owner_only_file(template_path)
    canaries = credential_canaries()
    stop_record = {
        "account": {
            "plan_renewal_date": account.get("plan_renewal_date"),
            "plan_searches_left": account.get("plan_searches_left"),
            "total_searches_left": account.get("total_searches_left"),
        },
        "fallback_template": template_relative,
        "min_quota": args.min_quota,
        "request_fingerprint": envelope.request_fingerprint,
        "template_preserved": preserved,
    }
    assert_canaries_absent(stop_record, canaries, boundary="research_quota_stop")
    revision = state.add_revision(
        run_id, "research_quota_stop", stop_record, schema_version="research-quota-stop-v1",
    )
    template_note = (
        "An earlier template at that path contains your edits and was preserved; "
        "import it or delete it to regenerate."
        if preserved else
        "Fill the template with Google Patents records (unedited REPLACE_WITH_* "
        "placeholders are rejected on import)."
    )
    fallback_command = " ".join((
        "research", "manual", shlex.quote(template_relative),
        "--run", shlex.quote(str(args.run)), "--run-id", shlex.quote(run_id),
        "--query", shlex.quote(args.query),
        "--documents-root", shlex.quote(str(args.documents_root)),
        "--workspace-root", shlex.quote(str(args.workspace_root)),
        "--allow-host", "patents.google.com",
    ))
    payload = {
        "command": "research.serpapi",
        "status": "quota_exhausted",
        "run_id": run_id,
        "searches_left": account.get("total_searches_left"),
        "plan_renewal_date": account.get("plan_renewal_date"),
        "fallback_template": template_relative,
        "template_preserved": preserved,
        "artifact_ids": [revision.revision_id],
        "message": (
            "SerpApi monthly search quota exhausted; no further search will be spent. "
            f"{template_note} Then run: {fallback_command}"
        ),
        "started_at": started_at,
        "ended_at": utc_now(),
    }
    if extra:
        payload.update(extra)
    assert_canaries_absent(payload, canaries, boundary="research_quota_stop")
    return payload, 12


def _research_serpapi(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = utc_now()
    documents_root = private_root(args.documents_root, "documents root")
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    run_root = contained_input(args.run, workspace_root, "research run", directory=True)
    database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "research database")
    api_key = environment_secret("SERPAPI_API_KEY")

    # The offline seams are all-or-nothing: a half-configured seam would silently
    # send the real credential to the live account endpoint.
    if (args.fixture_response is None) != (args.fixture_account is None):
        raise CliError("--fixture-response and --fixture-account must be supplied together")
    response_transport = None
    account_transport = None
    if args.fixture_response is not None:
        response_transport = _json_fixture_transport(_bounded_bytes(
            contained_input(args.fixture_response, documents_root, "serpapi response fixture"),
            args.byte_budget, "serpapi response fixture",
        ))
        account_transport = _json_fixture_transport(_bounded_bytes(
            contained_input(args.fixture_account, documents_root, "serpapi account fixture"),
            args.byte_budget, "serpapi account fixture",
        ))

    adapter = GooglePatentsAdapter(api_key, transport=response_transport)
    projection: dict[str, Any] = {"word": normalize(args.query)}
    if args.num is not None:
        projection["num"] = args.num
    if normalize(args.country):
        projection["country"] = normalize(args.country)
    envelope = QueryEnvelope(
        run_id=normalize(args.run_id), adapter="google_patents", adapter_version="serpapi-v1",
        capability="word_search", allowed_scheme="https", allowed_host=SERPAPI_HOST,
        deadline_seconds=15, page=args.page, page_cap=5, result_budget=args.result_budget,
        byte_budget=args.byte_budget, retry_budget=0, retry_ownership="research_runner",
        query_projection=projection,
    )
    planned = PlannedQuery(envelope, args.query, args.query, "origin", 0)
    envelope.validate()
    run_id = envelope.run_id
    base_key = args.idempotency_key or "serpapi-" + digest({
        "request_fingerprint": envelope.request_fingerprint, "source_mode": "serpapi",
    })[:20]

    with connect_database(database_path) as connection:
        # Validate the run and its state before any network egress: a refused
        # operation must not send the credential anywhere first.
        state = StateStore(connection)
        try:
            prior = state.snapshot(run_id)
        except StateError as error:
            raise CliError(f"research run is not registered in the run database: {run_id}") from error
        if prior.state is RunState.CREDENTIAL_REQUIRED:
            raise RuntimeError("credential_required: a current decision must resume the suspended request")

        # Any supplied decision is validated locally first, whatever the key mode.
        decision_operation = (
            _serpapi_decision_operation(connection, run_id, args.decision_id)
            if args.decision_id else None
        )
        if args.idempotency_key:
            idempotency_key = base_key
            if decision_operation is not None and decision_operation != f"research.execute:{base_key}":
                raise CliError("credential decision does not match the current request")
            lookup = f"{base_key}:credential:{args.decision_id}" if args.decision_id else base_key
            stored = _serpapi_stored_execution(connection, run_id, lookup)
        elif decision_operation is not None:
            idempotency_key, stored = _serpapi_decision_key(
                connection, run_id, args.decision_id, decision_operation,
            )
        else:
            idempotency_key, stored = _serpapi_idempotency_key(connection, run_id, base_key)

        # A fresh attempt is refused here, before any network egress, unless the
        # state machine can actually accept it; replays are validated by run_research.
        # A run parked on a gate lists research_running as reachable, but only a
        # gate decision may take it there, so it is refused too.
        research_permitted = (
            prior.state is RunState.RESEARCH_RUNNING
            or RunState.RESEARCH_RUNNING in ALLOWED_TRANSITIONS.get(prior.state, frozenset())
        )
        if stored is None and prior.state in GATE_STATE_SET:
            raise CliError(
                f"run state {prior.state.value} requires a gate decision before research"
            )
        if stored is None and not research_permitted:
            raise CliError(f"research is not permitted from run state {prior.state.value}")

        # Free quota preflight: account.json does not consume a search. Replays of
        # a stored result never touch the network at all.
        quota_note = None
        if api_key and stored is None:
            try:
                account = serpapi_account(api_key, transport=account_transport)
            except (ValueError, OSError) as error:
                account = None
                quota_note = f"quota preflight unavailable: {_redacted_error(error)}"
            if account is not None:
                searches_left = account.get("total_searches_left")
                if searches_left is not None and searches_left <= args.min_quota:
                    return _serpapi_quota_exhausted(
                        args, documents_root, account, started_at,
                        state=state, run_id=run_id, envelope=envelope,
                    )

        try:
            result = run_research(
                connection, run_root=run_root, run_id=run_id, adapter=adapter,
                query=planned, idempotency_key=idempotency_key, retrieved_at=args.retrieved_at,
                credential_decision_id=args.decision_id,
            )
        except CredentialRequiredError as error:
            return ({
                "command": "research.serpapi",
                "status": "credential_required",
                "gate_id": error.gate.gate_id,
                "next_state": "credential_required",
                "run_id": run_id,
                "message": "Configure SERPAPI_API_KEY and approve the credential gate to proceed.",
                "started_at": started_at,
                "ended_at": utc_now(),
            }, 13)

        payload = result.as_dict()
        payload.update({"started_at": started_at, "ended_at": utc_now()})
        if quota_note:
            payload["quota_note"] = quota_note
        if payload.get("adapter_status", {}).get("failure_kind") == "rate_limit":
            if stored is not None:
                # A replay reports the stored attempt as-is: no account re-check,
                # no network egress, and no quota conversion.
                payload["rate_limit_note"] = (
                    "This replayed a stored rate-limited attempt without any network egress; "
                    "rerun without --idempotency-key/--decision-id to retry under a fresh attempt key."
                )
                return payload, 4
            # Reactive path: report exhaustion only when the free account endpoint
            # confirms it. A transient throttle must never fabricate a quota state.
            confirmed = None
            if api_key:
                try:
                    confirmed = serpapi_account(api_key, transport=account_transport)
                except (ValueError, OSError):
                    confirmed = None
            searches_left = confirmed.get("total_searches_left") if confirmed else None
            if searches_left is not None and searches_left <= args.min_quota:
                return _serpapi_quota_exhausted(
                    args, documents_root, confirmed, started_at,
                    state=state, run_id=run_id, envelope=envelope,
                    extra={"research_next_state": payload.get("next_state")},
                )
            retry_hint = (
                "rerunning with the same --idempotency-key will replay this stored failure — "
                "omit it to retry under a fresh attempt key."
                if args.idempotency_key else
                "retry shortly (a fresh retry attempt key is chosen automatically)."
            )
            payload["rate_limit_note"] = (
                "SerpApi throttled or rejected this search with a rate-limit response, "
                "but the free account endpoint did not confirm monthly quota exhaustion; "
                + retry_hint
            )
        return payload, 0 if payload["status"] == "complete" else 4


def _json_object(path: Path, byte_budget: int, label: str) -> dict[str, Any]:
    payload = strict_json_loads(_bounded_bytes(path, byte_budget, label))
    if not isinstance(payload, dict):
        raise CliError(f"{label} must be a JSON object")
    return payload


def _ideate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = utc_now()
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    run_root = contained_input(args.run, workspace_root, "ideation run", directory=True)
    profile_path = contained_input(args.profile, workspace_root, "ideation profile")
    profile_database_path = contained_input(
        args.profile_database, workspace_root, "ideation profile database"
    )
    input_path = contained_input(args.input, workspace_root, "ideation input")
    database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "ideation database")
    if profile_database_path == database_path:
        raise CliError("ideation profile database must be distinct from the run database")
    profile = _json_object(profile_path, args.byte_budget, "ideation profile")
    candidate_input = _json_object(input_path, args.byte_budget, "ideation input")
    with connect_database(profile_database_path) as profile_connection, connect_database(database_path) as connection:
        try:
            result = run_ideation(
                connection, profile_connection=profile_connection, run_root=run_root,
                run_id=normalize(args.run_id), profile=profile,
                candidate_input=candidate_input, config=load_evaluation_config(),
                domain_decision_id=args.decision_id,
            )
        except DomainPivotRequiredError as error:
            return ({
                "command": "ideate", "gate_id": error.gate.gate_id,
                "next_state": "domain_pivot_required", "run_id": normalize(args.run_id),
                "status": "domain_pivot_required",
            }, 6)
    payload = result.as_dict()
    payload.update({"ended_at": utc_now(), "started_at": started_at})
    return payload, 0


def _shortlist(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = utc_now()
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    run_root = contained_input(args.run, workspace_root, "shortlist run", directory=True)
    input_path = contained_input(args.input, workspace_root, "shortlist input")
    database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "shortlist database")
    shortlist_input = _json_object(input_path, args.byte_budget, "shortlist input")
    with connect_database(database_path) as connection:
        result = run_shortlist(
            connection, run_root=run_root, run_id=normalize(args.run_id),
            shortlist_input=shortlist_input, config=load_evaluation_config(),
        )
    payload = result.as_dict()
    if payload["status"] != "insufficient_evidence":
        # Advisory only: homogeneity smells never block; the user decides.
        payload["advisories"] = shortlist_advisories(shortlist_input.get("finalists", []))
    payload.update({"ended_at": utc_now(), "started_at": started_at})
    return payload, 5 if payload["status"] == "insufficient_evidence" else 0


def _prepare_contained_output(path: Path, root: Path, label: str) -> Path:
    """Create missing parent directories inside the root, then contain-check."""

    absolute_parent = (Path.cwd() / Path(path)).parent
    try:
        absolute_parent.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise CliError(f"{label} must stay under its private root") from exc
    pending: list[Path] = []
    probe = absolute_parent
    while not probe.exists():
        pending.append(probe)
        probe = probe.parent
    for directory in reversed(pending):
        directory.mkdir(mode=0o700)
    return contained_output(path, root, label)


def _scaffold(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = utc_now()
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    out_path = _prepare_contained_output(args.out, workspace_root, "scaffold output")
    command = args.scaffold_command
    extras: dict[str, Any] = {}
    if command in {"candidate", "shortlist", "audit-query"}:
        contained_input(args.run, workspace_root, "scaffold run", directory=True)
        database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "scaffold run database")
        run_id = normalize(args.run_id)
    if command == "candidate":
        profile_database = contained_input(
            args.profile_database or args.workspace_root / "profile.sqlite3",
            workspace_root, "scaffold profile database",
        )
        with connect_database(database_path) as connection, connect_database(profile_database) as profile_connection:
            draft = scaffold_candidate_input(
                connection, profile_connection, run_id=run_id, count=args.count,
            )
            extras["evidence"] = evidence_binding_table(connection, run_id)
    elif command == "shortlist":
        with connect_database(database_path) as connection:
            draft = scaffold_shortlist_input(connection, run_id=run_id, config=load_evaluation_config())
    elif command == "audit-query":
        with connect_database(database_path) as connection:
            draft = scaffold_audit_query_input(connection, run_id=run_id)
        extras["finalist_set_hash"] = draft["finalist_set_hash"]
    else:
        profile_database = contained_input(
            args.profile_database or args.workspace_root / "profile.sqlite3",
            workspace_root, "scaffold profile database",
        )
        with connect_database(profile_database) as profile_connection:
            draft = scaffold_report_input(profile_connection, language=args.language)
    out_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    out_path.chmod(0o600)
    return ({
        "command": "scaffold",
        "draft": command,
        "ended_at": utc_now(),
        "output_path": str(args.out),
        "started_at": started_at,
        "status": "scaffolded",
        "todo_count": count_todos(draft),
        **extras,
    }, 0)


def _audit(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = utc_now()
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    run_root = contained_input(args.run, workspace_root, "audit run", directory=True)
    database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "audit database")
    if args.audit_command == "retrieve":
        query_path = contained_input(args.query_input, workspace_root, "audit query input")
        query_input = _json_object(query_path, args.byte_budget, "audit query input")
        if args.live:
            if args.fixture_manifest is not None:
                raise CliError("audit retrieve --live does not take --fixture-manifest")
            service_key = environment_secret("KIPRIS_PLUS_API_KEY") or ""
            live_adapter = KiprisAdapter(service_key, credential_required=True)

            def adapter_factory(query, page, finalist):
                del query, page, finalist
                return live_adapter
        else:
            if args.fixture_manifest is None:
                raise CliError("audit retrieve requires --fixture-manifest unless --live is set")
            documents_root = private_root(args.documents_root, "documents root")
            manifest_path = contained_input(args.fixture_manifest, documents_root, "audit fixture manifest")
            manifest = _json_object(manifest_path, args.byte_budget, "audit fixture manifest")
            if set(manifest) != {"responses", "schema_version"} or manifest["schema_version"] != "audit-fixture-manifest-v1" or not isinstance(manifest["responses"], list):
                raise CliError("audit fixture manifest must be audit-fixture-manifest-v1")
            responses = {}
            for item in manifest["responses"]:
                if not isinstance(item, dict) or set(item) != {"finalist_id", "page", "source", "term"}:
                    raise CliError("audit fixture response has invalid fields")
                source = contained_input(Path(item["source"]), documents_root, "audit KIPRIS fixture")
                responses[(item["finalist_id"], normalize(item["term"]), item["page"])] = _bounded_bytes(source, args.byte_budget, "audit KIPRIS fixture")

            def adapter_factory(query, page, finalist):
                body = responses[(finalist, normalize(query["term"]), page)]

                def transport(url, timeout, byte_budget):
                    del url, timeout, byte_budget
                    return TransportResponse(200, {"Content-Type": "application/xml"}, body)

                return KiprisAdapter("fixture-only", transport=transport, credential_required=False)

        with connect_database(database_path) as connection:
            try:
                result = run_audit_retrieval(
                    connection, run_root=run_root, run_id=normalize(args.run_id),
                    query_input=query_input, config=load_similarity_config(), adapter_factory=adapter_factory,
                    credential_decision_id=args.decision_id, retrieved_at=args.retrieved_at,
                )
            except CredentialRequiredError as error:
                payload = _credential_gate_payload("audit", error, normalize(args.run_id))
                payload.update({"ended_at": utc_now(), "started_at": started_at})
                return payload, 5
        payload, code = result.as_dict(), 0
    else:
        feature_path = contained_input(args.feature_input, workspace_root, "audit feature input")
        feature_input = _json_object(feature_path, args.byte_budget, "audit feature input")
        with connect_database(database_path) as connection:
            result = run_audit_scoring(
                connection, run_root=run_root, run_id=normalize(args.run_id),
                feature_input=feature_input, config=load_similarity_config(),
            )
            advisories = []
            corpus_row = connection.execute(
                "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
                "ON ca.revision_id=ar.revision_id WHERE ca.run_id=? AND ca.kind='corpus_set'",
                (normalize(args.run_id),),
            ).fetchone()
            audit_row = connection.execute(
                "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
                "ON ca.revision_id=ar.revision_id WHERE ca.run_id=? AND ca.kind='audit_batch'",
                (normalize(args.run_id),),
            ).fetchone()
            if corpus_row is not None and audit_row is not None:
                advisories = audit_advisories(
                    json.loads(corpus_row["content_json"]), json.loads(audit_row["content_json"]),
                )
        payload = result.as_dict()
        payload["advisories"] = advisories
        code = 8 if payload["status"] == "decision_required" else 7 if payload["status"] == "coverage_insufficient" else 0
    payload.update({"ended_at": utc_now(), "started_at": started_at})
    return payload, code


def _gate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    run_root = contained_input(args.run, workspace_root, "decision run", directory=True)
    database_path = contained_output(args.run / "factory.sqlite3", workspace_root, "decision database")
    run_id, gate_id = normalize(args.run_id), normalize(args.gate_id)
    with connect_database(database_path) as connection:
        if args.gate_command == "inspect":
            return ({"command": "gate.inspect", **inspect_gate(connection, run_id, gate_id)}, 0)
        input_path = contained_input(args.input, workspace_root, "decision input")
        decision_input = _json_object(input_path, args.byte_budget, "decision input")
        if decision_input.get("gate_id") != gate_id:
            raise CliError("decision gate id does not match --gate-id")
        return resolve_gate(
            connection, run_root=run_root, run_id=run_id, decision_input=decision_input,
        ).as_dict(), 0


def _report_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_root = private_root(args.workspace_root, "workspace root", create=True)
    run_root = contained_input(args.run, workspace_root, f"{args.command} run", directory=True)
    database_path = contained_output(args.run / "factory.sqlite3", workspace_root, f"{args.command} database")
    with connect_database(database_path) as connection:
        if args.command == "validate":
            run_id = resolve_run_id(connection, args.run_id)
            return validate_and_complete(connection, run_root=run_root, run_id=run_id).as_dict(), 0
        input_path = contained_input(args.input, workspace_root, f"{args.command} input")
        value = _json_object(input_path, args.byte_budget, f"{args.command} input")
        if args.command == "draft":
            return publish_report(
                connection, run_root=run_root, run_id=normalize(args.run_id), report_input=value,
            ).as_dict(), 0
        if args.command == "review":
            result = run_review(
                connection, run_root=run_root, run_id=normalize(args.run_id), review_input=value,
            )
            return result.as_dict(), 10 if result.next_state == "revision_required" else 0
        try:
            result = share_report(
                connection, run_root=run_root, run_id=normalize(args.run_id),
                share_input=value, decision_id=args.decision_id,
            )
        except SensitiveDisclosureRequiredError as exc:
            return ({
                "actions": ["approve", "redact", "stop"], "command": "share",
                "gate_id": exc.gate.gate_id, "next_state": "sensitive_disclosure_required",
                "run_id": normalize(args.run_id), "status": "sensitive_disclosure_required",
            }, 9)
        return result.as_dict(), 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    started_at = utc_now()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv)
        if args.command == "init":
            emit(_cli_result(
                _initialize(args.documents, args.workspace), args=args,
                started_at=started_at, ended_at=utc_now(), failure_code=None,
            ))
            return 0
        if args.command == "run":
            payload, code = _run_start(args)
        elif args.command == "research":
            payload, code = _research(args)
        elif args.command == "ideate":
            payload, code = _ideate(args)
        elif args.command == "shortlist":
            payload, code = _shortlist(args)
        elif args.command == "scaffold":
            payload, code = _scaffold(args)
        elif args.command == "audit":
            payload, code = _audit(args)
        elif args.command == "gate":
            payload, code = _gate(args)
        elif args.command in {"draft", "review", "validate", "share"}:
            payload, code = _report_command(args)
        elif args.command == "delete-run":
            payload, code = _delete_run_command(args)
        elif args.command == "profile" and args.profile_command in {"conflict-inspect", "conflict-decide"}:
            payload, code = _profile_conflict(args)
        else:
            payload, code = _profile(args)
        emit(_cli_result(
            payload, args=args, started_at=started_at, ended_at=utc_now(), failure_code=None,
        ))
        return code
    except (CliError, OSError, OverflowError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        emit(_cli_result(
            {"error": _redacted_error(exc), "status": "error"}, args=args,
            started_at=started_at, ended_at=utc_now(), failure_code=_failure_code(exc),
        ))
        return 2
