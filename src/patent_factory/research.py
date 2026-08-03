from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .adapters.base import SearchAdapter
from .database import FaultInjector, immediate_transaction, inject_fault, utc_now
from .models import AdapterResult, GateEnvelope, GateKind, QueryEnvelope, RunState
from .provenance import canonical_json, digest, evidence_revision_id, normalize
from .privacy import assert_canaries_absent, credential_canaries
from .retrieval import execute_paginated
from .state import ALLOWED_TRANSITIONS, GATE_STATES, StateStore, workspace_export_directories

# The frozen page_cap fossil's value (ResearchBudget.page_cap, QueryEnvelope.
# page_cap below). Named here — rather than left as a bare `5` wherever an
# `effective_pages` derivation needs "the paging-on page count" — so cli.py's
# `--paging` wiring cannot silently drift from the value the fossil is frozen
# at (PR #49 review, Architect minor finding).
FROZEN_PAGE_CAP = 5

# The cross-field live-request ceiling (PR #49 review finding #3): shared by
# `ResearchBudget.validate()`'s `max_calls * effective_pages` check and
# `run_research_batch`'s own self-enforcement of the same ceiling at the
# egress boundary, so the two cannot drift apart either.
MAX_BATCH_REQUESTS = 100


@dataclass(frozen=True)
class ResearchBudget:
    max_depth: int = 1
    max_calls: int = 12
    per_adapter_results: int = 30
    retry_budget: int = 0
    # Frozen replay-compat fossil (models.py: QueryEnvelope.page_cap). This
    # field is hashed into every envelope's `request_body()`, so it must stay
    # byte-identical to what shipped before paging existed — pre-upgrade runs
    # replayed and suspended credential gates resumed at page_cap=5, and
    # changing this default re-mints both (PR #49 review finding #2). It is no
    # longer a live control: how many pages actually run is `effective_pages`,
    # a plain parameter threaded through `run_research_batch` /
    # `retrieval.execute_paginated`, derived from the unhashed CLI `--paging`
    # flag (`effective_pages = FROZEN_PAGE_CAP if paging else 1`) and never
    # written back into this field or any envelope.
    page_cap: int = FROZEN_PAGE_CAP
    byte_budget: int = 1_000_000

    def validate(self, *, effective_pages: int = 1) -> None:
        if not 0 <= self.max_depth <= 3:
            raise ValueError("research_budget.max_depth: must be between 0 and 3")
        if not 1 <= self.max_calls <= 100:
            raise ValueError("research_budget.max_calls: must be between 1 and 100")
        if not 1 <= self.per_adapter_results <= 500:
            raise ValueError("research_budget.per_adapter_results: must be between 1 and 500")
        if not 0 <= self.retry_budget <= 3:
            raise ValueError("research_budget.retry_budget: must be between 0 and 3")
        if not 1 <= self.page_cap <= 100 or not 1 <= self.byte_budget <= 10_000_000:
            raise ValueError("research_budget: page or byte budget is invalid")
        # Cross-field ceiling on ACTUAL live requests (PR #49 review finding #3):
        # `max_calls` alone bounds planned *terms*, but each term can now issue
        # up to `effective_pages` requests. `effective_pages` is not a field on
        # this budget (it is derived from `--paging`, outside the hashed
        # surface), so callers that know it — `_research_kipris` is the only
        # one today — must pass it explicitly; the default of 1 makes this a
        # no-op for every caller that does not page. This is a *planning-time*
        # convenience check; `run_research_batch` enforces the same ceiling
        # again at the egress boundary itself (see MAX_BATCH_REQUESTS there),
        # since not every caller constructs its queries through a
        # `ResearchBudget` in the first place.
        if self.max_calls * effective_pages > MAX_BATCH_REQUESTS:
            raise ValueError(
                f"research_budget: max_calls * effective_pages must not exceed {MAX_BATCH_REQUESTS} "
                f"(max_calls={self.max_calls}, effective_pages={effective_pages})"
            )


@dataclass(frozen=True)
class PlannedQuery:
    envelope: QueryEnvelope
    origin_query: str
    term: str
    term_kind: str
    depth: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "origin_query": normalize(self.origin_query),
            "term": normalize(self.term),
            "term_kind": self.term_kind,
        }


@dataclass(frozen=True)
class ResearchExecution:
    run_id: str
    query_id: str
    event_id: str
    observation_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    status: str
    failure_kind: str | None
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "evidence_ids": list(self.evidence_ids),
            "failure_kind": self.failure_kind, "observation_ids": list(self.observation_ids),
            "query_id": self.query_id, "run_id": self.run_id, "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, replayed: bool = False) -> "ResearchExecution":
        return cls(
            run_id=value["run_id"], query_id=value["query_id"], event_id=value["event_id"],
            observation_ids=tuple(value["observation_ids"]), evidence_ids=tuple(value["evidence_ids"]),
            status=value["status"], failure_kind=value.get("failure_kind"), replayed=replayed,
        )


@dataclass(frozen=True)
class ResearchRun:
    run_id: str
    prior_state: str
    next_state: str
    execution: ResearchExecution
    bundle: Mapping[str, Any]
    artifact_revision_id: str
    transition_event_ids: tuple[str, ...]
    replayed: bool

    def _incomplete_reason(self) -> str | None:
        """Say WHY a run is incomplete, so `incomplete` is not a dead end.

        A run whose adapter succeeded but contributed no NEW evidence is
        `incomplete` purely because every record deduplicated against evidence
        already in the run. Without this, re-importing after an
        excessive-similarity `replace` reroute returns `incomplete`/exit 4 with a
        non-zero `evidence_count` and no indication that the fix is "supply a
        reference that is not already here".
        """

        if self.next_state == RunState.RESEARCH_COMPLETE.value:
            return None
        if self.execution.status == "success" and not self.execution.evidence_ids:
            return (
                "no_new_evidence: the adapter succeeded but every record already exists in "
                "this run, so nothing was added. Supply at least one reference not already "
                "retrieved, or proceed with the evidence you have."
            )
        return f"adapter_status_{self.execution.status}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_status": {
                "failure_kind": self.execution.failure_kind,
                "status": self.execution.status,
            },
            "artifact_ids": [self.artifact_revision_id],
            "command": "research",
            "evidence_count": len(self.execution.evidence_ids),
            "incomplete_reason": self._incomplete_reason(),
            "manifest": self.bundle["manifest"],
            "next_state": self.next_state,
            "prior_state": self.prior_state,
            "query_id": self.execution.query_id,
            "replayed": self.replayed,
            "run_id": self.run_id,
            "status": "complete" if self.next_state == RunState.RESEARCH_COMPLETE.value else "incomplete",
            "transition_event_ids": list(self.transition_event_ids),
        }


class CredentialRequiredError(RuntimeError):
    """A credential-bound research request was transactionally suspended."""

    def __init__(self, gate: GateEnvelope) -> None:
        super().__init__("credential_required: configure and approve the exact research request")
        self.gate = gate


class LiveResearchReentryRefusedError(RuntimeError):
    """A live second research pass was refused: the re_research binding is stale.

    Repurposed from the pre-#48 blanket "offline-only" refusal: the live second
    pass is now conditionally allowed (force-gated), and this code names the
    one remaining hard refusal — an intervening upstream revision invalidated
    the resolution the re-entry is anchored to.
    """

    code = "live_research_reentry_refused_issue_48"

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"run {run_id}: the latest re_research resolution no longer binds current "
            "state (an intervening upstream revision invalidated it) — resolve the "
            "current gate before retrying a live second research pass"
        )
        self.run_id = run_id


class LiveResearchReentryPlanUnboundError(RuntimeError):
    """A live second research pass has no bounded re_research plan to execute."""

    code = "live_research_reentry_plan_unbound_issue_48"

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"run {run_id}: the re_research resolution carries no bounded plan, so a "
            "live second research pass has nothing approved to execute — record a "
            "bounded plan in the checkpoint resolution"
        )
        self.run_id = run_id


class LiveResearchReentryPlanMismatchError(RuntimeError):
    """The re_research resolution's plan does not reproduce its recorded plan_hash."""

    code = "live_research_reentry_plan_mismatch_issue_48"

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"run {run_id}: the re_research resolution's plan does not match its "
            "recorded plan_hash — the binding cannot scope a live second research pass"
        )
        self.run_id = run_id


