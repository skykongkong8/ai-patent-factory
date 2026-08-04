"""RALPLAN-DR: the re-entry guard's reachability, suspendability and coordinates.

Issue #48 shipped the force-gate and the salt behind a `prior.state is
RESEARCH_RUNNING` predicate. That predicate is a PROXY for the fact the guard
actually needs — "this run is inside an authorized second pass" — and the two
disagree the moment a second pass ends `RESEARCH_INCOMPLETE`: the run leaves
`research_running`, the anchor stays live, and the retry egresses unsalted and
ungated on unapproved terms.

This module pins the repair. Its companion `test_research_reentry_gate.py`
keeps the shipped locks (salt shape, first-pass byte identity, the finish
collision) untouched — nothing here edits them.

Four properties, each with its own mutation lock:

* **suspendability** (T-U3) — the guard must be able to complete the branch it
  chooses. Every credential suspend returns to the state it actually suspended
  from, and an attempt that cannot legally raise the gate is refused BEFORE any
  egress rather than crashing after it.
* **reachability** (T-U1) — the anchor is evaluated from every credentialed
  entry state, on BOTH the fresh-key and the replayed-`research.start` path.
  The two paths hole different state sets, and neither test sees the other's.
* **coordinates** (T-U4, T-I3, T-I3b, T-I8) — a re-raised gate gets a distinct
  id instead of an `IntegrityError`, and a spent attempt coordinate is refused
  instead of silently replaying a prior attempt's bundle — without breaking the
  designed replay of a completed attempt by its own decision.
* **escape hatch** (T-I7) — an offline pass quiets even a stale anchor, so the
  widened refusals can never wedge a run.
"""
import tempfile
import unittest
from pathlib import Path

from patent_factory.adapters.manual_web import ManualWebAdapter
from patent_factory.database import connect_database, utc_now
from patent_factory.models import AdapterFailureKind, QueryEnvelope, RunState
from patent_factory.provenance import digest
from patent_factory.research import (
    CredentialRequiredError,
    LiveResearchReentryRefusedError,
    LiveResearchReentrySpentCoordinateError,
    re_research_reentry_anchor,
    run_research,
    run_research_batch,
    salted_reentry_key,
)
from patent_factory.state import GateMismatchError
from tests.integration.test_g009_research_batch import failure, plan, ready, success
from tests.integration.test_research_reentry_gate import (
    BOUND_PLAN,
    CredentialStubAdapter,
    seed_re_research_reentry,
)

RETRIEVED_AT = "2026-01-01T00:00:00Z"
ALL_SUCCEED = {"센서": success(), "감지기": success(), "sensor": success()}
ALL_FAIL = {"센서": failure(), "감지기": failure(), "sensor": failure()}

# The two tables of §0 B-11, which differ by WHICH CHECK RUNS FIRST. On a fresh
# key `_validate_direct_transition` refuses any transition out of a gate state,
# so only three states reach the guard; a replayed `research.start` early-
# returns before that check, so all six do.
FRESH_KEY_HOLED = ("research_ready", "research_incomplete", "insufficient_evidence")
REPLAYED_KEY_HOLED = FRESH_KEY_HOLED + (
    "domain_pivot_required", "coverage_insufficient", "decision_required",
)
# A CREDENTIAL gate suspends legally from exactly these three, so on the
# replayed path — where the run is never advanced — only these two of the six
# can force-gate; the rest are refused before egress by the Decision-3 check.
CREDENTIAL_SUSPENDABLE = ("research_ready", "research_running", "research_incomplete")


class GuardTestCase(unittest.TestCase):
    """One fresh database per scenario, not per test method.

    `state.transition` commits its own transaction, so a scenario that crashes
    mid-pass leaves an advanced run state, orphan `research.start` records and
    a pending gate behind — and the next scenario in the same database then
    goes misleadingly green (or dies on `one_pending_gate_per_run`). Every
    parametrized case calls `reset()` first.
    """

    connection = None

    def setUp(self):
        self._temporaries = []
        self.reset()

    def reset(self):
        temporary = tempfile.TemporaryDirectory()
        self._temporaries.append(temporary)
        if self.connection is not None:
            self.connection.close()
        self.root = Path(temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.store = ready(self.connection)

    def tearDown(self):
        self.connection.close()
        self.connection = None
        for temporary in self._temporaries:
            temporary.cleanup()

    def batch(self, adapter, key, *, terms=None, **kwargs):
        queries = plan() if terms is None else plan(korean=terms[0], english=terms[1])
        return run_research_batch(
            self.connection, run_root=self.root, run_id="run", adapter=adapter,
            queries=queries, idempotency_key=key, retrieved_at=RETRIEVED_AT, **kwargs,
        )

    def park(self, state_value):
        """Set an entry state directly — most are unreachable by legal transition."""

        self.connection.execute(
            "UPDATE runs SET state=?, state_version=state_version+1, updated_at=? "
            "WHERE run_id='run'",
            (state_value, utc_now()),
        )

    def seed_start_record(self, key):
        """Record `research.start` at `key`, so the state-check transition REPLAYS.

        This is the whole of Path 2: `state.transition` early-returns on an
        existing record before it validates anything, so the run is never
        advanced and the gate-state protection never runs.
        """

        event_id = "te_start_" + digest({"key": key})[:12]
        self.connection.execute(
            "INSERT OR IGNORE INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, "run", "test", "research_ready", "research_running",
             "seeded replayed start", "[]", None, utc_now()),
        )
        self.connection.execute(
            "INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)",
            ("run", "research.start", key, event_id, None,
             RunState.RESEARCH_RUNNING.value, utc_now()),
        )

    def approve(self, gate, action="configure_and_verify"):
        decision, _ = self.store.decide_gate(
            gate.gate_id, action=action, actor="user", reason="approved",
            subject_revision_hash=gate.subject_revision_hash,
            approval_scope=dict(gate.approval_scope),
            suspended_operation=gate.suspended_operation, return_state=gate.return_state,
        )
        return decision


