#!/usr/bin/env python3
"""Build redacted, reproducible release evidence for one authoritative run."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import sqlite3
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from calibration_check import check_calibration
from patent_factory import __version__
from patent_factory.config import load_similarity_config
from patent_factory.database import SCHEMA_VERSION
from patent_factory.paths import (
    contained_input, contained_output, owner_only_file, private_contained_directory,
    private_root,
)
from patent_factory.privacy import environment_secret
from patent_factory.provenance import canonical_json, digest, strict_json_loads
from patent_factory.report import load_report_policy

RELEASE_VERSION = "release-evidence-v1"
MAX_PRIVACY_SCAN_BYTES = 1_000_000
E2E_EVIDENCE = {
    "E2E-1-redacted-happy-path": [
        "tests.integration.test_g004_cli.G004CliTests.test_cli_happy_path_is_private_deterministic_and_redacted",
        "tests.integration.test_g005_cli.G005CliTests.test_fixture_cli_runs_three_separate_finalist_groups",
        "tests.integration.test_g007_report_review_validation.G007ReportTests.test_cli_draft_review_validate_contract",
    ],
    "E2E-2-high-similarity-decision": [
        "tests.integration.test_g005_audit.G005AuditTests.test_identical_terms_make_separate_post_shortlist_groups_and_atomic_excessive_gate",
        "tests.integration.test_g006_decisions.G006DecisionTests.test_complete_retain_batch_approves_warns_is_private_and_replays",
    ],
    "E2E-3-source-failure-no-fabrication": [
        "tests.integration.test_g003_research_cli.ResearchCliTests.test_malformed_fixture_degrades_to_incomplete_without_fabricated_evidence",
        "tests.integration.test_g003_research_persistence.ResearchPersistenceTests.test_failure_persists_event_observation_and_limitation_but_no_evidence",
    ],
    "E2E-4-privacy-credential-egress": [
        "tests.integration.test_g003_research_cli.CredentialScriptTests.test_all_diagnostics_are_useful_offline_and_never_reveal_canary",
        "tests.integration.test_g005_audit.G005AuditTests.test_response_credential_canary_is_rejected_before_research_persistence",
        "tests.integration.test_g007_report_review_validation.G007SensitiveTests.test_external_share_requires_exact_one_use_approval",
    ],
    "E2E-5-legal-overclaim": [
        "tests.integration.test_g007_report_review_validation.G007ReportTests.test_validity_and_non_infringement_claims_fail_review_even_when_checkbox_passes",
        "tests.integration.test_g007_report_review_validation.G007ReportTests.test_legal_language_is_sentence_local_and_advisory_findings_are_allowed",
    ],
    "E2E-6-crash-concurrency-stale": [
        "tests.integration.test_g002_publish_register_integration.PublishRegisterIntegrationTests.test_concurrent_same_key_different_payloads_publish_only_the_winner",
        "tests.integration.test_g006_decisions.G006DecisionTests.test_competing_different_concurrent_decisions_have_one_winner",
        "tests.integration.test_g007_report_review_validation.G007ReportTests.test_report_publish_fault_boundaries_leave_no_authoritative_partial_state",
    ],
    "E2E-7-resumable-gates": [
        "tests.integration.test_g002_gate_matrix.GateMatrixTests.test_every_gate_kind_resumes_only_its_recorded_state_and_operation",
        "tests.integration.test_g006_decisions.G006DecisionTests.test_coverage_dispatch_and_fault_rollback",
        "tests.integration.test_g007_report_review_validation.G007SensitiveTests.test_share_approval_rejects_changed_scope_content_recipient_and_purpose",
    ],
}


def required_test_ids() -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        test_id
        for test_ids in E2E_EVIDENCE.values()
        for test_id in test_ids
    ))


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_command(argv: list[str], *, acceptable: set[int] | None = None) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    started_at = utc_now()
    result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, check=False)
    record = {
        "argv": argv,
        "ended_at": utc_now(),
        "exit_code": result.returncode,
        "started_at": started_at,
        "status": "passed" if result.returncode in (acceptable or {0}) else "failed",
        "stderr_hash": sha256_bytes(result.stderr.encode("utf-8")),
        "stdout_hash": sha256_bytes(result.stdout.encode("utf-8")),
    }
    return record, result


def _required_result_summary(
    result: unittest.TestResult, ids: Sequence[str], load_errors: int,
) -> dict[str, Any]:
    """Derive mandatory-test accounting from a real result, never from constants."""

    tests_run = result.testsRun
    skipped = len(result.skipped)
    failures = len(result.failures)
    errors = len(result.errors)
    unexpected = len(result.unexpectedSuccesses)
    expected_failures = len(result.expectedFailures)
    clean = (
        load_errors == 0
        and tests_run == len(ids)
        and skipped == 0
        and failures == 0
        and errors == 0
        and unexpected == 0
        and expected_failures == 0
    )
    return {
        "count": len(ids),
        "errors": errors,
        "expected_failures": expected_failures,
        "failures": failures,
        "ids": list(ids),
        "load_errors": load_errors,
        "reason": None,
        "skipped": skipped != 0,
        "skipped_count": skipped,
        "status": "passed" if clean else "failed",
        "tests_run": tests_run,
        "unexpected_successes": unexpected,
    }


def run_required_tests(ids: Sequence[str]) -> dict[str, Any]:
    """Run mandatory E2E ids in-process and record a machine-readable result.

    Every mandatory id must run exactly once and pass; any skip, failure, error,
    unexpected success, or load failure fails the gate. Captured test output is
    hashed, never echoed, so a leaked secret cannot reach the manifest.
    """

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    load_errors = 0
    for test_id in ids:
        try:
            suite.addTests(loader.loadTestsFromName(test_id))
        except Exception:
            load_errors += 1
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        result = unittest.TextTestRunner(stream=buffer, verbosity=0).run(suite)
    summary = _required_result_summary(result, ids, load_errors)
    summary["output_hash"] = sha256_bytes(buffer.getvalue().encode("utf-8"))
    return summary


def release_status(
    *, core_failed: bool, tests_status: str, calibration_status: str,
    required_tests_status: str = "passed",
) -> str:
    """Require core, test, and calibration evidence before a release can pass."""

    if core_failed or tests_status == "failed" or required_tests_status == "failed":
        return "failed"
    if (
        tests_status != "passed"
        or required_tests_status != "passed"
        or calibration_status != "eligible"
    ):
        return "review_blocked"
    return "passed"


def resolve_run(run_path: Path, run_id: str | None) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    if not run_path.is_absolute():
        raise ValueError("release_verifier.run: trusted absolute run path required")
    try:
        database_path = (run_path / "factory.sqlite3").relative_to(Path.cwd())
    except ValueError as exc:
        raise ValueError("release_verifier.database: path outside repository root") from exc
    database = contained_input(
        database_path, run_path, "release authoritative database",
    )
    uri = f"file:{quote(str(database))}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall()
        if run_id is None:
            if len(rows) != 1:
                raise ValueError("release_verifier.run_id: omitted id requires exactly one authoritative run")
            run_id = str(rows[0]["run_id"])
        if not any(row["run_id"] == run_id for row in rows):
            raise ValueError("release_verifier.run_id: run is absent from authoritative database")
        artifacts = [dict(row) for row in connection.execute(
            "SELECT ar.kind,ar.revision_id,ar.content_hash,ar.schema_version "
            "FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id=? AND ar.stale=0 ORDER BY ar.kind", (run_id,),
        )]
        validation = connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar "
            "JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id=? AND ca.kind='validation' AND ar.stale=0", (run_id,),
        ).fetchone()
    if validation is None:
        raise ValueError("release_verifier.validation: current validation artifact required")
    value = strict_json_loads(validation["content_json"])
    if not isinstance(value, dict) or value.get("status") != "passed":
        raise ValueError("release_verifier.validation: passed validation artifact required")
    versions = {
        "schema_versions": value.get("schema_versions"),
        "scoring_version": value.get("scoring_version"),
        "validator_version": value.get("validator_version"),
        "workflow_version": value.get("workflow_version"),
    }
    return run_id, artifacts, versions


def schema_hashes() -> dict[str, str]:
    return {
        path.name: sha256_bytes(path.read_bytes())
        for path in sorted((ROOT / "schemas").glob("*.json"))
    }


def _scan_payload(payload: str | bytes, secrets: Sequence[bytes]) -> tuple[bool, bool]:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(data) > MAX_PRIVACY_SCAN_BYTES:
        return False, True
    return any(secret in data for secret in secrets), False


def _scan_file(path: Path, secrets: Sequence[bytes]) -> tuple[bool, bool]:
    try:
        if path.stat(follow_symlinks=False).st_size > MAX_PRIVACY_SCAN_BYTES:
            return False, True
        return _scan_payload(path.read_bytes(), secrets)
    except OSError:
        return False, True


def privacy_scan(
    run_path: Path, canaries: Sequence[str], *,
    captured: Sequence[subprocess.CompletedProcess[str]] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    commands: list[dict[str, Any]] = []
    privacy_results = list(captured)
    tracked_record, tracked = run_command([
        "git", "ls-files", "-z", "--", ".env", "documents", "workspace", "reports",
    ])
    commands.append(tracked_record)
    privacy_results.append(tracked)
    tracked_paths = [item for item in tracked.stdout.split("\0") if item]
    allowed = {"documents/README.md", "workspace/README.md"}
    unsafe_tracked = sorted(path for path in tracked_paths if path not in allowed)
    # The restricted query above governs only the private-path tracking policy.
    # Content scanning must cover every tracked file so a canary committed under
    # src/, scripts/, config/, schemas/, or any ordinary path cannot slip through.
    content_record, content = run_command(["git", "ls-files", "-z"])
    commands.append(content_record)
    privacy_results.append(content)
    scan_paths = [item for item in content.stdout.split("\0") if item]
    roots_ignored = True
    for probe in (
        ".env", "documents/private-probe", "workspace/private-probe", "reports/private-probe",
    ):
        ignore_record, ignored = run_command(["git", "check-ignore", "--quiet", probe])
        commands.append(ignore_record)
        privacy_results.append(ignored)
        roots_ignored = roots_ignored and ignored.returncode == 0
    secrets = [value.encode("utf-8") for value in canaries if value]
    live_secret = environment_secret("KIPRIS_PLUS_API_KEY")
    if live_secret:
        secrets.append(live_secret.encode("utf-8"))
    if not run_path.is_absolute():
        raise ValueError("release_verifier.run: trusted absolute run path required")
    root = run_path
    run_files_scanned = 0
    tracked_files_scanned = 0
    command_streams_scanned = 0
    unscannable = 0
    canary_found = False
    for result in privacy_results:
        for payload in (result.stdout, result.stderr):
            found, oversized = _scan_payload(payload, secrets)
            command_streams_scanned += 1
            canary_found = canary_found or found
            unscannable += int(oversized)
    for tracked_path in scan_paths:
        try:
            path = contained_input(Path(tracked_path), ROOT, "tracked privacy file")
        except (OSError, ValueError):
            unscannable += 1
            continue
        found, unreadable = _scan_file(path, secrets)
        tracked_files_scanned += 1
        canary_found = canary_found or found
        unscannable += int(unreadable)
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                unscannable += 1
                continue
            if not path.is_file():
                continue
            found, unreadable = _scan_file(path, secrets)
            run_files_scanned += 1
            canary_found = canary_found or found
            unscannable += int(unreadable)
    status = (
        "passed"
        if not unsafe_tracked and roots_ignored and not canary_found and not unscannable
        else "failed"
    )
    return {
        "canaries_absent": not canary_found,
        "command_stream_count": command_streams_scanned,
        "ignored_private_roots": roots_ignored,
        "run_file_count": run_files_scanned,
        "status": status,
        "tracked_file_count": tracked_files_scanned,
        "tracked_private_path_count": len(unsafe_tracked),
        "tracked_private_paths_hash": sha256_bytes(canonical_json(unsafe_tracked).encode("utf-8")),
        "unscannable_count": unscannable,
    }, commands


def _fsync_directory(path: Path) -> None:
    """Fsync a directory so a published entry survives a crash.

    Platforms and filesystems that do not support directory fsync fail softly;
    the release output is still linked, we only forgo the extra durability barrier.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_manifest(path: Path, manifest: dict[str, Any], workspace_root: Path) -> None:
    root = Path(workspace_root).resolve(strict=True)
    parent = (Path.cwd() / path).parent.resolve(strict=False)
    if parent != root:
        private_contained_directory(path.parent, root, "release output parent", create=True)
    destination = contained_output(path, root, "release output")
    payload = canonical_json(manifest).encode("utf-8") + b"\n"
    if destination.exists():
        owner_only_file(destination)
        if destination.stat().st_size == len(payload) and destination.read_bytes() == payload:
            return
        raise FileExistsError("release output already exists with different content")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        owner_only_file(temporary)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            owner_only_file(destination)
            if destination.stat().st_size == len(payload) and destination.read_bytes() == payload:
                return
            raise FileExistsError("release output already exists with different content") from None
        owner_only_file(destination)
        _fsync_directory(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
            _fsync_directory(destination.parent)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--calibration-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--canary", action="append", default=[])
    parser.add_argument("--skip-tests", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace_root = private_root(
        args.workspace_root or Path("workspace"), "release workspace root",
    )
    run_root = contained_input(args.run, workspace_root, "release run", directory=True)
    commands: list[dict[str, Any]] = []
    captured: list[subprocess.CompletedProcess[str]] = []
    test_argv = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    required_ids = required_test_ids()
    if args.skip_tests:
        tests = {"argv": test_argv, "reason": "explicit_skip", "status": "skipped"}
        required_tests = {
            "count": len(required_ids), "ids": list(required_ids),
            "reason": "explicit_skip", "skipped": True, "status": "skipped",
        }
    else:
        record, result = run_command(test_argv)
        commands.append(record)
        captured.append(result)
        tests = {**record, "reason": None}
        required_tests = run_required_tests(required_ids)
    record, result = run_command([sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests"])
    commands.append(record)
    captured.append(result)
    validate = [sys.executable, "scripts/validate_run.py", "--run", str(args.run)]
    if args.run_id:
        validate.extend(("--run-id", args.run_id))
    if args.workspace_root:
        validate.extend(("--workspace-root", str(args.workspace_root)))
    record, result = run_command(validate)
    commands.append(record)
    captured.append(result)
    validation_passed = record["status"] == "passed"
    credential_record, credential = run_command(
        [sys.executable, "scripts/check_credentials.py"], acceptable={0, 1},
    )
    commands.append(credential_record)
    captured.append(credential)
    privacy, privacy_commands = privacy_scan(run_root, args.canary, captured=captured)
    commands.extend(privacy_commands)
    calibration = check_calibration(args.calibration_manifest)
    if not validation_passed:
        run_id, artifacts, validation_versions = args.run_id, [], {}
        run_error = "validation_command_failed"
    else:
        try:
            run_id, artifacts, validation_versions = resolve_run(run_root, args.run_id)
            run_error = None
        except (OSError, sqlite3.Error, UnicodeError, ValueError) as exc:
            run_id, artifacts, validation_versions = args.run_id, [], {}
            run_error = type(exc).__name__
    scorer = load_similarity_config()
    policy = load_report_policy()
    core_failed = run_error is not None or privacy["status"] != "passed" or any(
        item["status"] == "failed" for item in commands
    ) or required_tests["status"] == "failed"
    outcome = release_status(
        core_failed=core_failed,
        tests_status=str(tests["status"]),
        calibration_status=calibration.release_status,
        required_tests_status=str(required_tests["status"]),
    )
    manifest = {
        "artifacts": artifacts,
        "calibration": calibration.as_dict(),
        "commands": commands,
        "credential_coverage": {
            "credential_configuration": "present" if credential.returncode == 0 else "missing",
            "live_credential_test": "skipped_no_network",
            "network_egress": "not_exercised",
        },
        "e2e_evidence": E2E_EVIDENCE,
        "generated_at": utc_now(),
        "platform": {
            "machine": platform.machine(), "release": platform.release(),
            "system": platform.system(),
        },
        "privacy": privacy,
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "release_status": outcome,
        "required_tests": required_tests,
        "run_error": run_error,
        "run_id": run_id,
        "schema_version": RELEASE_VERSION,
        "tests": tests,
        "versions": {
            "database_schema": SCHEMA_VERSION,
            "package": __version__,
            "report_policy": {"content_hash": digest(policy), "version": policy["version"]},
            "schemas": schema_hashes(),
            "scorer": {"content_hash": scorer.content_hash, "version": scorer.version},
            "validation": validation_versions,
        },
    }
    if args.output:
        write_manifest(args.output, manifest, workspace_root)
    print(canonical_json(manifest))
    return 2 if outcome == "failed" else 3 if outcome == "review_blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