class LiveResearchReentrySpentCoordinateError(RuntimeError):
    """This attempt is reusing an attempt coordinate a prior attempt already spent.

    One code for two symptoms of the same fact, because they partition the
    retry space rather than overlap it:

    * a prior attempt already PUBLISHED the finish coordinate this attempt
      would land on, so proceeding would silently replay that attempt's bundle
      instead of publishing this one's evidence (the credential spent, the
      decision consumed-but-unused, the run never advancing); or
    * the run's `research.start` record at this coordinate was already
      recorded, so the state-check transition replayed and left the run
      un-advanced in a state a CREDENTIAL gate cannot legally suspend from —
      the guard would choose a branch it cannot complete.

    Both refuse BEFORE any egress. The recovery is the same in both cases and
    is the one the CLI already names: retry under a fresh attempt key.
    """

    code = "live_research_reentry_spent_coordinate_issue_48"

    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(
            f"run {run_id}: {detail} — rerun WITHOUT --idempotency-key so an "
            "authorized retry advances to a fresh attempt key of its own, or pass "
            "--idempotency-key with a value this run has not already used"
        )
        self.run_id = run_id


def _suspendable_state(state: StateStore, run_id: str) -> RunState:
    """Read the state a credential gate would suspend FROM, and guarantee it can.

    Decision 3. Two defects sit here, and one check closes both.

    `suspend_gate` requires `return_state == prior.state` at suspend time, but
    every caller used to pass either a snapshot taken BEFORE the state-check
    transition (stale by construction once that transition fires) or a
    hardcoded `RESEARCH_RUNNING` (correct only when it fired). Reading the
    state here, at suspend time, is the only formulation that is right on both
    paths.

    `suspend_gate` also requires the gate transition itself to be legal, and a
    CREDENTIAL gate can only suspend from `research_ready`, `research_running`
    and `research_incomplete`. Normally the state-check transition moves the
    run to `RESEARCH_RUNNING` first — but when that transition REPLAYS
    (`state.transition` early-returns on an existing idempotency record before
    it validates anything) the run stays where it was, and four entry states
    then cannot carry the gate at all. Refusing here, before any egress, turns
    that into a defined code instead of a `StateError` raised after the
    credential has already been spent.

    NOT "the run must have reached `research_running`": an ordinary first pass
    suspends from `research_ready`, which the state-check transition skips by
    design, and that rule would refuse every one of them.
    """

    current = state.snapshot(run_id).state
    if GATE_STATES[GateKind.CREDENTIAL] not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise LiveResearchReentrySpentCoordinateError(
            run_id,
            f"a credential gate cannot suspend this run from {current.value}, because the "
            "research.start record at this attempt coordinate was already recorded and left "
            "the run un-advanced",
        )
    return current


def _is_own_decisions_replay(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    credential_decision_id: str | None,
    published_event_id: str | None,
) -> bool:
    """Is this attempt the SUPPORTED idempotent replay of a completed attempt?

    Re-running a credentialed attempt with the decision that produced it is a
    designed path: `_verify_and_consume_credential_decision` takes its
    `used_at` branch, the store replays every page with zero transport, and the
    finish replays the same bundle. A published finish record at the
    coordinate is therefore exactly what a legitimate replay looks like, so
    neither the spent-coordinate refusal nor the suspendability check may fire
    on it — the record's own decision is the discriminator.
    """

    if published_event_id is None or not credential_decision_id:
        return False
    decision = connection.execute(
        "SELECT consumed_by_event_id FROM gate_decisions WHERE decision_id=? AND run_id=?",
        (credential_decision_id, run_id),
    ).fetchone()
    return decision is not None and decision["consumed_by_event_id"] == published_event_id


def _refuse_spent_attempt_coordinate(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    idempotency_key: str,
    credential_decision_id: str | None,
    force_gate: bool,
) -> None:
    """Refuse an attempt whose finish coordinate a prior attempt already published.

    Decision 4b. Without this, such an attempt does not fail — it SILENTLY
    REPLAYS that finish (`publish_transition`'s `_published_replay` early-
    returns before the consumed-decision validation). Its freshly fetched
    evidence is discarded, the credential is spent for nothing, the decision is
    left consumed-but-unused and the run never advances. Within one second pass
    that replay is silent rather than loud, and the retry-within-a-second-pass
    path is exactly the traffic the anchor-keyed guard newly creates, so it is
    refused here rather than assumed to fail noisily somewhere downstream.

    Placed above the credential-decision consume — the one point both legs of
    the guard pass through — so a refused attempt leaves its decision reusable
    instead of consumed-but-unused, which is the very accounting fault this
    refusal exists to prevent. And above the state-check transition too, so a
    refused attempt is not merely egress-free but SIDE-EFFECT-free: it leaves
    the run in the state it found it, with no `research.start` record and no
    request revision written on its way to being turned away.

    The scoping is what keeps it from becoming a blanket refusal: it fires on
    the force-gate branch (where by construction no decision can claim the
    record) and on a supplied decision that did NOT produce the record, and
    never on the record's own decision.
    """

    published = _published_finish_at(connection, run_id, idempotency_key)
    if published is None or not (force_gate or credential_decision_id):
        return
    if _is_own_decisions_replay(
        connection,
        run_id=run_id,
        credential_decision_id=credential_decision_id,
        published_event_id=published["event_id"],
    ):
        return
    raise LiveResearchReentrySpentCoordinateError(
        run_id,
        f"a prior attempt already published the finish coordinate {idempotency_key!r}, "
        "so this attempt would replay that bundle instead of publishing its own evidence",
    )


@dataclass(frozen=True)
class ReResearchAnchor:
    """The ONE state-derived coordinate of a `re_research` re-entry (issue #48).

    Derived from persisted state only — never from a caller-supplied decision
    id (`--decision-id` is the CREDENTIAL decision and is None on the
    credential-in-env live path). Present iff the run's research_running state
    was entered via the latest `re_research` checkpoint resolution with no
    `research_complete` published since.
    """

    gate_id: str
    decision_id: str
    stale: bool
    plan: Mapping[str, Any] | None
    plan_hash: str | None


def re_research_reentry_anchor(connection: sqlite3.Connection, run_id: str) -> ReResearchAnchor | None:
    """Derive the re-entry anchor, or None when this is not a re-entry.

    Discriminator (RC5), anchored to concrete persisted events rather than a
    fragile clock comparison alone: a re-entry exists iff the latest
    `gate_decisions` row with `action='re_research'` has no
    `transition_events` row with `next_state='research_complete'` LATER than
    it (both sides of a resolution are written from the same `now` inside
    `publish_gate_resolution`'s one transaction, so the anchor and its own
    transition event always agree). A run with no re_research history and the
    cycle-back route (re_research -> publish -> a later COVERAGE-expand
    re-entry) both return None — those paths behave exactly as before.

    The bound plan lives in the gate-resolution ARTIFACT, not on
    `gate_decisions` (there is no plan column and no decision_id->content FK):
    `idempotency_records.operation = "gate.resolve:{gate_id}"` joined to
    `artifact_revisions` — the `_durable_resolution_replay` pattern.
    """

    row = connection.execute(
        "SELECT gate_id, decision_id, stale, created_at FROM gate_decisions "
        "WHERE run_id=? AND action='re_research' ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    published_since = connection.execute(
        "SELECT 1 FROM transition_events WHERE run_id=? AND next_state=? AND created_at>? LIMIT 1",
        (run_id, RunState.RESEARCH_COMPLETE.value, row["created_at"]),
    ).fetchone()
    if published_since is not None:
        return None
    resolution = connection.execute(
        "SELECT ar.content_json FROM idempotency_records ir "
        "JOIN artifact_revisions ar ON ar.revision_id=ir.artifact_revision_id "
        "WHERE ir.run_id=? AND ir.operation=? "
        "ORDER BY ir.created_at DESC LIMIT 1",
        (run_id, f"gate.resolve:{row['gate_id']}"),
    ).fetchone()
    plan: Mapping[str, Any] | None = None
    plan_hash: str | None = None
    if resolution is not None:
        content = json.loads(resolution["content_json"])
        if isinstance(content.get("plan"), Mapping):
            plan = content["plan"]
        if isinstance(content.get("plan_hash"), str):
            plan_hash = content["plan_hash"]
    return ReResearchAnchor(row["gate_id"], row["decision_id"], bool(row["stale"]), plan, plan_hash)