class SuspendabilityTests(GuardTestCase):
    """T-U3 — the guard must be able to complete the branch it chooses.

    All six cases run on runs with NO `re_research` history, so none of them can
    pass through the new anchor-keyed guard by accident: they fault the suspend
    machinery itself, which is what PM-1 ("we fixed the guard and shipped a
    crash") is about.

    M-2: reverting any of the four suspend sites fails this class — both
    runners', which is why the single-query runner has its own case here even
    though every other test in this module drives the batch one. The two sites
    that hardcoded `RESEARCH_RUNNING` are visible only in the replayed-start
    cases; on a fresh key the run really has reached `research_running`, so a
    fresh-key test passes with the bug in place.
    """

    def test_fresh_key_suspend_returns_to_the_post_transition_state(self):
        """The stale `return_state=prior.state` crash (B-4), on both entry states.

        `research_incomplete` and `insufficient_evidence` both transition to
        `research_running` first, so the gate must return THERE — not to the
        state the caller snapshotted before the transition fired.
        """

        for entry in ("research_incomplete", "insufficient_evidence"):
            with self.subTest(entry=entry):
                self.reset()
                self.park(entry)
                adapter = CredentialStubAdapter(present=False)
                with self.assertRaises(CredentialRequiredError) as captured:
                    self.batch(adapter, f"fresh-{entry}")
                self.assertEqual(adapter.calls, [])
                gate = captured.exception.gate
                self.assertEqual(gate.return_state, RunState.RESEARCH_RUNNING)
                self.assertEqual(gate.suspended_state, RunState.RESEARCH_RUNNING)
                self.assertEqual(
                    self.store.snapshot("run").state, RunState.CREDENTIAL_REQUIRED,
                )
                # AC-1: approving returns the run there and the pass resumes.
                decision = self.approve(gate)
                self.assertEqual(
                    self.store.snapshot("run").state, RunState.RESEARCH_RUNNING,
                )
                resumed = self.batch(
                    CredentialStubAdapter(present=True, results=ALL_SUCCEED),
                    f"fresh-{entry}", credential_decision_id=decision.decision_id,
                )
                self.assertEqual(resumed.next_state, RunState.RESEARCH_COMPLETE.value)

    def test_replayed_start_refuses_before_egress_when_the_gate_cannot_suspend(self):
        """PM-1's distinguishing variant: the run is never advanced.

        A replayed `research.start` leaves the run in its entry state, and a
        CREDENTIAL gate cannot legally suspend from any of these four. Before
        this fix the suspend went ahead and `suspend_gate` raised a bare
        `StateError` naming an internal state rule; now the attempt is refused
        with its own code, before the gate is attempted and before any egress.
        """

        for entry in (
            "insufficient_evidence", "domain_pivot_required",
            "coverage_insufficient", "decision_required",
        ):
            with self.subTest(entry=entry):
                self.reset()
                self.assertNotIn(entry, CREDENTIAL_SUSPENDABLE)
                key = f"replayed-{entry}"
                self.seed_start_record(key)
                self.park(entry)
                adapter = CredentialStubAdapter(present=False)
                with self.assertRaises(LiveResearchReentrySpentCoordinateError) as caught:
                    self.batch(adapter, key)
                self.assertEqual(
                    caught.exception.code,
                    "live_research_reentry_spent_coordinate_issue_48",
                )
                self.assertEqual(adapter.calls, [])
                self.assertEqual(self.store.snapshot("run").state.value, entry)

    def test_replayed_start_still_gates_cleanly_where_the_gate_is_legal(self):
        """The other half of the split: two of the six suspend, they do not refuse.

        Asserted so the Decision-3 check cannot regress into a blanket refusal
        on the replayed path.
        """

        for entry in ("research_ready", "research_incomplete"):
            with self.subTest(entry=entry):
                self.reset()
                self.assertIn(entry, CREDENTIAL_SUSPENDABLE)
                key = f"legal-replayed-{entry}"
                self.seed_start_record(key)
                self.park(entry)
                adapter = CredentialStubAdapter(present=False)
                with self.assertRaises(CredentialRequiredError) as captured:
                    self.batch(adapter, key)
                self.assertEqual(adapter.calls, [])
                self.assertEqual(captured.exception.gate.return_state.value, entry)

    def test_first_pass_gate_is_not_refused_and_still_returns_to_research_ready(self):
        """AC-1c — the control that rules out "the run must reach research_running".

        A first pass suspends from `research_ready`, whose state-check
        transition is skipped by design. Any legality rule phrased as "must be
        `research_running`" refuses every ordinary first pass; this asserts the
        rule actually shipped is the transition-legality one.
        """

        adapter = CredentialStubAdapter(present=False)
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(adapter, "first-pass")
        gate = captured.exception.gate
        self.assertEqual(adapter.calls, [])
        self.assertEqual(gate.return_state, RunState.RESEARCH_READY)
        self.assertEqual(gate.suspended_state, RunState.RESEARCH_READY)
        self.assertEqual(gate.suspended_operation, "research.execute:first-pass")

    def single(self, adapter, key, **kwargs):
        """Drive the SINGLE-QUERY runner, whose suspend sites are separate code.

        `run_research` and `run_research_batch` are hand-parallel twins: each
        carries its own copy of the guard, the refusal and both suspends. Every
        other test in this module drives the batch runner, so without this the
        single-query copies could be reverted with the whole suite green.
        """

        envelope = QueryEnvelope(
            run_id="run", adapter="kipris", adapter_version="plus-xml-v1",
            capability="word_search", allowed_scheme="https",
            allowed_host="plus.kipris.or.kr", deadline_seconds=10, page=1, page_cap=1,
            result_budget=10, byte_budget=1_000_000, retry_budget=0,
            retry_ownership="research_runner",
            query_projection={
                "word": "센서", "year": 0, "patent": True, "utility": True,
                "num_of_rows": 10,
            },
        )
        return run_research(
            self.connection, run_root=self.root, run_id="run", adapter=adapter,
            query=envelope, idempotency_key=key, retrieved_at=RETRIEVED_AT, **kwargs,
        )

    def test_the_single_query_runner_suspends_the_same_way(self):
        """Both of `run_research`'s suspend sites, which the batch tests cannot see.

        First the pre-egress suspend from a state the state-check transition
        advances — the site that passed a snapshot taken before that transition
        and so crashed. Then the auth-failure suspend on a replayed start,
        where the run is never advanced and the site that hardcoded
        `RESEARCH_RUNNING` is simply wrong.
        """

        self.park("research_incomplete")
        missing = CredentialStubAdapter(present=False)
        with self.assertRaises(CredentialRequiredError) as captured:
            self.single(missing, "single-incomplete")
        self.assertEqual(missing.calls, [])
        self.assertEqual(captured.exception.gate.return_state, RunState.RESEARCH_RUNNING)
        self.assertEqual(captured.exception.gate.suspended_state, RunState.RESEARCH_RUNNING)

        self.reset()
        key = "single-replayed"
        self.seed_start_record(key)
        self.park("research_incomplete")
        rejected = CredentialStubAdapter(
            present=True, results={"센서": failure(AdapterFailureKind.AUTH)},
        )
        with self.assertRaises(CredentialRequiredError) as captured:
            self.single(rejected, key)
        self.assertEqual(rejected.calls, ["센서"])
        self.assertEqual(
            captured.exception.gate.return_state, RunState.RESEARCH_INCOMPLETE,
        )

    def test_auth_failure_suspend_returns_to_the_state_it_suspended(self):
        """The other two suspend sites: the adapter rejected the credential.

        Reached only after the execution loop, so they must re-read the state
        rather than reuse the pre-loop value — the `research.start` transition
        below the guard may have advanced the run since.

        The REPLAYED case is the one that distinguishes the fix. These two
        sites used to hardcode `RESEARCH_RUNNING`, which is correct whenever
        that transition fired — so a fresh-key test passes in both worlds and
        proves nothing. On the replayed path the run is never advanced, and the
        hardcoded value is simply wrong.
        """

        auth_failed = {
            term: failure(AdapterFailureKind.AUTH)
            for term in ("센서", "감지기", "sensor")
        }
        for entry, replayed in (("research_running", False), ("research_incomplete", True)):
            with self.subTest(entry=entry, replayed=replayed):
                self.reset()
                key = f"auth-failure-{entry}"
                if replayed:
                    self.seed_start_record(key)
                    self.park(entry)
                adapter = CredentialStubAdapter(present=True, results=auth_failed)
                with self.assertRaises(CredentialRequiredError) as captured:
                    self.batch(adapter, key)
                gate = captured.exception.gate
                self.assertEqual(gate.return_state.value, entry)
                self.assertEqual(gate.suspended_state.value, entry)
                self.assertEqual(adapter.calls, ["센서"])


