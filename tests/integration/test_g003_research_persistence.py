import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KIPRIS_HOST, KiprisAdapter
from patent_factory.adapters.manual_web import ManualWebAdapter
from patent_factory.artifacts import ArtifactError
from patent_factory.database import SCHEMA_VERSION, InjectedFailure, connect_database
from patent_factory.models import (
    AdapterFailure,
    AdapterFailureKind,
    AdapterRecord,
    AdapterResult,
    QueryEnvelope,
    RunState,
)
from patent_factory.provenance import digest, evidence_revision_id
from patent_factory.research import (
    CredentialRequiredError,
    PlannedQuery,
    ResearchBudget,
    ResearchStore,
    plan_keyword_queries,
    run_research,
)
from patent_factory.state import StateStore


class FixtureAdapter:
    name = "fixture"
    version = "v1"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def search(self, envelope):
        self.calls += 1
        return self.result


def query(word="센서"):
    return QueryEnvelope(
        run_id="run", adapter="fixture", adapter_version="v1", capability="word_search",
        allowed_scheme="https", allowed_host="fixture.invalid", deadline_seconds=1,
        page=1, page_cap=2, result_budget=10, byte_budget=1000, retry_budget=0,
        retry_ownership="research_runner", query_projection={"word": word},
    )


def success():
    record = AdapterRecord(
        source_type="fixture", source_locator="kr-patent:1020240012345",
        original_identifier="10-2024-0012345", title="센서 장치",
        content_hash=digest({"public": "normalized metadata"}), language="ko",
        limitations=("fixture",),
    )
    return AdapterResult((record,), "response-hash", "fixture terms", {"usable": 1})


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


class ResearchPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temporary.name) / "factory.sqlite3")
        StateStore(self.connection).create_run("run")
        self.store = ResearchStore(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_stable_evidence_deduplicates_across_queries_and_dates_but_preserves_edges(self):
        adapter = FixtureAdapter(success())
        first = self.store.execute(adapter, query("센서"), idempotency_key="one", retrieved_at="2026-01-01T00:00:00Z")
        second = self.store.execute(adapter, query("제어"), idempotency_key="two", retrieved_at="2026-02-01T00:00:00Z")
        expected = evidence_revision_id(success().records[0].source_locator, success().records[0].content_hash)
        self.assertEqual(first.evidence_ids, (expected,))
        self.assertEqual(second.evidence_ids, (expected,))
        self.assertNotEqual(first.observation_ids, second.observation_ids)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_queries").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM retrieval_observations").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_edges").fetchone()[0], 2)

    def test_failure_persists_event_observation_and_limitation_but_no_evidence(self):
        result = AdapterResult(
            (), None, "fixture terms", {"usable": 0},
            failure=AdapterFailure(AdapterFailureKind.TIMEOUT, "bounded timeout", True),
        )
        execution = self.store.execute(FixtureAdapter(result), query(), idempotency_key="failure")
        self.assertEqual((execution.status, execution.failure_kind), ("failure", "timeout"))
        self.assertEqual(execution.evidence_ids, ())
        self.assertEqual(self.connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT access_status FROM retrieval_observations").fetchone()[0], "failure")
        self.assertEqual(self.connection.execute("SELECT failure_kind FROM coverage_limitations").fetchone()[0], "timeout")
        self.assertEqual(self.connection.execute("SELECT status FROM adapter_events").fetchone()[0], "failure")

    def test_empty_success_persists_explicit_observation_and_rate_limit_without_evidence(self):
        result = AdapterResult(
            (), "empty-response", "fixture terms", {"usable": 0},
            rate_limit={"limit": "50", "remaining": "49"},
        )
        execution = self.store.execute(FixtureAdapter(result), query(), idempotency_key="empty")
        self.assertEqual(execution.status, "success")
        self.assertEqual(execution.evidence_ids, ())
        self.assertEqual(len(execution.observation_ids), 1)
        observation = self.connection.execute(
            "SELECT access_status,evidence_id FROM retrieval_observations"
        ).fetchone()
        self.assertEqual((observation["access_status"], observation["evidence_id"]), ("success", None))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_edges").fetchone()[0], 0)
        self.assertEqual(json.loads(self.connection.execute(
            "SELECT rate_limit_json FROM adapter_events"
        ).fetchone()[0]), {"limit": "50", "remaining": "49"})

    def test_idempotent_replay_makes_no_second_adapter_call(self):
        adapter = FixtureAdapter(success())
        first = self.store.execute(adapter, query(), idempotency_key="same")
        second = self.store.execute(adapter, query(), idempotency_key="same")
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(first.event_id, second.event_id)
        self.assertTrue(second.replayed)
        with self.assertRaisesRegex(ValueError, "different query"):
            self.store.execute(adapter, query("other"), idempotency_key="same")

    def test_duplicate_records_in_one_result_collapse_to_first_rank_without_rollback(self):
        record = success().records[0]
        duplicated = AdapterResult((record, record), "response-hash", "fixture terms", {"usable": 2})
        execution = self.store.execute(FixtureAdapter(duplicated), query(), idempotency_key="duplicates")
        self.assertEqual(len(execution.evidence_ids), 1)
        self.assertEqual(len(execution.observation_ids), 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_edges").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT source_rank FROM research_edges").fetchone()[0], 1)

    def test_source_locator_is_the_cross_adapter_identity_boundary(self):
        shared_hash = "a" * 64
        kipris = AdapterRecord(
            source_type="kipris_patent", source_locator="kr-patent:1020240012345",
            original_identifier="10-2024-0012345", title="same title",
            content_hash=shared_hash, language="ko", provenance="kipris_plus_api",
        )
        manual = AdapterRecord(
            source_type="manual_web", source_locator="https://example.com/patent/1020240012345",
            original_identifier="10-2024-0012345", title="same title",
            content_hash=shared_hash, language="ko", provenance="user_import",
        )
        self.store.execute(
            FixtureAdapter(AdapterResult((kipris,), "one", "terms", {"usable": 1})),
            query("kipris"), idempotency_key="locator-one",
        )
        self.store.execute(
            FixtureAdapter(AdapterResult((manual,), "two", "terms", {"usable": 1})),
            query("manual"), idempotency_key="locator-two",
        )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_edges").fetchone()[0], 2)

    def test_manual_core_boundary_rejects_private_unknown_field_before_query_or_adapter_event(self):
        record = {
            "canonical_url": "https://example.com/public/1", "identifier": "manual-1",
            "title": "public", "content_hash": "a" * 64, "language": "ko",
            "provenance": "user_import", "raw_document": "CORE-MANUAL-CANARY",
        }
        envelope = QueryEnvelope(
            run_id="run", adapter="manual_web", adapter_version="import-v1", capability="import",
            allowed_scheme="https", allowed_host="example.com", deadline_seconds=1,
            page=1, page_cap=1, result_budget=10, byte_budget=10_000, retry_budget=0,
            retry_ownership="research_runner",
            query_projection={"content_type": "application/json", "records": [record]},
        )
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            self.store.execute(
                ManualWebAdapter(("example.com",)), envelope, idempotency_key="manual-canary",
            )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_queries").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 0)
        self.assertNotIn(b"CORE-MANUAL-CANARY", Path(self.temporary.name, "factory.sqlite3").read_bytes())

    def test_fault_rolls_back_entire_authoritative_result_and_retry_succeeds(self):
        adapter = FixtureAdapter(success())
        with self.assertRaises(InjectedFailure):
            self.store.execute(adapter, query(), idempotency_key="retry", fault_at="after_evidence_record")
        for table in ("research_queries", "adapter_events", "evidence_records", "retrieval_observations",
                      "research_edges", "research_operations"):
            self.assertEqual(self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)
        retried = self.store.execute(adapter, query(), idempotency_key="retry")
        self.assertEqual(retried.status, "success")

    def test_manifest_is_deterministically_ordered_and_query_secrets_are_absent(self):
        self.store.execute(FixtureAdapter(success()), query(), idempotency_key="manifest")
        manifest = self.store.manifest("run")
        self.assertEqual(len(manifest["queries"]), 1)
        self.assertNotIn("ServiceKey", repr(manifest))
        self.assertEqual(manifest["edges"][0]["source_rank"], 1)


