import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from patent_factory.database import InjectedFailure, connect_database
from patent_factory.decisions import resolve_gate
from patent_factory.models import GateKind, RunState
from patent_factory.provenance import digest
from patent_factory.report import _bound_decision, _excessive_decision
from patent_factory.state import StateStore

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None


class G006DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audit_exports = self.root / "audit-exports"
        self.audit_exports.mkdir(mode=0o700)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.store = StateStore(self.connection, export_directories=(self.audit_exports,))
        self.store.create_run("run")
        self.connection.execute("UPDATE runs SET state='audit_running' WHERE run_id='run'")

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def _excessive(self):
        finalists = [{"candidate_id": f"ca_{index}", "finalist_id": f"fi_{index}"} for index in range(1, 4)]
        finalist = self.store.add_revision("run", "finalist_set", {"finalists": finalists})
        corpora = [{"corpus_hash": digest({"corpus": index}), "finalist_id": f"fi_{index}"} for index in range(1, 4)]
        corpus = self.store.add_revision("run", "corpus_set", {"corpora": corpora}, dependencies=(finalist.revision_id,))
        maps = [{"feature_map": {}, "finalist_id": f"fi_{index}", "map_id": f"fm_{index}"} for index in range(1, 4)]
        feature = self.store.add_revision("run", "feature_map_set", {"maps": maps}, dependencies=(finalist.revision_id, corpus.revision_id))
        config = self.store.add_revision("run", "scorer_config", {"version": "simrisk-v1.0.0"})
        results = [{
            "candidate_id": f"ca_{index}", "corpus_hash": corpora[index - 1]["corpus_hash"],
            "finalist_id": f"fi_{index}",
            "outcome": "decision_required" if index < 3 else "audit_approved",
        } for index in range(1, 4)]
        audit_content = {
            "corpus_set_hash": corpus.content_hash,
            "results": results,
            "version": "audit-batch-v1",
        }
        scope = {
            "affected_finalist_ids": ["fi_1", "fi_2"], "audit_hash": digest(audit_content),
            "decision_bindings": [{
                "corpus_hash": corpora[index - 1]["corpus_hash"],
                "finalist_hash": digest(finalists[index - 1]), "finalist_id": f"fi_{index}",
                "map_id": f"fm_{index}",
            } for index in (1, 2)],
            "corpus_set_hash": corpus.content_hash,
            "feature_map_set_hash": feature.content_hash, "finalist_set_hash": finalist.content_hash,
            "outcome": "decision_required", "scorer_config_hash": config.content_hash,
        }
        result, _export, gate = self.store.publish_gate_transition(
            "run", GateKind.EXCESSIVE_SIMILARITY, actor="audit", reason="risk",
            operation="audit.finalize", idempotency_key="audit", approval_scope=scope,
            artifact_kind="audit_batch", artifact_content=audit_content,
            artifact_schema_version="audit-batch-v1",
            dependencies=(finalist.revision_id, corpus.revision_id, feature.revision_id, config.revision_id),
            export_directory=self.audit_exports,
        )
        return gate, result.artifact, scope

    def _coverage(self):
        audit_content = {"results": [], "version": "audit-batch-v1"}
        scope = {"affected_finalist_ids": ["fi_1"], "audit_hash": digest(audit_content), "outcome": "coverage_insufficient"}
        result, _export, gate = self.store.publish_gate_transition(
            "run", GateKind.COVERAGE, actor="audit", reason="coverage",
            operation="audit.finalize", idempotency_key="coverage", approval_scope=scope,
            artifact_kind="audit_batch", artifact_content=audit_content,
            artifact_schema_version="audit-batch-v1", export_directory=self.audit_exports,
        )
        return gate, result.artifact, scope

    @staticmethod
    def _input(gate, scope, action, decisions=None, plan=None):
        return {
            "action": action, "actor": "user", "approval_scope": scope,
            "decisions": decisions or [], "gate_id": gate.gate_id, "plan": plan or {},
            "reason": "reviewed current evidence", "schema_version": "gate-decision-input-v1",
            "subject_revision_hash": gate.subject_revision_hash,
        }

    def _decision_content(self, revision_id):
        row = self.connection.execute(
            "SELECT content_json FROM artifact_revisions WHERE revision_id=?", (revision_id,),
        ).fetchone()
        return json.loads(row["content_json"])

    def _validate_schema(self, content):
        if Draft202012Validator is not None:
            schema = json.loads((Path(__file__).resolve().parents[2] / "schemas/decision.schema.json").read_text(encoding="utf-8"))
            Draft202012Validator(schema).validate(content)

    def test_complete_retain_batch_approves_warns_is_private_and_replays(self):
        gate, _audit, scope = self._excessive()
        entries = [{"action": "retain_with_warning", "finalist_id": item, "reason": "retain with explicit risk"} for item in ("fi_1", "fi_2")]
        request = self._input(gate, scope, "retain_with_warning", entries)
        resolved = resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request)
        self.assertEqual(resolved.next_state, RunState.AUDIT_APPROVED.value)
        artifact = self.connection.execute("SELECT content_json FROM artifact_revisions WHERE revision_id=?", (resolved.artifact_revision_id,)).fetchone()
        content = json.loads(artifact["content_json"])
        self.assertTrue(all(item["warning"] for item in content["decisions"]))
        self._validate_schema(content)
        row = self.connection.execute("SELECT used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?", (resolved.decision_id,)).fetchone()
        self.assertTrue(row["used_at"] and row["consumed_by_event_id"])
        self.assertEqual(stat.S_IMODE((self.root / "decision-exports").stat().st_mode), 0o700)
        replay = resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.artifact_revision_id, resolved.artifact_revision_id)

    def test_missing_extra_duplicate_and_wrong_aggregate_write_nothing(self):
        gate, _audit, scope = self._excessive()
        before = (self.store.snapshot("run").state_version, self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0])
        invalid = (
            [{"action": "refine", "finalist_id": "fi_1", "reason": "one"}],
            [{"action": "refine", "finalist_id": item, "reason": "x"} for item in ("fi_1", "fi_2", "fi_3")],
            [{"action": "refine", "finalist_id": "fi_1", "reason": "x"}, {"action": "replace", "finalist_id": "fi_1", "reason": "y"}],
        )
        for entries in invalid:
            with self.assertRaises(ValueError):
                resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=self._input(gate, scope, "refine", entries))
        mixed = [{"action": "refine", "finalist_id": "fi_1", "reason": "x"}, {"action": "replace", "finalist_id": "fi_2", "reason": "y"}]
        with self.assertRaisesRegex(ValueError, "policy-derived"):
            resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=self._input(gate, scope, "refine", mixed))
        self.assertEqual((self.store.snapshot("run").state_version, self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0]), before)

    def test_wrong_run_and_caller_target_overrides_are_rejected_without_write(self):
        gate, _audit, scope = self._coverage()
        self.store.create_run("other")
        request = self._input(gate, scope, "retry", plan={"attempt": 1})
        with self.assertRaisesRegex(Exception, "unavailable"):
            resolve_gate(self.connection, run_root=self.root, run_id="other", decision_input=request)
        for field in ("return_state", "target_state"):
            forged = dict(request)
            forged[field] = "audit_approved"
            with self.assertRaisesRegex(ValueError, "exact gate-decision-input-v1"):
                resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=forged)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)

    def test_changed_audit_finalist_corpus_map_and_config_bindings_are_stale(self):
        gate, audit, scope = self._excessive()
        entries = [{"action": "retain_with_warning", "finalist_id": item, "reason": "retain"} for item in ("fi_1", "fi_2")]
        request = self._input(gate, scope, "retain_with_warning", entries)
        audit_original = self.connection.execute("SELECT stale,content_hash FROM artifact_revisions WHERE revision_id=?", (audit.revision_id,)).fetchone()
        self.connection.execute("UPDATE artifact_revisions SET stale=1 WHERE revision_id=?", (audit.revision_id,))
        with self.assertRaises(Exception):
            resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request)
        self.connection.execute("UPDATE artifact_revisions SET stale=?,content_hash=? WHERE revision_id=?", (audit_original["stale"], audit_original["content_hash"], audit.revision_id))
        self.connection.execute("UPDATE artifact_revisions SET content_hash=? WHERE revision_id=?", ("e" * 64, audit.revision_id))
        with self.assertRaises(Exception):
            resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request)
        self.connection.execute("UPDATE artifact_revisions SET content_hash=? WHERE revision_id=?", (audit_original["content_hash"], audit.revision_id))

        for kind in ("finalist_set", "corpus_set", "feature_map_set", "scorer_config"):
            with self.subTest(kind=kind):
                row = self.connection.execute(
                    "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind=?",
                    (kind,),
                ).fetchone()
                content = json.loads(row["content_json"])
                changed = json.loads(json.dumps(content))
                if kind == "corpus_set":
                    changed["corpora"][0]["corpus_hash"] = "f" * 64
                elif kind == "feature_map_set":
                    changed["maps"][0]["map_id"] = "fm_changed"
                else:
                    changed["adversarial_change"] = True
                self.connection.execute(
                    "UPDATE artifact_revisions SET content_json=?,content_hash=? WHERE revision_id=?",
                    (json.dumps(changed), digest(changed), row["revision_id"]),
                )
                with self.assertRaisesRegex(Exception, "stale"):
                    resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request)
                self.connection.execute(
                    "UPDATE artifact_revisions SET content_json=?,content_hash=? WHERE revision_id=?",
                    (row["content_json"], row["content_hash"], row["revision_id"]),
                )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)

    def test_metadata_only_current_corpus_set_change_invalidates_frozen_aggregate_binding(self):
        gate, _audit, scope = self._excessive()
        entries = [{"action": "retain_with_warning", "finalist_id": item, "reason": "retain"} for item in ("fi_1", "fi_2")]
        request = self._input(gate, scope, "retain_with_warning", entries)
        row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='corpus_set'"
        ).fetchone()
        changed = json.loads(row["content_json"])
        changed["unaffected_metadata"] = {"note": "aggregate revision changed"}
        self.connection.execute(
            "UPDATE artifact_revisions SET content_json=?,content_hash=? WHERE revision_id=?",
            (json.dumps(changed), digest(changed), row["revision_id"]),
        )
        with self.assertRaisesRegex(Exception, "stale"):
            resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)

    def test_legacy_excessive_scope_without_aggregate_corpus_hash_fails_closed(self):
        gate, _audit, scope = self._excessive()
        legacy_scope = dict(scope)
        legacy_scope.pop("corpus_set_hash")
        self.connection.execute(
            "UPDATE gate_envelopes SET approval_scope_json=?,approval_scope_hash=? WHERE gate_id=?",
            (json.dumps(legacy_scope, sort_keys=True, separators=(",", ":")), digest(legacy_scope), gate.gate_id),
        )
        entries = [{"action": "retain_with_warning", "finalist_id": item, "reason": "retain"} for item in ("fi_1", "fi_2")]
        with self.assertRaisesRegex(Exception, "stale"):
            resolve_gate(
                self.connection, run_root=self.root, run_id="run",
                decision_input=self._input(gate, legacy_scope, "retain_with_warning", entries),
            )

    def test_refine_only_batch_enters_ideation_running(self):
        gate, _audit, scope = self._excessive()
        entries = [{"action": "refine", "finalist_id": item, "reason": "refine mechanism"} for item in ("fi_1", "fi_2")]
        resolved = resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=self._input(gate, scope, "refine", entries))
        self.assertEqual(resolved.next_state, RunState.IDEATION_RUNNING.value)

    def test_mixed_replace_takes_precedence_and_preserves_old_audit(self):
        gate, audit, scope = self._excessive()
        entries = [{"action": "retain_with_warning", "finalist_id": "fi_1", "reason": "retain"}, {"action": "replace", "finalist_id": "fi_2", "reason": "replace and research"}]
        resolved = resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=self._input(gate, scope, "replace", entries))
        self.assertEqual(resolved.next_state, RunState.RESEARCH_RUNNING.value)
        self.assertEqual(self.connection.execute("SELECT stale FROM artifact_revisions WHERE revision_id=?", (audit.revision_id,)).fetchone()[0], 0)

    def test_coverage_dispatch_and_fault_rollback(self):
        gate, _audit, scope = self._coverage()
        request = self._input(gate, scope, "expand", plan={"query_budget": 3, "strategy": "bilingual expansion"})
        with self.assertRaises(InjectedFailure):
            resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request, fault_at="after_decision")
        self.assertEqual(self.store.snapshot("run").state, RunState.COVERAGE_INSUFFICIENT)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)
        StateStore(self.connection, export_directories=(self.audit_exports, self.root / "decision-exports"))
        resolved = resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request)
        self.assertEqual(resolved.next_state, RunState.RESEARCH_RUNNING.value)
        self._validate_schema(self._decision_content(resolved.artifact_revision_id))

    def test_stop_is_terminal_without_partial_finalist_choices(self):
        gate, _audit, scope = self._excessive()
        resolved = resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=self._input(gate, scope, "stop"))
        self.assertEqual(resolved.next_state, RunState.STOPPED.value)

    def test_coverage_stop_is_terminal_and_never_approves(self):
        gate, _audit, scope = self._coverage()
        resolved = resolve_gate(
            self.connection, run_root=self.root, run_id="run",
            decision_input=self._input(gate, scope, "stop"),
        )
        self.assertEqual(resolved.next_state, RunState.STOPPED.value)

    def test_concurrent_identical_resolution_has_one_write_and_one_replay(self):
        gate, _audit, scope = self._coverage()
        request = self._input(gate, scope, "retry", plan={"attempt": 1})
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def decide():
            connection = connect_database(self.root / "factory.sqlite3", busy_timeout_ms=5000)
            try:
                barrier.wait()
                outcome = resolve_gate(connection, run_root=self.root, run_id="run", decision_input=request).replayed
            finally:
                connection.close()
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=decide) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 1)

    def test_competing_different_concurrent_decisions_have_one_winner(self):
        gate, _audit, scope = self._coverage()
        requests = (
            self._input(gate, scope, "retry", plan={"attempt": 1}),
            self._input(gate, scope, "expand", plan={"query_budget": 2}),
        )
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def decide(request):
            connection = connect_database(self.root / "factory.sqlite3", busy_timeout_ms=5000)
            try:
                barrier.wait()
                try:
                    outcome = ("success", resolve_gate(connection, run_root=self.root, run_id="run", decision_input=request).action)
                except Exception as error:
                    outcome = ("blocked", str(error))
            finally:
                connection.close()
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=decide, args=(request,)) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(item[0] for item in outcomes), ["blocked", "success"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM artifact_revisions WHERE kind='gate_resolution'").fetchone()[0], 1)

    def test_legacy_excessive_resolution_still_dispatches_through_the_report_binder_and_rejects_v2(self):
        """AC-7 + R8: post_audit_checkpoint landing must not disturb legacy replay.

        The report binder now dispatches on the gate KIND bound to audit_hash
        (report._bound_decision), not on `affected` (RF#1). This proves a
        persisted excessive_similarity resolution is still reached through
        that dispatch unchanged, and that a v2 payload can never resolve a
        legacy excessive gate (R8's second half; the first half — a v1
        payload rejected by a checkpoint gate — is covered in
        test_g010_checkpoint.py, which has a real checkpoint gate to submit
        it to).
        """
        gate, audit, scope = self._excessive()
        entries = [{"action": "retain_with_warning", "finalist_id": item, "reason": "retain with explicit risk"} for item in ("fi_1", "fi_2")]
        request = self._input(gate, scope, "retain_with_warning", entries)
        resolved = resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=request)
        direct_row, direct_content = _excessive_decision(self.connection, "run", audit.content_hash, audit.content)
        dispatched_row, dispatched_content = _bound_decision(self.connection, "run", audit.content_hash, audit.content)
        self.assertEqual(dispatched_row["revision_id"], direct_row["revision_id"])
        self.assertEqual(dispatched_content, direct_content)
        self.assertEqual(dispatched_content["gate_kind"], "excessive_similarity")
        v2_payload = dict(request)
        v2_payload["schema_version"] = "gate-decision-input-v2"
        v2_payload["feedback"] = [{"boring": "x", "finalist_id": "fi_1", "interesting": "y"}]
        with self.assertRaisesRegex(ValueError, "exact gate-decision-input-v1"):
            resolve_gate(self.connection, run_root=self.root, run_id="run", decision_input=v2_payload)
        self.assertTrue(resolved.decision_id)

    def test_non_audit_gate_kinds_publish_exact_authorizations_without_target_choice(self):
        cases = (
            (GateKind.CONFLICT_RESOLUTION, RunState.PROFILE_PENDING, "choose_value"),
            (GateKind.CREDENTIAL, RunState.PROFILE_READY, "approve"),
            (GateKind.SENSITIVE_DISCLOSURE, RunState.DRAFT_READY, "approve"),
            (GateKind.DOMAIN_PIVOT, RunState.RESEARCH_READY, "approve"),
        )
        for index, (kind, state, action) in enumerate(cases):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                connection = connect_database(root / "factory.sqlite3")
                try:
                    store = StateStore(connection)
                    run_id = f"run-{index}"
                    store.create_run(run_id)
                    connection.execute("UPDATE runs SET state=? WHERE run_id=?", (state.value, run_id))
                    subject = store.add_revision(run_id, f"{kind.value}_subject", {"kind": kind.value})
                    scope = {"kind": kind.value, "purpose": "exact authorization"}
                    gate = store.suspend_gate(
                        run_id, kind, suspended_operation=f"resume:{kind.value}",
                        subject_revision_hash=subject.content_hash, approval_scope=scope,
                        return_state=state, actor="test", reason="gate",
                    )
                    request = self._input(gate, scope, action)
                    resolved = resolve_gate(connection, run_root=root, run_id=run_id, decision_input=request)
                    self.assertEqual(resolved.next_state, state.value)
                    artifact = connection.execute(
                        "SELECT content_json FROM artifact_revisions WHERE revision_id=?", (resolved.artifact_revision_id,),
                    ).fetchone()
                    self._validate_schema(json.loads(artifact["content_json"]))
                    decision = connection.execute(
                        "SELECT consumed_at,used_at FROM gate_decisions WHERE decision_id=?",
                        (resolved.decision_id,),
                    ).fetchone()
                    self.assertEqual(tuple(decision), (None, None))
                finally:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