class AnchorReachabilityTests(GuardTestCase):
    """T-U1 — the anchor is evaluated from EVERY credentialed entry state.

    Two parametrizations, one per path, and neither alone is sufficient.
    M-1 (the lock on the whole change): revert the shared helper to
    `prior.state is RunState.RESEARCH_RUNNING` and both holed-state tests fail
    — all three fresh-key cases and all six replayed-key cases — along with
    T-I1 and T-I2.

    The other two tests here survive that mutation ON PURPOSE and must not be
    counted as locks. One asserts a TRANSITION-LAYER refusal, which is
    unaffected by the guard predicate and is recorded precisely so that a
    relaxation there surfaces as a failure. The other is the no-anchor control,
    which the reverted predicate covers identically because there is nothing
    for the guard to find.
    """

    def seed_live_anchor(self, entry):
        seed_re_research_reentry(self.connection, "run")
        self.park(entry)

    def test_fresh_key_holed_states_are_force_gated_with_no_egress(self):
        """The three states that egress every term at HEAD on a fresh key.

        The other three of B-11's six are absent here on purpose: on a fresh
        key `_validate_direct_transition` refuses any transition OUT of a gate
        state, so they never reach the guard at all. That refusal is an
        accident of the transition layer, not a property of this guard — which
        is exactly why the replayed-key parametrization below exists.
        """

        for entry in FRESH_KEY_HOLED:
            with self.subTest(entry=entry):
                self.reset()
                self.seed_live_anchor(entry)
                adapter = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
                with self.assertRaises(CredentialRequiredError) as captured:
                    self.batch(adapter, f"holed-{entry}")
                self.assertEqual(adapter.calls, [])
                scope = captured.exception.gate.approval_scope
                self.assertEqual(scope["plan_hash"], digest(BOUND_PLAN))
                self.assertEqual(scope["re_research_decision_id"], "gd_seeded_re_research")
                self.assertIn(
                    ":re_research:gd_seeded_re_research",
                    captured.exception.gate.suspended_operation,
                )

    def test_gate_states_are_refused_at_the_transition_layer_on_a_fresh_key(self):
        """Asserted explicitly so a relaxation of that layer surfaces as a failure.

        This is NOT guard coverage and must not be allowed to stand in for it:
        the very next test is the path where this refusal does not run.
        """

        for entry in ("domain_pivot_required", "coverage_insufficient", "decision_required"):
            with self.subTest(entry=entry):
                self.reset()
                self.seed_live_anchor(entry)
                adapter = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
                with self.assertRaises(GateMismatchError):
                    self.batch(adapter, f"gate-state-{entry}")
                self.assertEqual(adapter.calls, [])

    def test_replayed_start_holed_states_never_egress(self):
        """All SIX non-running states, which only this path can reach.

        A replayed `research.start` early-returns before the gate-state check,
        so the run is never advanced and every one of the six arrives at the
        guard. At HEAD all six egress every term and only then raise, at the
        publish, with the credential already spent.

        The outcome after the fix is NOT uniform, and asserting a uniform
        force-gate would be wrong: a force-gate needs `suspend_gate` to
        succeed, and here `current_state` is the UN-ADVANCED entry state. Two
        of the six can carry a credential gate; the other four are refused
        before egress by the Decision-3 legality check. Both halves assert
        `adapter.calls == []`, which is the security claim.
        """

        for entry in REPLAYED_KEY_HOLED:
            with self.subTest(entry=entry):
                self.reset()
                key = f"replayed-anchor-{entry}"
                self.seed_live_anchor(entry)
                # Seed the start record at BOTH keys, because the two worlds
                # look up different ones and a single seeding silently degrades
                # the case. The salt reassigns `idempotency_key` BEFORE the
                # state-check transition reads it, so:
                #   * the BARE key is what the mutant (reverted predicate, no
                #     salt) finds — it replays, stays un-advanced and egresses
                #     three terms, which is M-1's sensitivity for the three
                #     gate states;
                #   * the SALTED key is what the fixed code finds — it replays,
                #     stays un-advanced, and exercises the split below.
                # The salted key is DERIVED from the run's real anchor, not
                # written out as a literal: a literal that stops matching what
                # the guard derives would seed a record nothing looks up, and
                # every case here would quietly become a Path-1 test wearing a
                # Path-2 label. Asserting the two keys differ is what actually
                # detects that — re-reading a row we just inserted proves
                # nothing.
                salted = salted_reentry_key(key, re_research_reentry_anchor(self.connection, "run"))
                self.assertNotEqual(
                    salted, key,
                    "the salt did not change the key, so both worlds look up the same record "
                    "and this case no longer distinguishes the paths",
                )
                self.seed_start_record(key)
                self.seed_start_record(salted)
                adapter = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
                if entry in CREDENTIAL_SUSPENDABLE:
                    with self.assertRaises(CredentialRequiredError) as captured:
                        self.batch(adapter, key)
                    self.assertEqual(captured.exception.gate.return_state.value, entry)
                else:
                    with self.assertRaises(LiveResearchReentrySpentCoordinateError) as caught:
                        self.batch(adapter, key)
                    self.assertEqual(
                        caught.exception.code,
                        "live_research_reentry_spent_coordinate_issue_48",
                    )
                self.assertEqual(adapter.calls, [])

    def test_research_ready_without_an_anchor_is_the_byte_identity_control(self):
        """`research_ready` appears in two roles; they must not be conflated.

        WITH a live anchor it is a holed state that must force-gate (above).
        WITHOUT one it is the only entry state whose state-check transition is
        skipped, and the control every "first passes are untouched" claim rests
        on — no salt, no re-entry scope fields, and the pre-#48 key.
        """

        adapter = CredentialStubAdapter(present=False)
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(adapter, "control-first-pass")
        gate = captured.exception.gate
        self.assertEqual(gate.suspended_operation, "research.execute:control-first-pass")
        self.assertNotIn("re_research", gate.suspended_operation)
        self.assertNotIn("plan_hash", gate.approval_scope)
        self.assertEqual(gate.return_state, RunState.RESEARCH_READY)


