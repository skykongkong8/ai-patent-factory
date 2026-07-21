"""The research runner follows `next_cursor`, and only when `--paging` asks it to.

Driven through the REAL `KiprisAdapter` against responses shaped like the
recorded live one (`tests/fixtures/kipris/word-search-live-v1.xml`: `<count>` is
a SIBLING of `<body>`, not nested inside it). A mock adapter would prove nothing
here — the whole point of the paging contract is the cursor/page cross-check the
real adapter performs (`kipris.py`: "pagination values are invalid",
"pagination cursor does not match page"), and issue #38 shipped a live path that
a hand-authored fixture certified as working.

`OffsetHonouringTransport` actually respects the requested `numOfRows` and
`pageNo` the way the live KIPRIS service does — offset = (pageNo - 1) *
numOfRows — unlike the retired `PagingTransport`, which always served a fixed
canned page regardless of what was requested and so could not see review
finding #1 (the shrinking-window duplicate-window bug) or prove finding #9's
gap in the original fixture. Adapted from the session's `repro_finding1_offset.py`.

Design recap (freeze-hash, per `.omc/plans/ralplan-pr49-51-resolution.md`):
`page_cap` (`ResearchBudget`/`QueryEnvelope`) is a hashed, frozen fossil —
always 5, never CLI-controlled. `--paging` is the live, unhashed control,
threaded through as the plain `effective_pages` parameter (5 when passed, 1
by default). The loop bound moved off `envelope.page_cap` entirely (RC1).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.database import connect_database
from patent_factory.models import RunState
from patent_factory.provenance import canonical_json, digest
from patent_factory.research import (
    CredentialRequiredError,
    ResearchBudget,
    ResearchStore,
    plan_keyword_queries,
    run_research_batch,
)
from patent_factory.retrieval import execute_paginated
from patent_factory.state import StateStore


def item_xml(index: int) -> str:
    return (
        "<item>"
        f"<inventionTitle>센서 장치 {index}</inventionTitle>"
        "<ipcNumber>G06F 1/00</ipcNumber>"
        f"<applicationNumber>10-2024-{index:06d}</applicationNumber>"
        "<applicationDate>20240102</applicationDate>"
        "<applicantName>공개 출원인</applicantName>"
        f"<astrtCont>초록 {index}</astrtCont>"
        "<registerStatus>공개</registerStatus>"
        "</item>"
    )


def page_xml(page: int, rows: int, indices: list[int], total: int) -> bytes:
    """One KIPRIS word-search page, in the recorded live shape (`<count>` a
    sibling of `<body>`), covering exactly the given record indices."""

    items = "".join(item_xml(index) for index in indices)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><response>'
        "<header><successYN>Y</successYN><resultCode>00</resultCode>"
        "<resultMsg>NORMAL SERVICE.</resultMsg></header>"
        f"<body><items>{items}</items></body>"
        f"<count><numOfRows>{rows}</numOfRows><pageNo>{page}</pageNo>"
        f"<totalCount>{total}</totalCount></count></response>"
    ).encode("utf-8")


class OffsetHonouringTransport:
    """Serves exactly the window (pageNo, numOfRows) actually requested, the way
    the live KIPRIS service does: offset = (pageNo - 1) * numOfRows. Records
    every request and the index range it served, so a test can assert directly
    that no two requests' served ranges overlap (review finding #1) and that no
    gap is left below `total` when the loop is supposed to cover it.
    """

    def __init__(self, *, total: int) -> None:
        self.total = total
        self.requests: list[tuple[int, int]] = []  # (pageNo, numOfRows)
        self.served_ranges: list[tuple[int | None, int | None]] = []

    def __call__(self, url: str, timeout: float, byte_budget: int) -> TransportResponse:
        del timeout, byte_budget
        query = parse_qs(urlsplit(url).query)
        page = int(query["pageNo"][0])
        rows = int(query["numOfRows"][0])
        self.requests.append((page, rows))
        offset = (page - 1) * rows
        indices = list(range(offset + 1, min(offset + rows, self.total) + 1))
        self.served_ranges.append((indices[0], indices[-1]) if indices else (None, None))
        return TransportResponse(200, {}, page_xml(page, rows, indices, self.total))

    @property
    def pages(self) -> list[int]:
        return [page for page, _ in self.requests]


def ready(connection, run_id="run"):
    store = StateStore(connection)
    store.create_run(run_id)
    for target, operation in (
        ("profile_pending", "ready.profile"),
        ("profile_ready", "ready.profile-finish"),
        ("research_ready", "ready.research"),
    ):
        store.transition(run_id, target, actor="test", reason="setup",
                         operation=operation, idempotency_key="1")
    return store


class ResearchPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.store = ready(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def plan(self, **budget):
        return plan_keyword_queries(
            run_id="run", origin_query="센서", budget=ResearchBudget(**budget),
        )

    def batch(self, adapter, queries, key="batch", **kwargs):
        return run_research_batch(
            self.connection, run_root=self.root, run_id="run", adapter=adapter,
            queries=queries, idempotency_key=key, retrieved_at="2026-01-01T00:00:00Z",
            **kwargs,
        )

    def operation_keys(self):
        return sorted(
            row["idempotency_key"]
            for row in self.connection.execute(
                "SELECT idempotency_key FROM research_operations WHERE run_id='run'",
            )
        )

    def test_default_invocation_issues_exactly_one_request_per_term(self):
        """RC1: the loop bound is `effective_pages`, not `envelope.page_cap`.

        `page_cap` is frozen at 5 (the fossil) regardless of `--paging`, so a
        fingerprint byte-identity check alone cannot catch a loop that
        mistakenly still bounds itself by `envelope.page_cap` — it would issue
        5x the live requests at the very defaults this asserts stay at 1.
        """

        transport = OffsetHonouringTransport(total=1000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)
        queries = self.plan()
        self.assertEqual(queries[0].envelope.page_cap, 5)  # frozen fossil, not 1

        result = self.batch(adapter, queries)  # effective_pages defaults to 1

        self.assertEqual(transport.pages, [1])
        self.assertEqual(result.next_state, RunState.RESEARCH_COMPLETE.value)
        self.assertEqual(self.operation_keys(), ["batch:q00"])
        payload = result.as_dict()
        self.assertEqual(payload["planned_count"], 1)
        self.assertEqual(payload["page_count"], 1)
        self.assertEqual(payload["succeeded_pages"], 1)

    def test_default_effective_pages_caps_at_one_page_even_with_budget_room_to_spare(self):
        """RC1, discriminating: at the literal shipped default
        (`--result-budget 30`), a single full page always exhausts the whole
        ceiling on its own — that alone would make `test_default_invocation_
        issues_exactly_one_request_per_term` pass even with a loop bound left
        on `envelope.page_cap` (frozen at 5), because the `remaining <= 0`
        check stops it regardless. Raising `per_adapter_results` so the
        ceiling has room left after page 1 (`remaining=45>0`) — while a large
        `total` guarantees the source would keep reporting a cursor if asked —
        isolates the loop bound itself: only `effective_pages` (not
        `envelope.page_cap`) may be the reason page 2 is never requested.
        """

        transport = OffsetHonouringTransport(total=1000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)
        queries = self.plan(per_adapter_results=75)
        self.assertEqual(queries[0].envelope.page_cap, 5)  # would allow up to 5 pages

        result = self.batch(adapter, queries)  # effective_pages defaults to 1

        self.assertEqual(transport.requests, [(1, 30)])
        self.assertEqual(result.as_dict()["page_count"], 1)

    def test_paging_on_uses_a_constant_window_with_no_duplicates_or_gaps(self):
        """Review finding #1: pages must not re-fetch a window an earlier page
        already returned, nor skip a block. `per_adapter_results=75` needs
        more than one 30-row page, so `--paging` (`effective_pages=5`) must
        actually buy the operator additional, non-overlapping records.
        """

        transport = OffsetHonouringTransport(total=1000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)

        result = self.batch(adapter, self.plan(per_adapter_results=75), effective_pages=5)

        # Constant numOfRows=30 every page — never shrunk toward `remaining`.
        self.assertEqual(transport.requests, [(1, 30), (2, 30), (3, 30)])
        # Contiguous, non-overlapping windows: page 2 starts exactly where
        # page 1 ended, and page 3 where page 2 ended.
        self.assertEqual(transport.served_ranges, [(1, 30), (31, 60), (61, 90)])
        self.assertEqual(result.next_state, RunState.RESEARCH_COMPLETE.value)
        payload = result.as_dict()
        self.assertEqual(payload["planned_count"], 1)
        self.assertEqual(payload["page_count"], 3)
        # 90 distinct records across 3 non-overlapping pages: none lost to a
        # backwards-moving window, none double-counted.
        self.assertEqual(payload["evidence_count"], 90)
        self.assertEqual(
            self.operation_keys(),
            ["batch:q00", "batch:q00:p02", "batch:q00:p03"],
        )

    def test_pagination_stops_when_the_source_reports_no_further_cursor(self):
        # total=30 at a 30-row page: page 1 already covers the whole result
        # set, so the adapter emits no cursor and the loop must not ask for
        # page 2 even though effective_pages allows up to 5.
        transport = OffsetHonouringTransport(total=30)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)

        result = self.batch(adapter, self.plan(per_adapter_results=75), effective_pages=5)

        self.assertEqual(transport.pages, [1])
        self.assertEqual(result.as_dict()["evidence_count"], 30)

    def test_result_budget_stops_paging_before_the_page_budget(self):
        # ceiling=40 at a constant 30-row page: page 1 leaves remaining=10,
        # page 2 exhausts it, so paging stops at 2 even though effective_pages
        # allows 5 and the source still reports a cursor (total is huge).
        transport = OffsetHonouringTransport(total=100_000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)

        result = self.batch(adapter, self.plan(per_adapter_results=40), effective_pages=5)

        self.assertEqual(transport.requests, [(1, 30), (2, 30)])
        self.assertEqual(transport.served_ranges, [(1, 30), (31, 60)])
        self.assertEqual(result.as_dict()["page_count"], 2)

    def test_each_page_is_a_distinct_persisted_query_with_its_own_adapter_event(self):
        transport = OffsetHonouringTransport(total=1000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)

        self.batch(adapter, self.plan(per_adapter_results=75), effective_pages=5)

        rows = self.connection.execute(
            "SELECT envelope_json FROM research_queries WHERE run_id='run' ORDER BY created_at,query_id",
        ).fetchall()
        self.assertEqual(len(rows), 3)
        events = self.connection.execute(
            "SELECT COUNT(*) AS total FROM adapter_events WHERE run_id='run'",
        ).fetchone()
        self.assertEqual(events["total"], 3)
        # The cursor convention: page 1 carries none, later pages carry their own
        # page number — the exact cross-check the real adapter enforces.
        pages = sorted(
            (item["page"], item["cursor"])
            for item in (json.loads(row["envelope_json"]) for row in rows)
        )
        self.assertEqual(pages, [(1, None), (2, "2"), (3, "3")])
        # And every later page's envelope carries the same, constant, explicit
        # num_of_rows — never shrinking toward the remaining ceiling.
        rows_requested = sorted(
            item["query_projection"]["num_of_rows"]
            for item in (json.loads(row["envelope_json"]) for row in rows)
            if item["page"] > 1
        )
        self.assertEqual(rows_requested, [30, 30])

    def test_counters_stay_in_one_unit_each_with_paging_on(self):
        """Review finding #12: `planned_count` (terms) must not mix units with
        `page_count`/`succeeded_pages` (executions) — with paging on, a single
        planned term spans several pages, so the two units diverge visibly.
        """

        transport = OffsetHonouringTransport(total=1000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)

        result = self.batch(adapter, self.plan(per_adapter_results=75), effective_pages=5)
        payload = result.as_dict()

        self.assertEqual(payload["planned_count"], 1)  # one planned TERM
        self.assertEqual(payload["page_count"], 3)  # three EXECUTIONS
        self.assertEqual(payload["succeeded_pages"], 3)  # also execution-unit
        self.assertGreater(payload["page_count"], payload["planned_count"])

    def test_seeded_pre_upgrade_operation_replays_with_zero_transport_calls(self):
        """Review finding #2 / #10, proven for the actual paging code path:
        an operation recorded before paging existed — same idempotency-key
        shape, same query_id (because `page_cap` stayed frozen at 5 and the
        rest of `request_body()` is unchanged) — must replay from
        `research_operations` rather than issuing a live request, at the
        shipped `--paging`-off default.
        """

        transport = OffsetHonouringTransport(total=1000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)
        queries = self.plan()
        envelope = queries[0].envelope
        query_id = "qu_" + digest({"run_id": envelope.run_id, "fingerprint": envelope.request_fingerprint})[:20]
        event_id = "ae_seed00000000000000"
        seeded_result = {
            "event_id": event_id,
            "evidence_ids": [],
            "failure_kind": None,
            "observation_ids": [],
            "query_id": query_id,
            "run_id": "run",
            "status": "success",
        }
        # A pre-upgrade-shaped row set: research_queries + adapter_events are the
        # foreign-key parents research_operations requires. Their shape (this
        # query's own envelope/fingerprint) is exactly what a run recorded
        # before paging existed would have persisted for page 1 — freeze-hash
        # means that shape is unchanged by this fix.
        self.connection.execute(
            "INSERT INTO research_queries(query_id,run_id,request_fingerprint,envelope_json,plan_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (query_id, "run", envelope.request_fingerprint, canonical_json(envelope.request_body()),
             canonical_json(queries[0].as_dict()), "2025-01-01T00:00:00Z"),
        )
        self.connection.execute(
            "INSERT INTO adapter_events(event_id,run_id,query_id,adapter,adapter_version,retrieved_at,status,"
            "response_hash,failure_kind,failure_json,terms_note,coverage_json,next_cursor) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "run", query_id, "kipris", "plus-xml-v1", "2025-01-01T00:00:00Z", "success",
             "seed-response-hash", None, None, "seed terms", canonical_json({"received": 0}), None),
        )
        self.connection.execute(
            "INSERT INTO research_operations VALUES(?,?,?,?,?,?)",
            ("run", "pre-upgrade-batch:q00", query_id, event_id,
             canonical_json(seeded_result), "2025-01-01T00:00:00Z"),
        )
        self.connection.commit()

        result = self.batch(adapter, queries, key="pre-upgrade-batch")

        self.assertEqual(transport.requests, [])
        self.assertTrue(result.executions[0].replayed)
        self.assertEqual(result.as_dict()["page_count"], 1)


class ApprovalScopeBoundsTests(unittest.TestCase):
    """RC2: `effective_pages` sits outside the hashed surface on purpose, so it
    needs its own bounds enforcement at two independent loci — the consume
    check (i) and the paging loop itself (ii) — rather than relying on
    `subject_revision_hash` equality, which cannot see it at all.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.store = ready(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def plan(self, origin_query="센서", **budget):
        return plan_keyword_queries(
            run_id="run", origin_query=origin_query, budget=ResearchBudget(**budget),
        )

    def batch(self, adapter, queries, key="batch", **kwargs):
        return run_research_batch(
            self.connection, run_root=self.root, run_id="run", adapter=adapter,
            queries=queries, idempotency_key=key, retrieved_at="2026-01-01T00:00:00Z",
            **kwargs,
        )

    def suspend(self, queries, *, effective_pages, key="batch"):
        missing = KiprisAdapter(None, transport=lambda *a: (_ for _ in ()).throw(AssertionError("no network")))
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(missing, queries, key=key, effective_pages=effective_pages)
        return captured.exception.gate

    def approve(self, gate):
        decision, _ = self.store.decide_gate(
            gate.gate_id, action="configure_and_verify", actor="user", reason="approved",
            subject_revision_hash=gate.subject_revision_hash,
            approval_scope=dict(gate.approval_scope),
            suspended_operation=gate.suspended_operation,
            return_state=gate.return_state,
        )
        return decision

    def configured_adapter(self, total=1000):
        transport = OffsetHonouringTransport(total=total)
        return KiprisAdapter("configured-secret", transport=transport, credential_required=True), transport

    def test_approval_scope_publishes_the_real_wire_ceiling(self):
        queries = self.plan(per_adapter_results=75)
        gate = self.suspend(queries, effective_pages=5)
        self.assertEqual(gate.approval_scope["effective_pages"], 5)
        self.assertEqual(gate.approval_scope["result_budget"], 75)
        self.assertEqual(gate.approval_scope["max_requests"], len(queries) * 5)

    def test_resume_at_the_approved_ceiling_succeeds(self):
        queries = self.plan(per_adapter_results=75)
        gate = self.suspend(queries, effective_pages=5)
        decision = self.approve(gate)
        adapter, transport = self.configured_adapter()

        result = self.batch(
            adapter, queries, credential_decision_id=decision.decision_id, effective_pages=5,
        )
        self.assertEqual(result.next_state, RunState.RESEARCH_COMPLETE.value)
        self.assertEqual(transport.requests, [(1, 30), (2, 30), (3, 30)])

    def test_fresh_resume_rejects_an_effective_pages_escalation_the_hash_cannot_see(self):
        """RC2 locus (i), first (non-replayed) consumption of the decision.

        `effective_pages` never enters any envelope, so the page-1 envelopes —
        and therefore `subject_revision_hash` — are IDENTICAL whether the
        operator approved `--paging` off or on. Hash equality alone would let
        this escalation through; the explicit bounds check must not.
        """

        queries = self.plan(per_adapter_results=75)
        gate = self.suspend(queries, effective_pages=1)  # approved WITHOUT paging
        decision = self.approve(gate)
        adapter, transport = self.configured_adapter()

        with self.assertRaisesRegex(RuntimeError, "does not match the current request"):
            self.batch(
                adapter, queries, credential_decision_id=decision.decision_id,
                effective_pages=5,  # resuming WITH paging: an escalation
            )
        self.assertEqual(transport.requests, [])

    def test_replayed_resume_still_rejects_an_effective_pages_escalation(self):
        """RC2 locus (i), the ALREADY-USED (replay) branch: consuming the
        decision once at the approved ceiling, then reusing the same
        decision_id with an escalated `effective_pages`, must be rejected too
        — the bounds check runs before the fresh/replay branch split.
        """

        queries = self.plan(per_adapter_results=75)
        gate = self.suspend(queries, effective_pages=1)
        decision = self.approve(gate)
        adapter, transport = self.configured_adapter()
        first = self.batch(
            adapter, queries, credential_decision_id=decision.decision_id, effective_pages=1,
        )
        self.assertEqual(first.next_state, RunState.RESEARCH_COMPLETE.value)
        transport.requests.clear()

        with self.assertRaisesRegex(RuntimeError, "does not match the current request"):
            self.batch(
                adapter, queries, credential_decision_id=decision.decision_id,
                effective_pages=5,
            )
        self.assertEqual(transport.requests, [])

    def test_resume_rejects_a_result_budget_escalation(self):
        queries = self.plan(per_adapter_results=30)
        gate = self.suspend(queries, effective_pages=1)
        decision = self.approve(gate)
        adapter, transport = self.configured_adapter()
        escalated = self.plan(per_adapter_results=90)  # same term, bigger ceiling

        with self.assertRaisesRegex(RuntimeError, "does not match the current request"):
            self.batch(
                adapter, escalated, credential_decision_id=decision.decision_id,
                effective_pages=1,
            )
        self.assertEqual(transport.requests, [])

    def test_resume_rejects_an_altered_term(self):
        queries = self.plan(origin_query="센서")
        gate = self.suspend(queries, effective_pages=1)
        decision = self.approve(gate)
        adapter, transport = self.configured_adapter()
        altered = self.plan(origin_query="다른 검색어")

        with self.assertRaisesRegex(RuntimeError, "does not match the current request"):
            self.batch(
                adapter, altered, credential_decision_id=decision.decision_id,
                effective_pages=1,
            )
        self.assertEqual(transport.requests, [])

    def test_effective_pages_escalation_is_rejected_from_the_research_running_reentry_state(self):
        """RC2 must be route-independent. PR #51's #12 fix refuses live
        research verbs only on the checkpoint `re_research` re-entry — the
        COVERAGE-expand second pass stays live-capable BY DESIGN (a
        legitimate route back into `RESEARCH_RUNNING`, `state.py:139`:
        `(GateKind.COVERAGE, "expand") -> RESEARCH_RUNNING`). Every other test
        in this class starts from `RESEARCH_READY` (a first pass); this one
        simulates the state that route leaves a run in BEFORE invoking
        `run_research_batch` again, and shows the same escalation is still
        rejected. The bounds checks live in generic per-invocation code (the
        consume check, the paging loop) with no branch on `prior.state`, so
        this is expected to hold structurally — this test pins it so a future
        change that DOES special-case first-pass vs re-entry cannot silently
        loosen the re-entry route.
        """

        self.store.transition(
            "run", RunState.RESEARCH_RUNNING, actor="test",
            reason="simulate a COVERAGE-expand re-entry", operation="test.reentry",
            idempotency_key="reentry-marker",
        )
        queries = self.plan(per_adapter_results=75)
        gate = self.suspend(queries, effective_pages=1)  # approved WITHOUT paging
        decision = self.approve(gate)
        adapter, transport = self.configured_adapter()

        with self.assertRaisesRegex(RuntimeError, "does not match the current request"):
            self.batch(
                adapter, queries, credential_decision_id=decision.decision_id,
                effective_pages=5,  # resuming WITH paging: an escalation
            )
        self.assertEqual(transport.requests, [])

    def test_execute_paginated_clamps_pages_to_the_approved_ceiling_independent_of_the_caller(self):
        """RC2 locus (ii), unit-tested directly against `retrieval.
        execute_paginated`, with no gate involved at all: even if a caller
        asks for more pages than was approved, the loop itself must not
        exceed the approved page ceiling. `approved_result_budget` is left
        generous here (and `per_adapter_results` large) specifically so the
        ceiling clamp cannot be the thing stopping the loop — only the page
        clamp can be. This is what makes locus (ii) load-bearing rather than a
        redundant re-check of locus (i): it is the only enforcement left once
        pages 2+ are being minted, after locus (i) has already returned.
        """

        transport = OffsetHonouringTransport(total=100_000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)
        queries = self.plan(per_adapter_results=300)
        store = ResearchStore(self.connection)

        executions = execute_paginated(
            store, adapter, queries[0], connection=self.connection,
            idempotency_key="clamp-test",
            effective_pages=10,  # the (hypothetically escalated) caller ask
            approved_effective_pages=2,  # what was actually approved
            approved_result_budget=None,  # not the thing under test here
        )

        self.assertEqual(len(executions), 2)
        self.assertEqual(transport.requests, [(1, 30), (2, 30)])

    def test_execute_paginated_clamps_result_budget_to_the_approved_ceiling_independent_of_the_caller(self):
        """RC2 locus (ii), the `result_budget` half: `approved_effective_pages`
        is left generous (well above what the budget alone would ever reach)
        specifically so the page clamp cannot be the thing stopping the loop —
        only the budget clamp can be.
        """

        transport = OffsetHonouringTransport(total=100_000)
        adapter = KiprisAdapter("k", transport=transport, credential_required=False)
        queries = self.plan(per_adapter_results=300)
        store = ResearchStore(self.connection)

        executions = execute_paginated(
            store, adapter, queries[0], connection=self.connection,
            idempotency_key="clamp-test",
            effective_pages=10,
            approved_effective_pages=10,  # not the thing under test here
            approved_result_budget=45,  # what was actually approved
        )

        self.assertEqual(len(executions), 2)
        self.assertEqual(transport.requests, [(1, 30), (2, 30)])


class ResearchBudgetCrossFieldGuardTests(unittest.TestCase):
    """Review finding #3: `--max-calls` alone bounds planned TERMS, but once
    paging is on each term can issue up to `effective_pages` requests, with no
    cross-term ceiling. `--max-calls 100 --page-cap 100` used to pass
    `ResearchBudget.validate()` while threatening ~1,700 live requests from one
    command. `validate(effective_pages=...)` closes that gap directly.
    """

    def test_within_the_hundred_request_ceiling_is_accepted(self):
        ResearchBudget(max_calls=20).validate(effective_pages=5)  # 100: at the edge, accepted

    def test_over_the_hundred_request_ceiling_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_calls \\* effective_pages"):
            ResearchBudget(max_calls=21).validate(effective_pages=5)  # 105

    def test_default_effective_pages_of_one_leaves_existing_callers_unaffected(self):
        # plan_keyword_queries/plan_bibliography_queries call validate() with no
        # effective_pages argument; max_calls alone (already bounded to <=100)
        # must stay sufficient for every caller that never pages.
        ResearchBudget(max_calls=100).validate()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
