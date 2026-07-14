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

from patent_factory.cli import main
from patent_factory.database import (
    InjectedFailure, RunIdResolutionError, connect_database, export_profile, ingest,
    resolve_run_id,
)
from patent_factory.models import RunState
from patent_factory.paths import PathPolicyError, private_contained_directory
from patent_factory.privacy import DeletionReport
from patent_factory.profile import interview_facts
from patent_factory.runs import start_run
from patent_factory.state import StateStore


ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args, environment=None):
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT / "src")
    if environment:
        merged.update(environment)
    return subprocess.run(
        [sys.executable, "-m", "patent_factory", *map(str, args)], cwd=ROOT,
        env=merged, text=True, capture_output=True, check=False,
    )


class G008CliCoreTests(unittest.TestCase):
    def setUp(self):
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.outside_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.workspace = Path(self.workspace_context.name)
        self.outside = Path(self.outside_context.name)
        self.profile_database = self.workspace / "profile.sqlite3"
        self.profile_export = self.workspace / "profile.json"
        with connect_database(self.profile_database) as connection:
            ingest(connection, "interview", interview_facts({
                "expertise": "센서 제어 구현", "name": "redacted inventor",
                "project_summary": "센서 오차 저감", "technical_domain": "센서 시스템",
            }))
            self.profile = export_profile(connection, self.profile_export)

    def tearDown(self):
        self.outside_context.cleanup()
        self.workspace_context.cleanup()

    def relative(self, path):
        return Path(path).relative_to(ROOT)

    def start_args(self, run_root, run_id="run-1"):
        return (
            "run", "start", "--run", self.relative(run_root), "--run-id", run_id,
            "--profile", self.relative(self.profile_export),
            "--profile-database", self.relative(self.profile_database),
            "--workspace-root", self.relative(self.workspace),
        )

    def assert_envelope(self, payload, command):
        self.assertEqual(payload["schema_version"], "cli-result-v1")
        self.assertEqual(payload["envelope_version"], "cli-envelope-v1")
        self.assertEqual(payload["command"], command)
        for field in (
            "artifact_ids", "ended_at", "event_ids", "failure_code", "next_state",
            "prior_state", "started_at",
        ):
            self.assertIn(field, payload)

    def test_run_start_bootstraps_registered_profile_context_and_exactly_replays(self):
        run_root = self.workspace / "runs" / "run-1"
        first = run_cli(*self.start_args(run_root))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        payload = json.loads(first.stdout)
        self.assert_envelope(payload, "run.start")
        self.assertEqual((payload["prior_state"], payload["next_state"]), ("new", "research_ready"))
        self.assertFalse(payload["replayed"])
        self.assertEqual(len(payload["artifact_ids"]), 1)
        self.assertEqual(len(payload["event_ids"]), 4)
        self.assertIsNone(payload["failure_code"])
        self.assertEqual(stat.S_IMODE(run_root.stat().st_mode), 0o700)
        exported = Path(payload["export_path"])
        self.assertEqual(stat.S_IMODE(exported.stat().st_mode), 0o600)

        with connect_database(run_root / "factory.sqlite3") as connection:
            store = StateStore(connection, export_directories=(run_root / "bootstrap-exports",))
            snapshot = store.snapshot("run-1")
            self.assertEqual(snapshot.state, RunState.RESEARCH_READY)
            self.assertIn("profile_context", snapshot.current_revisions)
            transitions = [tuple(row) for row in connection.execute(
                "SELECT prior_state,next_state FROM transition_events ORDER BY created_at,event_id"
            )]
            self.assertEqual(transitions, [
                ("new", "new"), ("new", "profile_pending"),
                ("profile_pending", "profile_ready"), ("profile_ready", "research_ready"),
            ])
            before = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("artifact_exports", "artifact_revisions", "idempotency_records", "transition_events")
            }

        second = run_cli(*self.start_args(run_root))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        replay = json.loads(second.stdout)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["artifact_ids"], payload["artifact_ids"])
        self.assertEqual(replay["event_ids"], payload["event_ids"])
        with connect_database(run_root / "factory.sqlite3") as connection:
            after = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in before
            }
        self.assertEqual(after, before)

    def test_run_start_recovers_atomic_publication_failure_and_orphan_export(self):
        run_root = self.workspace / "runs" / "recover"
        run_root.mkdir(parents=True, mode=0o700)
        with connect_database(self.profile_database) as profile_connection, connect_database(run_root / "factory.sqlite3") as connection:
            with self.assertRaises(InjectedFailure):
                start_run(
                    connection, profile_connection=profile_connection, run_root=run_root,
                    run_id="recover", profile=self.profile,
                    fault_at=("profile_context", "after_state"),
                )
            self.assertEqual(StateStore(connection).snapshot("recover").state, RunState.PROFILE_READY)
            self.assertEqual(connection.execute("SELECT count(*) FROM artifact_exports").fetchone()[0], 0)
            self.assertEqual(len(tuple((run_root / "bootstrap-exports").glob("ar_*.json"))), 1)

            recovered = start_run(
                connection, profile_connection=profile_connection, run_root=run_root,
                run_id="recover", profile=self.profile,
            )
            self.assertEqual(recovered.next_state, "research_ready")
            self.assertFalse(recovered.replayed)
            self.assertEqual(len(tuple((run_root / "bootstrap-exports").glob("ar_*.json"))), 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM artifact_exports").fetchone()[0], 1)

    def test_common_envelope_preserves_payload_and_redacts_error_canary(self):
        initialized = run_cli(
            "init", "--documents", self.relative(self.workspace / "docs"),
            "--workspace", self.relative(self.workspace / "private"),
        )
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        ready = json.loads(initialized.stdout)
        self.assert_envelope(ready, "init")
        self.assertEqual(ready["status"], "ready")
        self.assertIn("created", ready)

        canary = "G008-CLI-ENVELOPE-CANARY"
        canary_database = self.workspace / "canary-profile.sqlite3"
        canary_export = self.workspace / "canary-profile.json"
        with connect_database(canary_database) as connection:
            ingest(connection, "interview", interview_facts({"technical_domain": canary}))
            export_profile(connection, canary_export)
        blocked_root = self.workspace / "runs" / "canary"
        blocked = run_cli(
            "run", "start", "--run", self.relative(blocked_root),
            "--run-id", "canary", "--profile", self.relative(canary_export),
            "--profile-database", self.relative(canary_database),
            "--workspace-root", self.relative(self.workspace),
            environment={"KIPRIS_PLUS_API_KEY": canary},
        )
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertNotIn(canary, blocked.stdout + blocked.stderr)
        failure = json.loads(blocked.stdout)
        self.assert_envelope(failure, "run.start")
        self.assertEqual(failure["failure_code"], "invalid_input")
        self.assertEqual(failure["status"], "error")
        self.assertFalse(blocked_root.exists())

    def test_invalid_changed_or_not_ready_profiles_leave_no_run_residue(self):
        changed_export = self.workspace / "changed-profile.json"
        changed = dict(self.profile)
        changed["profile_revision"] = "changed"
        changed_export.write_text(json.dumps(changed), encoding="utf-8")

        pending_database = self.workspace / "pending-profile.sqlite3"
        pending_export = self.workspace / "pending-profile.json"
        with connect_database(pending_database) as connection:
            export_profile(connection, pending_export)

        cases = (
            ("changed", changed_export, self.profile_database),
            ("not-ready", pending_export, pending_database),
        )
        for name, profile_export, profile_database in cases:
            run_root = self.workspace / "runs" / name
            result = run_cli(
                "run", "start", "--run", self.relative(run_root), "--run-id", name,
                "--profile", self.relative(profile_export),
                "--profile-database", self.relative(profile_database),
                "--workspace-root", self.relative(self.workspace),
            )
            with self.subTest(name=name):
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(run_root.exists())

    def test_validate_infers_only_one_authoritative_run_and_rejects_mismatch(self):
        from tests.integration.test_g007_report_review_validation import G007Fixture

        case = G007Fixture(methodName="runTest")
        case.setUp()
        try:
            case.complete()
            validated = run_cli(
                "validate", "--run", case.run_root.relative_to(ROOT),
                "--workspace-root", case.workspace.relative_to(ROOT),
            )
            self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
            payload = json.loads(validated.stdout)
            self.assert_envelope(payload, "validate")
            self.assertEqual(payload["run_id"], "run")
            self.assertEqual(payload["next_state"], "complete")

            mismatch = run_cli(
                "validate", "--run", case.run_root.relative_to(ROOT), "--run-id", "other",
                "--workspace-root", case.workspace.relative_to(ROOT),
            )
            self.assertEqual(mismatch.returncode, 2, mismatch.stdout + mismatch.stderr)
            self.assertEqual(json.loads(mismatch.stdout)["failure_code"], "run_id_mismatch")
        finally:
            case.tearDown()

        empty = self.workspace / "empty-run"
        empty.mkdir(mode=0o700)
        with connect_database(empty / "factory.sqlite3"):
            pass
        missing = run_cli(
            "validate", "--run", self.relative(empty),
            "--workspace-root", self.relative(self.workspace),
        )
        self.assertEqual(json.loads(missing.stdout)["failure_code"], "run_id_missing")

        ambiguous = self.workspace / "ambiguous-run"
        ambiguous.mkdir(mode=0o700)
        with connect_database(ambiguous / "factory.sqlite3") as connection:
            StateStore(connection).create_run("one")
            StateStore(connection).create_run("two")
        result = run_cli(
            "validate", "--run", self.relative(ambiguous),
            "--workspace-root", self.relative(self.workspace),
        )
        self.assertEqual(json.loads(result.stdout)["failure_code"], "run_id_ambiguous")

        with connect_database(empty / "factory.sqlite3") as connection:
            with self.assertRaises(RunIdResolutionError) as caught:
                resolve_run_id(connection)
        self.assertEqual(caught.exception.code, "run_id_missing")

    def test_private_directory_permission_enforcement_fails_closed(self):
        existing = self.workspace / "runs" / "permission-check"
        existing.mkdir(parents=True, mode=0o700)
        with patch.object(Path, "chmod", side_effect=PermissionError("denied")):
            with self.assertRaises(PathPolicyError):
                private_contained_directory(
                    self.relative(existing), self.workspace, "run root", create=True,
                )

        run_root = self.workspace / "runs" / "bootstrap-permission-check"
        run_root.mkdir(mode=0o700)
        with connect_database(run_root / "factory.sqlite3") as connection, connect_database(
            self.profile_database
        ) as profile_connection, patch.object(
            Path, "chmod", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(PathPolicyError):
                start_run(
                    connection, profile_connection=profile_connection, run_root=run_root,
                    run_id="permission-check", profile=self.profile,
                )
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_delete_run_is_path_scoped_preserves_links_and_reports_partial_failure(self):
        run_root = self.workspace / "runs" / "delete-me"
        sibling = self.workspace / "runs" / "keep-me"
        run_root.mkdir(parents=True, mode=0o700)
        sibling.mkdir(mode=0o700)
        (run_root / "factory.sqlite3").write_text("private", encoding="utf-8")
        (sibling / "keep").write_text("sibling", encoding="utf-8")
        (self.outside / "keep").write_text("outside", encoding="utf-8")
        try:
            (run_root / "outside-link").symlink_to(self.outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        deleted = run_cli(
            "delete-run", "--run", self.relative(run_root),
            "--workspace-root", self.relative(self.workspace),
        )
        self.assertEqual(deleted.returncode, 0, deleted.stdout + deleted.stderr)
        payload = json.loads(deleted.stdout)
        self.assert_envelope(payload, "delete-run")
        self.assertEqual(payload["status"], "deleted")
        self.assertFalse(run_root.exists())
        self.assertEqual((sibling / "keep").read_text(encoding="utf-8"), "sibling")
        self.assertEqual((self.outside / "keep").read_text(encoding="utf-8"), "outside")

        partial = self.workspace / "runs" / "partial"
        partial.mkdir(mode=0o700)
        fake = DeletionReport(
            "runs/partial", (), ({"code": "PermissionError", "path": "runs/partial/blocked"},),
        )
        output = io.StringIO()
        with patch("patent_factory.cli.delete_run", return_value=fake), redirect_stdout(output):
            code = main([
                "delete-run", "--run", str(self.relative(partial)),
                "--workspace-root", str(self.relative(self.workspace)),
            ])
        self.assertEqual(code, 11)
        failed = json.loads(output.getvalue())
        self.assert_envelope(failed, "delete-run")
        self.assertEqual(failed["status"], "partial_failure")
        self.assertEqual(failed["failures"], list(fake.failures))

    def test_invalid_arguments_emit_one_json_envelope_while_help_and_version_stay_plain(self):
        for argv in ((), ("unknown-command",), ("run", "start", "--run")):
            with self.subTest(argv=argv):
                result = run_cli(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, "")
                payload = json.loads(result.stdout)
                self.assert_envelope(payload, "unknown")
                self.assertEqual(payload["failure_code"], "invalid_arguments")
                self.assertEqual(payload["status"], "error")
                self.assertEqual(len(result.stdout.strip().splitlines()), 1)

        for option in ("--help", "--version"):
            with self.subTest(option=option):
                result = run_cli(option)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