class IncompleteRetryTests(GuardTestCase):
    """T-I1 / T-I2 / T-I4 — the reported defect and its first-pass control."""

    def incomplete_second_pass(self, key="pass-two"):
        """Force-gate, approve, and run an authorized pass whose terms all fail."""

        seed_re_research_reentry(self.connection, "run")
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(CredentialStubAdapter(present=True), key)
        decision = self.approve(captured.exception.gate)
        outcome = self.batch(
            CredentialStubAdapter(present=True, results=ALL_FAIL), key,
            credential_decision_id=decision.decision_id,
        )
        self.assertEqual(outcome.next_state, RunState.RESEARCH_INCOMPLETE.value)
        self.assertEqual(self.store.snapshot("run").state, RunState.RESEARCH_INCOMPLETE)
        # The anchor is still live: nothing published `research_complete`.
        self.assertIsNotNone(re_research_reentry_anchor(self.connection, "run"))
        return decision

    def test_incomplete_retry_with_new_terms_is_force_gated(self):
        """T-I1, the reported bypass (B-1).

        At HEAD this retry egressed all three unapproved terms with the
        credential in env, reached `research_complete`, and wrote its store
        rows in pass 1's UNSALTED namespace — no gate, no salt, no operator
        approval of plan or terms.
        """

        self.incomplete_second_pass()
        retry = CredentialStubAdapter(present=True, results={
            "센서": success("10-2026-9990001"), "탐지기": success("10-2026-9990002"),
            "detector": success("10-2026-9990003"),
        })
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(retry, "retry-new-terms", terms=(("탐지기",), ("detector",)))
        self.assertEqual(retry.calls, [])
        scope = captured.exception.gate.approval_scope
        self.assertEqual(
            sorted(
                field for field in (
                    "needed_research", "plan", "plan_hash",
                    "re_research_decision_id", "second_pass_terms",
                ) if field in scope
            ),
            ["needed_research", "plan", "plan_hash", "re_research_decision_id",
             "second_pass_terms"],
        )
        self.assertEqual(scope["second_pass_terms"], ["센서", "탐지기", "detector"])
        unsalted = self.connection.execute(
            "SELECT count(*) FROM research_operations "
            "WHERE idempotency_key LIKE 'retry-new-terms%' AND idempotency_key NOT LIKE '%:re_research:%'"
        ).fetchone()[0]
        self.assertEqual(unsalted, 0)

    def test_incomplete_retry_without_the_credential_still_gets_the_plan_bound_scope(self):
        """T-I2 (B-2): the bypass also stripped the plan binding from the scope.

        `_reentry_scope_fields` returns `{}` for a `None` anchor, so a retry
        evaluated without one could not carry `plan_hash`, `plan`,
        `needed_research`, `second_pass_terms` or `re_research_decision_id`
        however the gate was raised. Only observable once the suspend itself
        stops crashing, which is why this is asserted here and not at HEAD.
        """

        self.incomplete_second_pass()
        adapter = CredentialStubAdapter(present=False)
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(adapter, "retry-no-credential", terms=(("탐지기",), ("detector",)))
        self.assertEqual(adapter.calls, [])
        scope = captured.exception.gate.approval_scope
        self.assertEqual(scope["plan"], BOUND_PLAN)
        self.assertEqual(scope["plan_hash"], digest(BOUND_PLAN))
        self.assertEqual(scope["needed_research"], BOUND_PLAN["needed_research"])
        self.assertEqual(scope["re_research_decision_id"], "gd_seeded_re_research")
        self.assertEqual(scope["second_pass_terms"], ["센서", "탐지기", "detector"])

    def test_first_incomplete_pass_without_a_re_research_decision_retries_unchanged(self):
        """T-I4 / AC-5 (B-6): no re_research history means no guard, at all.

        The anchor query returns `None` before any other work when no
        `re_research` decision exists, so a first pass that ends
        `RESEARCH_INCOMPLETE` retries with byte-identical keys and no gate —
        exactly as it did before issue #48 and before this change.
        """

        first = self.batch(CredentialStubAdapter(present=True, results=ALL_FAIL), "first-pass")
        self.assertEqual(first.next_state, RunState.RESEARCH_INCOMPLETE.value)
        self.assertIsNone(re_research_reentry_anchor(self.connection, "run"))
        retry = CredentialStubAdapter(present=True, results={
            "센서": success("10-2026-8880001"), "탐지기": success("10-2026-8880002"),
            "detector": success("10-2026-8880003"),
        })
        result = self.batch(retry, "first-pass-retry", terms=(("탐지기",), ("detector",)))
        self.assertEqual(result.next_state, RunState.RESEARCH_COMPLETE.value)
        self.assertEqual(retry.calls, ["센서", "탐지기", "detector"])
        keys = sorted(
            row[0] for row in self.connection.execute(
                "SELECT idempotency_key FROM research_operations "
                "WHERE idempotency_key LIKE 'first-pass-retry%'"
            )
        )
        self.assertEqual(keys, [f"first-pass-retry:q{index:02d}" for index in range(3)])

    def test_coverage_expand_reentry_inside_a_live_re_research_era_is_force_gated(self):
        """T-I5 — INTENT DOCUMENTATION, deliberately NOT a mutation lock.

        A COVERAGE-expand resolution lands the run at `research_running`
        (`gate_action_target`), which is the one state the pre-change predicate
        already covered — so this passes identically with the fix and with the
        mutation, and counting it as a lock would be self-deception. It is kept
        because the widened surface is an intended consequence a reader should
        see stated: inside a live `re_research` era, a downstream re-entry is
        force-gated and salted under that anchor.
        """

        seed_re_research_reentry(self.connection, "run")
        self.park("research_running")
        adapter = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
        with self.assertRaises(CredentialRequiredError):
            self.batch(adapter, "coverage-expand")
        self.assertEqual(adapter.calls, [])

    def test_decision_required_reentry_inside_a_live_re_research_era_is_force_gated(self):
        """T-I6 — INTENT DOCUMENTATION, deliberately NOT a mutation lock.

        Same reasoning as T-I5: `(POST_AUDIT_CHECKPOINT, "re_research")` also
        targets `research_running`, so resolving the gate lands on the covered
        state. The genuinely mutation-sensitive `decision_required` case is
        T-U1's replayed-key parametrization, where the run is never advanced.
        """

        seed_re_research_reentry(self.connection, "run")
        adapter = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
        with self.assertRaises(CredentialRequiredError):
            self.batch(adapter, "decision-required-reentry")
        self.assertEqual(adapter.calls, [])


