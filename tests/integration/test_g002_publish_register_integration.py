import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from patent_factory.database import SCHEMA_VERSION, InjectedFailure, connect_database
from patent_factory.models import GateKind, RunState
from patent_factory.privacy import DataClass, EgressApproval, guarded_hosted_call
from patent_factory.state import GateMismatchError, StaleRevisionError, StateStore


TABLES = (
    "artifact_revisions",
    "artifact_dependencies",
    "artifact_exports",
    "current_artifacts",
    "transition_events",
    "idempotency_records",
)


class PublishRegisterIntegrationTests(unittest.TestCase):
    def counts(self, connection):
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}

    def setup_run(self, temporary):
        connection = connect_database(Path(temporary) / "factory.sqlite3")
        exports = Path(temporary) / "exports"
        exports.mkdir()
        store = StateStore(connection, export_directories=(exports,))
        store.create_run("run")
        upstream = store.add_revision("run", "profile", {"name": "홍길동"})
        return connection, store, upstream, exports

    def publish(self, store, upstream, exports, **changes):
        values = {
            "run_id": "run",
            "next_state": RunState.PROFILE_PENDING,
            "actor": "tester",
            "reason": "publish",
            "operation": "publish:query",
            "idempotency_key": "key-1",
            "artifact_kind": "query",
            "artifact_content": {"query": "산업 센서"},
            "export_directory": exports,
            "dependencies": (upstream.revision_id,),
        }
        values.update(changes)
        return store.publish_transition(**values)

    def test_v2_to_v3_registry_migration_rolls_back_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            connection = connect_database(path)
            connection.execute("INSERT INTO runs VALUES('kept','new',0,'t','t')")
            connection.execute("DROP TABLE artifact_exports")
            connection.execute("PRAGMA user_version=2")
            connection.close()
            with self.assertRaises(InjectedFailure):
                connect_database(path, fault_at="migration_v3")
            raw = sqlite3.connect(path)
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(raw.execute("SELECT state FROM runs WHERE run_id='kept'").fetchone()[0], "new")
            self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_exports'").fetchone())
            raw.close()
            migrated = connect_database(path)
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(migrated.execute("SELECT state FROM runs WHERE run_id='kept'").fetchone()[0], "new")
            migrated.close()

    def test_export_failure_and_pre_export_fault_leave_database_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, store, upstream, exports = self.setup_run(temporary)
            before = self.counts(connection)
            with self.assertRaises(InjectedFailure):
                self.publish(store, upstream, exports, fault_at="before_export")
            self.assertEqual(self.counts(connection), before)
            self.assertEqual(tuple(exports.iterdir()), ())

            def fail_before_publish(stage):
                if stage == "file_fsynced":
                    raise RuntimeError("export failed")

            with self.assertRaisesRegex(RuntimeError, "export failed"):
                self.publish(store, upstream, exports, export_fault_hook=fail_before_publish)
            self.assertEqual(self.counts(connection), before)
            self.assertEqual(tuple(exports.iterdir()), ())
            self.assertEqual(store.snapshot("run").state, RunState.NEW)
            connection.close()

    def test_publish_cannot_create_an_export_while_bypassing_a_mandatory_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, store, upstream, exports = self.setup_run(temporary)
            connection.execute("UPDATE runs SET state='audit_running' WHERE run_id='run'")
            with self.assertRaisesRegex(GateMismatchError, "requires a gate envelope"):
                self.publish(
                    store,
                    upstream,
                    exports,
                    next_state=RunState.DECISION_REQUIRED,
                )
            self.assertEqual(tuple(exports.iterdir()), ())
            self.assertEqual(self.counts(connection)["artifact_exports"], 0)
            connection.close()

    def test_post_publish_and_every_database_boundary_roll_back_authoritative_state(self):
        boundaries = (
            "after_export_publish",
            "before_database",
            "after_revision",
            "after_dependency",
            "after_export_registry",
            "after_invalidation",
            "after_pointer",
            "after_event",
            "after_state",
            "after_idempotency",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                connection, store, upstream, exports = self.setup_run(temporary)
                before = self.counts(connection)
                with self.assertRaises(InjectedFailure):
                    self.publish(store, upstream, exports, fault_at=boundary)
                self.assertEqual(self.counts(connection), before)
                self.assertEqual(store.snapshot("run").state, RunState.NEW)
                published = tuple(exports.glob("*.json"))
                self.assertEqual(len(published), 1)
                self.assertTrue(published[0].name.startswith("ar_"))
                connection.close()

    def test_success_registers_semantic_and_byte_hashes_atomically_and_replays(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, store, upstream, exports = self.setup_run(temporary)
            result, exported = self.publish(store, upstream, exports)
            self.assertEqual(result.snapshot.state, RunState.PROFILE_PENDING)
            row = connection.execute("SELECT * FROM artifact_exports WHERE revision_id=?", (result.artifact.revision_id,)).fetchone()
            self.assertEqual((row["path"], row["byte_hash"], row["byte_size"]), (exported.path, exported.content_hash, exported.size))
            self.assertNotEqual(result.artifact.content_hash, row["byte_hash"])
            self.assertEqual(Path(row["path"]).name, f"{result.artifact.revision_id}.json")
            hashes = json.loads(connection.execute("SELECT evidence_hashes_json FROM transition_events WHERE event_id=?", (result.event_id,)).fetchone()[0])
            self.assertIn(result.artifact.content_hash, hashes)
            self.assertIn(exported.content_hash, hashes)
            replay, replay_export = self.publish(store, upstream, exports)
            self.assertTrue(replay.replayed)
            self.assertTrue(replay_export.reused)
            self.assertEqual(self.counts(connection)["artifact_exports"], 1)
            connection.close()

    def test_completed_replay_returns_prior_artifact_without_publishing_changed_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, store, upstream, exports = self.setup_run(temporary)
            first, first_export = self.publish(store, upstream, exports)
            export_calls = []

            replay, replay_export = self.publish(
                store,
                upstream,
                exports,
                artifact_content={"query": "changed payload"},
                export_fault_hook=lambda stage: export_calls.append(stage),
            )

            self.assertTrue(replay.replayed)
            self.assertEqual(replay.artifact.revision_id, first.artifact.revision_id)
            self.assertEqual(replay_export.path, first_export.path)
            self.assertTrue(replay_export.reused)
            self.assertEqual(export_calls, [])
            self.assertEqual(tuple(exports.glob("*.json")), (Path(first_export.path),))
            self.assertEqual(self.counts(connection)["artifact_exports"], 1)
            connection.close()

    def test_concurrent_same_key_different_payloads_publish_only_the_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            exports = Path(temporary) / "exports"
            exports.mkdir()
            setup = connect_database(path, busy_timeout_ms=5000)
            store = StateStore(setup, export_directories=(exports,))
            store.create_run("run")
            upstream = store.add_revision("run", "profile", {"name": "홍길동"})
            setup.close()

            barrier = threading.Barrier(2)
            outcomes = []
            lock = threading.Lock()

            def publish(payload):
                connection = connect_database(path, busy_timeout_ms=5000)
                try:
                    barrier.wait()
                    result, exported = StateStore(connection, export_directories=(exports,)).publish_transition(
                        "run",
                        RunState.PROFILE_PENDING,
                        actor="tester",
                        reason="concurrent replay",
                        operation="publish:query",
                        idempotency_key="shared-key",
                        artifact_kind="query",
                        artifact_content={"query": payload},
                        export_directory=exports,
                        dependencies=(upstream.revision_id,),
                    )
                    outcome = (result.replayed, result.artifact.revision_id, exported.path)
                finally:
                    connection.close()
                with lock:
                    outcomes.append(outcome)

            threads = [
                threading.Thread(target=publish, args=(payload,))
                for payload in ("first payload", "second payload")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(sorted(item[0] for item in outcomes), [False, True])
            self.assertEqual(len({item[1] for item in outcomes}), 1)
            self.assertEqual(len({item[2] for item in outcomes}), 1)
            self.assertEqual(len(tuple(exports.glob("ar_*.json"))), 1)
            verify = connect_database(path)
            self.assertEqual(verify.execute("SELECT count(*) FROM artifact_exports").fetchone()[0], 1)
            self.assertEqual(verify.execute("SELECT count(*) FROM idempotency_records").fetchone()[0], 1)
            verify.close()

    def test_consumed_exact_gate_can_persist_returned_egress_manifest_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            exports = Path(temporary) / "exports"
            exports.mkdir()
            connection = connect_database(Path(temporary) / "factory.sqlite3")
            store = StateStore(connection, export_directories=(exports,))
            store.create_run("run")
            connection.execute("UPDATE runs SET state='draft_ready' WHERE run_id='run'")
            draft = store.add_revision("run", "draft", {"body": "비공개 발명 설명"})
            operation = "hosted:review"
            gate_scope = {
                "approval_scope": "fields:body",
                "data_classes": ["confidential"],
                "model_class": "fixture-model",
                "purpose": "review",
                "recipient": "fixture.local",
            }
            envelope = store.suspend_gate(
                "run",
                GateKind.SENSITIVE_DISCLOSURE,
                suspended_operation=operation,
                subject_revision_hash=draft.content_hash,
                approval_scope=gate_scope,
                return_state=RunState.DRAFT_READY,
                actor="user",
                reason="hosted review approval",
            )
            decision, _ = store.decide_gate(
                envelope.gate_id,
                action="approve",
                actor="user",
                reason="approved",
                subject_revision_hash=draft.content_hash,
                approval_scope=gate_scope,
            )
            consumed = store.consume_decision(
                decision.decision_id,
                suspended_operation=operation,
                subject_revision_hash=draft.content_hash,
                approval_scope=gate_scope,
            )
            approval = EgressApproval(
                decision_id=consumed.decision_id,
                subject_revision_hash=draft.content_hash,
                recipient="fixture.local",
                model_class="fixture-model",
                purpose="review",
                approval_scope="fields:body",
                approved_data_classes=(DataClass.CONFIDENTIAL,),
            )
            calls = []
            response, manifest = guarded_hosted_call(
                lambda current: calls.append(current.manifest_id) or {"fixture": "ok"},
                approval=approval,
                subject_revision_hash=draft.content_hash,
                recipient="fixture.local",
                model_class="fixture-model",
                purpose="review",
                approval_scope="fields:body",
                data_classes=(DataClass.CONFIDENTIAL,),
                content_hashes=(draft.content_hash,),
                payload={"body": "redacted fixture"},
            )
            self.assertEqual(response, {"fixture": "ok"})
            self.assertEqual(calls, [manifest.manifest_id])
            result, _ = store.publish_transition(
                "run",
                RunState.REVIEW_REQUIRED,
                actor="system",
                reason="persist egress manifest",
                operation=operation,
                idempotency_key=manifest.manifest_id,
                artifact_kind="egress_manifest",
                artifact_content=manifest.as_dict(),
                export_directory=exports,
                dependencies=(draft.revision_id,),
                consumed_decision_id=decision.decision_id,
            )
            self.assertEqual(result.snapshot.state, RunState.REVIEW_REQUIRED)
            self.assertEqual(result.artifact.content, manifest.as_dict())
            registry = connection.execute("SELECT byte_hash,path FROM artifact_exports WHERE revision_id=?", (result.artifact.revision_id,)).fetchone()
            self.assertTrue(Path(registry["path"]).is_file())
            used = connection.execute("SELECT used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?",(decision.decision_id,)).fetchone()
            self.assertIsNotNone(used["used_at"])
            self.assertEqual(used["consumed_by_event_id"],result.event_id)
            replay, replay_export = store.publish_transition(
                "run",
                RunState.REVIEW_REQUIRED,
                actor="ignored",
                reason="idempotent replay",
                operation=operation,
                idempotency_key=manifest.manifest_id,
                artifact_kind="egress_manifest",
                artifact_content=manifest.as_dict(),
                export_directory=exports,
                dependencies=(draft.revision_id,),
                consumed_decision_id=decision.decision_id,
            )
            self.assertTrue(replay.replayed)
            self.assertTrue(replay_export.reused)
            with self.assertRaisesRegex(GateMismatchError,"current consumed approval"):
                store.publish_transition(
                    "run",
                    RunState.REVIEW_REQUIRED,
                    actor="system",
                    reason="reuse blocked",
                    operation=operation,
                    idempotency_key="distinct-key",
                    artifact_kind="egress_manifest",
                    artifact_content=manifest.as_dict(),
                    export_directory=exports,
                    dependencies=(draft.revision_id,),
                    consumed_decision_id=decision.decision_id,
                )
            connection.close()

    def _consumed_publish_gate(self, path, exports):
        connection = connect_database(path, busy_timeout_ms=5000)
        store = StateStore(connection, export_directories=(exports,))
        store.create_run("run")
        connection.execute("UPDATE runs SET state='draft_ready' WHERE run_id='run'")
        draft = store.add_revision("run","draft",{"body":"private"})
        scope = {"recipient":"attorney"}
        envelope = store.suspend_gate("run",GateKind.SENSITIVE_DISCLOSURE,suspended_operation="export:report",subject_revision_hash=draft.content_hash,approval_scope=scope,return_state=RunState.DRAFT_READY,actor="user",reason="gate")
        decision,_ = store.decide_gate(envelope.gate_id,action="approve",actor="user",reason="yes",subject_revision_hash=draft.content_hash,approval_scope=scope)
        store.consume_decision(decision.decision_id,suspended_operation="export:report",subject_revision_hash=draft.content_hash,approval_scope=scope)
        return connection,draft,decision

    def test_approval_claim_rolls_back_with_publish_and_concurrent_reuse_has_one_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            exports = Path(temporary) / "exports"
            exports.mkdir()
            connection,draft,decision = self._consumed_publish_gate(path, exports)
            values = {
                "run_id":"run", "next_state":RunState.REVIEW_REQUIRED, "actor":"system", "reason":"publish",
                "operation":"export:report", "artifact_kind":"report", "artifact_content":{"body":"safe"},
                "export_directory":exports, "dependencies":(draft.revision_id,), "consumed_decision_id":decision.decision_id,
            }
            with self.assertRaises(InjectedFailure):
                StateStore(connection, export_directories=(exports,)).publish_transition(idempotency_key="rollback",fault_at="after_decision_claim",**values)
            claimed = connection.execute("SELECT used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?",(decision.decision_id,)).fetchone()
            self.assertEqual(tuple(claimed),(None,None))
            connection.close()

            barrier = threading.Barrier(2)
            outcomes = []
            lock = threading.Lock()

            def publish(key):
                worker = connect_database(path,busy_timeout_ms=5000)
                try:
                    barrier.wait()
                    result = StateStore(worker, export_directories=(exports,)).publish_transition(idempotency_key=key,**values)[0]
                    outcome = ("success",result.event_id)
                except GateMismatchError as error:
                    outcome = ("blocked",str(error))
                finally:
                    worker.close()
                with lock:
                    outcomes.append(outcome)

            threads = [threading.Thread(target=publish,args=(key,)) for key in ("concurrent-a","concurrent-b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(item[0] for item in outcomes),["blocked","success"])
            verify = connect_database(path)
            row = verify.execute("SELECT used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?",(decision.decision_id,)).fetchone()
            self.assertIsNotNone(row["used_at"])
            self.assertEqual(row["consumed_by_event_id"],[item[1] for item in outcomes if item[0] == "success"][0])
            self.assertEqual(verify.execute("SELECT count(*) FROM idempotency_records WHERE operation='export:report'").fetchone()[0],1)
            verify.close()


    def test_invalidated_published_artifact_cannot_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, store, upstream, exports = self.setup_run(temporary)
            first, _ = self.publish(store, upstream, exports)
            store.add_revision("run", "profile", {"name": "changed"})
            with self.assertRaisesRegex(StaleRevisionError, "invalidated"):
                self.publish(store, upstream, exports)
            self.assertEqual(connection.execute("SELECT stale FROM artifact_revisions WHERE revision_id=?", (first.artifact.revision_id,)).fetchone()[0], 1)
            connection.close()

if __name__ == "__main__":
    unittest.main()
