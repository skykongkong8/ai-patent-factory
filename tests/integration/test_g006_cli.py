import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database
from patent_factory.models import GateKind
from patent_factory.provenance import digest
from patent_factory.state import StateStore


ROOT = Path(__file__).resolve().parents[2]


class G006CliTests(unittest.TestCase):
    def setUp(self):
        self.context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.workspace = Path(self.context.name)
        self.run = self.workspace / "run"
        self.run.mkdir(mode=0o700)
        exports = self.run / "audit-exports"
        exports.mkdir(mode=0o700)
        with connect_database(self.run / "factory.sqlite3") as connection:
            store = StateStore(connection, export_directories=(exports,))
            store.create_run("run")
            connection.execute("UPDATE runs SET state='audit_running' WHERE run_id='run'")
            content = {"results": [], "version": "audit-batch-v1"}
            self.scope = {"affected_finalist_ids": ["fi_1"], "audit_hash": digest(content), "outcome": "coverage_insufficient"}
            _result, _export, self.gate = store.publish_gate_transition(
                "run", GateKind.COVERAGE, actor="audit", reason="coverage",
                operation="audit.finalize", idempotency_key="coverage", approval_scope=self.scope,
                artifact_kind="audit_batch", artifact_content=content,
                artifact_schema_version="audit-batch-v1", export_directory=exports,
            )

    def tearDown(self):
        self.context.cleanup()

    def _run(self, *args, **environment):
        env = os.environ.copy()
        env.update(environment)
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [sys.executable, "-m", "patent_factory", *map(str, args)], cwd=ROOT,
            env=env, text=True, capture_output=True, check=False,
        )

    def _relative(self, path):
        return path.relative_to(ROOT)

    def test_inspect_then_decide_dispatches_coverage_retry(self):
        common = ("--run", self._relative(self.run), "--run-id", "run", "--gate-id", self.gate.gate_id, "--workspace-root", self._relative(self.workspace))
        inspected = self._run("gate", "inspect", *common)
        self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)["actions"], ["expand", "retry", "stop"])
        request = {
            "action": "retry", "actor": "user", "approval_scope": self.scope,
            "decisions": [], "gate_id": self.gate.gate_id,
            "plan": {"attempt": 1, "scope": "same bounded audit queries"},
            "reason": "retry bounded audit", "schema_version": "gate-decision-input-v1",
            "subject_revision_hash": self.gate.subject_revision_hash,
        }
        path = self.workspace / "decision.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        decided = self._run("gate", "decide", *common, "--input", self._relative(path))
        self.assertEqual(decided.returncode, 0, decided.stdout + decided.stderr)
        self.assertEqual(json.loads(decided.stdout)["next_state"], "audit_running")

    def test_secret_canary_is_rejected_before_any_decision_write(self):
        secret = "G006-DECISION-CANARY"
        request = {
            "action": "expand", "actor": "user", "approval_scope": self.scope,
            "decisions": [], "gate_id": self.gate.gate_id, "plan": {"term": secret},
            "reason": "expand", "schema_version": "gate-decision-input-v1",
            "subject_revision_hash": self.gate.subject_revision_hash,
        }
        path = self.workspace / "private-decision.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        result = self._run(
            "gate", "decide", "--run", self._relative(self.run), "--run-id", "run",
            "--gate-id", self.gate.gate_id, "--input", self._relative(path),
            "--workspace-root", self._relative(self.workspace), KIPRIS_PLUS_API_KEY=secret,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        with connect_database(self.run / "factory.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