class EscapeHatchTests(GuardTestCase):
    """T-I7 (Decision 6) — the widened refusals can never wedge a run."""

    def test_offline_pass_quiets_even_a_stale_anchor(self):
        """A stale binding refuses the live path; an offline pass clears it.

        `re_research_reentry_anchor` returns `None` on `published_since` BEFORE
        `validated_reentry_anchor` reaches its stale check, so publishing
        `research_complete` from an offline pass quiets even a stale anchor.
        That makes the recovery a documented, zero-egress operator action
        rather than a support ticket — and it doubles as a benign guard-disarm
        path, since it requires publishing real offline evidence.
        """

        seed_re_research_reentry(self.connection, "run", stale=1)
        refused = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
        with self.assertRaises(LiveResearchReentryRefusedError):
            self.batch(refused, "stale-live")
        self.assertEqual(refused.calls, [])

        record = {
            "canonical_url": "https://example.test/offline-recovery",
            "identifier": "offline-recovery-1", "title": "Offline recovery reference",
            "content_hash": digest("offline recovery content"), "language": "en",
            "provenance": "reviewed_import",
        }
        envelope = QueryEnvelope(
            run_id="run", adapter="manual_web", adapter_version="import-v1",
            capability="import", allowed_scheme="https", allowed_host="example.test",
            deadline_seconds=1, page=1, page_cap=1, result_budget=10, byte_budget=10_000,
            retry_budget=0, retry_ownership="research_runner",
            query_projection={"content_type": "application/json", "records": [record]},
        )
        offline = run_research(
            self.connection, run_root=self.root, run_id="run",
            adapter=ManualWebAdapter(("example.test",)), query=envelope,
            idempotency_key="offline-recovery", retrieved_at=RETRIEVED_AT,
        )
        self.assertEqual(offline.next_state, RunState.RESEARCH_COMPLETE.value)
        self.assertIsNone(re_research_reentry_anchor(self.connection, "run"))

        # The live path now proceeds normally: no anchor, so no guard.
        self.park("research_incomplete")
        live = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
        recovered = self.batch(live, "after-recovery")
        self.assertEqual(recovered.next_state, RunState.RESEARCH_COMPLETE.value)
        self.assertEqual(live.calls, ["센서", "감지기", "sensor"])


