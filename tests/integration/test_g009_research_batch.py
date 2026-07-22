import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from patent_factory import cli
from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KIPRIS_HOST, KiprisAdapter
from patent_factory.database import connect_database
from patent_factory.models import (
    AdapterFailure,
    AdapterFailureKind,
    AdapterRecord,
    AdapterResult,
    RunState,
)
from patent_factory.provenance import digest
from patent_factory.research import (
    CredentialRequiredError,
    ResearchBudget,
    plan_keyword_queries,
    refuse_stale_re_research_reentry,
    run_research_batch,
)
from patent_factory.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_XML = ROOT / "tests" / "fixtures" / "kipris" / "word-search-v1.xml"


def ready(connection, run_id="run"):
    store = StateStore(connection)
    store.create_run(run_id)
    store.transition(run_id, "profile_pending", actor="test", reason="start",
                     operation="ready.profile", idempotency_key="1")
    store.transition(run_id, "profile_ready", actor="test", reason="profile",
                     operation="ready.profile-finish", idempotency_key="1")
    store.transition(run_id, "research_ready", actor="test", reason="research",
                     operation="ready.research", idempotency_key="1")
    return store


def success(identifier="10-2024-0012345"):
    record = AdapterRecord(
        source_type="fixture", source_locator=f"kr-patent:{identifier.replace('-', '')}",
        original_identifier=identifier, title="센서 장치",
        content_hash=digest({"public": identifier}), language="ko",
        limitations=("fixture",),
    )
    return AdapterResult((record,), "response-hash", "fixture terms", {"usable": 1})


def failure(kind=AdapterFailureKind.TIMEOUT):
    return AdapterResult(
        (), None, "fixture terms", {"usable": 0},
        failure=AdapterFailure(kind, "bounded failure", True),
    )


class WordAdapter:
    """Per-term canned results; ignores kipris envelope identity like other fixtures."""

    name = "fixture"
    version = "v1"

    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, envelope):
        word = envelope.query_projection["word"]
        self.calls.append(word)
        return self.results[word]


def plan(run_id="run", *, korean=("감지기",), english=("sensor",), max_calls=12):
    return plan_keyword_queries(
        run_id=run_id, origin_query="센서", korean_synonyms=korean,
        english_synonyms=english, budget=ResearchBudget(max_calls=max_calls),
    )


class ResearchBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.store = ready(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def batch(self, adapter, queries, key="batch", **kwargs):
        return run_research_batch(
            self.connection, run_root=self.root, run_id="run", adapter=adapter,
            queries=queries, idempotency_key=key,
            retrieved_at="2026-01-01T00:00:00Z", **kwargs,
        )

    def test_batch_executes_all_planned_queries_dedupes_evidence_and_completes(self):
        queries = plan()
        self.assertEqual(len(queries), 3)
        adapter = WordAdapter({"센서": success(), "감지기": success(), "sensor": success()})
        result = self.batch(adapter, queries)
        self.assertEqual(result.next_state, "research_complete")
        payload = result.as_dict()
        self.assertEqual(payload["planned_count"], 3)
        self.assertEqual(payload["succeeded_count"], 3)
        self.assertEqual(payload["evidence_count"], 1)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(adapter.calls, ["센서", "감지기", "sensor"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_queries").fetchone()[0], 3)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_edges").fetchone()[0], 3)
        bundle_queries = result.bundle["queries"]
        self.assertEqual(len(bundle_queries), 3)

    def test_non_auth_failure_records_limitation_and_the_batch_continues(self):
        adapter = WordAdapter({"센서": success(), "감지기": failure(), "sensor": success("10-2024-0099999")})
        result = self.batch(adapter, plan())
        self.assertEqual(result.next_state, "research_complete")
        payload = result.as_dict()
        self.assertEqual(payload["succeeded_count"], 2)
        self.assertEqual(payload["adapter_status"], {"failure_kinds": ["timeout"], "status": "success"})
        self.assertEqual(
            [item["failure_kind"] for item in payload["queries"]], [None, "timeout", None],
        )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM coverage_limitations").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 2)

    def test_all_failures_leave_research_incomplete(self):
        adapter = WordAdapter({"센서": failure(), "감지기": failure(), "sensor": failure()})
        result = self.batch(adapter, plan())
        self.assertEqual(result.next_state, "research_incomplete")
        self.assertEqual(result.as_dict()["status"], "incomplete")
        self.assertEqual(result.as_dict()["adapter_status"]["status"], "failure")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 0)

    def test_exact_batch_replay_is_idempotent(self):
        adapter = WordAdapter({"센서": success(), "감지기": success(), "sensor": success()})
        first = self.batch(adapter, plan())
        counts = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("research_queries", "evidence_records", "research_operations",
                          "artifact_revisions", "transition_events")
        }
        second = self.batch(adapter, plan())
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(
            {
                table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in counts
            },
            counts,
        )

    def test_missing_credential_suspends_whole_batch_before_any_network(self):
        calls = []
        missing = KiprisAdapter(None, transport=lambda *args: calls.append(args))
        queries = plan()
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(missing, queries, key="credential-batch")
        gate = captured.exception.gate
        self.assertEqual(calls, [])
        self.assertEqual(self.store.snapshot("run").state, RunState.CREDENTIAL_REQUIRED)
        self.assertEqual(gate.return_state, RunState.RESEARCH_READY)
        self.assertEqual(gate.approval_scope["query_count"], 3)
        self.assertEqual(gate.approval_scope["allowed_host"], KIPRIS_HOST)
        self.assertEqual(gate.approval_scope["credential_name"], "KIPRIS_PLUS_API_KEY")

        configured_calls = []
        body = FIXTURE_XML.read_bytes()
        configured = KiprisAdapter(
            "configured-secret",
            transport=lambda *_: configured_calls.append(True) or TransportResponse(200, {}, body),
        )
        decision, _ = self.store.decide_gate(
            gate.gate_id, action="configure_and_verify", actor="user", reason="configured",
            subject_revision_hash=gate.subject_revision_hash,
            approval_scope=dict(gate.approval_scope),
            suspended_operation=gate.suspended_operation,
            return_state=gate.return_state,
        )
        completed = self.batch(
            configured, queries, key="credential-batch",
            credential_decision_id=decision.decision_id,
        )
        self.assertEqual(completed.next_state, "research_complete")
        self.assertEqual(len(configured_calls), 3)
        decision_row = self.connection.execute(
            "SELECT used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?",
            (decision.decision_id,),
        ).fetchone()
        self.assertTrue(decision_row["used_at"] and decision_row["consumed_by_event_id"])
        replayed = self.batch(
            configured, queries, key="credential-batch",
            credential_decision_id=decision.decision_id,
        )
        self.assertTrue(replayed.replayed)
        self.assertEqual(len(configured_calls), 3)

    def test_fresh_run_with_no_re_research_history_is_never_refused(self):
        # Finding #12, path 1: a run that has never had a re_research gate
        # resolution must never be refused — this is the legitimate
        # first-pass (or in-flight retry) research_running case.
        refuse_stale_re_research_reentry(self.connection, "run")  # must not raise
        adapter = WordAdapter({"센서": success(), "감지기": success(), "sensor": success()})
        result = self.batch(adapter, plan())
        self.assertEqual(result.next_state, "research_complete")

    def test_auth_rejection_mid_batch_suspends_with_running_return_state(self):
        rejected = b"<response><successYN>N</successYN><resultCode>30</resultCode></response>"
        adapter = KiprisAdapter(
            "invalid-secret", transport=lambda *_: TransportResponse(200, {}, rejected),
        )
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(adapter, plan(), key="auth-batch")
        gate = captured.exception.gate
        self.assertEqual(self.store.snapshot("run").state, RunState.CREDENTIAL_REQUIRED)
        self.assertEqual(gate.return_state, RunState.RESEARCH_RUNNING)
        self.assertEqual(
            self.connection.execute("SELECT failure_kind FROM adapter_events").fetchone()[0],
            "auth",
        )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 0)


