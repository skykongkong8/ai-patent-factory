import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database
from patent_factory.models import RunState
from patent_factory.state import StateStore


ROOT = Path(__file__).resolve().parents[2]
FIXED_TIME = "2026-07-13T00:00:00Z"


def run_cli(*args, environment=None):
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT / "src")
    if environment:
        merged.update(environment)
    return subprocess.run(
        [sys.executable, "-m", "patent_factory", *map(str, args)],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def prepare_run(run_root: Path, run_id: str, *, ready=True) -> None:
    run_root.mkdir(mode=0o700)
    with connect_database(run_root / "factory.sqlite3") as connection:
        store = StateStore(connection)
        store.create_run(run_id)
        store.transition(
            run_id, RunState.PROFILE_PENDING, actor="test", reason="profile start",
            operation="prepare.profile", idempotency_key="1",
        )
        store.transition(
            run_id, RunState.PROFILE_READY, actor="test", reason="profile ready",
            operation="prepare.ready", idempotency_key="1",
        )
        if ready:
            store.transition(
                run_id, RunState.RESEARCH_READY, actor="test", reason="research ready",
                operation="prepare.research", idempotency_key="1",
            )


class ResearchCliTests(unittest.TestCase):
    def setUp(self):
        self.documents_context = tempfile.TemporaryDirectory(dir=ROOT / "documents")
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.documents = Path(self.documents_context.name)
        self.workspace = Path(self.workspace_context.name)

    def tearDown(self):
        self.workspace_context.cleanup()
        self.documents_context.cleanup()

    def relative(self, path: Path) -> Path:
        return path.relative_to(ROOT)

    def common(self, run_root: Path, run_id: str):
        return (
            "--run", self.relative(run_root), "--run-id", run_id,
            "--query", "센서", "--retrieved-at", FIXED_TIME,
            "--documents-root", self.relative(self.documents),
            "--workspace-root", self.relative(self.workspace),
        )

    def test_run_start_bootstrap_then_fixture_completes(self):
        # Regression: `run start` registers a bootstrap export, so research must scope
        # its state store to every workspace export (not only research-exports) or the
        # finish transition rejects the run's own bootstrap artifact.
        responses = self.documents / "interview.json"
        responses.write_text(
            json.dumps(
                {"expertise": "분산", "name": "홍", "project_summary": "요약", "technical_domain": "산업"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        profiled = run_cli(
            "profile", "interview", "--responses", self.relative(responses),
            "--documents-root", self.relative(self.documents),
            "--workspace-root", self.relative(self.workspace),
        )
        self.assertEqual(profiled.returncode, 0, profiled.stdout + profiled.stderr)
        run_root = self.workspace / "bootstrapped-run"
        started = run_cli(
            "run", "start", "--run", self.relative(run_root), "--run-id", "bootstrapped-run",
            "--workspace-root", self.relative(self.workspace),
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(json.loads(started.stdout)["next_state"], "research_ready")
        self.assertTrue((run_root / "bootstrap-exports").is_dir())
        fixture = self.documents / "kipris.xml"
        fixture.write_bytes((ROOT / "tests/fixtures/kipris/word-search-v1.xml").read_bytes())
        result = run_cli("research", "fixture", self.relative(fixture), *self.common(run_root, "bootstrapped-run"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual((output["prior_state"], output["next_state"]), ("research_ready", "research_complete"))
        self.assertTrue((run_root / "research-exports").is_dir())

    def test_fixture_success_is_private_redacted_deterministic_and_idempotent(self):
        run_root = self.workspace / "fixture-run"
        prepare_run(run_root, "fixture-run")
        fixture = self.documents / "kipris.xml"
        fixture.write_bytes((ROOT / "tests/fixtures/kipris/word-search-v1.xml").read_bytes())
        args = ("research", "fixture", self.relative(fixture), *self.common(run_root, "fixture-run"))

        first = run_cli(*args, environment={"KIPRIS_PLUS_API_KEY": "G003-CANARY-SECRET"})
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        output = json.loads(first.stdout)
        self.assertEqual((output["prior_state"], output["next_state"]),
                         ("research_ready", "research_complete"))
        self.assertEqual(len(output["artifact_ids"]), 1)
        self.assertTrue(output["artifact_ids"][0].startswith("ar_"))
        self.assertFalse(output["replayed"])
        self.assertNotIn("G003-CANARY-SECRET", first.stdout + first.stderr)

        exports = run_root / "research-exports"
        before = {path.name: path.read_bytes() for path in exports.iterdir()}
        self.assertEqual(stat.S_IMODE(exports.stat().st_mode), 0o700)
        self.assertTrue(before)
        self.assertTrue(all(stat.S_IMODE((exports / name).stat().st_mode) == 0o600 for name in before))
        self.assertNotIn(b"G003-CANARY-SECRET", (run_root / "factory.sqlite3").read_bytes())
        self.assertTrue(all(b"G003-CANARY-SECRET" not in payload for payload in before.values()))

        with connect_database(run_root / "factory.sqlite3") as connection:
            state_version = StateStore(connection).snapshot("fixture-run").state_version
            transitions = [tuple(row) for row in connection.execute(
                "SELECT prior_state,next_state FROM transition_events WHERE run_id=? ORDER BY created_at,event_id",
                ("fixture-run",),
            )]
            self.assertEqual(transitions[-2:], [
                ("research_ready", "research_running"),
                ("research_running", "research_complete"),
            ])
            self.assertEqual(connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 1)
            self.assertNotIn("G003-CANARY-SECRET", connection.execute(
                "SELECT envelope_json FROM research_queries"
            ).fetchone()[0])

        second = run_cli(*args, environment={"KIPRIS_PLUS_API_KEY": "G003-CANARY-SECRET"})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(json.loads(second.stdout)["replayed"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in exports.iterdir()})
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(StateStore(connection).snapshot("fixture-run").state_version, state_version)
            self.assertEqual(connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM transition_events").fetchone()[0], 6)

    def test_manual_import_succeeds_without_network_and_persists_provenance(self):
        run_root = self.workspace / "manual-run"
        prepare_run(run_root, "manual-run")
        source = self.documents / "manual.json"
        source.write_text(json.dumps({"records": [{
            "canonical_url": "https://example.com/public/1",
            "identifier": "manual-1",
            "title": "공개 센서 문서",
            "content_hash": "a" * 64,
            "language": "ko",
            "provenance": "user_import",
            "limitations": ["manual fixture"],
        }]}, ensure_ascii=False), encoding="utf-8")
        result = run_cli(
            "research", "manual", self.relative(source),
            *self.common(run_root, "manual-run"),
            "--allow-host", "example.com",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["next_state"], "research_complete")
        with connect_database(run_root / "factory.sqlite3") as connection:
            evidence = connection.execute(
                "SELECT source_type,canonical_url,provenance,record_json FROM evidence_records"
            ).fetchone()
            self.assertEqual((evidence["source_type"], evidence["canonical_url"]),
                             ("manual_web", "https://example.com/public/1"))
            self.assertEqual(evidence["provenance"], "user_import")
            self.assertIn("manual fixture", evidence["record_json"])

    def test_manual_unknown_private_field_is_rejected_before_fingerprint_or_persistence(self):
        run_root = self.workspace / "manual-private"
        prepare_run(run_root, "manual-private")
        source = self.documents / "private.json"
        source.write_text(json.dumps({"records": [{
            "canonical_url": "https://example.com/public/1", "identifier": "manual-1",
            "title": "public", "content_hash": "a" * 64, "language": "ko",
            "provenance": "user_import", "raw_document": "MANUAL-PRIVATE-CANARY",
        }]}), encoding="utf-8")
        result = run_cli(
            "research", "manual", self.relative(source),
            *self.common(run_root, "manual-private"), "--allow-host", "example.com",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("MANUAL-PRIVATE-CANARY", result.stdout + result.stderr)
        self.assertNotIn(b"MANUAL-PRIVATE-CANARY", (run_root / "factory.sqlite3").read_bytes())
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM research_queries").fetchone()[0], 0)
            self.assertEqual(StateStore(connection).snapshot("manual-private").state, RunState.RESEARCH_READY)

    def test_malformed_fixture_degrades_to_incomplete_without_fabricated_evidence(self):
        run_root = self.workspace / "failed-run"
        prepare_run(run_root, "failed-run")
        fixture = self.documents / "bad.xml"
        fixture.write_text("<not-closed>", encoding="utf-8")
        result = run_cli(
            "research", "fixture", self.relative(fixture),
            *self.common(run_root, "failed-run"),
        )
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual((output["status"], output["next_state"]),
                         ("incomplete", "research_incomplete"))
        self.assertEqual(output["adapter_status"]["failure_kind"], "malformed")
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT failure_kind FROM coverage_limitations").fetchone()[0],
                             "malformed")
            transitions = [tuple(row) for row in connection.execute(
                "SELECT prior_state,next_state FROM transition_events WHERE run_id=? ORDER BY created_at,event_id",
                ("failed-run",),
            )]
            self.assertEqual(transitions[-2:], [
                ("research_ready", "research_running"),
                ("research_running", "research_incomplete"),
            ])

    def test_wrong_prior_state_is_rejected_before_adapter_or_export_activity(self):
        run_root = self.workspace / "blocked-run"
        prepare_run(run_root, "blocked-run", ready=False)
        fixture = self.documents / "kipris.xml"
        fixture.write_bytes((ROOT / "tests/fixtures/kipris/word-search-v1.xml").read_bytes())
        result = run_cli(
            "research", "fixture", self.relative(fixture),
            *self.common(run_root, "blocked-run"),
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("illegal transition", json.loads(result.stdout)["error"])
        self.assertFalse((run_root / "research-exports").exists())
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(StateStore(connection).snapshot("blocked-run").state, RunState.PROFILE_READY)
            self.assertEqual(connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 0)

    def test_success_export_is_registered_and_tamper_is_rejected_on_startup(self):
        run_root = self.workspace / "registered-run"
        prepare_run(run_root, "registered-run")
        fixture = self.documents / "kipris.xml"
        fixture.write_bytes((ROOT / "tests/fixtures/kipris/word-search-v1.xml").read_bytes())
        result = run_cli(
            "research", "fixture", self.relative(fixture),
            *self.common(run_root, "registered-run"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        exports = run_root / "research-exports"
        files = tuple(exports.glob("ar_*.json"))
        self.assertEqual(len(files), 1)
        with connect_database(run_root / "factory.sqlite3") as connection:
            registry = connection.execute(
                "SELECT path,byte_hash,byte_size FROM artifact_exports"
            ).fetchone()
            self.assertEqual(Path(registry["path"]), files[0])
        files[0].write_text("tampered", encoding="utf-8")
        replay = run_cli(
            "research", "fixture", self.relative(fixture),
            *self.common(run_root, "registered-run"),
        )
        self.assertEqual(replay.returncode, 2, replay.stdout + replay.stderr)
        self.assertIn("registered export mismatch", replay.stdout)


class CredentialScriptTests(unittest.TestCase):
    def run_check(self, *args, secret=None):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("KIPRIS_PLUS_API_KEY", None)
        if secret is not None:
            environment["KIPRIS_PLUS_API_KEY"] = secret
        return subprocess.run(
            [sys.executable, "scripts/check_credentials.py", "--check-name", "KIPRIS_PLUS_API_KEY", *args],
            cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
        )

    def test_all_diagnostics_are_useful_offline_and_never_reveal_canary(self):
        cases = (
            ((), None, 1, "missing"),
            ((), "CREDENTIAL-CANARY", 0, "present"),
            (("--simulate-invalid",), "CREDENTIAL-CANARY", 1, "simulated_invalid"),
            (("--fixture-usable",), "CREDENTIAL-CANARY", 0, "fixture_usable"),
        )
        for args, secret, code, status in cases:
            with self.subTest(status=status):
                result = self.run_check(*args, secret=secret)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], status)
                self.assertNotIn("CREDENTIAL-CANARY", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