def validated_reentry_anchor(connection: sqlite3.Connection, run_id: str) -> ReResearchAnchor | None:
    """The issue-48 guard, as a four-way branch (relaxed, never removed).

    - Not a re-entry (no re_research history, or cycle-back) -> None; the
      caller proceeds exactly as before.
    - Stale binding (the resolution's decision was invalidated by an
      intervening upstream revision) -> LiveResearchReentryRefusedError.
    - Plan absent or unbounded -> LiveResearchReentryPlanUnboundError; plan
      not reproducing its recorded plan_hash ->
      LiveResearchReentryPlanMismatchError. Structural binding only: the
      NL-direction <-> search-term correspondence is deliberately operator-
      owned, surfaced as literal terms in the force-gate's approval scope.
    - Otherwise -> the anchor. The caller force-gates when no fresh
      credential decision is bound, and allows when one is.
    """

    anchor = re_research_reentry_anchor(connection, run_id)
    if anchor is None:
        return None
    if anchor.stale:
        raise LiveResearchReentryRefusedError(run_id)
    # Local import: decisions.py owns plan-boundedness (its resolve-time twin
    # check) and never imports this module at module level, but the lazy form
    # keeps this edge cycle-proof either way.
    from .decisions import _is_bounded_plan

    if anchor.plan is None or not _is_bounded_plan(anchor.plan):
        raise LiveResearchReentryPlanUnboundError(run_id)
    if anchor.plan_hash != digest(anchor.plan):
        raise LiveResearchReentryPlanMismatchError(run_id)
    return anchor


def salted_reentry_key(idempotency_key: str, anchor: ReResearchAnchor) -> str:
    """Layer 2 of the issue-48 fix: the anchor-keyed base-key salt.

    The force-gate's `:credential:{decision_id}` suffix differentiates only
    the STORE key; `credential_operation` and the finish transition use the
    bare key, and `state.publish_transition`'s `_published_replay` early-
    returns on `(run_id, operation, bare key)` BEFORE the consumed-decision
    validation — so an unsalted same-term second pass would silently replay
    the first pass's published bundle (evidence fetched, credential spent,
    never published). Salting the bare key BEFORE `credential_operation` is
    derived shifts credential_operation, the finish operation/key, and the
    store base key together; the suspend<->resume contract still holds
    because both re-derive the same salted key from the same stable anchor.
    A first pass has no anchor and is never salted — byte-identical keys.

    Containment (not equality) makes the salt idempotent across the layers
    that apply it — the serpapi CLI salts its base key before the key/replay
    machinery, then hands derived keys (including `-rN` retry suffixes) to
    `run_research`, which salts again on the same anchor.
    """

    salt = f":re_research:{anchor.decision_id}"
    if salt in idempotency_key:
        return idempotency_key
    return f"{idempotency_key}{salt}"


def _apply_reentry_guard(
    connection: sqlite3.Connection, run_id: str, idempotency_key: str,
) -> tuple["ReResearchAnchor | None", str]:
    """The issue-48 guard and its salt, as ONE implementation (Decision 2).

    The predicate that decides "this run is inside an authorized second pass"
    existed in three hand-copies and its salt application in four, and the
    `RESEARCH_INCOMPLETE` bypass was in every one of them. Both live here now,
    so a caller cannot hold one without the other and the copies cannot desync.

    Callers still own their CALL SITE — the CLI must salt before its own
    key/replay machinery, the runners before `credential_operation` is derived
    — but not the decision.

    THE GUARD IS THE ANCHOR, NOT A RUN STATE (Decision 1B). Issue #48 asked
    `prior.state is RESEARCH_RUNNING` first, which is a PROXY for the fact the
    anchor already states outright, and the two disagree the moment a second
    pass ends `RESEARCH_INCOMPLETE`: the run leaves `research_running` while
    the anchor stays live, and the retry then egresses unsalted and ungated on
    terms no operator approved.

    Enumerating the states instead would still be incomplete today, not merely
    fragile. On a fresh key the three gate states are stopped by
    `_validate_direct_transition` — an accident of the transition layer, not a
    property of this guard — but a replayed `research.start` early-returns
    before that check ever runs, and on that path all six non-running states
    reach here and egress every term. The anchor's own contract ("latest
    re_research resolution, with no `research_complete` since") is the only
    formulation that is total over entry states, including ones not yet
    invented.

    The guard stays inside `requires_credential` at every call site: an offline
    second pass is unsalted and ungated by design.
    """

    anchor = validated_reentry_anchor(connection, run_id)
    if anchor is None:
        return None, idempotency_key
    # Before `credential_operation` is derived: the salt must shift the
    # finish/consume coordinate together with the store key.
    return anchor, salted_reentry_key(idempotency_key, anchor)


def _published_finish_at(connection: sqlite3.Connection, run_id: str, key: str) -> sqlite3.Row | None:
    """The decision-bound finish record at one attempt coordinate, if any."""

    return connection.execute(
        "SELECT event_id FROM idempotency_records WHERE run_id=? AND operation=? AND idempotency_key=?",
        (run_id, f"research.execute:{key}", key),
    ).fetchone()


def attempt_coordinate(
    connection: sqlite3.Connection,
    run_id: str,
    idempotency_key: str,
    *,
    credential_decision_id: str | None,
    advance: bool,
) -> str:
    """The coordinate this attempt lands on — the retry convention, in one place.

    Decision 4c, and it is TWO halves that only work together.

    * A FRESH attempt advances past any coordinate this run already published a
      decision-bound finish at, using the same `-rN` shape the serpapi path has
      always used. Without it, an honest same-term retry after an incomplete
      attempt lands on the spent coordinate and has to be re-keyed by hand.
    * A RESUME takes the exact key its decision is bound to. This is the half
      that cannot be left behind: the kipris key is recomputed from the request
      fingerprint on every invocation, so it reproduces the UNADVANCED key, and
      a resume that used it would no longer match the decision's
      `suspended_operation`. Hoisting only the advance would break every
      approved retry.

    The advance deliberately keys on `research.execute:` coordinates only. An
    attempt with no decision finishes under `research.finish`, and a re-run of
    one of those is a designed zero-transport replay — advancing past it would
    turn a replay into a fresh egress.

    `advance` gates ONLY the advance, and callers scope it to exactly the case
    the spent-coordinate refusal covers. Two rules follow, and both matter:

    * It must not fire wider than that refusal. A run with no re_research
      anchor at all reaches this function too, and moving its coordinate would
      change an identifier on a path that succeeds today — the one thing this
      whole change promised not to do.
    * It must not gate the RESUME half. An operator who pins the coordinate
      they were advanced to still has to be able to resume the decision bound
      to it; switching both halves off together would break exactly the retry
      the advance just created.
    """

    if credential_decision_id:
        row = connection.execute(
            "SELECT suspended_operation FROM gate_decisions WHERE decision_id=? AND run_id=?",
            (credential_decision_id, run_id),
        ).fetchone()
        bound = (row["suspended_operation"] or "") if row is not None else ""
        bound = bound[len("research.execute:"):] if bound.startswith("research.execute:") else ""
        # Undo THIS convention's own advance and nothing else: the suffix must
        # be exactly the `-r<digits>` shape minted below, so any other
        # disagreement between the supplied key and the decision stays a
        # genuine mismatch for `_verify_and_consume_credential_decision` to
        # reject rather than something silently adopted here.
        advanced = bound[len(idempotency_key) + 2:] if bound.startswith(f"{idempotency_key}-r") else ""
        if bound and (bound == idempotency_key or advanced.isdigit()):
            return bound
        return idempotency_key
    if not advance:
        return idempotency_key
    candidate, attempt = idempotency_key, 1
    while _published_finish_at(connection, run_id, candidate) is not None:
        attempt += 1
        candidate = f"{idempotency_key}-r{attempt}"
    return candidate


def needs_reentry_force_gate(
    reentry_anchor: "ReResearchAnchor | None", credential_decision_id: str | None,
) -> bool:
    """Option X (issue #48): does this attempt need an explicit approval first?

    A `re_research` second pass with no fresh credential decision force-raises
    a gate EVEN WHEN the credential is in env, so no second pass ever egresses
    on a silently-reused first-pass approval and the operator sees the plan
    binding and the literal terms before anything leaves the machine.

    The serpapi CLI consults this before its free quota preflight and the
    runners before their suspend, so the predicate itself is never duplicated.
    That is a claim about the predicate, not about its inputs: a caller that
    knows more about whether a supplied decision can authorize THIS attempt is
    free to pass a stricter input, and the CLI preflight does exactly that,
    because it runs before the runner is ever entered and so cannot wait for
    the runner's own rejection.
    """

    return reentry_anchor is not None and not normalize(credential_decision_id or "")


