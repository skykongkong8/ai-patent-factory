"""Opt-in LIVE KIPRIS smoke: one tiny credentialed keyword batch, redacted output.

Ordinary CI stays fully offline; this script is the separately authorized live
check described in docs/kipris-contract-spike.md. It requires BOTH an explicit
--confirm-live flag and a present KIPRIS_PLUS_API_KEY, performs at most
--max-calls live requests, prints a redacted JSON summary (never the key,
never raw response bodies), and removes its scratch workspace unless
--keep-run is passed.

Usage:
    PYTHONPATH=src python3 scripts/live_kipris_smoke.py --confirm-live
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patent_factory.privacy import credential_diagnostic  # noqa: E402

SMOKE_WORKSPACE = Path("workspace") / "live-smoke"
SMOKE_DOCUMENT = Path("documents") / "live-smoke-profile.md"
SMOKE_RUN = SMOKE_WORKSPACE / "runs" / "smoke"
RUN_ID = "live-smoke"


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def run_cli(*argv: object) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "patent_factory", *map(str, argv)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )


def cleanup() -> None:
    if (ROOT / SMOKE_RUN).is_dir():
        run_cli("delete-run", "--run", SMOKE_RUN, "--workspace-root", SMOKE_WORKSPACE)
    target = ROOT / SMOKE_WORKSPACE
    if target.is_dir() and target.name == "live-smoke" and target.parent == ROOT / "workspace":
        shutil.rmtree(target)
    document = ROOT / SMOKE_DOCUMENT
    if document.is_file():
        document.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true",
                        help="explicitly authorize live network requests to plus.kipris.or.kr")
    parser.add_argument("--term", default="센서", help="origin Korean query term")
    parser.add_argument("--english-term", default="sensor", help="one English synonym term")
    parser.add_argument("--max-calls", type=int, default=2, help="live request cap (default 2)")
    parser.add_argument("--result-budget", type=int, default=5)
    parser.add_argument("--keep-run", action="store_true", help="keep the scratch workspace for inspection")
    args = parser.parse_args(argv)

    if not args.confirm_live:
        emit({"reason": "confirm_live_required", "status": "skipped"})
        return 1
    diagnostic = credential_diagnostic("KIPRIS_PLUS_API_KEY")
    if diagnostic["status"] != "present":
        emit({"reason": "credential_missing", "status": "skipped"})
        return 1

    cleanup()
    try:
        (ROOT / SMOKE_DOCUMENT).write_text(
            "name: live-smoke\n"
            "expertise: 스모크 검증\n"
            "project_summary: 라이브 KIPRIS 스모크 검증 실행\n"
            "technical_domain: 검증\n",
            encoding="utf-8",
        )
        for step in (
            ("profile", "document", SMOKE_DOCUMENT, "--workspace-root", SMOKE_WORKSPACE),
            ("run", "start", "--run", SMOKE_RUN, "--run-id", RUN_ID,
             "--profile", SMOKE_WORKSPACE / "profile.json",
             "--profile-database", SMOKE_WORKSPACE / "profile.sqlite3",
             "--workspace-root", SMOKE_WORKSPACE),
        ):
            completed = run_cli(*step)
            if completed.returncode != 0:
                emit({"status": "setup_failed", "step": str(step[0]) + "." + str(step[1]),
                      "exit_code": completed.returncode})
                return 2

        (ROOT / SMOKE_RUN).mkdir(parents=True, exist_ok=True)
        completed = run_cli(
            "research", "kipris", "--run", SMOKE_RUN, "--run-id", RUN_ID,
            "--query", args.term, "--english-synonym", args.english_term,
            "--max-calls", args.max_calls, "--result-budget", args.result_budget,
            "--workspace-root", SMOKE_WORKSPACE,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            emit({"exit_code": completed.returncode, "status": "malformed_cli_output"})
            return 2
        emit({
            "adapter_summary": payload.get("adapter_summary"),
            "evidence_count": payload.get("evidence_count"),
            "exit_code": completed.returncode,
            "next_state": payload.get("next_state"),
            # Two different units, kept visibly distinct (PR #49 review finding
            # #12): planned_count is planned TERMS; page_count/succeeded_pages
            # are EXECUTIONS (one per page). This smoke run never pages, so
            # they are equal here, but the field names must not imply that.
            "page_count": payload.get("page_count"),
            "planned_count": payload.get("planned_count"),
            "queries": [
                {"failure_kind": item.get("failure_kind"), "status": item.get("status")}
                for item in payload.get("queries", [])
            ],
            "status": payload.get("status"),
        })
        return completed.returncode
    finally:
        if not args.keep_run:
            cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