class QueryPlanningTests(unittest.TestCase):
    def test_bilingual_expansion_is_deterministic_deduplicated_and_hard_bounded(self):
        kwargs = dict(
            run_id="run", origin_query="센서", korean_synonyms=("감지기", "센서"),
            english_synonyms=("sensor", "Detector"), discovered_terms=("depth-two",),
            classifications=("G06F",), applicants=("Applicant",), inventors=("Inventor",),
            budget=ResearchBudget(max_depth=1, max_calls=5, per_adapter_results=7, retry_budget=1),
        )
        first = plan_keyword_queries(**kwargs)
        second = plan_keyword_queries(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual([item.term for item in first], ["센서", "감지기", "Detector", "sensor", "G06F"])
        self.assertEqual(len(first), 5)
        self.assertNotIn("depth-two", [item.term for item in first])
        self.assertTrue(all(item.envelope.result_budget == 7 for item in first))
        self.assertTrue(all(item.envelope.retry_budget == 1 for item in first))


class ResearchMigrationTests(unittest.TestCase):
    def test_v4_to_v5_is_atomic_and_preserves_existing_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            connection = connect_database(path)
            connection.execute("INSERT INTO runs VALUES('kept','new',0,'t','t')")
            for table in ("research_operations", "coverage_limitations", "research_edges",
                          "retrieval_observations", "evidence_records", "adapter_events", "research_queries"):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("PRAGMA user_version=4")
            connection.close()
            with self.assertRaises(InjectedFailure):
                connect_database(path, fault_at="migration_v5")
            raw = sqlite3.connect(path)
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(raw.execute("SELECT state FROM runs WHERE run_id='kept'").fetchone()[0], "new")
            self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name='research_queries'").fetchone())
            raw.close()
            migrated = connect_database(path)
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(migrated.execute("SELECT state FROM runs WHERE run_id='kept'").fetchone()[0], "new")
            migrated.close()

    def test_v5_to_v6_rate_limit_and_provenance_migration_is_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            connection = connect_database(path)
            connection.execute("INSERT INTO runs VALUES('legacy','new',0,'t','t')")
            records = (
                ("manual-json", "manual_web", "https://example.com/1", "{\"provenance\":\"reviewed_import\"}"),
                ("manual-missing", "manual_web", "https://example.com/2", "{}"),
                ("kipris-missing", "kipris_patent", "kr-patent:1", "{}"),
            )
            for evidence_id, source_type, locator, record_json in records:
                connection.execute(
                    "INSERT INTO evidence_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("legacy", evidence_id, source_type, locator, evidence_id, evidence_id,
                     None, evidence_id, "ko", record_json, "t", "placeholder"),
                )
            connection.execute("ALTER TABLE adapter_events DROP COLUMN rate_limit_json")
            connection.execute("ALTER TABLE evidence_records DROP COLUMN provenance")
            connection.execute("PRAGMA user_version=5")
            connection.close()
            with self.assertRaises(InjectedFailure):
                connect_database(path, fault_at="migration_v6")
            raw = sqlite3.connect(path)
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertNotIn("rate_limit_json", {row[1] for row in raw.execute("PRAGMA table_info(adapter_events)")})
            self.assertNotIn("provenance", {row[1] for row in raw.execute("PRAGMA table_info(evidence_records)")})
            raw.close()
            migrated = connect_database(path)
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertIn("rate_limit_json", {row["name"] for row in migrated.execute("PRAGMA table_info(adapter_events)")})
            self.assertIn("provenance", {row["name"] for row in migrated.execute("PRAGMA table_info(evidence_records)")})
            self.assertEqual(
                dict(migrated.execute("SELECT evidence_id,provenance FROM evidence_records ORDER BY evidence_id")),
                {
                    "kipris-missing": "adapter_retrieval",
                    "manual-json": "reviewed_import",
                    "manual-missing": "user_import",
                },
            )
            migrated.close()