@dataclass(frozen=True)
class ResearchBatchRun:
    run_id: str
    prior_state: str
    next_state: str
    executions: tuple[ResearchExecution, ...]
    bundle: Mapping[str, Any]
    artifact_revision_id: str
    transition_event_ids: tuple[str, ...]
    replayed: bool
    # The number of planned *terms*, which stopped being len(executions) once one
    # term could span several pages. Defaulted so the positional construction
    # below stays valid; the caller always passes the real count.
    planned_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        succeeded = sum(
            1 for item in self.executions if item.status == "success" and item.evidence_ids
        )
        failure_kinds = sorted({
            item.failure_kind for item in self.executions if item.failure_kind
        })
        return {
            "adapter_status": {
                "failure_kinds": failure_kinds,
                "status": "success" if succeeded else "failure",
            },
            "artifact_ids": [self.artifact_revision_id],
            "command": "research",
            "evidence_count": len({
                evidence_id for item in self.executions for evidence_id in item.evidence_ids
            }),
            "manifest": self.bundle["manifest"],
            "next_state": self.next_state,
            "page_count": len(self.executions),
            "planned_count": self.planned_count or len(self.executions),
            "prior_state": self.prior_state,
            "queries": [
                {
                    "evidence_count": len(item.evidence_ids),
                    "failure_kind": item.failure_kind,
                    "query_id": item.query_id,
                    "status": item.status,
                }
                for item in self.executions
            ],
            "replayed": self.replayed,
            "run_id": self.run_id,
            "status": "complete" if self.next_state == RunState.RESEARCH_COMPLETE.value else "incomplete",
            # Named _pages, not _count: this is an execution-unit count (one
            # per page, like page_count), not a term-unit count (planned_count).
            # Review finding #12: the two units silently mixed under one name.
            "succeeded_pages": succeeded,
            "transition_event_ids": list(self.transition_event_ids),
        }


def _terms(values: Iterable[str]) -> tuple[str, ...]:
    normalized = {normalize(value) for value in values if normalize(value)}
    return tuple(sorted(normalized, key=lambda value: (value.casefold(), value)))


def plan_keyword_queries(
    *,
    run_id: str,
    origin_query: str,
    korean_synonyms: Sequence[str] = (),
    english_synonyms: Sequence[str] = (),
    discovered_terms: Sequence[str] = (),
    classifications: Sequence[str] = (),
    applicants: Sequence[str] = (),
    inventors: Sequence[str] = (),
    budget: ResearchBudget = ResearchBudget(),
    adapter: str = "kipris",
    adapter_version: str = "plus-xml-v1",
    allowed_host: str = "plus.kipris.or.kr",
) -> tuple[PlannedQuery, ...]:
    budget.validate()
    origin = normalize(origin_query)
    if not origin:
        raise ValueError("origin_query: required")
    groups = (
        ("origin", 0, (origin,)),
        ("synonym_ko", 1, _terms(korean_synonyms)),
        ("synonym_en", 1, _terms(english_synonyms)),
        ("discovered", 2, _terms(discovered_terms)),
        ("classification", 1, _terms(classifications)),
        ("applicant", 1, _terms(applicants)),
        ("inventor", 1, _terms(inventors)),
    )
    planned: list[PlannedQuery] = []
    seen: set[str] = set()
    for kind, depth, values in groups:
        if depth > budget.max_depth:
            continue
        for term in values:
            identity = term.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            envelope = QueryEnvelope(
                run_id=run_id, adapter=adapter, adapter_version=adapter_version,
                capability="word_search", allowed_scheme="https", allowed_host=allowed_host,
                deadline_seconds=10, page=1, page_cap=budget.page_cap,
                result_budget=budget.per_adapter_results, byte_budget=budget.byte_budget,
                retry_budget=budget.retry_budget, retry_ownership="research_runner",
                query_projection={"word": term, "year": 0, "patent": True, "utility": True},
            )
            planned.append(PlannedQuery(envelope, origin, term, kind, depth))
            if len(planned) >= budget.max_calls:
                return tuple(planned)
    return tuple(planned)


def plan_bibliography_queries(
    *,
    run_id: str,
    application_numbers: Sequence[str],
    budget: ResearchBudget = ResearchBudget(),
    adapter: str = "kipris",
    adapter_version: str = "plus-xml-v1",
    allowed_host: str = "plus.kipris.or.kr",
) -> tuple[PlannedQuery, ...]:
    """Plan one bibliography-summary lookup per application number.

    Kept separate from `plan_keyword_queries` rather than parameterized by
    capability: the two capabilities take different projections entirely
    (`{"word": ...}` versus `{"application_number": ...}`), so a shared planner
    would have to branch on capability at every step and could emit a projection
    the adapter rejects.
    """

    budget.validate()
    numbers = tuple(dict.fromkeys(
        normalized for value in application_numbers if (normalized := normalize(value))
    ))
    if not numbers:
        raise ValueError("application_numbers: at least one value required")
    planned: list[PlannedQuery] = []
    for number in numbers:
        envelope = QueryEnvelope(
            run_id=run_id, adapter=adapter, adapter_version=adapter_version,
            capability="bibliography_summary", allowed_scheme="https", allowed_host=allowed_host,
            deadline_seconds=10, page=1, page_cap=budget.page_cap,
            result_budget=budget.per_adapter_results, byte_budget=budget.byte_budget,
            retry_budget=budget.retry_budget, retry_ownership="research_runner",
            query_projection={"application_number": number},
        )
        planned.append(PlannedQuery(envelope, number, number, "bibliography", 0))
        if len(planned) >= budget.max_calls:
            break
    return tuple(planned)


class ResearchStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _prior(self, run_id: str, idempotency_key: str) -> tuple[str, ResearchExecution] | None:
        row = self.connection.execute(
            "SELECT query_id,result_json FROM research_operations WHERE run_id=? AND idempotency_key=?",
            (run_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return row["query_id"], ResearchExecution.from_dict(json.loads(row["result_json"]), replayed=True)

    def execute(
        self,
        adapter: SearchAdapter,
        query: PlannedQuery | QueryEnvelope,
        *,
        idempotency_key: str,
        retrieved_at: str | None = None,
        fault_at: FaultInjector = None,
    ) -> ResearchExecution:
        envelope = query.envelope if isinstance(query, PlannedQuery) else query
        prepare = getattr(adapter, "prepare_envelope", None)
        if callable(prepare):
            envelope = prepare(envelope)
            if not isinstance(envelope, QueryEnvelope):
                raise TypeError("adapter prepare_envelope must return QueryEnvelope")
            if isinstance(query, PlannedQuery):
                query = replace(query, envelope=envelope)
        plan = query.as_dict() if isinstance(query, PlannedQuery) else {}
        envelope.validate()
        if not normalize(idempotency_key):
            raise ValueError("idempotency_key: required")
        query_id = "qu_" + digest({"run_id": envelope.run_id, "fingerprint": envelope.request_fingerprint})[:20]
        prior = self._prior(envelope.run_id, idempotency_key)
        if prior:
            if prior[0] != query_id:
                raise ValueError("idempotency_key reused for a different query")
            return prior[1]

        result = adapter.search(envelope)
        result.validate()
        canaries = credential_canaries()
        assert_canaries_absent(
            {
                "coverage": dict(result.coverage), "failure": result.failure.as_dict() if result.failure else None,
                "next_cursor": result.next_cursor, "rate_limit": dict(result.rate_limit) if result.rate_limit else None,
                "records": [record.as_dict() for record in result.records], "terms_note": result.terms_note,
            },
            canaries, boundary="adapter_response",
        )
        at = retrieved_at or utc_now()
        event_id = "ae_" + digest({
            "run_id": envelope.run_id, "query_id": query_id, "idempotency_key": idempotency_key,
            "retrieved_at": at,
        })[:20]
        status = "success" if result.successful else "failure"
        failure_kind = result.failure.kind.value if result.failure else None
        observation_ids: list[str] = []
        evidence_ids: list[str] = []

        with immediate_transaction(self.connection):
            concurrent = self._prior(envelope.run_id, idempotency_key)
            if concurrent:
                if concurrent[0] != query_id:
                    raise ValueError("idempotency_key reused for a different query")
                return concurrent[1]
            self.connection.execute(
                "INSERT INTO research_queries(query_id,run_id,request_fingerprint,envelope_json,plan_json,created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,request_fingerprint) DO NOTHING",
                (query_id, envelope.run_id, envelope.request_fingerprint, canonical_json(envelope.request_body()),
                 canonical_json(plan), at),
            )
            inject_fault(fault_at, "after_research_query")
            self.connection.execute(
                "INSERT INTO adapter_events(event_id,run_id,query_id,adapter,adapter_version,retrieved_at,status,"
                "response_hash,failure_kind,failure_json,terms_note,coverage_json,next_cursor,rate_limit_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, envelope.run_id, query_id, envelope.adapter, envelope.adapter_version, at, status,
                 result.response_hash, failure_kind, canonical_json(result.failure.as_dict()) if result.failure else None,
                 result.terms_note, canonical_json(dict(result.coverage)), result.next_cursor,
                 canonical_json(dict(result.rate_limit)) if result.rate_limit else None),
            )
            inject_fault(fault_at, "after_adapter_event")

            if result.failure:
                observation_id = "ob_" + digest({"event_id": event_id, "failure": failure_kind})[:20]
                self.connection.execute(
                    "INSERT INTO retrieval_observations(observation_id,run_id,query_id,event_id,evidence_id,retrieved_at,"
                    "response_hash,access_status,terms_note) VALUES(?,?,?,?,NULL,?,?,?,?)",
                    (observation_id, envelope.run_id, query_id, event_id, at, result.response_hash, "failure", result.terms_note),
                )
                observation_ids.append(observation_id)
                limitation_id = "li_" + digest({"event_id": event_id, "failure": failure_kind})[:20]
                self.connection.execute(
                    "INSERT INTO coverage_limitations VALUES(?,?,?,?,?,?,?)",
                    (limitation_id, envelope.run_id, query_id, event_id, failure_kind,
                     normalize(result.failure.message), at),
                )
                inject_fault(fault_at, "after_coverage_limitation")
            else:
                seen_evidence: set[str] = set()
                for rank, record in enumerate(result.records, start=1):
                    record_data = record.as_dict()
                    evidence_id = evidence_revision_id(record.source_locator, record.content_hash)
                    if evidence_id in seen_evidence:
                        continue
                    seen_evidence.add(evidence_id)
                    self.connection.execute(
                        "INSERT INTO evidence_records(run_id,evidence_id,source_type,source_locator,original_identifier,title,"
                        "canonical_url,content_hash,language,record_json,created_at,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(run_id,evidence_id) DO NOTHING",
                        (envelope.run_id, evidence_id, record.source_type, record.source_locator,
                         record.original_identifier, record.title, record.canonical_url, record.content_hash,
                         record.language, canonical_json(record_data), at, record.provenance),
                    )
                    evidence_ids.append(evidence_id)
                    inject_fault(fault_at, "after_evidence_record")
                    observation_id = "ob_" + digest({"event_id": event_id, "evidence_id": evidence_id})[:20]
                    self.connection.execute(
                        "INSERT INTO retrieval_observations VALUES(?,?,?,?,?,?,?,?,?)",
                        (observation_id, envelope.run_id, query_id, event_id, evidence_id, at,
                         result.response_hash, "success", result.terms_note),
                    )
                    observation_ids.append(observation_id)
                    self.connection.execute(
                        "INSERT INTO research_edges VALUES(?,?,?,?,?)",
                        (envelope.run_id, query_id, observation_id, evidence_id, rank),
                    )
                    inject_fault(fault_at, "after_research_edge")
                if not result.records:
                    observation_id = "ob_" + digest({"event_id": event_id, "empty": True})[:20]
                    self.connection.execute(
                        "INSERT INTO retrieval_observations VALUES(?,?,?,?,NULL,?,?,?,?)",
                        (observation_id, envelope.run_id, query_id, event_id, at,
                         result.response_hash, "success", result.terms_note),
                    )
                    observation_ids.append(observation_id)
                    inject_fault(fault_at, "after_empty_observation")

            execution = ResearchExecution(
                envelope.run_id, query_id, event_id, tuple(observation_ids), tuple(evidence_ids),
                status, failure_kind,
            )
            self.connection.execute(
                "INSERT INTO research_operations VALUES(?,?,?,?,?,?)",
                (envelope.run_id, idempotency_key, query_id, event_id, canonical_json(execution.as_dict()), at),
            )
            inject_fault(fault_at, "after_research_operation")
        return execution

    def manifest(self, run_id: str) -> dict[str, Any]:
        """Build the research stage's own manifest — never the whole run.

        `audit.py` retrieves its similarity corpus through this same store
        (`store.execute`, `audit.py:306-308`), tagging every query it plans with
        a `term_kind` of `audit_{language}`. Those rows share this run_id and
        land in the same tables, so an unfiltered read here would hand the
        audit's own search terms and evidence back to the research stage on any
        route that calls `manifest()` again after an audit has run (the
        COVERAGE-expand re-entry). `term_kind` is only recorded on
        `research_queries.plan_json` (`PlannedQuery.as_dict()`), so every other
        table is scoped by joining back to the research-stage query_id set;
        `evidence_records` has no query_id at all, so it is scoped through
        `research_edges` — an evidence row is kept if ANY of its edges comes
        from a research-stage query, since the same content-addressed record
        can legitimately surface from both stages.
        """

        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in self.connection.execute(sql, (run_id,))]

        all_queries = rows("SELECT * FROM research_queries WHERE run_id=? ORDER BY created_at,query_id")
        research_query_ids = {
            row["query_id"] for row in all_queries
            if not str(json.loads(row["plan_json"]).get("term_kind", "")).startswith("audit_")
        }

        def scoped(sql: str) -> list[dict[str, Any]]:
            return [row for row in rows(sql) if row["query_id"] in research_query_ids]

        edges = scoped("SELECT * FROM research_edges WHERE run_id=? ORDER BY query_id,source_rank,evidence_id")
        research_evidence_ids = {row["evidence_id"] for row in edges}

        return {
            "adapter_events": scoped("SELECT * FROM adapter_events WHERE run_id=? ORDER BY retrieved_at,event_id"),
            "coverage_limitations": scoped("SELECT * FROM coverage_limitations WHERE run_id=? ORDER BY created_at,limitation_id"),
            "edges": edges,
            "evidence": [
                row for row in rows("SELECT * FROM evidence_records WHERE run_id=? ORDER BY evidence_id")
                if row["evidence_id"] in research_evidence_ids
            ],
            "observations": scoped("SELECT * FROM retrieval_observations WHERE run_id=? ORDER BY retrieved_at,observation_id"),
            "queries": [row for row in all_queries if row["query_id"] in research_query_ids],
            "run_id": run_id,
        }


