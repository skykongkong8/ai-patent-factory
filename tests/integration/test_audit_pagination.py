"""Issue #52: the audit retrieval loop's constant page window and replay safety.

The pre-fix loop shrank ``num_of_rows`` to the per-page ``remaining`` — KIPRIS
offsets by ``(pageNo - 1) * numOfRows``, so pages 2+ re-served rows page 1 had
already returned while a block further out was never requested. The fix routes
audit through the shared ``retrieval.execute_paginated`` loop with a constant
``min(30, results_per_query)`` window on every page INCLUDING page 1, which
changes page-1's request fingerprint — so a completed pre-fix operation must
replay through the operation-level short-circuit with zero transport, never a
live re-fetch and never ``ValueError("idempotency_key reused …")``.
"""
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.audit import _query_input, run_audit_retrieval
from patent_factory.config import load_evaluation_config, load_similarity_config
from patent_factory.corpus import build_retained_corpus
from patent_factory.database import connect_database
from patent_factory.evaluation import run_shortlist
from patent_factory.ideation import run_ideation
from patent_factory.models import QueryEnvelope, RunState
from patent_factory.provenance import digest
from patent_factory.research import PlannedQuery, ResearchStore
from patent_factory.state import StateStore
from tests.integration.test_g004_ideation_and_shortlist import (
    candidate_input, ready_profile, ready_research, shortlist_input,
)
from tests.integration.test_g005_audit import kipris_xml

RETRIEVED_AT = "2026-07-19T00:00:00Z"
TOTAL = 100


def paged_kipris_xml(page: int, rows: int, total: int) -> bytes:
    """A KIPRIS body honouring the requested offset window, live <count> shape."""

    start = (page - 1) * rows + 1
    count = max(0, min(rows, total - (page - 1) * rows))
    items = "".join(
        f"<item><inventionTitle>공통 감사 기술 {index}</inventionTitle>"
        f"<ipcNumber>G06F 1/00|G06N 3/04</ipcNumber>"
        f"<applicationNumber>10-2026-{index:07d}</applicationNumber>"
        f"<applicationDate>20260101</applicationDate><applicantName>공개 출원인</applicantName>"
        f"<astrtCont>동일 메커니즘 공개 초록 {index}</astrtCont></item>"
        for index in range(start, start + count)
    )
    return (
        "<?xml version='1.0' encoding='UTF-8'?><response><header><successYN>Y</successYN>"
        "<resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>"
        f"<body><items>{items}</items></body>"
        f"<count><numOfRows>{rows}</numOfRows><pageNo>{page}</pageNo><totalCount>{total}</totalCount></count>"
        "</response>"
    ).encode()


class AuditPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.profile_connection, self.profile = ready_profile(self.root / "profile.sqlite3")
        evidence, span, _ = ready_research(self.connection, self.root)
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.root,
            run_id="run", profile=self.profile, candidate_input=candidate_input(3, evidence, span),
            config=load_evaluation_config(),
        )
        run_shortlist(
            self.connection, run_root=self.root, run_id="run",
            shortlist_input=shortlist_input(ideation.candidate_ids, evidence, span),
            config=load_evaluation_config(),
        )
        self.config = load_similarity_config()

    def tearDown(self):
        self.connection.close()
        self.profile_connection.close()
        self.temporary.cleanup()

    def finalist_fixture(self):
        row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='finalist_set'"
        ).fetchone()
        finalists = json.loads(row["content_json"])["finalists"]
        return row, finalists, {
            "schema_version": "audit-query-input-v1", "finalist_set_hash": row["content_hash"],
            "groups": [{
                "finalist_id": finalist["finalist_id"],
                "queries": [{"language": "ko", "term": "동일 검색어"}, {"language": "en", "term": "same query"}],
            } for finalist in finalists],
        }

    def offset_factory(self, requests):
        """One adapter per query whose transport honours pageNo/numOfRows."""

        def factory(query, page, finalist):
            del query, page, finalist

            def transport(url, timeout, byte_budget):
                del timeout, byte_budget
                parameters = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                page_number = int(parameters["pageNo"][0])
                rows = int(parameters["numOfRows"][0])
                requests.append((page_number, rows))
                return TransportResponse(
                    200, {"Content-Type": "application/xml"},
                    paged_kipris_xml(page_number, rows, TOTAL),
                )

            return KiprisAdapter("fixture", transport=transport, credential_required=False)

        return factory

    def seed_pre_fix_operation(self, *, complete: bool) -> tuple[str, dict]:
        """Transcribe the pre-#52 loop: shrinking window, per-page keys.

        ``complete=True`` also binds the corpus_set, i.e. a completed recorded
        operation; ``complete=False`` leaves adapter rows with no corpus_set —
        the crashed-mid-way partial operation.
        """

        finalist_row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='finalist_set'"
        ).fetchone()
        finalists = {item["finalist_id"]: item for item in json.loads(finalist_row["content_json"])["finalists"]}
        _row, _finalists, query_input = self.finalist_fixture()
        groups = _query_input(query_input, finalists, finalist_row["content_hash"])
        state = StateStore(self.connection)
        config = self.config
        config_revision = state.add_revision(
            "run", "scorer_config", {
                "config": config.as_dict(), "config_hash": config.content_hash,
                "finalist_set_hash": finalist_row["content_hash"],
                "supersedes": None, "version": "scorer-config-v1",
            }, schema_version="scorer-config-v1",
        )
        query_payload = {
            "config_hash": config.content_hash, "finalist_set_hash": finalist_row["content_hash"],
            "groups": groups, "run_id": "run", "version": "audit-query-set-v1",
        }
        operation_hash = digest(query_payload)
        started = state.transition(
            "run", RunState.AUDIT_RUNNING, actor="audit-cli", reason="finalist-specific KIPRIS audit started",
            operation="audit.retrieve.start", idempotency_key=operation_hash,
            artifact_kind="audit_query_set", artifact_content=query_payload,
            artifact_schema_version="audit-query-set-v1",
            dependencies=(finalist_row["revision_id"], config_revision.revision_id),
        )
        query_revision = started.artifact
        store = ResearchStore(self.connection)
        body = kipris_xml("10-2026-0012345")
        corpora = []
        for group in groups:
            query_ids, logical_queries, failures = [], {}, []
            for query_index, query in enumerate(group["queries"]):
                logical_query_id = "lq_" + digest({
                    "query_group_id": group["query_group_id"], "query_index": query_index,
                    "language": query["language"], "term": query["term"],
                })[:20]
                logical_received = 0
                for page in range(1, config.page_cap + 1):
                    remaining = config.results_per_query - logical_received
                    if remaining <= 0:
                        break
                    envelope = QueryEnvelope(
                        run_id="run", adapter="kipris", adapter_version="plus-xml-v1", capability="word_search",
                        allowed_scheme="https", allowed_host="plus.kipris.or.kr", deadline_seconds=10,
                        page=page, page_cap=config.page_cap, result_budget=remaining,
                        byte_budget=1_000_000, retry_budget=0, retry_ownership="audit_runner",
                        query_projection={
                            "word": query["term"], "year": 0, "patent": True, "utility": True,
                            "num_of_rows": remaining,
                        },
                        cursor=str(page) if page > 1 else None,
                        audit_binding={
                            "purpose": "final_similarity_audit", "finalist_set_hash": finalist_row["content_hash"],
                            "finalist_id": group["finalist_id"], "query_group_id": group["query_group_id"],
                        },
                    )
                    planned = PlannedQuery(envelope, query["term"], query["term"], f"audit_{query['language']}", 0)
                    execution = store.execute(
                        KiprisAdapter(
                            "fixture", credential_required=False,
                            transport=lambda url, timeout, byte_budget: TransportResponse(200, {}, body),
                        ),
                        planned,
                        idempotency_key=f"audit:{operation_hash}:{group['finalist_id']}:{query_index}:{page}",
                        retrieved_at=RETRIEVED_AT,
                    )
                    query_ids.append(execution.query_id)
                    logical_queries[execution.query_id] = logical_query_id
                    event = self.connection.execute(
                        "SELECT next_cursor,coverage_json FROM adapter_events WHERE event_id=?",
                        (execution.event_id,),
                    ).fetchone()
                    coverage = json.loads(event["coverage_json"])
                    logical_received += min(remaining, int(coverage.get("received", len(execution.evidence_ids))))
                    if not event["next_cursor"]:
                        break
            marks = ",".join("?" for _ in query_ids)
            rows = self.connection.execute(
                f"SELECT re.query_id,re.source_rank,er.evidence_id,er.content_hash,er.record_json "
                f"FROM research_edges re JOIN evidence_records er ON er.run_id=re.run_id AND er.evidence_id=re.evidence_id "
                f"WHERE re.run_id=? AND re.query_id IN ({marks}) ORDER BY re.query_id,re.source_rank,er.evidence_id",
                ("run", *query_ids),
            ).fetchall()
            hits = [{
                "query_id": row["query_id"], "source_rank": row["source_rank"], "evidence_id": row["evidence_id"],
                "logical_query_id": logical_queries[row["query_id"]],
                "content_hash": row["content_hash"], "record": json.loads(row["record_json"]),
            } for row in rows]
            corpora.append(build_retained_corpus(
                finalist_id=group["finalist_id"], query_group_id=group["query_group_id"],
                hits=hits, failures=failures, limit=config.corpus_limit,
            ))
        seeded = {"query_revision_id": query_revision.revision_id, "corpora": corpora}
        if complete:
            corpus_payload = {
                "config_hash": config.content_hash, "corpora": corpora,
                "finalist_set_hash": finalist_row["content_hash"], "run_id": "run", "version": "corpus-set-v1",
            }
            corpus_revision = state.add_revision(
                "run", "corpus_set", corpus_payload, schema_version="corpus-set-v1",
                dependencies=(query_revision.revision_id, config_revision.revision_id),
            )
            seeded["corpus_revision_id"] = corpus_revision.revision_id
        return operation_hash, seeded

    def test_constant_window_covers_contiguous_offsets_with_page_one_at_30(self):
        _row, _finalists, query_input = self.finalist_fixture()
        requests: list[tuple[int, int]] = []
        result = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=self.config, adapter_factory=self.offset_factory(requests),
            retrieved_at=RETRIEVED_AT,
        )
        # 100-row ceiling at a constant 30-row window: pages 1-4, never a
        # shrunken numOfRows, for every one of the 6 planned queries (the
        # preflight builds no transport call).
        per_query = [(1, 30), (2, 30), (3, 30), (4, 30)]
        self.assertEqual(requests, per_query * 6)
        envelopes = [
            json.loads(row["envelope_json"]) for row in self.connection.execute(
                "SELECT envelope_json FROM research_queries WHERE run_id='run' "
                "AND envelope_json LIKE '%final_similarity_audit%' ORDER BY created_at,query_id"
            )
        ]
        self.assertEqual(len(envelopes), 24)
        for body in envelopes:
            self.assertEqual(body["query_projection"]["num_of_rows"], 30)
            self.assertEqual(body["result_budget"], self.config.results_per_query)
        pages = sorted({body["page"] for body in envelopes})
        self.assertEqual(pages, [1, 2, 3, 4])
        self.assertEqual(
            {body["cursor"] for body in envelopes},
            {None, "2", "3", "4"},
        )
        # Contiguous coverage 1..100 per finalist: the filled offsets 31..100
        # are exactly the rows the shrinking window used to skip.
        corpus_row = self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='corpus_set'"
        ).fetchone()
        for corpus in json.loads(corpus_row["content_json"])["corpora"]:
            numbers = sorted(
                int(item["record"]["original_identifier"].rsplit("-", 1)[1])
                for item in corpus["records"]
            )
            self.assertEqual(numbers, list(range(1, TOTAL + 1)))
        self.assertEqual(len(result.corpus_hashes), 3)
        self.assertFalse(result.replayed)

    def test_completed_pre_fix_operation_replays_with_zero_transport(self):
        operation_hash, seeded = self.seed_pre_fix_operation(complete=True)
        _row, _finalists, query_input = self.finalist_fixture()
        queries_before = self.connection.execute("SELECT count(*) FROM research_queries").fetchone()[0]

        def forbidden_factory(query, page, finalist):
            raise AssertionError("a completed operation must replay with zero transport")

        replay = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=self.config, adapter_factory=forbidden_factory, retrieved_at=RETRIEVED_AT,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.query_set_revision_id, seeded["query_revision_id"])
        self.assertEqual(replay.corpus_set_revision_id, seeded["corpus_revision_id"])
        self.assertEqual(
            replay.corpus_hashes,
            tuple(item["corpus_hash"] for item in seeded["corpora"]),
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM research_queries").fetchone()[0],
            queries_before,
        )
        # The old per-page key namespace is untouched and no new-namespace key
        # was minted: the short-circuit fired above the store entirely.
        keys = [row[0] for row in self.connection.execute(
            "SELECT idempotency_key FROM research_operations WHERE idempotency_key LIKE 'audit:%'"
        )]
        self.assertTrue(keys)
        self.assertTrue(all(key.startswith(f"audit:{operation_hash}:") for key in keys))

    def test_rebuild_branch_runs_the_loop_after_upstream_invalidation(self):
        _row, _finalists, query_input = self.finalist_fixture()
        requests: list[tuple[int, int]] = []
        first = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=self.config, adapter_factory=self.offset_factory(requests),
            retrieved_at=RETRIEVED_AT,
        )
        store = StateStore(self.connection)
        store.add_revision("run", "scorer_config", {"version": "drift"}, schema_version="drift")
        self.assertNotIn("audit_query_set", store.snapshot("run").current_revisions)
        requests.clear()
        rebuilt = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=self.config, adapter_factory=self.offset_factory(requests),
            retrieved_at=RETRIEVED_AT,
        )
        # A freshly-minted query_revision has no bound corpus_set, so the
        # short-circuit must NOT fire: the loop re-runs (replaying recorded
        # pages from the store — zero live transport is a store replay here,
        # not an operation-level short-circuit; the distinguishing assertion
        # is the fresh corpus_set revision bound to the fresh query_revision).
        self.assertNotEqual(rebuilt.query_set_revision_id, first.query_set_revision_id)
        self.assertNotEqual(rebuilt.corpus_set_revision_id, first.corpus_set_revision_id)
        self.assertEqual(rebuilt.corpus_hashes, first.corpus_hashes)

    def test_misreporting_source_cannot_stall_the_page_budget(self):
        """The forward-progress accounting audit newly adopted with the loop.

        `max(1, min(remaining, counted))` differs from the audit loop's old
        `min(remaining, received)` ONLY when a source reports a next cursor
        alongside zero usable records — the old accounting added zero forever
        and could spin at the same offset for the whole page budget. The
        KiprisAdapter itself can never emit that shape (a positive totalCount
        without items is MALFORMED), so this drives the shared loop directly
        with a duck-typed misreporting adapter and an audit-shaped envelope.
        """

        from patent_factory.models import AdapterResult
        from patent_factory.retrieval import execute_paginated

        class MisreportingAdapter:
            name = "kipris"
            requires_credential = False
            calls = 0

            def search(self, envelope):
                type(self).calls += 1
                return AdapterResult(
                    (), "0" * 64, "terms", {"received": 0, "usable": 0},
                    next_cursor=str(envelope.page + 1),
                )

        finalist_row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='finalist_set'"
        ).fetchone()
        envelope = QueryEnvelope(
            run_id="run", adapter="kipris", adapter_version="plus-xml-v1", capability="word_search",
            allowed_scheme="https", allowed_host="plus.kipris.or.kr", deadline_seconds=10,
            page=1, page_cap=self.config.page_cap, result_budget=self.config.results_per_query,
            byte_budget=1_000_000, retry_budget=0, retry_ownership="audit_runner",
            query_projection={"word": "동일 검색어", "year": 0, "patent": True, "utility": True, "num_of_rows": 30},
            audit_binding={
                "purpose": "final_similarity_audit", "finalist_set_hash": finalist_row["content_hash"],
                "finalist_id": "fin_misreport", "query_group_id": "aq_misreport",
            },
        )
        planned = PlannedQuery(envelope, "동일 검색어", "동일 검색어", "audit_ko", 0)
        executions = execute_paginated(
            ResearchStore(self.connection), MisreportingAdapter(), planned,
            connection=self.connection, idempotency_key="audit:misreport:fin:0",
            retrieved_at=RETRIEVED_AT, effective_pages=self.config.page_cap,
        )
        # One charged unit per page: the loop terminates at the page budget
        # instead of spinning, and never exceeds it.
        self.assertEqual(len(executions), self.config.page_cap)
        self.assertEqual(MisreportingAdapter.calls, self.config.page_cap)

    def test_partial_pre_fix_operation_refetches_live_under_fresh_keys(self):
        """The documented (b) branch of the partial-operation edge.

        A pre-fix operation that crashed before its corpus_set exists only as
        adapter rows under the old per-page key namespace. The adopted loop's
        fresh key namespace misses those rows, so the re-run silently
        re-fetches live (correct data, spends transport) and completes —
        loudly documented here rather than left to surprise.
        """

        operation_hash, _seeded = self.seed_pre_fix_operation(complete=False)
        _row, _finalists, query_input = self.finalist_fixture()
        requests: list[tuple[int, int]] = []
        result = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=self.config, adapter_factory=self.offset_factory(requests),
            retrieved_at=RETRIEVED_AT,
        )
        self.assertTrue(requests)
        self.assertEqual(len(result.corpus_hashes), 3)
        keys = [row[0] for row in self.connection.execute(
            "SELECT idempotency_key FROM research_operations WHERE idempotency_key LIKE ?",
            (f"audit:{operation_hash}:%",),
        )]
        # Old namespace: audit:{op}:{finalist}:{qi}:{page} (5 segments, digit
        # tail); new namespace: audit:{op}:{finalist}:{qi} for page 1 (4
        # segments) plus :pNN suffixes. The partial op's recorded rows survive
        # alongside the re-run's fresh namespace.
        self.assertTrue(any(len(key.split(":")) == 5 and key.rsplit(":", 1)[1].isdigit() for key in keys))
        self.assertTrue(any(len(key.split(":")) == 4 for key in keys))


if __name__ == "__main__":
    unittest.main()
