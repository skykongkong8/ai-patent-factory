import sqlite3
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import SCHEMA_VERSION, InjectedFailure, connect_database
from patent_factory.models import (
    AdapterFailure,
    AdapterFailureKind,
    AdapterRecord,
    AdapterResult,
    QueryEnvelope,
)
from patent_factory.provenance import digest, evidence_revision_id
from patent_factory.research import ResearchBudget, ResearchStore, plan_keyword_queries
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

    def test_idempotent_replay_makes_no_second_adapter_call(self):
        adapter = FixtureAdapter(success())
        first = self.store.execute(adapter, query(), idempotency_key="same")
        second = self.store.execute(adapter, query(), idempotency_key="same")
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(first.event_id, second.event_id)
        self.assertTrue(second.replayed)
        with self.assertRaisesRegex(ValueError, "different query"):
            self.store.execute(adapter, query("other"), idempotency_key="same")

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


if __name__ == "__main__":
    unittest.main()