def research_bundle(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Build the single deterministic payload registered by StateStore publication."""

    return {
        "adapter_events": manifest["adapter_events"],
        "coverage_limitations": manifest["coverage_limitations"],
        "edges": manifest["edges"],
        "evidence": manifest["evidence"],
        "observations": manifest["observations"],
        "queries": manifest["queries"],
        "run_id": manifest["run_id"],
        "version": "research-bundle-v1",
    }


def _private_export_directory(run_root: Path, *, create: bool) -> tuple[Path, Path]:
    root = Path(run_root).absolute()
    if not root.is_dir() or stat.S_ISLNK(root.lstat().st_mode):
        raise ValueError("research_export: safe run directory required")
    exports = root / "research-exports"
    if exports.exists() and (stat.S_ISLNK(exports.lstat().st_mode) or not exports.is_dir()):
        raise ValueError("research_export: unsafe export directory")
    if create:
        exports.mkdir(mode=0o700, exist_ok=True)
        try:
            os.chmod(exports, 0o700, follow_symlinks=False)
        except OSError:
            pass
    return root, exports


def _literal_query_terms(envelopes: Sequence[QueryEnvelope]) -> list[str]:
    """The literal terms a batch will actually send, for operator approval."""

    terms: list[str] = []
    for envelope in envelopes:
        projection = envelope.query_projection
        term = normalize(projection.get("word") or projection.get("application_number") or "")
        if term and term not in terms:
            terms.append(term)
    return terms


def _reentry_scope_fields(
    reentry_anchor: "ReResearchAnchor | None", envelopes: Sequence[QueryEnvelope],
) -> dict[str, Any]:
    """The plan binding an operator approves on a `re_research` second pass.

    Egress honesty (issue #48): `request_fingerprint` is a digest an operator
    cannot read, and the re_research plan is NL directions, not search terms —
    so the scope surfaces the recorded plan, its hash, and the LITERAL terms
    this batch will send, making the direction<->term correspondence an
    explicit, approved element of the decision. Gated on the second-pass path
    ONLY (anchor present), evaluated at suspend time: adding these fields
    unconditionally would shift every FIRST-pass credential
    `approval_scope_hash`, re-minting recorded gates for no reason.
    """

    if reentry_anchor is None:
        return {}
    plan = dict(reentry_anchor.plan or {})
    return {
        "needed_research": normalize(plan.get("needed_research", [])),
        "plan": normalize(plan),
        "plan_hash": reentry_anchor.plan_hash,
        "re_research_decision_id": reentry_anchor.decision_id,
        "second_pass_terms": _literal_query_terms(envelopes),
    }


def _credential_scope(
    envelope: QueryEnvelope,
    *,
    auth_attempt: str,
    credential_name: str,
    reentry_anchor: "ReResearchAnchor | None" = None,
) -> dict[str, Any]:
    return {
        "adapter": normalize(envelope.adapter),
        "adapter_version": normalize(envelope.adapter_version),
        "allowed_host": normalize(envelope.allowed_host).casefold(),
        "auth_attempt": auth_attempt,
        "capability": normalize(envelope.capability),
        "credential_name": credential_name,
        "request_fingerprint": envelope.request_fingerprint,
        **_reentry_scope_fields(reentry_anchor, (envelope,)),
    }


def _batch_credential_scope(
    envelopes: Sequence[QueryEnvelope],
    *,
    auth_attempt: str,
    credential_name: str,
    effective_pages: int,
    result_budget: int,
    reentry_anchor: "ReResearchAnchor | None" = None,
) -> dict[str, Any]:
    """The human-readable ceiling an operator approves at `gate decide`.

    `effective_pages` and `result_budget` are egress-honesty fields (PR #49
    review finding #4): pages 2+ do not exist as envelopes yet when this scope
    is built (they are minted lazily inside `execute_paginated`, after the
    decision is consumed), so `request_fingerprint`/`query_count` above only
    ever describe page-1 shapes. Without an explicit ceiling here, an operator
    approving `query_count: 12` at defaults would see nothing telling them the
    approved batch could actually issue `12 * effective_pages` requests.
    """

    first = envelopes[0]
    return {
        "adapter": normalize(first.adapter),
        "adapter_version": normalize(first.adapter_version),
        "allowed_host": normalize(first.allowed_host).casefold(),
        "auth_attempt": auth_attempt,
        "capability": normalize(first.capability),
        "credential_name": credential_name,
        "effective_pages": effective_pages,
        "max_requests": len(envelopes) * effective_pages,
        "query_count": len(envelopes),
        "request_fingerprint": digest({
            "fingerprints": [envelope.request_fingerprint for envelope in envelopes],
        }),
        "result_budget": result_budget,
        **_reentry_scope_fields(reentry_anchor, envelopes),
    }


def _verify_and_consume_credential_decision(
    connection: sqlite3.Connection,
    state: StateStore,
    *,
    run_id: str,
    credential_decision_id: str,
    credential_operation: str,
    subject_revision_hash: str,
    idempotency_key: str,
    effective_pages: int = 1,
    result_budget: int | None = None,
) -> dict[str, Any]:
    """Bind a user decision to the exact suspended request, consuming it once.

    Returns the approval scope so the caller can enforce it beyond this one
    check (RC2 locus ii: `execute_paginated` re-applies these same ceilings
    per page, independently of this function, because pages 2+ are minted
    after this call returns).

    `effective_pages` is deliberately outside the hashed `subject_revision_hash`
    surface (it is never part of any `request_body()`), so hash equality alone
    cannot see a resume-time paging escalation: an operator could approve a
    batch at `--paging` off and resume the same decision with `--paging` on
    without the page-1 envelopes — and therefore the hash — ever changing.
    This is RC2 locus (i): reject the resuming request outright if its
    declared `effective_pages`/`result_budget` exceed what was approved.
    `result_budget` IS hashed (it is a `QueryEnvelope` field), so an increase
    there is normally already caught by the `subject_revision_hash` mismatch
    below; the explicit `<=` check is a second, independent path against that
    same escalation, not a replacement for it.
    """

    row = connection.execute(
        "SELECT ge.approval_scope_json,gd.stale,gd.subject_revision_hash,"
        "gd.suspended_operation,gd.used_at,gd.consumed_by_event_id FROM gate_decisions gd "
        "JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
        "WHERE gd.decision_id=? AND gd.run_id=?",
        (credential_decision_id, run_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("credential decision is unavailable")
    approval_scope = json.loads(row["approval_scope_json"])
    approved_pages = approval_scope.get("effective_pages", 1)
    approved_budget = approval_scope.get("result_budget")
    if (
        row["stale"]
        or row["subject_revision_hash"] != subject_revision_hash
        or row["suspended_operation"] != credential_operation
        or effective_pages > approved_pages
        or (result_budget is not None and approved_budget is not None and result_budget > approved_budget)
    ):
        raise RuntimeError("credential decision does not match the current request")
    if row["used_at"]:
        replay = connection.execute(
            "SELECT event_id FROM idempotency_records "
            "WHERE run_id=? AND operation=? AND idempotency_key=?",
            (run_id, credential_operation, idempotency_key),
        ).fetchone()
        if replay is None or replay["event_id"] != row["consumed_by_event_id"]:
            raise RuntimeError("credential decision was used by a different operation")
    else:
        state.consume_decision(
            credential_decision_id,
            suspended_operation=credential_operation,
            subject_revision_hash=subject_revision_hash,
            approval_scope=approval_scope,
        )
    return approval_scope


def run_research(
    connection: sqlite3.Connection,
    *,
    run_root: Path,
    run_id: str,
    adapter: SearchAdapter,
    query: PlannedQuery | QueryEnvelope,
    idempotency_key: str,
    retrieved_at: str | None = None,
    credential_decision_id: str | None = None,
    fault_at: FaultInjector = None,
    advance_spent_key: bool = True,
) -> ResearchRun:
    """Execute one bounded research operation through the authoritative state machine."""

    envelope = query.envelope if isinstance(query, PlannedQuery) else query
    prepare = getattr(adapter, "prepare_envelope", None)
    if callable(prepare):
        envelope = prepare(envelope)
        if not isinstance(envelope, QueryEnvelope):
            raise TypeError("adapter prepare_envelope must return QueryEnvelope")
        if isinstance(query, PlannedQuery):
            query = replace(query, envelope=envelope)
        else:
            query = envelope
    envelope.validate()
    if envelope.run_id != normalize(run_id):
        raise ValueError("research run_id does not match the query envelope")
    root, exports = _private_export_directory(run_root, create=False)
    own = (exports,) if exports.exists() else ()
    state = StateStore(connection, export_directories=workspace_export_directories(connection, root, own))
    prior = state.snapshot(run_id)
    if prior.state is RunState.CREDENTIAL_REQUIRED:
        raise RuntimeError("credential_required: a current decision must resume the suspended request")

    requires_credential = bool(getattr(adapter, "requires_credential", False))
    credential_name = normalize(getattr(adapter, "credential_name", ""))
    if requires_credential and not credential_name:
        raise ValueError("credential-requiring adapter must declare its credential name")
    # Guard symmetry: this single-query entry point is generic over ANY
    # credential-requiring adapter, not only the CLI's own kipris/serpapi
    # callers — the CLI-level SerpAPI preflight is one caller, not the only
    # one. Guarding here too makes `run_research` self-protecting regardless of
    # caller, matching `run_research_batch`'s identical guard.
    reentry_anchor, idempotency_key = (
        _apply_reentry_guard(connection, run_id, idempotency_key)
        if requires_credential else (None, idempotency_key)
    )
    force_gate = needs_reentry_force_gate(reentry_anchor, credential_decision_id)
    if requires_credential:
        # Decision 4c, after the salt so a second pass advances inside its own
        # namespace, and before `credential_operation` is derived so the whole
        # finish/consume coordinate moves together. `force_gate` scopes the
        # advance to the same attempts the spent-coordinate refusal covers.
        idempotency_key = attempt_coordinate(
            connection, run_id, idempotency_key,
            credential_decision_id=credential_decision_id,
            advance=advance_spent_key and force_gate,
        )
    credential_operation = f"research.execute:{idempotency_key}"
    request_revision = None
    if requires_credential:
        _refuse_spent_attempt_coordinate(
            connection, run_id=run_id, idempotency_key=idempotency_key,
            credential_decision_id=credential_decision_id, force_gate=force_gate,
        )
        if prior.state not in {RunState.RESEARCH_READY, RunState.RESEARCH_RUNNING}:
            state.transition(
                run_id, RunState.RESEARCH_RUNNING, actor="research-cli", reason="state check",
                operation="research.start", idempotency_key=idempotency_key,
            )
        request_revision = state.add_revision(
            run_id,
            "research_request",
            {
                "plan": query.as_dict() if isinstance(query, PlannedQuery) else {},
                "request": envelope.request_body(),
            },
            schema_version="research-request-v1",
        )
        if credential_decision_id:
            _verify_and_consume_credential_decision(
                connection,
                state,
                run_id=run_id,
                credential_decision_id=credential_decision_id,
                credential_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                idempotency_key=idempotency_key,
            )
        if force_gate or not bool(getattr(adapter, "credential_present", False)):
            scope = _credential_scope(
                envelope,
                auth_attempt=credential_decision_id or "preflight",
                credential_name=credential_name,
                reentry_anchor=reentry_anchor,
            )
            gate = state.suspend_gate(
                run_id,
                GateKind.CREDENTIAL,
                suspended_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                approval_scope=scope,
                # Decision 3, and the only point that reaches every branch this
                # suspend is chosen from: read the state the gate will actually
                # suspend from, and refuse before any egress when a credential
                # gate cannot legally suspend from it.
                return_state=_suspendable_state(state, run_id),
                actor="research-cli",
                reason=(
                    "re_research second pass requires an explicit credential approval"
                    if force_gate else "required adapter credential is unavailable"
                ),
            )
            raise CredentialRequiredError(gate)

    transition_event_ids: list[str] = []
    if prior.state is not RunState.RESEARCH_RUNNING:
        started = state.transition(
            run_id,
            RunState.RESEARCH_RUNNING,
            actor="research-cli",
            reason="bounded research started",
            operation="research.start",
            idempotency_key=idempotency_key,
        )
        transition_event_ids.append(started.event_id)
    else:
        started = None
    execution_key = (
        f"{idempotency_key}:credential:{credential_decision_id}"
        if credential_decision_id else idempotency_key
    )
    execution = ResearchStore(connection).execute(
        adapter,
        query,
        idempotency_key=execution_key,
        retrieved_at=retrieved_at,
    )
    if requires_credential and execution.failure_kind == "auth":
        if request_revision is None:
            raise RuntimeError("credential adapter has no request revision")
        scope = _credential_scope(
            envelope,
            auth_attempt=credential_decision_id or "remote_auth",
            credential_name=credential_name,
            reentry_anchor=reentry_anchor,
        )
        gate = state.suspend_gate(
            run_id,
            GateKind.CREDENTIAL,
            suspended_operation=credential_operation,
            subject_revision_hash=request_revision.content_hash,
            approval_scope=scope,
            # Decision 3 again: the research.start transition below the guard
            # may have advanced the run since, so read the state here too.
            return_state=_suspendable_state(state, run_id),
            actor="research-cli",
            reason="adapter rejected the configured credential",
        )
        raise CredentialRequiredError(gate)
    manifest = ResearchStore(connection).manifest(run_id)
    payload = research_bundle(manifest)
    target = (
        RunState.RESEARCH_COMPLETE
        if execution.status == "success" and execution.evidence_ids
        else RunState.RESEARCH_INCOMPLETE
    )
    _root, exports = _private_export_directory(root, create=True)
    state = StateStore(connection, export_directories=workspace_export_directories(connection, root, (exports,)))
    final_operation = credential_operation if credential_decision_id else "research.finish"
    finished, exported = state.publish_transition(
        run_id,
        target,
        actor="research-cli",
        reason="bounded research persisted",
        operation=final_operation,
        idempotency_key=idempotency_key,
        evidence_hashes=execution.evidence_ids,
        artifact_kind="research_bundle",
        artifact_content=payload,
        artifact_schema_version="research-bundle-v1",
        export_directory=exports,
        dependencies=(request_revision.revision_id,) if request_revision else (),
        consumed_decision_id=credential_decision_id,
        fault_at=fault_at,
    )
    if finished.artifact is None:
        raise RuntimeError("research finish transition did not produce its bundle revision")
    transition_event_ids.append(finished.event_id)
    bundle = {
        **payload,
        "manifest": {
            "artifact_id": exported.artifact_id,
            "byte_hash": exported.content_hash,
            "byte_size": exported.size,
            "path": Path(exported.path).relative_to(root).as_posix(),
        },
    }
    return ResearchRun(
        run_id,
        prior.state.value,
        finished.snapshot.state.value,
        execution,
        bundle,
        finished.artifact.revision_id,
        tuple(transition_event_ids),
        bool(started and started.replayed and execution.replayed and finished.replayed),
    )


def run_research_batch(
    connection: sqlite3.Connection,
    *,
    run_root: Path,
    run_id: str,
    adapter: SearchAdapter,
    queries: Sequence[PlannedQuery],
    idempotency_key: str,
    retrieved_at: str | None = None,
    credential_decision_id: str | None = None,
    fault_at: FaultInjector = None,
    advance_spent_key: bool = True,
    effective_pages: int = 1,
) -> ResearchBatchRun:
    """Execute a bounded batch of planned queries in one research session.

    Mirrors run_research's authoritative handling, but performs every planned
    query between the single start transition and the single finish
    publication — the same store pattern the audit retrieval loop uses. A
    non-auth source failure is recorded as an adapter event and coverage
    limitation and the batch continues; an auth failure suspends the exact
    batch behind a credential gate.

    `effective_pages` is the live paging control (unhashed; derived from CLI
    `--paging` by the caller). It defaults to 1 — page 1 only, byte-identical
    to the pre-paging behaviour — so every existing caller that does not pass
    it is unaffected.
    """

    if not queries:
        raise ValueError("research batch requires at least one planned query")
    if len(queries) > MAX_BATCH_REQUESTS:
        raise ValueError(f"research batch exceeds the maximum of {MAX_BATCH_REQUESTS} planned queries")
    # Self-enforced at the egress boundary, not just relied on from
    # `ResearchBudget.validate(effective_pages=...)` (PR #49 review, Security
    # LOW finding): that check only runs for callers that plan their queries
    # through a `ResearchBudget` and remember to pass `effective_pages` to it
    # — `_research_kipris` does today, but nothing stops a future non-CLI
    # caller from building `PlannedQuery` objects directly and handing this
    # executor a large `effective_pages` with no such check ever having run.
    # This is the same ceiling, checked again here, before the paging loop
    # below can issue a single request.
    if len(queries) * effective_pages > MAX_BATCH_REQUESTS:
        raise ValueError(
            f"research batch: planned queries * effective_pages must not exceed "
            f"{MAX_BATCH_REQUESTS} (planned={len(queries)}, effective_pages={effective_pages})"
        )
    if not normalize(idempotency_key):
        raise ValueError("idempotency_key: required")
    prepare = getattr(adapter, "prepare_envelope", None)
    resolved: list[PlannedQuery] = []
    for query in queries:
        envelope = query.envelope
        if callable(prepare):
            envelope = prepare(envelope)
            if not isinstance(envelope, QueryEnvelope):
                raise TypeError("adapter prepare_envelope must return QueryEnvelope")
            query = replace(query, envelope=envelope)
        envelope.validate()
        if envelope.run_id != normalize(run_id):
            raise ValueError("research run_id does not match a query envelope")
        resolved.append(query)

    root, exports = _private_export_directory(run_root, create=False)
    own = (exports,) if exports.exists() else ()
    state = StateStore(connection, export_directories=workspace_export_directories(connection, root, own))
    prior = state.snapshot(run_id)
    if prior.state is RunState.CREDENTIAL_REQUIRED:
        raise RuntimeError("credential_required: a current decision must resume the suspended request")

    requires_credential = bool(getattr(adapter, "requires_credential", False))
    credential_name = normalize(getattr(adapter, "credential_name", ""))
    if requires_credential and not credential_name:
        raise ValueError("credential-requiring adapter must declare its credential name")
    # Issue-48 four-way guard: refuses stale/unbound/mismatched bindings,
    # returns None on a first-pass retry or cycle-back, and otherwise yields
    # the anchor that force-gates and salts this second pass.
    reentry_anchor, idempotency_key = (
        _apply_reentry_guard(connection, run_id, idempotency_key)
        if requires_credential else (None, idempotency_key)
    )
    force_gate = needs_reentry_force_gate(reentry_anchor, credential_decision_id)
    if requires_credential:
        # Decision 4c, after the salt so a second pass advances inside its own
        # namespace, and before `credential_operation` is derived so the whole
        # finish/consume coordinate moves together. `force_gate` scopes the
        # advance to the same attempts the spent-coordinate refusal covers.
        idempotency_key = attempt_coordinate(
            connection, run_id, idempotency_key,
            credential_decision_id=credential_decision_id,
            advance=advance_spent_key and force_gate,
        )
    credential_operation = f"research.execute:{idempotency_key}"
    # Representative per-term ceiling for the approval scope and the two-locus
    # enforcement below. plan_keyword_queries/plan_bibliography_queries give
    # every envelope in one batch the same `result_budget`, so the first is
    # exact, not an approximation.
    batch_result_budget = resolved[0].envelope.result_budget
    request_revision = None
    approved_scope: dict[str, Any] | None = None
    if requires_credential:
        _refuse_spent_attempt_coordinate(
            connection, run_id=run_id, idempotency_key=idempotency_key,
            credential_decision_id=credential_decision_id, force_gate=force_gate,
        )
        if prior.state not in {RunState.RESEARCH_READY, RunState.RESEARCH_RUNNING}:
            state.transition(
                run_id, RunState.RESEARCH_RUNNING, actor="research-cli", reason="state check",
                operation="research.start", idempotency_key=idempotency_key,
            )
        request_revision = state.add_revision(
            run_id,
            "research_request",
            {
                "plan": [query.as_dict() for query in resolved],
                "requests": [query.envelope.request_body() for query in resolved],
            },
            schema_version="research-request-v1",
        )
        if credential_decision_id:
            # RC2 locus (i): reject a resume that asks for more pages or more
            # rows-per-term than the operator actually approved. Effective_pages
            # is outside the hash, so this is the only check that can see it.
            approved_scope = _verify_and_consume_credential_decision(
                connection,
                state,
                run_id=run_id,
                credential_decision_id=credential_decision_id,
                credential_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                idempotency_key=idempotency_key,
                effective_pages=effective_pages,
                result_budget=batch_result_budget,
            )
        if force_gate or not bool(getattr(adapter, "credential_present", False)):
            scope = _batch_credential_scope(
                [query.envelope for query in resolved],
                auth_attempt=credential_decision_id or "preflight",
                credential_name=credential_name,
                effective_pages=effective_pages,
                result_budget=batch_result_budget,
                reentry_anchor=reentry_anchor,
            )
            gate = state.suspend_gate(
                run_id,
                GateKind.CREDENTIAL,
                suspended_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                approval_scope=scope,
                # Decision 3, and the only point that reaches every branch this
                # suspend is chosen from: read the state the gate will actually
                # suspend from, and refuse before any egress when a credential
                # gate cannot legally suspend from it.
                return_state=_suspendable_state(state, run_id),
                actor="research-cli",
                reason=(
                    "re_research second pass requires an explicit credential approval"
                    if force_gate else "required adapter credential is unavailable"
                ),
            )
            raise CredentialRequiredError(gate)

    transition_event_ids: list[str] = []
    if prior.state is not RunState.RESEARCH_RUNNING:
        started = state.transition(
            run_id,
            RunState.RESEARCH_RUNNING,
            actor="research-cli",
            reason="bounded research batch started",
            operation="research.start",
            idempotency_key=idempotency_key,
        )
        transition_event_ids.append(started.event_id)
    else:
        started = None
    base_key = (
        f"{idempotency_key}:credential:{credential_decision_id}"
        if credential_decision_id else idempotency_key
    )
    store = ResearchStore(connection)
    executions: list[ResearchExecution] = []
    for index, query in enumerate(resolved):
        # One planned term can now span several pages. `execute_paginated`
        # follows the adapter's `next_cursor` up to `effective_pages`, which is
        # what finally gives `--paging` an effect on this path; with the
        # shipped default (`effective_pages=1`) this behaves exactly as the
        # single call it replaces.
        #
        # RC2 locus (ii): pass the approved ceilings straight through, sourced
        # independently from `approved_scope` rather than trusted from
        # `effective_pages`/`batch_result_budget` above. Pages 2+ are minted
        # here, after the one-time consume in `_verify_and_consume_credential_
        # decision` returns, so that check alone cannot bound what this loop
        # actually requests — this is a second, independent enforcement of the
        # same ceiling, load-bearing rather than redundant, and it is the only
        # enforcement at all on the no-prior-gate path where no decision is
        # ever consumed.
        paged = execute_paginated(
            store,
            adapter,
            query,
            connection=connection,
            idempotency_key=f"{base_key}:q{index:02d}",
            retrieved_at=retrieved_at,
            effective_pages=effective_pages,
            approved_effective_pages=approved_scope.get("effective_pages") if approved_scope else None,
            approved_result_budget=approved_scope.get("result_budget") if approved_scope else None,
        )
        executions.extend(paged)
        if requires_credential and any(item.failure_kind == "auth" for item in paged):
            if request_revision is None:
                raise RuntimeError("credential adapter has no request revision")
            scope = _batch_credential_scope(
                [query.envelope for query in resolved],
                auth_attempt=credential_decision_id or "remote_auth",
                credential_name=credential_name,
                effective_pages=effective_pages,
                result_budget=batch_result_budget,
                reentry_anchor=reentry_anchor,
            )
            gate = state.suspend_gate(
                run_id,
                GateKind.CREDENTIAL,
                suspended_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                approval_scope=scope,
                # Decision 3 again: the research.start transition below the guard
                # may have advanced the run since, so read the state here too.
                return_state=_suspendable_state(state, run_id),
                actor="research-cli",
                reason="adapter rejected the configured credential",
            )
            raise CredentialRequiredError(gate)
    manifest = ResearchStore(connection).manifest(run_id)
    payload = research_bundle(manifest)
    succeeded = any(item.status == "success" and item.evidence_ids for item in executions)
    target = RunState.RESEARCH_COMPLETE if succeeded else RunState.RESEARCH_INCOMPLETE
    _root, exports = _private_export_directory(root, create=True)
    state = StateStore(connection, export_directories=workspace_export_directories(connection, root, (exports,)))
    final_operation = credential_operation if credential_decision_id else "research.finish"
    evidence_hashes = tuple(dict.fromkeys(
        evidence_id for item in executions for evidence_id in item.evidence_ids
    ))
    finished, exported = state.publish_transition(
        run_id,
        target,
        actor="research-cli",
        reason="bounded research batch persisted",
        operation=final_operation,
        idempotency_key=idempotency_key,
        evidence_hashes=evidence_hashes,
        artifact_kind="research_bundle",
        artifact_content=payload,
        artifact_schema_version="research-bundle-v1",
        export_directory=exports,
        dependencies=(request_revision.revision_id,) if request_revision else (),
        consumed_decision_id=credential_decision_id,
        fault_at=fault_at,
    )
    if finished.artifact is None:
        raise RuntimeError("research finish transition did not produce its bundle revision")
    transition_event_ids.append(finished.event_id)
    bundle = {
        **payload,
        "manifest": {
            "artifact_id": exported.artifact_id,
            "byte_hash": exported.content_hash,
            "byte_size": exported.size,
            "path": Path(exported.path).relative_to(root).as_posix(),
        },
    }
    return ResearchBatchRun(
        run_id,
        prior.state.value,
        finished.snapshot.state.value,
        tuple(executions),
        bundle,
        finished.artifact.revision_id,
        tuple(transition_event_ids),
        bool(
            started and started.replayed
            and all(item.replayed for item in executions)
            and finished.replayed
        ),
        planned_count=len(resolved),
    )
