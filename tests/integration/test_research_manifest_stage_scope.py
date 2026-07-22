"""Regression for PR49 review #5 / plan J1.

`audit.py` retrieves its final similarity corpus through the same
`ResearchStore` the research stage uses (`audit.py:306`), tagging every query
it plans with `term_kind` `audit_{language}`. Both stages' rows land in the
same `research_queries` / `evidence_records` / `research_edges` tables under
the same `run_id`. Before `ResearchStore.manifest` stage-scoped its reads, any
caller that re-read the manifest after an audit had already run — the
COVERAGE-`expand` re-entry chief among them — got the audit's own search terms
and evidence back as if the research stage had found them: the persisted
`research_bundle` was contaminated, and so was report.py's section 4
("Research Scope and Method"), which renders straight from that bundle.

This test writes one legitimate research-stage row and one audit-shaped row
directly through `ResearchStore.execute` (bypassing the CLI/audit.py, matching
the plan's "offline run" framing), then asserts both `ResearchStore.manifest`
and the rendered section 4 text exclude the audit-tagged row and its evidence.
`tests/e2e/test_research_reentry.py` proves the same fix end-to-end through
the real COVERAGE-expand CLI route; this test isolates the manifest/report
boundary so the regression is pinned without a subprocess-driven CLI journey.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database
from patent_factory.models import AdapterRecord, AdapterResult, QueryEnvelope, RunState
from patent_factory.provenance import digest
from patent_factory.report import _section_bodies, load_report_policy
from patent_factory.research import (
    PlannedQuery, ResearchStore, _private_export_directory, research_bundle, run_research_batch,
)
from patent_factory.state import StateStore, workspace_export_directories

ROOT = Path(__file__).resolve().parents[2]


class _FixtureAdapter:
    """Serves one canned record per call; ignores envelope identity."""

    name = "fixture"
    version = "v1"

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier

    def search(self, envelope: QueryEnvelope) -> AdapterResult:
        del envelope
        record = AdapterRecord(
            source_type="fixture", source_locator=f"fixture:{self.identifier}",
            original_identifier=self.identifier, title=f"title {self.identifier}",
            content_hash=digest({"id": self.identifier}), language="ko",
            limitations=("fixture",),
        )
        return AdapterResult((record,), f"response-{self.identifier}", "fixture terms", {"usable": 1})


class ResearchManifestStageContaminationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.connection = connect_database(Path(self.temporary.name) / "factory.sqlite3")
        StateStore(self.connection).create_run("run")
        self.store = ResearchStore(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def _envelope(self, term: str) -> QueryEnvelope:
        return QueryEnvelope(
            run_id="run", adapter="fixture", adapter_version="v1", capability="word_search",
            allowed_scheme="https", allowed_host="example.test", deadline_seconds=1,
            page=1, page_cap=1, result_budget=10, byte_budget=10_000, retry_budget=0,
            retry_ownership="research_runner", query_projection={"word": term},
        )

    def _execute(self, *, term: str, term_kind: str, identifier: str, idempotency_key: str):
        planned = PlannedQuery(self._envelope(term), term, term, term_kind, 0)
        return self.store.execute(
            _FixtureAdapter(identifier), planned,
            idempotency_key=idempotency_key, retrieved_at="2026-01-01T00:00:00Z",
        )

    def test_manifest_and_section_4_exclude_audit_tagged_rows(self):
        # The research stage's own query, as `plan_keyword_queries` would plan it.
        self._execute(
            term="on-device inference", term_kind="origin",
            identifier="legit-record", idempotency_key="research-pass",
        )
        # What audit.py:306 writes through the same store: `PlannedQuery(...,
        # query["term"], query["term"], f"audit_{query['language']}", 0)`.
        self._execute(
            term="온디바이스 추론", term_kind="audit_ko",
            identifier="audit-only-record", idempotency_key="audit-pass",
        )

        manifest = self.store.manifest("run")
        term_kinds = {
            json.loads(row["plan_json"]).get("term_kind") for row in manifest["queries"]
        }
        self.assertEqual(term_kinds, {"origin"})
        self.assertEqual(
            [row["original_identifier"] for row in manifest["evidence"]], ["legit-record"],
        )
        self.assertTrue(all(
            row["query_id"] in {q["query_id"] for q in manifest["queries"]}
            for row in manifest["edges"]
        ))

        bundle = research_bundle(manifest)
        policy = load_report_policy("en")
        sections = _section_bodies(
            policy=policy, report_input={
                "report_date": "2026-01-01", "profile_fields": [],
                "handoff_questions": [], "recommended_investigations": [],
            },
            profile={}, research=bundle, candidates=[], finalists=[], corpus={}, audit={},
            decision=None, evidence={}, cited_ids=[], scorer={}, language="en",
        )
        section_4 = sections[3]
        self.assertIn("on-device inference (origin)", section_4)
        self.assertNotIn("audit_ko", section_4)
        self.assertNotIn("audit-only-record", section_4)
        self.assertNotIn("온디바이스 추론", section_4)
        self.assertIn("Evidence record count (research-stage scope): 1", section_4)
        self.assertIn("Query record count: 1", section_4)


class ResearchBundleReplayCompatibilityTests(unittest.TestCase):
    """RC3 (plan 1.2): a pre-upgrade DB can already hold a `research_bundle`
    built by the OLD unfiltered `ResearchStore.manifest`, contaminated with the
    audit stage's own rows, plus the `idempotency_records` row that published
    it — the COVERAGE-expand re-entry route predates both this fix and PR49
    itself, so this is not a hypothetical.

    Replaying that EXACT operation (same run, same `operation`, same
    `idempotency_key`) after the upgrade must not brick. The safety net the
    plan names is `StateStore._add_revision` inserting a new revision on a
    content_hash change rather than raising (state.py:399-405) — but the path
    this test actually exercises is `StateStore._published_replay`
    (state.py:423-435, invoked from `publish_transition` at state.py:532)
    matching the stale `(run_id, operation, idempotency_key)` and returning the
    OLD recorded artifact BEFORE the newly (and now correctly) filtered content
    `run_research_batch` computes is ever considered. That is correct replay
    semantics, not a residual bug: replaying one specific past operation must
    reproduce what that operation actually published, not silently rewrite it
    under today's logic. This test pins that no exception occurs and documents
    why, per the plan's instruction to cite the short-circuit if that is what
    it hits.

    Reaching "RESEARCH_RUNNING after a completed RESEARCH_COMPLETE" for real
    requires the full COVERAGE-gate machinery (`decide_gate` explicitly refuses
    to resolve COVERAGE gates itself — state.py: "coverage ... decisions
    require an atomic resolution artifact" — that atomic resolution lives in
    decisions.py and is already exercised end-to-end by
    tests/e2e/test_research_reentry.py). This test only needs the fact of that
    state, not its ceremony, so — following the same shortcut
    tests/integration/test_g007_report_review_validation.py's fixture takes
    (`UPDATE runs SET state=... WHERE run_id=...`) — it sets `runs.state`
    directly rather than re-deriving a coverage gate.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.run_root = Path(self.temporary.name)
        self.connection = connect_database(self.run_root / "factory.sqlite3")
        StateStore(self.connection).create_run("run")
        self.connection.execute("UPDATE runs SET state='research_ready' WHERE run_id='run'")
        self.store = ResearchStore(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def _plan(self, term: str, term_kind: str) -> PlannedQuery:
        envelope = QueryEnvelope(
            run_id="run", adapter="fixture", adapter_version="v1", capability="word_search",
            allowed_scheme="https", allowed_host="example.test", deadline_seconds=1,
            page=1, page_cap=1, result_budget=10, byte_budget=10_000, retry_budget=0,
            retry_ownership="research_runner", query_projection={"word": term},
        )
        return PlannedQuery(envelope, term, term, term_kind, 0)

    def _legacy_unfiltered_manifest(self) -> dict:
        """What `ResearchStore.manifest` returned before J1 stage-scoped it:
        every row for the run, with no `term_kind` filter at all."""

        def rows(sql: str) -> list[dict]:
            return [dict(row) for row in self.connection.execute(sql, ("run",))]

        return {
            "adapter_events": rows("SELECT * FROM adapter_events WHERE run_id=? ORDER BY retrieved_at,event_id"),
            "coverage_limitations": rows("SELECT * FROM coverage_limitations WHERE run_id=? ORDER BY created_at,limitation_id"),
            "edges": rows("SELECT * FROM research_edges WHERE run_id=? ORDER BY query_id,source_rank,evidence_id"),
            "evidence": rows("SELECT * FROM evidence_records WHERE run_id=? ORDER BY evidence_id"),
            "observations": rows("SELECT * FROM retrieval_observations WHERE run_id=? ORDER BY retrieved_at,observation_id"),
            "queries": rows("SELECT * FROM research_queries WHERE run_id=? ORDER BY created_at,query_id"),
            "run_id": "run",
        }

    def test_replaying_a_pre_upgrade_contaminated_operation_does_not_brick(self):
        RC3_KEY = "reentry-rc3-legacy-key"

        # -- A clean first pass, exactly like a real run: RESEARCH_READY ->
        # RESEARCH_RUNNING -> RESEARCH_COMPLETE, through the real fixed code.
        first = run_research_batch(
            self.connection, run_root=self.run_root, run_id="run",
            adapter=_FixtureAdapter("legit-record"), queries=(self._plan("on-device inference", "origin"),),
            idempotency_key="first-pass", retrieved_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(first.next_state, "research_complete")

        # -- The audit stage writes its own tagged rows through the same store
        # (audit.py:306), independent of any state transition.
        self.store.execute(
            _FixtureAdapter("audit-only-record"),
            self._plan("온디바이스 추론", "audit_ko"),
            idempotency_key="audit-pass", retrieved_at="2026-01-01T00:00:00Z",
        )

        # -- Seed the pre-upgrade fact pattern: a SECOND research_bundle publish,
        # built by the OLD unfiltered manifest (so it is genuinely contaminated),
        # recorded under the operation/key a real re-entry publish would use.
        # `UPDATE runs SET state=...` stands in for the COVERAGE-gate ceremony
        # that would really have produced RESEARCH_RUNNING here (see class
        # docstring); nothing below depends on how that state was reached.
        self.connection.execute("UPDATE runs SET state='research_running' WHERE run_id='run'")
        legacy_manifest = self._legacy_unfiltered_manifest()
        legacy_term_kinds = {
            json.loads(row["plan_json"]).get("term_kind") for row in legacy_manifest["queries"]
        }
        self.assertEqual(
            legacy_term_kinds, {"origin", "audit_ko"},
            "the seeded legacy bundle must actually be contaminated",
        )
        legacy_payload = research_bundle(legacy_manifest)
        root, exports = _private_export_directory(self.run_root, create=True)
        legacy_state = StateStore(
            self.connection, export_directories=workspace_export_directories(self.connection, root, (exports,)),
        )
        seeded, _seeded_export = legacy_state.publish_transition(
            "run", RunState.RESEARCH_COMPLETE, actor="test", reason="seed a pre-upgrade contaminated publish",
            operation="research.finish", idempotency_key=RC3_KEY,
            artifact_kind="research_bundle", artifact_content=legacy_payload,
            artifact_schema_version="research-bundle-v1", export_directory=exports,
        )
        self.assertFalse(seeded.replayed)
        legacy_revision_id = seeded.artifact.revision_id

        # -- Post-upgrade: a genuine re-entry reaches RESEARCH_RUNNING again
        # (same shortcut as above) and replays the EXACT prior operation/key —
        # e.g. a retried CLI invocation after the software was upgraded.
        self.connection.execute("UPDATE runs SET state='research_running' WHERE run_id='run'")
        try:
            replayed = run_research_batch(
                self.connection, run_root=self.run_root, run_id="run",
                adapter=_FixtureAdapter("kv-cache-eviction"),
                queries=(self._plan("kv cache eviction policy", "origin"),),
                idempotency_key=RC3_KEY, retrieved_at="2026-01-01T00:00:00Z",
            )
        except Exception as error:  # pragma: no cover - the assertion IS "this must not happen"
            self.fail(f"replaying the pre-upgrade operation bricked instead of replaying: {error!r}")

        # No brick. And because the replay short-circuits before the freshly
        # (correctly) filtered content is considered, the CURRENT research_bundle
        # is still the old, contaminated one — proving this was a genuine
        # idempotent replay, not a silent re-derivation.
        self.assertEqual(replayed.artifact_revision_id, legacy_revision_id)
        current = self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='research_bundle'",
        ).fetchone()
        current_term_kinds = {
            json.loads(row["plan_json"]).get("term_kind")
            for row in json.loads(current["content_json"])["queries"]
        }
        self.assertEqual(current_term_kinds, {"origin", "audit_ko"})


if __name__ == "__main__":
    unittest.main()
