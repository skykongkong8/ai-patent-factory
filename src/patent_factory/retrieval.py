"""The paging half of the shared retrieval primitive.

Both retrieval stages in this project send the same free-text projection to the
same KIPRIS operation, but only one of them has ever followed a cursor
correctly. The research runner never followed one at all before this module
existed: `plan_keyword_queries` built every envelope at ``page=1``, and
`ResearchStore.execute` persisted the ``next_cursor`` the adapter computed
without any research code path ever reading it back. This module holds the
loop so the two stages can converge on one paging contract.

It deliberately takes the store, adapter, and planned query as plain
parameters and imports neither `ResearchStore` nor `PlannedQuery` at runtime —
`research.py` imports this module, so importing those back would be a cycle,
and duck-typing keeps the helper adoptable by the audit loop (which builds its
own envelopes with an ``audit_binding`` and a ``num_of_rows`` projection)
without this module having to know either shape.

Constant page window (PR #49 review finding #1): ``num_of_rows`` is fixed
ONCE, from ``min(30, ceiling)`` — the same default `kipris.py`'s
``_parameters`` derives when a projection omits it (`kipris.py:128`) — and
never changes across pages. Only ``remaining`` (the running countdown against
``ceiling``) shrinks, and it is used solely as the loop's stop condition,
never written into an envelope's ``result_budget``. KIPRIS offsets by
``(pageNo - 1) * numOfRows``; shrinking the requested row count between pages
moves that offset backwards, so pages 2+ would re-serve a window page 1
already returned while a block further out is never requested at all — the
exact defect this fixes. `envelope.result_budget` therefore stays at
``ceiling`` (the caller's whole-query budget) for every page; it is not a
per-page value.

Egress ceilings are two-locus (RC2 of the PR #49/#51 resolution plan):
`effective_pages` is a live, unhashed control — deliberately never written
into any `QueryEnvelope` or `request_body()` — so a credential gate's
`subject_revision_hash` cannot see a paging escalation on its own.
`research.py._verify_and_consume_credential_decision` rejects a mismatched
resume before this function is ever called (locus i); `approved_effective_pages`
/ `approved_result_budget` below are this function's OWN, independent
enforcement of the same ceilings (locus ii), load-bearing because pages 2+ are
minted here, after that one-time consume has already returned.

`byte_budget` is carried unchanged onto every page's envelope: it bounds each
individual HTTP response, not the paged sequence as a whole. A `--paging`
batch that runs N pages can receive up to N times a single page's
`--byte-budget` in total; only `result_budget`/`effective_pages` bound the
sequence.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids the research.py cycle
    from .research import PlannedQuery, ResearchExecution, ResearchStore

# The same default KIPRIS word-search page size `kipris.py:128` falls back to
# when a projection does not set `num_of_rows` explicitly. Named here so the
# constant page window below cannot silently drift from that derivation.
_DEFAULT_PAGE_SIZE = 30


def execute_paginated(
    store: "ResearchStore",
    adapter: Any,
    query: "PlannedQuery",
    *,
    connection: sqlite3.Connection,
    idempotency_key: str,
    retrieved_at: str | None = None,
    effective_pages: int = 1,
    approved_effective_pages: int | None = None,
    approved_result_budget: int | None = None,
) -> tuple["ResearchExecution", ...]:
    """Execute one planned query across its page budget, following ``next_cursor``.

    Stops at the first of: the page budget, an exhausted result budget, an
    adapter failure, or a response that reports no further cursor. Every page is
    a distinct request with its own envelope, query id, and adapter event, so a
    truncated page run is still fully auditable.

    Page 1 reuses the caller's idempotency key *verbatim*. An operation recorded
    before this loop existed therefore still replays from
    `research_operations` instead of re-issuing a live request; only pages 2+
    take a suffix. Reusing the bare key for page 1 is safe because page 1's
    envelope is left untouched, so its `request_fingerprint` — and hence the
    derived `query_id` that `ResearchStore.execute` cross-checks against a
    replayed key — is identical to the pre-paging one.

    `effective_pages` bounds how many pages actually run (the live paging
    control; see module docstring). `approved_effective_pages` and
    `approved_result_budget`, when given, are a credential gate's approved
    ceilings: the loop clamps to whichever of (caller-requested, approved) is
    smaller, so an escalated `effective_pages`/`ceiling` reaching this function
    — however that happened — cannot exceed what was actually approved.
    """

    envelope = query.envelope
    ceiling = envelope.result_budget
    if approved_result_budget is not None:
        ceiling = min(ceiling, approved_result_budget)
    pages_allowed = effective_pages
    if approved_effective_pages is not None:
        pages_allowed = min(pages_allowed, approved_effective_pages)
    # Defense in depth, beyond this function's own clamp above: every
    # envelope's `page_cap` is the frozen fossil (always 5 — see
    # `ResearchBudget.page_cap`/`FROZEN_PAGE_CAP` in research.py), and
    # `KiprisAdapter.search` derives `next_cursor` as `None` once
    # `page >= envelope.page_cap` (`kipris.py`: `page < envelope.page_cap and
    # page*rows < total`). So even an `effective_pages`/`pages_allowed` far
    # above 5 — a caller bug, a bad approval scope, whatever reached here —
    # cannot pull more than 5 live pages out of the real adapter; the `if not
    # row["next_cursor"]: break` below would stop the loop at page 5 first.
    # Fixed once, from the caller's whole-query budget — never from `remaining`.
    page_size = min(_DEFAULT_PAGE_SIZE, ceiling)
    executions: list["ResearchExecution"] = []
    received = 0
    for page in range(1, pages_allowed + 1):
        remaining = ceiling - received
        if remaining <= 0:
            break
        # The adapters reject a cursor that disagrees with the page
        # (kipris.py: "pagination cursor does not match page"), and treat a
        # first page as cursorless — the same convention audit.py:304 uses.
        # Page 1 is passed through completely untouched (not just result_budget-
        # preserved) so its envelope, and therefore its `request_fingerprint`,
        # is exactly what a pre-paging caller would have built.
        paged = query if page == 1 else replace(
            query,
            envelope=replace(
                envelope,
                page=page,
                cursor=str(page),
                # Explicit and constant: the fix for finding #1. Never
                # `result_budget=remaining` — that is what shrank the window.
                query_projection={**envelope.query_projection, "num_of_rows": page_size},
            ),
        )
        execution = store.execute(
            adapter,
            paged,
            idempotency_key=idempotency_key if page == 1 else f"{idempotency_key}:p{page:02d}",
            retrieved_at=retrieved_at,
        )
        executions.append(execution)
        if execution.failure_kind:
            break
        row = connection.execute(
            "SELECT next_cursor,coverage_json FROM adapter_events WHERE event_id=?",
            (execution.event_id,),
        ).fetchone()
        if row is None:
            break
        coverage = json.loads(row["coverage_json"]) if row["coverage_json"] else {}
        # Charge at least one against the ceiling even when a source reports a
        # cursor alongside zero usable records, so a misreporting upstream cannot
        # keep the loop at the same page for the whole page budget.
        counted = int(coverage.get("received", len(execution.evidence_ids)))
        received += max(1, min(remaining, counted))
        if not row["next_cursor"]:
            break
    return tuple(executions)


# Intentional divergences from the audit retrieval loop (`audit.py:290-321`),
# left as documentation rather than unified with it (PR #49 review finding
# #15; the audit copy's own shrinking-window fix is tracked separately as it
# changes audit's own hash surface — see issue #52):
#
#   * `num_of_rows`: audit sets it to the shrinking `remaining` every page
#     (`audit.py:303`) — the same bug finding #1 fixes here. This loop sets it
#     ONCE, to the constant `page_size`, precisely so it does not shrink.
#   * accounting: audit charges `min(remaining, received)` (`audit.py:319`),
#     which can add zero and stall on a misreporting source that reports a
#     cursor with no usable rows; this loop charges `max(1, min(remaining,
#     counted))` so the ceiling always makes forward progress.
#   * missing/absent coverage: this loop treats a missing `adapter_events` row
#     or a NULL `coverage_json` as "stop, nothing more to charge"
#     (`if row is None: break`, `... if row["coverage_json"] else {}`); the
#     audit loop assumes both are always present and would raise on
#     `json.loads(None)` if the assumption ever broke (`audit.py:317-318`).
#   * approval-scope bounds: this loop is reachable through a credential gate
#     with an approved `effective_pages`/`result_budget` ceiling (RC2 above);
#     audit's `page_cap`/`results_per_query` come from `SimilarityConfig`, a
#     different, non-hash-gated surface with no analogous two-locus check.
