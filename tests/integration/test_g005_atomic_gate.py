import tempfile
import unittest
from pathlib import Path

from patent_factory.database import InjectedFailure, connect_database
from patent_factory.models import GateKind, RunState
from patent_factory.state import StateStore


class AtomicAuditGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exports = self.root / "audit-exports"
        self.exports.mkdir(mode=0o700)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.store = StateStore(self.connection, export_directories=(self.exports,))
        self.store.create_run("run")
        transitions = (
            RunState.PROFILE_PENDING, RunState.PROFILE_READY, RunState.RESEARCH_READY,
            RunState.RESEARCH_RUNNING, RunState.RESEARCH_COMPLETE, RunState.IDEATION_RUNNING,
            RunState.CANDIDATES_READY, RunState.FINALISTS_READY, RunState.AUDIT_RUNNING,
        )
        for index, state in enumerate(transitions):
            self.store.transition("run", state, actor="test", reason="prepare", operation=f"prepare.{index}", idempotency_key="1")

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_publish_and_gate_are_atomic_recoverable_and_replayable(self):
        result, exported, gate = self.store.publish_gate_transition(
            "run", GateKind.COVERAGE, actor="audit", reason="coverage", operation="audit.finalize",
            idempotency_key="key", approval_scope={"affected": ["fi_1"]}, artifact_kind="audit_batch",
            artifact_content={"version": "audit-batch-v1"}, artifact_schema_version="audit-batch-v1",
            export_directory=self.exports,
        )
        self.assertEqual(result.snapshot.state, RunState.COVERAGE_INSUFFICIENT)
        self.assertEqual(gate.subject_revision_hash, result.artifact.content_hash)
        self.assertTrue(Path(exported.path).is_file())
        replay, replay_export, replay_gate = self.store.publish_gate_transition(
            "run", GateKind.COVERAGE, actor="audit", reason="coverage", operation="audit.finalize",
            idempotency_key="key", approval_scope={"affected": ["fi_1"]}, artifact_kind="audit_batch",
            artifact_content={"version": "audit-batch-v1"}, artifact_schema_version="audit-batch-v1",
            export_directory=self.exports,
        )
        self.assertTrue(replay.replayed)
        self.assertTrue(replay_export.reused)
        self.assertEqual(replay_gate.gate_id, gate.gate_id)

    def test_fault_rolls_back_database_and_recovery_removes_orphan(self):
        with self.assertRaises(InjectedFailure):
            self.store.publish_gate_transition(
                "run", GateKind.EXCESSIVE_SIMILARITY, actor="audit", reason="risk",
                operation="audit.finalize", idempotency_key="fault", approval_scope={"affected": ["fi_1"]},
                artifact_kind="audit_batch", artifact_content={"version": "audit-batch-v1"},
                artifact_schema_version="audit-batch-v1", export_directory=self.exports,
                fault_at="after_gate",
            )
        self.assertEqual(StateStore(self.connection).snapshot("run").state, RunState.AUDIT_RUNNING)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_envelopes").fetchone()[0], 0)
        StateStore(self.connection, export_directories=(self.exports,))
        self.assertFalse(tuple(self.exports.iterdir()))


if __name__ == "__main__":
    unittest.main()