class GateSequenceTests(GuardTestCase):
    """T-U4 / T-I8 — a re-raised gate gets a distinct id, and only then."""

    def insert_decided_gate(self, gate_id):
        self.connection.execute(
            "INSERT INTO gate_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (gate_id, "run", "credential", "credential_required", "research_ready",
             "research.execute:seq", "0" * 64, "{}", digest({}), "research_ready",
             utc_now(), "decided"),
        )

    def test_gate_id_sequence_is_collision_only(self):
        """Three properties: zero diff without a collision, determinism, monotonicity.

        Idempotence is NOT one of them and must not be asserted: every
        `suspend_gate` call inserts a row, so a second collision counts two
        rows and yields the NEXT sequence by design. What is guaranteed is that
        a given database state deterministically yields a given id — checked
        here by computing twice before inserting anything.
        """

        base = digest({"gate": "sequence probe"})
        untouched = "ge_" + base[:20]
        self.assertEqual(self.store._sequenced_gate_id(base), untouched)
        self.assertEqual(self.store._sequenced_gate_id(base), untouched)

        self.insert_decided_gate(untouched)
        first_collision = self.store._sequenced_gate_id(base)
        self.assertEqual(first_collision, f"{untouched}-2")
        self.assertEqual(self.store._sequenced_gate_id(base), first_collision)

        self.insert_decided_gate(first_collision)
        self.assertEqual(self.store._sequenced_gate_id(base), f"{untouched}-3")

        # Gap safety: monotonicity must not depend on the sequence being
        # dense. A row COUNT would return `-3` here — an id that already
        # exists — reinstating the raw IntegrityError this replaced.
        self.insert_decided_gate(f"{untouched}-3")
        self.connection.execute(
            "DELETE FROM gate_envelopes WHERE gate_id=?", (first_collision,),
        )
        self.assertEqual(self.store._sequenced_gate_id(base), f"{untouched}-4")

        # An unrelated base is unaffected: the sequence is per-coordinate.
        other = digest({"gate": "unrelated"})
        self.assertEqual(self.store._sequenced_gate_id(other), "ge_" + other[:20])

    def test_degrade_decision_allows_an_identical_gate_to_be_reraised(self):
        """T-I8 (B-5) end-to-end, through the route that killed the alternatives.

        `degrade` is a legal CREDENTIAL action that does not authorize, so the
        run returns to `research_running` with NOTHING published — which is why
        an "attempt ordinal counted from published finishes" could not
        disambiguate this, and why the sequence lives on the gate instead.
        """

        seed_re_research_reentry(self.connection, "run")
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(CredentialStubAdapter(present=True), "degrade-batch")
        first = captured.exception.gate
        self.approve(first, action="degrade")
        self.assertEqual(self.store.snapshot("run").state, RunState.RESEARCH_RUNNING)

        adapter = CredentialStubAdapter(present=True)
        with self.assertRaises(CredentialRequiredError) as recaptured:
            self.batch(adapter, "degrade-batch")
        second = recaptured.exception.gate
        self.assertEqual(adapter.calls, [])
        self.assertNotEqual(second.gate_id, first.gate_id)
        self.assertTrue(second.gate_id.startswith(f"{first.gate_id}-"))
        # The re-raised gate is resolvable, which returning the old envelope
        # could never be: gate_decisions.gate_id is NOT NULL UNIQUE.
        self.approve(second)