class StubKiprisAdapter:
    """CLI-injected stand-in mirroring the credential surface of KiprisAdapter."""

    name = "kipris"
    version = "plus-xml-v1"
    credential_name = "KIPRIS_PLUS_API_KEY"

    def __init__(self, service_key, *, transport=None, credential_required=True):
        del transport
        self._service_key = service_key
        self.requires_credential = credential_required

    @property
    def credential_present(self):
        return bool(self._service_key)

    def search(self, envelope):
        del envelope
        return success()


class ResearchKiprisCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        workspace = Path(self.temporary.name)
        self.workspace_rel = workspace.relative_to(ROOT)
        self.run_root = workspace / "run"
        self.run_root.mkdir(mode=0o700)
        connection = connect_database(self.run_root / "factory.sqlite3")
        try:
            ready(connection)
        finally:
            connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main([str(item) for item in argv])
        return json.loads(stream.getvalue()), code

    def kipris_argv(self, *extra):
        return (
            "research", "kipris", "--run", self.run_root.relative_to(ROOT),
            "--run-id", "run", "--query", "센서", "--korean-synonym", "감지기",
            "--english-synonym", "sensor", "--retrieved-at", "2026-01-01T00:00:00Z",
            "--workspace-root", self.workspace_rel, *extra,
        )

    def test_cli_kipris_batch_completes_with_configured_credential(self):
        with patch.object(cli, "KiprisAdapter", StubKiprisAdapter), patch.dict(
            os.environ, {"KIPRIS_PLUS_API_KEY": "stub-key"},
        ):
            payload, code = self.invoke(*self.kipris_argv())
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["next_state"], "research_complete")
        self.assertEqual(payload["planned_count"], 3)
        self.assertEqual(payload["schema_version"], "cli-result-v1")
        self.assertEqual(payload["adapter_summary"]["status"], "success")

    def test_cli_kipris_without_credential_reports_gate_and_exit_five(self):
        environment = {key: value for key, value in os.environ.items() if key != "KIPRIS_PLUS_API_KEY"}
        with patch.dict(os.environ, environment, clear=True):
            payload, code = self.invoke(*self.kipris_argv())
        self.assertEqual(code, 5, payload)
        self.assertEqual(payload["status"], "credential_required")
        self.assertEqual(payload["next_state"], "credential_required")
        self.assertEqual(payload["credential_name"], "KIPRIS_PLUS_API_KEY")
        self.assertTrue(payload["gate_id"])
        self.assertTrue(payload["subject_revision_hash"])
        self.assertNotIn("stub-key", json.dumps(payload))


class AuditLiveCliFlagTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        workspace = Path(self.temporary.name)
        self.workspace_rel = workspace.relative_to(ROOT)
        self.run_root = workspace / "run"
        self.run_root.mkdir(mode=0o700)
        query_input = workspace / "audit-query-input-v1.json"
        query_input.write_text(json.dumps({
            "finalist_set_hash": "0" * 64, "groups": [], "schema_version": "audit-query-input-v1",
        }), encoding="utf-8")
        self.query_rel = query_input.relative_to(ROOT)

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main([str(item) for item in argv])
        return json.loads(stream.getvalue()), code

    def test_live_and_fixture_manifest_are_mutually_exclusive(self):
        payload, code = self.invoke(
            "audit", "retrieve", "--run", self.run_root.relative_to(ROOT), "--run-id", "run",
            "--query-input", self.query_rel, "--live",
            "--fixture-manifest", "documents/never-read.json",
            "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "error")
        self.assertIn("--live", payload["error"])

    def test_fixture_mode_requires_manifest(self):
        payload, code = self.invoke(
            "audit", "retrieve", "--run", self.run_root.relative_to(ROOT), "--run-id", "run",
            "--query-input", self.query_rel,
            "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "error")
        self.assertIn("--fixture-manifest", payload["error"])


if __name__ == "__main__":
    unittest.main()
