from __future__ import annotations

import copy
import importlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import calibration_check
from calibration_check import check_calibration, validate_calibration_manifest
from patent_factory.config import load_similarity_config
from patent_factory.provenance import digest
import release_verify

E2E_EVIDENCE = release_verify.E2E_EVIDENCE
from tests.integration.test_g007_report_review_validation import G007Fixture


def run_script(name: str, *args: object) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / name), *map(str, args)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )


def calibration_manifest() -> dict[str, object]:
    scorer = load_similarity_config()
    reviewers = [
        {
            "affiliation": "independent-domain-lab", "independent": True,
            "reviewed_at": "2026-07-14T00:00:00Z", "reviewer_id": "domain-reviewer",
            "role": "domain_expert",
        },
        {
            "affiliation": "independent-patent-practice", "independent": True,
            "reviewed_at": "2026-07-14T00:01:00Z", "reviewer_id": "patent-reviewer",
            "role": "patent_attorney",
        },
    ]
    return {
        "calibration_kind": "independent_redacted",
        "producer_id": "release-producer",
        "records": [{
            "case_id": "redacted-case-1",
            "corpus_hash": digest({"corpus": 1}),
            "disagreement": {"present": False, "resolution_hash": None},
            "input_hash": digest({"input": 1}),
            "labels": [
                {
                    "rationale_hash": digest({"reason": reviewer["reviewer_id"]}),
                    "reviewer_id": reviewer["reviewer_id"], "risk_label": "moderate",
                }
                for reviewer in reviewers
            ],
            "threshold_sensitivity_hash": digest({"sensitivity": 1}),
        }],
        "reviewers": reviewers,
        "schema_version": "calibration-manifest-v1",
        "scorer_config_hash": scorer.content_hash,
        "scorer_version": scorer.version,
    }


class CalibrationGateTests(unittest.TestCase):
    def test_absent_evidence_is_deferred_and_review_blocked(self):
        result = check_calibration(None)
        self.assertEqual(result.calibration_status, "deferred_provisional")
        self.assertEqual(result.release_status, "review_blocked")
        self.assertEqual(result.code, "independent_calibration_absent")

    def test_synthetic_self_review_version_mismatch_and_extra_fields_are_rejected(self):
        mutations = {}
        synthetic = calibration_manifest()
        synthetic["calibration_kind"] = "synthetic"
        mutations["synthetic"] = synthetic
        self_review = calibration_manifest()
        self_review["producer_id"] = "domain-reviewer"
        mutations["self_review"] = self_review
        mismatch = calibration_manifest()
        mismatch["scorer_version"] = "simrisk-v0"
        mutations["version_mismatch"] = mismatch
        extra = calibration_manifest()
        extra["unqualified_notes"] = "not evidence"
        mutations["extra_field"] = extra
        for name, value in mutations.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_calibration_manifest(copy.deepcopy(value))

    def test_structurally_qualified_labels_require_fixed_repo_trust(self):
        first = validate_calibration_manifest(calibration_manifest())
        second = validate_calibration_manifest(copy.deepcopy(calibration_manifest()))
        self.assertEqual(first.calibration_status, "qualified_independent")
        self.assertEqual(first.release_status, "review_blocked")
        self.assertEqual(first.code, "calibration_untrusted")
        self.assertEqual(first.manifest_hash, second.manifest_hash)
        self.assertEqual((first.record_count, first.reviewer_count), (1, 2))

        trust = calibration_check.load_calibration_trust()
        self.assertEqual(trust["schema_version"], "calibration-trust-v1.0.0")
        self.assertIsNone(trust["approved_manifest_hash"])
        trusted = calibration_check.apply_calibration_trust(
            first,
            {
                "approved_manifest_hash": first.manifest_hash,
                "schema_version": "calibration-trust-v1.0.0",
            },
        )
        self.assertEqual(trusted.release_status, "eligible")
        self.assertEqual(trusted.code, "calibration_trusted")

        with tempfile.TemporaryDirectory(dir=ROOT / "workspace") as directory:
            manifest_path = Path(directory) / "local-structural-claim.json"
            manifest_path.write_text(json.dumps(calibration_manifest()), encoding="utf-8")
            local = check_calibration(manifest_path)
        self.assertEqual(local.calibration_status, "qualified_independent")
        self.assertEqual(local.release_status, "review_blocked")

    def test_malformed_secret_is_redacted_and_review_times_are_canonical_utc(self):
        secret = "G008-CALIBRATION-SECRET-CANARY"
        with tempfile.TemporaryDirectory(dir=ROOT / "workspace") as directory:
            malformed = Path(directory) / "malformed.json"
            malformed.write_text('{"secret":"' + secret, encoding="utf-8")
            result = check_calibration(malformed)
        self.assertEqual(result.calibration_status, "deferred_provisional")
        self.assertEqual(result.release_status, "review_blocked")
        self.assertNotIn(secret, result.code)

        for reviewed_at in (
            "2026-07-14T00:00:00+00:00",
            "2026-07-14T00:00:00.000Z",
            "2026-7-14T00:00:00Z",
            "not-a-timestamp",
        ):
            manifest = calibration_manifest()
            manifest["reviewers"][0]["reviewed_at"] = reviewed_at
            with self.subTest(reviewed_at=reviewed_at), self.assertRaises(ValueError):
                validate_calibration_manifest(manifest)