class SpentCoordinateTests(GuardTestCase):
    """T-I3 / T-I3b — both sides of Decision 4b's scoping.

    M-3 removes the refusal and T-I3 goes red on the replayed bundle; M-3b
    widens it to the bare "a finish record exists" predicate and T-I3b goes red
    because the designed replay is refused. They bracket the scoping.
    """

    def published_second_pass_attempt(self, key="spent"):
        """One authorized second-pass attempt, published, anchor still live."""

        seed_re_research_reentry(self.connection, "run")
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(CredentialStubAdapter(present=True), key)
        decision = self.approve(captured.exception.gate)
        # All terms fail, so the attempt publishes RESEARCH_INCOMPLETE and no
        # `research_complete` quiets the anchor — the retry window this whole
        # change is about.
        published = self.batch(
            CredentialStubAdapter(present=True, results=ALL_FAIL), key,
            credential_decision_id=decision.decision_id,
        )
        self.assertEqual(published.next_state, RunState.RESEARCH_INCOMPLETE.value)
        return decision, published

    def current_bundle(self):
        row = self.connection.execute(
            "SELECT ar.revision_id, ar.content_json FROM artifact_revisions ar "
            "JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='research_bundle'",
        ).fetchone()
        return row["revision_id"], row["content_json"]

    def test_spent_attempt_coordinate_is_refused_before_any_egress(self):
        """T-I3 (B-8): the retry must not silently inherit attempt 1's bundle.

        The defect being pinned is a SILENCE. Left alone, this retry does not
        fail — it fetches fresh evidence, then `_published_replay` hands back
        attempt 1's bundle before the consumed-decision validation ever runs.
        The run reports the old incomplete outcome, the credential is spent,
        and the decision is left consumed-but-unused. So the assertions here
        are about the PUBLISHED BUNDLE, not merely about an exception being
        raised.

        The operator's way forward is a fresh attempt key. PR-B will mint one
        for them, at which point this test flips to "publishes its own bundle"
        and keeps exactly these content assertions.
        """

        _decision, published = self.published_second_pass_attempt()
        before_revision, before_content = self.current_bundle()
        self.assertEqual(before_revision, published.artifact_revision_id)
        self.assertEqual(published.bundle["evidence"], [])
        before_run = tuple(self.connection.execute(
            "SELECT state, state_version FROM runs WHERE run_id='run'",
        ).fetchone())
        decisions_before = self.connection.execute(
            "SELECT count(*) FROM gate_decisions WHERE run_id='run'"
        ).fetchone()[0]

        retry = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
        with self.assertRaises(LiveResearchReentrySpentCoordinateError) as caught:
            self.batch(retry, "spent")
        self.assertEqual(
            caught.exception.code, "live_research_reentry_spent_coordinate_issue_48",
        )
        self.assertEqual(retry.calls, [])
        # Attempt 1's bundle was never handed back as this attempt's result,
        # and nothing new was published at the coordinate.
        self.assertEqual(self.current_bundle(), (before_revision, before_content))
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM idempotency_records WHERE run_id='run' "
                "AND operation LIKE 'research.execute:%'"
            ).fetchone()[0],
            1,
        )
        # Refused before any egress AND before any state mutation: the refusal
        # sits above the `research.start` state-check transition and above the
        # request revision, so a turned-away attempt writes nothing. Sensitive
        # because the run is at `research_incomplete`, where that transition
        # would otherwise fire.
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT state, state_version FROM runs WHERE run_id='run'",
            ).fetchone()),
            before_run,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM idempotency_records WHERE run_id='run' "
                "AND operation='research.start'"
            ).fetchone()[0],
            0,
        )
        # Refused ABOVE the consume: no decision was spent by the refused attempt.
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM gate_decisions WHERE run_id='run'"
            ).fetchone()[0],
            decisions_before,
        )

    def test_completed_attempt_still_replays_for_its_own_decision(self):
        """T-I3b — the anti-blanket-refusal lock (B-10).

        Re-running a published credentialed attempt with THE DECISION THAT
        PRODUCED IT is a supported path: the `used_at` branch matches the
        record against `consumed_by_event_id`, the store replays every page
        with zero transport, and the finish replays the same bundle. A refusal
        keyed on "a record exists at the coordinate" alone would kill it.
        """

        decision, published = self.published_second_pass_attempt(key="replayable")
        adapter = CredentialStubAdapter(present=True, results=ALL_FAIL)
        replayed = self.batch(
            adapter, "replayable", credential_decision_id=decision.decision_id,
        )
        self.assertEqual(adapter.calls, [])
        self.assertEqual(replayed.artifact_revision_id, published.artifact_revision_id)
        self.assertEqual(replayed.bundle["evidence"], published.bundle["evidence"])

    def test_a_fresh_attempt_key_clears_the_refusal(self):
        """The documented recovery, asserted rather than left to the docs.

        `--idempotency-key` mints a fresh coordinate while leaving the anchor
        live, so the retry is still force-gated and still salted — the friction
        is a usability cost, not a way around the guard.
        """

        self.published_second_pass_attempt()
        adapter = CredentialStubAdapter(present=True, results=ALL_SUCCEED)
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(adapter, "spent-retry-2")
        self.assertEqual(adapter.calls, [])
        self.assertIn(
            ":re_research:gd_seeded_re_research",
            captured.exception.gate.suspended_operation,
        )


if __name__ == "__main__":
    unittest.main()
