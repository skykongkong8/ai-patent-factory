import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import SCHEMA_VERSION, InjectedFailure, RunBusyError, connect_database
from patent_factory.models import GateKind, RunState
from patent_factory.state import ALLOWED_TRANSITIONS, GATE_ACTIONS, GATE_STATE_SET, GateMismatchError, StaleRevisionError, StateError, StateStore


class TransactionalStateKernelTests(unittest.TestCase):
    def database(self, directory: str, name: str = "factory.sqlite3"):
        return connect_database(Path(directory) / name, busy_timeout_ms=25)

    def test_migration_preserves_v1_data_rolls_back_and_refuses_future_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            raw = sqlite3.connect(path)
            raw.execute("CREATE TABLE profile_facts(field TEXT PRIMARY KEY,value_json TEXT NOT NULL)")
            raw.execute("INSERT INTO profile_facts VALUES('name','\"kept\"')")
            raw.execute("PRAGMA user_version=1")
            raw.commit()
            raw.close()
            with self.assertRaises(InjectedFailure):
                connect_database(path, fault_at="migration_v2")
            raw = sqlite3.connect(path)
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(raw.execute("SELECT value_json FROM profile_facts").fetchone()[0], '"kept"')
            self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone())
            raw.close()
            migrated = connect_database(path)
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(migrated.execute("SELECT value_json FROM profile_facts").fetchone()[0], '"kept"')
            migrated.close()
            raw = sqlite3.connect(path)
            raw.execute("PRAGMA user_version=99")
            raw.close()
            with self.assertRaisesRegex(ValueError, "unsupported schema version 99"):
                connect_database(path)

    def test_v3_to_v4_migration_is_atomic_preserves_decisions_and_repairs_partial_columns(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            connection = connect_database(path)
            connection.execute("INSERT INTO runs VALUES('kept','draft_ready',0,'t','t')")
            connection.execute("INSERT INTO artifact_revisions VALUES('ar-kept','kept','draft','{}','hash','1','t',0)")
            connection.execute("INSERT INTO current_artifacts VALUES('kept','draft','ar-kept')")
            connection.execute("INSERT INTO gate_envelopes VALUES('gate','kept','sensitive_disclosure','sensitive_disclosure_required','draft_ready','export','hash','{}','scope','draft_ready','t','decided')")
            connection.execute("INSERT INTO gate_decisions (decision_id,gate_id,run_id,action,actor,subject_revision_hash,approval_scope_hash,suspended_operation,return_state,reason,created_at,stale,consumed_at) VALUES('decision','gate','kept','approve','user','hash','scope','export','draft_ready','yes','t',0,'t')")
            connection.execute("ALTER TABLE gate_decisions DROP COLUMN consumed_by_event_id")
            connection.execute("ALTER TABLE gate_decisions DROP COLUMN used_at")
            connection.execute("PRAGMA user_version=3")
            connection.close()

            with self.assertRaises(InjectedFailure):
                connect_database(path,fault_at="migration_v4")
            raw = sqlite3.connect(path)
            columns = {row[1] for row in raw.execute("PRAGMA table_info(gate_decisions)")}
            self.assertNotIn("used_at",columns)
            self.assertNotIn("consumed_by_event_id",columns)
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0],3)
            self.assertEqual(raw.execute("SELECT action,consumed_at FROM gate_decisions WHERE decision_id='decision'").fetchone(),("approve","t"))
            raw.execute("ALTER TABLE gate_decisions ADD COLUMN used_at TEXT")
            raw.commit()
            raw.close()

            migrated = connect_database(path)
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(gate_decisions)")}
            self.assertIn("used_at",columns)
            self.assertIn("consumed_by_event_id",columns)
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0],SCHEMA_VERSION)
            self.assertEqual(tuple(migrated.execute("SELECT action,consumed_at,used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id='decision'").fetchone()),("approve","t",None,None))
            migrated.close()

    def test_explicit_transition_table_allows_edges_rejects_skip_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            for index, (prior, targets) in enumerate(ALLOWED_TRANSITIONS.items()):
                for target in targets:
                    run_id = f"run-{index}-{target.value}"
                    now = "2026-01-01T00:00:00Z"
                    connection.execute("INSERT INTO runs VALUES(?,?,0,?,?)", (run_id,prior.value,now,now))
                    if prior in GATE_STATE_SET or target in GATE_STATE_SET:
                        with self.assertRaisesRegex(GateMismatchError, "mandatory gate state"):
                            store.transition(run_id,target,actor="tester",reason="edge",operation="edge",idempotency_key=target.value)
                        continue
                    if target is RunState.COMPLETE:
                        with self.assertRaisesRegex(StateError, "completion requires current report"):
                            store.transition(run_id,target,actor="tester",reason="edge",operation="edge",idempotency_key=target.value)
                        continue
                    result = store.transition(run_id,target,actor="tester",reason="edge",operation="edge",idempotency_key=target.value,evidence_hashes=("hash-b","hash-a"))
                    self.assertEqual(result.snapshot.state,target)
                    event = connection.execute("SELECT * FROM transition_events WHERE event_id=?", (result.event_id,)).fetchone()
                    self.assertEqual((event["actor"],event["prior_state"],event["next_state"],event["reason"]),("tester",prior.value,target.value,"edge"))
                    self.assertEqual(json.loads(event["evidence_hashes_json"]),["hash-a","hash-b"])
            store.create_run("illegal")
            before = connection.execute("SELECT count(*) FROM transition_events WHERE run_id='illegal'").fetchone()[0]
            with self.assertRaisesRegex(StateError,"illegal transition"):
                store.transition("illegal",RunState.COMPLETE,actor="x",reason="skip",operation="skip",idempotency_key="1")
            self.assertEqual(connection.execute("SELECT count(*) FROM transition_events WHERE run_id='illegal'").fetchone()[0],before)
            connection.close()

    def test_mandatory_gate_states_cannot_be_entered_or_exited_without_gate_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            now = "2026-01-01T00:00:00Z"
            connection.execute("INSERT INTO runs VALUES('audit','audit_running',0,?,?)", (now,now))
            with self.assertRaisesRegex(GateMismatchError, "requires a gate envelope"):
                store.transition(
                    "audit",
                    RunState.DECISION_REQUIRED,
                    actor="bypass",
                    reason="skip envelope",
                    operation="audit",
                    idempotency_key="1",
                )
            connection.execute("INSERT INTO runs VALUES('decision','decision_required',0,?,?)", (now,now))
            with self.assertRaisesRegex(GateMismatchError, "requires a gate decision"):
                store.transition(
                    "decision",
                    RunState.AUDIT_APPROVED,
                    actor="bypass",
                    reason="skip decision",
                    operation="decide",
                    idempotency_key="1",
                )
            cancelled = store.transition(
                "decision",
                RunState.CANCELLED,
                actor="user",
                reason="cancel",
                operation="cancel",
                idempotency_key="1",
            )
            self.assertEqual(cancelled.snapshot.state, RunState.CANCELLED)
            self.assertEqual(connection.execute("SELECT count(*) FROM gate_envelopes").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0],0)
            connection.close()

    def test_immutable_revisions_idempotency_and_atomic_dag_invalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            store.create_run("run")
            profile = store.add_revision("run","profile",{"name":"가"})
            same = store.add_revision("run","profile",{"name":"가"})
            self.assertEqual(profile.revision_id,same.revision_id)
            query = store.add_revision("run","query",{"q":"센서"},dependencies=(profile.revision_id,))
            candidate = store.add_revision("run","candidate",{"title":"A"},dependencies=(query.revision_id,))
            changed = store.add_revision("run","profile",{"name":"나"})
            self.assertNotEqual(changed.content_hash,profile.content_hash)
            stale = dict(connection.execute("SELECT revision_id,stale FROM artifact_revisions"))
            self.assertEqual((stale[query.revision_id],stale[candidate.revision_id]),(1,1))
            pointers = dict(connection.execute("SELECT kind,revision_id FROM current_artifacts WHERE run_id='run'"))
            self.assertEqual(pointers,{"profile":changed.revision_id})
            changed_query = store.add_revision("run","query",{"q":"변경"},dependencies=(changed.revision_id,))
            changed_candidate = store.add_revision("run","candidate",{"title":"B"},dependencies=(changed_query.revision_id,))
            reactivated = store.add_revision("run","profile",{"name":"가"})
            self.assertEqual(reactivated.revision_id,profile.revision_id)
            pointers = dict(connection.execute("SELECT kind,revision_id FROM current_artifacts WHERE run_id='run'"))
            self.assertEqual(pointers,{"profile":profile.revision_id})
            stale = dict(connection.execute("SELECT revision_id,stale FROM artifact_revisions"))
            self.assertEqual((stale[changed_query.revision_id],stale[changed_candidate.revision_id]),(1,1))
            with self.assertRaisesRegex(Exception,"stale revision"):
                store.add_revision("run","bad",{"x":1},dependencies=(candidate.revision_id,))
            first = store.transition("run",RunState.PROFILE_PENDING,actor="a",reason="start",operation="profile",idempotency_key="fixed",artifact_kind="profile-step",artifact_content={"ok":True})
            replay = store.transition("run",RunState.PROFILE_PENDING,actor="a",reason="ignored",operation="profile",idempotency_key="fixed",artifact_kind="profile-step",artifact_content={"ok":False})
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.artifact.revision_id,first.artifact.revision_id)
            self.assertEqual(connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0],1)
            connection.close()

    def test_revision_identity_binds_schema_version_and_exact_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            store.create_run("run")
            upstream_a = store.add_revision("run","upstream-a",{"value":"a"})
            upstream_b = store.add_revision("run","upstream-b",{"value":"b"})
            first = store.add_revision(
                "run",
                "derived",
                {"same":"content"},
                schema_version="1",
                dependencies=(upstream_a.revision_id,),
            )
            replay = store.add_revision(
                "run",
                "derived",
                {"same":"content"},
                schema_version="1",
                dependencies=(upstream_a.revision_id,),
            )
            schema_changed = store.add_revision(
                "run",
                "derived",
                {"same":"content"},
                schema_version="2",
                dependencies=(upstream_a.revision_id,),
            )
            dependency_changed = store.add_revision(
                "run",
                "derived",
                {"same":"content"},
                schema_version="2",
                dependencies=(upstream_b.revision_id,),
            )
            self.assertEqual(replay.revision_id, first.revision_id)
            self.assertEqual(len({first.revision_id,schema_changed.revision_id,dependency_changed.revision_id}),3)
            self.assertNotEqual(first.content_hash,schema_changed.content_hash)
            self.assertNotEqual(schema_changed.content_hash,dependency_changed.content_hash)
            store.add_revision("run","upstream-b",{"value":"changed"})
            self.assertEqual(
                connection.execute(
                    "SELECT stale FROM artifact_revisions WHERE revision_id=?",
                    (dependency_changed.revision_id,),
                ).fetchone()[0],
                1,
            )
            connection.close()

    def test_transition_fault_boundaries_rollback_revision_dependency_event_state_and_idempotency(self):
        boundaries = ("after_revision","after_dependency","after_invalidation","after_pointer","after_event","after_state","after_idempotency")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                connection = self.database(temporary)
                store = StateStore(connection)
                store.create_run("run")
                upstream = store.add_revision("run","upstream",{"v":1})
                before = {table:connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("artifact_revisions","artifact_dependencies","transition_events","idempotency_records")}
                with self.assertRaises(InjectedFailure):
                    store.transition("run",RunState.PROFILE_PENDING,actor="a",reason="fault",operation="op",idempotency_key="key",artifact_kind="derived",artifact_content={"v":2},dependencies=(upstream.revision_id,),fault_at=boundary)
                self.assertEqual(store.snapshot("run").state,RunState.NEW)
                after = {table:connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}
                self.assertEqual(after,before)
                connection.close()

    def test_busy_writer_is_bounded_redacted_and_retryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            first = connect_database(path,busy_timeout_ms=25)
            second = connect_database(path,busy_timeout_ms=25)
            first.execute("BEGIN IMMEDIATE")
            with self.assertRaises(RunBusyError) as caught:
                StateStore(second).create_run("busy")
            self.assertEqual(caught.exception.code,"run_busy")
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn(str(path),str(caught.exception))
            first.rollback()
            first.close()
            second.close()

    def _draft_ready(self, connection, store):
        store.create_run("run")
        now = "2026-01-01T00:00:00Z"
        connection.execute("UPDATE runs SET state='draft_ready',updated_at=? WHERE run_id='run'",(now,))
        return store.add_revision("run","draft",{"body":"비공개"})

    def test_hash_scope_bound_gate_resumes_and_consumes_only_exact_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            draft = self._draft_ready(connection,store)
            scope = {"recipient":"attorney","fields":["body"]}
            envelope = store.suspend_gate("run",GateKind.SENSITIVE_DISCLOSURE,suspended_operation="export:attorney",subject_revision_hash=draft.content_hash,approval_scope=scope,return_state=RunState.DRAFT_READY,actor="user",reason="approval needed")
            self.assertEqual(store.snapshot("run").state,RunState.SENSITIVE_DISCLOSURE_REQUIRED)
            with self.assertRaises(GateMismatchError):
                store.decide_gate(envelope.gate_id,action="approve",actor="user",reason="wrong",subject_revision_hash=draft.content_hash,approval_scope={"recipient":"other"})
            decision,result = store.decide_gate(envelope.gate_id,action="approve",actor="user",reason="approved",subject_revision_hash=draft.content_hash,approval_scope=scope,suspended_operation="export:attorney",return_state=RunState.DRAFT_READY)
            self.assertEqual((result.snapshot.state,result.suspended_operation),(RunState.DRAFT_READY,"export:attorney"))
            with self.assertRaises(GateMismatchError):
                store.consume_decision(decision.decision_id,suspended_operation="share:anywhere",subject_revision_hash=draft.content_hash,approval_scope=scope)
            consumed = store.consume_decision(decision.decision_id,suspended_operation="export:attorney",subject_revision_hash=draft.content_hash,approval_scope=scope)
            self.assertEqual(consumed.decision_id,decision.decision_id)
            retried = store.consume_decision(decision.decision_id,suspended_operation="export:attorney",subject_revision_hash=draft.content_hash,approval_scope=scope)
            self.assertEqual(retried.consumed_at,consumed.consumed_at)
            connection.close()

    def test_gate_actions_are_explicit_and_unknown_or_non_authorizing_actions_write_nothing_reusable(self):
        self.assertEqual(GATE_ACTIONS, {
            GateKind.CONFLICT_RESOLUTION: frozenset({"choose_source", "choose_value", "retain_unresolved", "stop"}),
            GateKind.CREDENTIAL: frozenset({"configure_and_verify", "approve", "degrade", "stop"}),
            GateKind.SENSITIVE_DISCLOSURE: frozenset({"approve", "redact", "stop"}),
            GateKind.DOMAIN_PIVOT: frozenset({"approve", "reject", "stop"}),
            GateKind.COVERAGE: frozenset({"expand", "retry", "stop"}),
            GateKind.EXCESSIVE_SIMILARITY: frozenset({"retain_with_warning", "refine", "replace", "stop"}),
            GateKind.POST_AUDIT_CHECKPOINT: frozenset({"approve", "re_ideate", "re_research", "stop"}),
        })
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            draft = self._draft_ready(connection,store)
            scope = {"recipient":"attorney"}
            envelope = store.suspend_gate("run",GateKind.SENSITIVE_DISCLOSURE,suspended_operation="export",subject_revision_hash=draft.content_hash,approval_scope=scope,return_state=RunState.DRAFT_READY,actor="user",reason="gate")
            before = {
                "decisions": connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0],
                "events": connection.execute("SELECT count(*) FROM transition_events").fetchone()[0],
                "state": tuple(connection.execute("SELECT state,state_version FROM runs WHERE run_id='run'").fetchone()),
                "status": connection.execute("SELECT status FROM gate_envelopes WHERE gate_id=?",(envelope.gate_id,)).fetchone()[0],
            }
            with self.assertRaisesRegex(GateMismatchError,"action is not allowed"):
                store.decide_gate(envelope.gate_id,action="invented",actor="user",reason="invalid",subject_revision_hash=draft.content_hash,approval_scope=scope)
            after = {
                "decisions": connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0],
                "events": connection.execute("SELECT count(*) FROM transition_events").fetchone()[0],
                "state": tuple(connection.execute("SELECT state,state_version FROM runs WHERE run_id='run'").fetchone()),
                "status": connection.execute("SELECT status FROM gate_envelopes WHERE gate_id=?",(envelope.gate_id,)).fetchone()[0],
            }
            self.assertEqual(after,before)
            decision,_ = store.decide_gate(envelope.gate_id,action="redact",actor="user",reason="redact",subject_revision_hash=draft.content_hash,approval_scope=scope)
            with self.assertRaisesRegex(GateMismatchError,"does not authorize"):
                store.consume_decision(decision.decision_id,suspended_operation="export",subject_revision_hash=draft.content_hash,approval_scope=scope)
            row = connection.execute("SELECT consumed_at,used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?",(decision.decision_id,)).fetchone()
            self.assertEqual(tuple(row),(None,None,None))
            connection.close()

    def test_pending_gate_rejects_subject_mutation_and_remains_decidable(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            draft = self._draft_ready(connection,store)
            scope = {"recipient":"attorney"}
            envelope = store.suspend_gate("run",GateKind.SENSITIVE_DISCLOSURE,suspended_operation="export",subject_revision_hash=draft.content_hash,approval_scope=scope,return_state=RunState.DRAFT_READY,actor="user",reason="gate")
            before = connection.execute("SELECT count(*) FROM artifact_revisions").fetchone()[0]
            with self.assertRaisesRegex(GateMismatchError,"pending gate"):
                store.add_revision("run","draft",{"body":"changed"})
            self.assertEqual(connection.execute("SELECT count(*) FROM artifact_revisions").fetchone()[0],before)
            self.assertEqual(connection.execute("SELECT status FROM gate_envelopes WHERE gate_id=?",(envelope.gate_id,)).fetchone()[0],"pending")
            self.assertEqual(store.snapshot("run").state,RunState.SENSITIVE_DISCLOSURE_REQUIRED)
            _,result = store.decide_gate(envelope.gate_id,action="approve",actor="user",reason="yes",subject_revision_hash=draft.content_hash,approval_scope=scope)
            self.assertEqual(result.snapshot.state,RunState.DRAFT_READY)
            connection.close()

    def test_changed_gate_scope_stales_prior_unused_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            draft = self._draft_ready(connection,store)
            first_scope = {"recipient":"attorney-a"}
            first = store.suspend_gate("run",GateKind.SENSITIVE_DISCLOSURE,suspended_operation="export",subject_revision_hash=draft.content_hash,approval_scope=first_scope,return_state=RunState.DRAFT_READY,actor="user",reason="first")
            decision,_ = store.decide_gate(first.gate_id,action="approve",actor="user",reason="yes",subject_revision_hash=draft.content_hash,approval_scope=first_scope)
            second_scope = {"recipient":"attorney-b"}
            second = store.suspend_gate("run",GateKind.SENSITIVE_DISCLOSURE,suspended_operation="export",subject_revision_hash=draft.content_hash,approval_scope=second_scope,return_state=RunState.DRAFT_READY,actor="user",reason="scope changed")
            self.assertEqual(second.approval_scope,second_scope)
            self.assertEqual(connection.execute("SELECT stale FROM gate_decisions WHERE decision_id=?",(decision.decision_id,)).fetchone()[0],1)
            with self.assertRaises(GateMismatchError):
                store.consume_decision(decision.decision_id,suspended_operation="export",subject_revision_hash=draft.content_hash,approval_scope=first_scope)
            connection.close()

    def test_gate_return_state_is_the_exact_suspended_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            draft = self._draft_ready(connection, store)
            before = tuple(connection.execute("SELECT state,state_version FROM runs WHERE run_id='run'").fetchone())
            with self.assertRaisesRegex(GateMismatchError, "exact suspended state"):
                store.suspend_gate(
                    "run",
                    GateKind.SENSITIVE_DISCLOSURE,
                    suspended_operation="export",
                    subject_revision_hash=draft.content_hash,
                    approval_scope={"recipient": "attorney"},
                    return_state=RunState.VALIDATED,
                    actor="user",
                    reason="invalid bypass",
                )
            self.assertEqual(connection.execute("SELECT count(*) FROM gate_envelopes").fetchone()[0], 0)
            self.assertEqual(tuple(connection.execute("SELECT state,state_version FROM runs WHERE run_id='run'").fetchone()), before)
            connection.close()

    def test_invalidated_idempotent_artifact_cannot_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            store.create_run("run")
            upstream = store.add_revision("run", "profile", {"value": 1})
            first = store.transition("run", RunState.PROFILE_PENDING, actor="system", reason="initial", operation="profile", idempotency_key="fixed", artifact_kind="profile-step", artifact_content={"value": 1}, dependencies=(upstream.revision_id,))
            store.add_revision("run", "profile", {"value": 2})
            self.assertEqual(connection.execute("SELECT stale FROM artifact_revisions WHERE revision_id=?", (first.artifact.revision_id,)).fetchone()[0], 1)
            with self.assertRaisesRegex(StaleRevisionError, "invalidated"):
                store.transition("run", RunState.PROFILE_PENDING, actor="system", reason="replay", operation="profile", idempotency_key="fixed")
            connection.close()

    def test_decision_boundary_rolls_back_and_changed_subject_stales_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = self.database(temporary)
            store = StateStore(connection)
            draft = self._draft_ready(connection,store)
            scope = {"recipient":"attorney"}
            envelope = store.suspend_gate("run",GateKind.SENSITIVE_DISCLOSURE,suspended_operation="export",subject_revision_hash=draft.content_hash,approval_scope=scope,return_state=RunState.DRAFT_READY,actor="user",reason="gate")
            with self.assertRaises(InjectedFailure):
                store.decide_gate(envelope.gate_id,action="approve",actor="user",reason="yes",subject_revision_hash=draft.content_hash,approval_scope=scope,fault_at="after_decision")
            self.assertEqual(connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT status FROM gate_envelopes").fetchone()[0],"pending")
            self.assertEqual(store.snapshot("run").state,RunState.SENSITIVE_DISCLOSURE_REQUIRED)
            decision,_ = store.decide_gate(envelope.gate_id,action="approve",actor="user",reason="yes",subject_revision_hash=draft.content_hash,approval_scope=scope)
            store.add_revision("run","draft",{"body":"changed"})
            self.assertEqual(connection.execute("SELECT stale FROM gate_decisions WHERE decision_id=?",(decision.decision_id,)).fetchone()[0],1)
            with self.assertRaises(GateMismatchError):
                store.consume_decision(decision.decision_id,suspended_operation="export",subject_revision_hash=draft.content_hash,approval_scope=scope)
            connection.close()


if __name__ == "__main__":
    unittest.main()
