import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database
from patent_factory.models import GateKind, RunState
from patent_factory.state import GateMismatchError, StateStore


class GateMatrixTests(unittest.TestCase):
    CASES = (
        (GateKind.CONFLICT_RESOLUTION, RunState.PROFILE_PENDING, "choose_value", RunState.PROFILE_PENDING, True),
        (GateKind.CREDENTIAL, RunState.PROFILE_READY, "approve", RunState.PROFILE_READY, True),
        (GateKind.SENSITIVE_DISCLOSURE, RunState.DRAFT_READY, "approve", RunState.DRAFT_READY, True),
        (GateKind.DOMAIN_PIVOT, RunState.RESEARCH_READY, "approve", RunState.RESEARCH_READY, True),
    )

    def _pending(self, directory, kind, state):
        connection = connect_database(Path(directory) / "factory.sqlite3")
        store = StateStore(connection)
        store.create_run("run")
        connection.execute("UPDATE runs SET state=? WHERE run_id='run'", (state.value,))
        subject = store.add_revision("run", f"{kind.value}-subject", {"kind": kind.value})
        scope = {"kind": kind.value, "scope": "exact"}
        envelope = store.suspend_gate(
            "run",
            kind,
            suspended_operation=f"resume:{kind.value}",
            subject_revision_hash=subject.content_hash,
            approval_scope=scope,
            return_state=state,
            actor="user",
            reason="gate required",
        )
        return connection, store, subject, scope, envelope

    def test_every_gate_kind_resumes_only_its_recorded_state_and_operation(self):
        for kind, suspended_state, action, expected_state, authorizes in self.CASES:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                connection, store, subject, scope, envelope = self._pending(temporary, kind, suspended_state)
                decision, result = store.decide_gate(
                    envelope.gate_id,
                    action=action,
                    actor="user",
                    reason="decided",
                    subject_revision_hash=subject.content_hash,
                    approval_scope=scope,
                )
                self.assertEqual(result.snapshot.state, expected_state)
                self.assertEqual(result.suspended_operation, f"resume:{kind.value}")
                if authorizes:
                    consumed = store.consume_decision(
                        decision.decision_id,
                        suspended_operation=f"resume:{kind.value}",
                        subject_revision_hash=subject.content_hash,
                        approval_scope=scope,
                    )
                    self.assertEqual(consumed.decision_id, decision.decision_id)
                else:
                    with self.assertRaisesRegex(GateMismatchError, "does not authorize"):
                        store.consume_decision(
                            decision.decision_id,
                            suspended_operation=f"resume:{kind.value}",
                            subject_revision_hash=subject.content_hash,
                            approval_scope=scope,
                        )
                connection.close()

    def test_audit_branch_gates_require_atomic_resolution_artifacts(self):
        for kind, state, action in (
            (GateKind.COVERAGE, RunState.AUDIT_RUNNING, "retry"),
            (GateKind.EXCESSIVE_SIMILARITY, RunState.AUDIT_RUNNING, "retain_with_warning"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                connection, store, subject, scope, envelope = self._pending(temporary, kind, state)
                with self.assertRaisesRegex(GateMismatchError, "atomic resolution artifact"):
                    store.decide_gate(
                        envelope.gate_id, action=action, actor="user", reason="decided",
                        subject_revision_hash=subject.content_hash, approval_scope=scope,
                    )
                self.assertEqual(store.snapshot("run").state, {GateKind.COVERAGE: RunState.COVERAGE_INSUFFICIENT, GateKind.EXCESSIVE_SIMILARITY: RunState.DECISION_REQUIRED}[kind])
                connection.close()

    def test_stop_is_terminal_for_every_gate_kind(self):
        for kind, suspended_state, _, _, _ in self.CASES:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                connection, store, subject, scope, envelope = self._pending(temporary, kind, suspended_state)
                decision, result = store.decide_gate(
                    envelope.gate_id,
                    action="stop",
                    actor="user",
                    reason="stop",
                    subject_revision_hash=subject.content_hash,
                    approval_scope=scope,
                )
                self.assertEqual(result.snapshot.state, RunState.STOPPED)
                with self.assertRaises(GateMismatchError):
                    store.consume_decision(
                        decision.decision_id,
                        suspended_operation=f"resume:{kind.value}",
                        subject_revision_hash=subject.content_hash,
                        approval_scope=scope,
                    )
                connection.close()


if __name__ == "__main__":
    unittest.main()