class ResearchPublicationAndCredentialTests(unittest.TestCase):
    def test_run_research_sanitizes_manual_envelope_before_state_or_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = connect_database(root / "factory.sqlite3")
            store = ready(connection)
            envelope = QueryEnvelope(
                run_id="run", adapter="manual_web", adapter_version="import-v1", capability="import",
                allowed_scheme="https", allowed_host="example.com", deadline_seconds=1,
                page=1, page_cap=1, result_budget=10, byte_budget=10_000, retry_budget=0,
                retry_ownership="research_runner", query_projection={
                    "content_type": "application/json", "records": [{
                        "canonical_url": "https://example.com/1", "identifier": "one",
                        "title": "public", "content_hash": "a" * 64, "language": "ko",
                        "provenance": "user_import", "private_note": "RUNNER-MANUAL-CANARY",
                    }],
                },
            )
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                run_research(
                    connection, run_root=root, run_id="run",
                    adapter=ManualWebAdapter(("example.com",)), query=envelope,
                    idempotency_key="runner-canary",
                )
            self.assertEqual(store.snapshot("run").state, RunState.RESEARCH_READY)
            self.assertEqual(connection.execute("SELECT count(*) FROM research_queries").fetchone()[0], 0)
            self.assertFalse((root / "research-exports").exists())
            self.assertNotIn(b"RUNNER-MANUAL-CANARY", (root / "factory.sqlite3").read_bytes())
            connection.close()
    def test_export_faults_leave_running_state_and_startup_recovery_removes_unregistered_file(self):
        for boundary in ("after_export_publish", "after_state"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                connection = connect_database(root / "factory.sqlite3")
                ready(connection)
                with self.assertRaises(InjectedFailure):
                    run_research(
                        connection, run_root=root, run_id="run",
                        adapter=FixtureAdapter(success()), query=query(),
                        idempotency_key="publish", retrieved_at="2026-01-01T00:00:00Z",
                        fault_at=boundary,
                    )
                exports = root / "research-exports"
                self.assertEqual(StateStore(connection).snapshot("run").state, RunState.RESEARCH_RUNNING)
                self.assertEqual(connection.execute("SELECT count(*) FROM artifact_exports").fetchone()[0], 0)
                self.assertEqual(len(tuple(exports.glob("ar_*.json"))), 1)
                StateStore(connection, export_directories=(exports,))
                self.assertEqual(tuple(exports.glob("ar_*.json")), ())
                retried = run_research(
                    connection, run_root=root, run_id="run",
                    adapter=FixtureAdapter(success()), query=query(),
                    idempotency_key="publish", retrieved_at="2026-01-01T00:00:00Z",
                )
                self.assertEqual(retried.next_state, "research_complete")
                self.assertEqual(connection.execute("SELECT count(*) FROM artifact_exports").fetchone()[0], 1)
                connection.close()

    def test_registered_export_tamper_is_rejected_by_startup_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = connect_database(root / "factory.sqlite3")
            ready(connection)
            run_research(
                connection, run_root=root, run_id="run", adapter=FixtureAdapter(success()),
                query=query(), idempotency_key="tamper", retrieved_at="2026-01-01T00:00:00Z",
            )
            export_path = Path(connection.execute("SELECT path FROM artifact_exports").fetchone()[0])
            export_path.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "registered export mismatch"):
                StateStore(connection, export_directories=(export_path.parent,))
            connection.close()

    def test_missing_kipris_credential_suspends_before_network_and_current_decision_retries_exact_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = connect_database(root / "factory.sqlite3")
            store = ready(connection)
            calls = []
            envelope = QueryEnvelope(
                run_id="run", adapter="kipris", adapter_version="plus-xml-v1",
                capability="word_search", allowed_scheme="https", allowed_host=KIPRIS_HOST,
                deadline_seconds=3, page=1, page_cap=2, result_budget=10, byte_budget=100_000,
                retry_budget=0, retry_ownership="research_runner", query_projection={
                    "word": "센서", "year": 0, "patent": True, "utility": True,
                },
            )
            planned = PlannedQuery(envelope, "센서", "센서", "origin", 0)
            missing = KiprisAdapter(None, transport=lambda *args: calls.append(args))
            with self.assertRaises(CredentialRequiredError) as captured:
                run_research(
                    connection, run_root=root, run_id="run", adapter=missing, query=planned,
                    idempotency_key="credential", retrieved_at="2026-01-01T00:00:00Z",
                )
            gate = captured.exception.gate
            self.assertEqual(calls, [])
            self.assertEqual(store.snapshot("run").state, RunState.CREDENTIAL_REQUIRED)
            self.assertEqual((gate.suspended_state, gate.return_state),
                             (RunState.RESEARCH_READY, RunState.RESEARCH_READY))
            self.assertIn("credential", gate.suspended_operation)
            self.assertEqual(gate.approval_scope["allowed_host"], KIPRIS_HOST)

            configured_calls = []
            body = Path("tests/fixtures/kipris/word-search-v1.xml").read_bytes()
            configured = KiprisAdapter(
                "configured-secret",
                transport=lambda *_: configured_calls.append(True) or TransportResponse(200, {}, body),
            )
            with self.assertRaises(Exception):
                run_research(
                    connection, run_root=root, run_id="run", adapter=configured, query=planned,
                    idempotency_key="credential", retrieved_at="2026-01-01T00:00:00Z",
                )
            self.assertEqual(configured_calls, [])

            decision, _ = store.decide_gate(
                gate.gate_id, action="configure_and_verify", actor="user", reason="configured",
                subject_revision_hash=gate.subject_revision_hash,
                approval_scope=dict(gate.approval_scope),
                suspended_operation=gate.suspended_operation,
                return_state=gate.return_state,
            )
            completed = run_research(
                connection, run_root=root, run_id="run", adapter=configured, query=planned,
                idempotency_key="credential", retrieved_at="2026-01-01T00:00:00Z",
                credential_decision_id=decision.decision_id,
            )
            self.assertEqual(completed.next_state, "research_complete")
            self.assertEqual(configured_calls, [True])
            decision_row = connection.execute(
                "SELECT used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?",
                (decision.decision_id,),
            ).fetchone()
            self.assertTrue(decision_row["used_at"] and decision_row["consumed_by_event_id"])
            replayed = run_research(
                connection, run_root=root, run_id="run", adapter=configured, query=planned,
                idempotency_key="credential", retrieved_at="2026-01-01T00:00:00Z",
                credential_decision_id=decision.decision_id,
            )
            self.assertTrue(replayed.replayed)
            self.assertEqual(configured_calls, [True])
            connection.close()

    def test_remote_auth_failure_is_recorded_then_suspended_and_decision_retries_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = connect_database(root / "factory.sqlite3")
            store = ready(connection)
            envelope = QueryEnvelope(
                run_id="run", adapter="kipris", adapter_version="plus-xml-v1",
                capability="word_search", allowed_scheme="https", allowed_host=KIPRIS_HOST,
                deadline_seconds=3, page=1, page_cap=2, result_budget=10, byte_budget=100_000,
                retry_budget=0, retry_ownership="research_runner", query_projection={
                    "word": "센서", "year": 0, "patent": True, "utility": True,
                },
            )
            planned = PlannedQuery(envelope, "센서", "센서", "origin", 0)
            calls = []
            rejected = b"<response><successYN>N</successYN><resultCode>30</resultCode></response>"
            with self.assertRaises(CredentialRequiredError) as captured:
                run_research(
                    connection, run_root=root, run_id="run",
                    adapter=KiprisAdapter(
                        "invalid-secret",
                        transport=lambda *_: calls.append("rejected") or TransportResponse(200, {}, rejected),
                    ),
                    query=planned, idempotency_key="remote-auth", retrieved_at="2026-01-01T00:00:00Z",
                )
            gate = captured.exception.gate
            self.assertEqual(calls, ["rejected"])
            self.assertEqual(store.snapshot("run").state, RunState.CREDENTIAL_REQUIRED)
            self.assertEqual(connection.execute("SELECT failure_kind FROM adapter_events").fetchone()[0], "auth")
            self.assertEqual(connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 0)

            decision, _ = store.decide_gate(
                gate.gate_id, action="configure_and_verify", actor="user", reason="rotated",
                subject_revision_hash=gate.subject_revision_hash,
                approval_scope=dict(gate.approval_scope),
            )
            success_body = Path("tests/fixtures/kipris/word-search-v1.xml").read_bytes()
            completed = run_research(
                connection, run_root=root, run_id="run",
                adapter=KiprisAdapter(
                    "rotated-secret",
                    transport=lambda *_: calls.append("accepted") or TransportResponse(200, {}, success_body),
                ),
                query=planned, idempotency_key="remote-auth", retrieved_at="2026-01-02T00:00:00Z",
                credential_decision_id=decision.decision_id,
            )
            self.assertEqual(completed.next_state, "research_complete")
            self.assertEqual(calls, ["rejected", "accepted"])
            self.assertEqual(connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM coverage_limitations").fetchone()[0], 1)
            connection.close()


if __name__ == "__main__":
    unittest.main()