class ReleaseEvidenceTests(unittest.TestCase):
    def test_seven_scenario_map_references_real_integration_tests(self):
        self.assertEqual(
            set(E2E_EVIDENCE),
            {
                "E2E-1-redacted-happy-path", "E2E-2-high-similarity-decision",
                "E2E-3-source-failure-no-fabrication", "E2E-4-privacy-credential-egress",
                "E2E-5-legal-overclaim", "E2E-6-crash-concurrency-stale",
                "E2E-7-resumable-gates",
            },
        )
        for scenario, references in E2E_EVIDENCE.items():
            self.assertGreaterEqual(len(references), 2, scenario)
            for reference in references:
                module_name, class_name, method_name = reference.rsplit(".", 2)
                case = getattr(importlib.import_module(module_name), class_name)
                self.assertTrue(callable(getattr(case, method_name)), reference)

    def test_completed_run_materializes_and_path_only_validation_replays(self):
        case = G007Fixture(methodName="runTest")
        case.setUp()
        try:
            _report, _review, validation = case.complete()
            run_path = case.run_root.relative_to(ROOT)
            before = case.connection.execute(
                "SELECT state,state_version FROM runs WHERE run_id='run'"
            ).fetchone()
            revision_count = case.connection.execute(
                "SELECT COUNT(*) FROM artifact_revisions WHERE run_id='run'"
            ).fetchone()[0]
            result = run_script("validate_run.py", "--run", run_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], "cli-result-v1")
            self.assertEqual(payload["command"], "validate")
            self.assertEqual(payload["run_id"], "run")
            self.assertTrue(payload["replayed"])
            self.assertIn(validation.artifact.revision_id, payload["artifact_ids"])
            after = case.connection.execute(
                "SELECT state,state_version FROM runs WHERE run_id='run'"
            ).fetchone()
            self.assertEqual(tuple(before), tuple(after))
            self.assertEqual(
                case.connection.execute(
                    "SELECT COUNT(*) FROM artifact_revisions WHERE run_id='run'"
                ).fetchone()[0],
                revision_count,
            )
        finally:
            case.tearDown()

    def test_release_manifest_is_review_blocked_without_labels_and_detects_canary(self):
        case = G007Fixture(methodName="runTest")
        case.setUp()
        try:
            case.complete()
            run_path = case.run_root.relative_to(ROOT)
            result = run_script("release_verify.py", "--run", run_path, "--skip-tests")
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["schema_version"], "release-evidence-v1")
            self.assertEqual(manifest["release_status"], "review_blocked")
            self.assertEqual(manifest["calibration"]["calibration_status"], "deferred_provisional")
            self.assertEqual(manifest["calibration"]["release_status"], "review_blocked")
            self.assertEqual(manifest["privacy"]["status"], "passed")
            self.assertEqual(manifest["run_id"], "run")
            self.assertEqual(len(manifest["e2e_evidence"]), 7)
            self.assertIn("calibration-manifest.schema.json", manifest["versions"]["schemas"])
            self.assertTrue(all(command["status"] == "passed" for command in manifest["commands"]))

            canary = "G008-RELEASE-PRIVATE-CANARY"
            (case.run_root / "private-canary.bin").write_text(canary, encoding="utf-8")
            failed = run_script(
                "release_verify.py", "--run", run_path, "--skip-tests", "--canary", canary,
            )
            self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
            failed_manifest = json.loads(failed.stdout)
            self.assertEqual(failed_manifest["release_status"], "failed")
            self.assertFalse(failed_manifest["privacy"]["canaries_absent"])
            self.assertNotIn(canary, failed.stdout)
        finally:
            case.tearDown()

    def test_skipped_tests_are_explicit_and_cannot_pass_with_qualified_calibration(self):
        case = G007Fixture(methodName="runTest")
        case.setUp()
        try:
            case.complete()
            run_path = case.run_root.relative_to(ROOT)
            calibration_path = case.workspace / "qualified-calibration.json"
            calibration_path.write_text(json.dumps(calibration_manifest()), encoding="utf-8")
            result = run_script(
                "release_verify.py", "--run", run_path, "--skip-tests",
                "--workspace-root", case.workspace.relative_to(ROOT),
                "--calibration-manifest", calibration_path,
            )
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["tests"]["status"], "skipped")
            self.assertEqual(manifest["tests"]["reason"], "explicit_skip")
            self.assertEqual(manifest["calibration"]["release_status"], "review_blocked")
            self.assertEqual(manifest["release_status"], "review_blocked")
            self.assertTrue(manifest["required_tests"]["skipped"])
            self.assertEqual(manifest["required_tests"]["status"], "skipped")
        finally:
            case.tearDown()

    def test_release_status_requires_core_tests_and_calibration_without_running_suite(self):
        self.assertEqual(
            release_verify.release_status(
                core_failed=False, tests_status="passed", calibration_status="eligible",
            ),
            "passed",
        )
        self.assertEqual(
            release_verify.release_status(
                core_failed=False, tests_status="skipped", calibration_status="eligible",
            ),
            "review_blocked",
        )
        self.assertEqual(
            release_verify.release_status(
                core_failed=True, tests_status="passed", calibration_status="eligible",
            ),
            "failed",
        )
        self.assertEqual(
            release_verify.release_status(
                core_failed=False, tests_status="passed", calibration_status="eligible",
                required_tests_status="failed",
            ),
            "failed",
        )

    def test_verifier_rejects_uncontained_runs_before_commands_or_reads(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "workspace") as workspace_directory:
            workspace = Path(workspace_directory)
            outside = workspace.parent / f"{workspace.name}-outside"
            outside.mkdir(mode=0o700)
            symlink = workspace / "linked-run"
            try:
                symlink.symlink_to(outside, target_is_directory=True)
            except OSError:
                symlink = None
            candidates = [outside.relative_to(ROOT), workspace.relative_to(ROOT) / ".." / outside.name]
            if symlink is not None:
                candidates.append(symlink.relative_to(ROOT))
            try:
                for candidate in candidates:
                    with self.subTest(candidate=candidate), patch.object(
                        release_verify, "run_command"
                    ) as run_command, patch.object(
                        release_verify, "privacy_scan"
                    ) as privacy_scan, patch.object(
                        release_verify, "resolve_run"
                    ) as resolve_run, self.assertRaises(ValueError):
                        release_verify.main([
                            "--run", str(candidate), "--skip-tests",
                            "--workspace-root", str(workspace.relative_to(ROOT)),
                        ])
                    run_command.assert_not_called()
                    privacy_scan.assert_not_called()
                    resolve_run.assert_not_called()
            finally:
                outside.rmdir()

    def test_release_output_is_workspace_contained_and_owner_only(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "workspace") as workspace_directory:
            workspace = Path(workspace_directory)
            output = workspace / "release" / "manifest.json"
            release_verify.write_manifest(
                output.relative_to(ROOT), {"release_status": "review_blocked"}, workspace,
            )
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["release_status"], "review_blocked")
            original = output.read_bytes()
            original_stat = output.stat()
            with patch.object(release_verify.os, "replace") as replace:
                release_verify.write_manifest(
                    output.relative_to(ROOT), {"release_status": "review_blocked"}, workspace,
                )
            replace.assert_not_called()
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(output.stat().st_ino, original_stat.st_ino)
            with self.assertRaises(FileExistsError):
                release_verify.write_manifest(
                    output.relative_to(ROOT), {"release_status": "passed"}, workspace,
                )
            self.assertEqual(output.read_bytes(), original)
            with self.assertRaises(ValueError):
                release_verify.write_manifest(
                    workspace.relative_to(ROOT) / ".." / "outside.json", {}, workspace,
                )

    def test_database_leaf_symlink_is_rejected_before_sqlite_read(self):
        case = G007Fixture(methodName="runTest")
        case.setUp()
        try:
            case.complete()
            linked_run = case.workspace / "linked-run"
            linked_run.mkdir(mode=0o700)
            try:
                (linked_run / "factory.sqlite3").symlink_to(case.run_root / "factory.sqlite3")
            except OSError:
                self.skipTest("symlinks unavailable")
            with patch.object(release_verify.sqlite3, "connect") as connect, self.assertRaises(ValueError):
                release_verify.resolve_run(linked_run, "run")
            connect.assert_not_called()
        finally:
            case.tearDown()

    def test_failed_core_validation_never_reads_authoritative_database(self):
        case = G007Fixture(methodName="runTest")
        case.setUp()
        try:
            case.complete()
            results = iter((
                subprocess.CompletedProcess(["compile"], 0, "", ""),
                subprocess.CompletedProcess(["validate"], 2, "", "validation failed"),
                subprocess.CompletedProcess(["credentials"], 1, "", ""),
            ))

            def fake_run(_argv, *, acceptable=None):
                completed = next(results)
                accepted = acceptable or {0}
                return {
                    "exit_code": completed.returncode,
                    "status": "passed" if completed.returncode in accepted else "failed",
                }, completed

            output = io.StringIO()
            with patch.object(release_verify, "run_command", side_effect=fake_run), patch.object(
                release_verify, "privacy_scan",
                return_value=({"canaries_absent": True, "status": "passed"}, []),
            ), patch.object(release_verify, "resolve_run") as resolve_run, redirect_stdout(output):
                code = release_verify.main([
                    "--run", str(case.run_root.relative_to(ROOT)), "--run-id", "run",
                    "--workspace-root", str(case.workspace.relative_to(ROOT)), "--skip-tests",
                ])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["run_error"], "validation_command_failed")
            resolve_run.assert_not_called()
        finally:
            case.tearDown()

    def test_privacy_scan_covers_subprocess_output_and_tracked_file_bytes(self):
        canary = "G008-EPHEMERAL-PRIVACY-CANARY"
        with tempfile.TemporaryDirectory(dir=ROOT / "workspace") as directory:
            run_root = Path(directory)
            tracked = run_root / "tracked.txt"
            tracked.write_text("safe", encoding="utf-8")

            def fake_git(argv, *, acceptable=None):
                stdout = str(tracked.relative_to(ROOT)) + "\0" if "ls-files" in argv else ""
                completed = subprocess.CompletedProcess(argv, 0, stdout, "")
                return {
                    "exit_code": 0, "status": "passed",
                    "stderr_hash": release_verify.sha256_bytes(b""),
                    "stdout_hash": release_verify.sha256_bytes(stdout.encode("utf-8")),
                }, completed

            captured = (subprocess.CompletedProcess(["helper"], 0, canary, ""),)
            with patch.object(release_verify, "run_command", side_effect=fake_git):
                subprocess_leak, records = release_verify.privacy_scan(
                    run_root, (canary,), captured=captured,
                )
            self.assertEqual(subprocess_leak["status"], "failed")
            self.assertFalse(subprocess_leak["canaries_absent"])
            self.assertNotIn(canary, json.dumps([subprocess_leak, records]))

            tracked.write_text(canary, encoding="utf-8")
            with patch.object(release_verify, "run_command", side_effect=fake_git):
                tracked_leak, records = release_verify.privacy_scan(
                    run_root, (canary,), captured=(),
                )
            self.assertEqual(tracked_leak["status"], "failed")
            self.assertGreaterEqual(tracked_leak["tracked_file_count"], 1)
            self.assertNotIn(canary, json.dumps([tracked_leak, records]))

    def test_required_e2e_ids_execute_as_explicit_nonplaintext_gate(self):
        case = G007Fixture(methodName="runTest")
        case.setUp()
        try:
            case.complete()
            calls = []

            def fake_run(argv, *, acceptable=None):
                calls.append(list(argv))
                returncode = 1 if "check_credentials.py" in argv else 0
                completed = subprocess.CompletedProcess(argv, returncode, "", "")
                accepted = acceptable or {0}
                return {
                    "ended_at": "2026-07-15T00:00:00Z",
                    "exit_code": returncode,
                    "started_at": "2026-07-15T00:00:00Z",
                    "status": "passed" if returncode in accepted else "failed",
                    "stderr_hash": release_verify.sha256_bytes(b""),
                    "stdout_hash": release_verify.sha256_bytes(b""),
                }, completed

            output = io.StringIO()
            with patch.object(release_verify, "run_command", side_effect=fake_run), patch.object(
                release_verify, "privacy_scan",
                return_value=({"canaries_absent": True, "status": "passed"}, []),
            ), patch.object(
                release_verify, "resolve_run", return_value=("run", [], {}),
            ), redirect_stdout(output):
                code = release_verify.main([
                    "--run", str(case.run_root.relative_to(ROOT)), "--run-id", "run",
                    "--workspace-root", str(case.workspace.relative_to(ROOT)),
                ])
            manifest = json.loads(output.getvalue())
            required_ids = release_verify.required_test_ids()
            self.assertIn([sys.executable, "-m", "unittest", *required_ids], calls)
            self.assertEqual(manifest["required_tests"]["ids"], list(required_ids))
            self.assertEqual(manifest["required_tests"]["count"], len(required_ids))
            self.assertEqual(manifest["required_tests"]["status"], "passed")
            self.assertFalse(manifest["required_tests"]["skipped"])
            self.assertNotIn("argv", manifest["required_tests"])
            self.assertEqual(code, 3)
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
