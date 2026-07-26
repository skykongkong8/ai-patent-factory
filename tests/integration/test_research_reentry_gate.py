"""Issue #48: the force-gated, plan-bound, salted live second research pass.

Two complementary layers, both load-bearing:

* Option X force-gate — a `re_research` re-entry with no fresh credential
  decision raises a CREDENTIAL gate even when the credential is in env, so the
  operator approves the plan binding and literal terms before ANY egress.
* Anchor-keyed salt — the force-gate's `:credential:{decision_id}` suffix
  differentiates only the STORE key, while `credential_operation` and the
  finish transition use the bare key and `_published_replay` early-returns
  before the consumed-decision validation. The salt shifts that whole finish
  coordinate, so a same-term pass-2 publishes its OWN bundle instead of
  silently replaying pass-1's. The finish-collision test here FAILS if the
  salt is removed — it is the mutation guard that keeps it.

The real-pipeline twins (clean audit -> checkpoint -> re_research ->
force-gate) live in test_g010_checkpoint; this module seeds the durable shape
`resolve_gate` leaves behind so the gate/salt mechanics are testable in
isolation, mirroring `publish_gate_resolution`'s persistence exactly: the plan
lives ONLY in the gate_resolution artifact, reachable via
`idempotency_records.operation == "gate.resolve:{gate_id}"`.
"""
import json
import tempfile
import unittest
from pathlib import Path

from patent_factory.adapters.manual_web import ManualWebAdapter
from patent_factory.database import InjectedFailure, connect_database, utc_now
from patent_factory.models import QueryEnvelope, RunState
from patent_factory.provenance import digest
from patent_factory.research import (
    CredentialRequiredError,
    LiveResearchReentryPlanMismatchError,
    LiveResearchReentryPlanUnboundError,
    LiveResearchReentryRefusedError,
    re_research_reentry_anchor,
    run_research,
    run_research_batch,
    salted_reentry_key,
    validated_reentry_anchor,
)
from patent_factory.state import StateStore
from tests.integration.test_g009_research_batch import plan, ready, success

RETRIEVED_AT = "2026-01-01T00:00:00Z"
BOUND_PLAN = {"needed_research": ["broader prior-art sweep for the sensor mechanism"]}


def seed_re_research_reentry(
    connection, run_id, *, plan_content=None, with_resolution_artifact=True,
    plan_hash=None, stale=0,
):
    """Persist the durable shape a `re_research` resolution leaves behind.

    Mirrors `publish_gate_resolution`: a decided checkpoint gate, its
    re_research decision, the resolution artifact carrying `plan`/`plan_hash`,
    and the idempotency record that makes the artifact reachable by
    `gate.resolve:{gate_id}` — then parks the run at research_running.
    `utc_now()` (microsecond-granular) keeps the decision strictly later than
    any research_complete the caller published before seeding.
    """

    now = utc_now()
    gate_id, decision_id = "ge_seeded_re_research", "gd_seeded_re_research"
    connection.execute(
        "INSERT INTO gate_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            gate_id, run_id, "post_audit_checkpoint", "decision_required", "audit_running",
            "audit.finalize:seeded", "0" * 64, "{}", digest({}), "audit_running", now, "decided",
        ),
    )
    connection.execute(
        "INSERT INTO gate_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision_id, gate_id, run_id, "re_research", "inventor", "0" * 64, digest({}),
            "audit.finalize:seeded", "research_running", "second research pass requested",
            now, stale, None, None, None,
        ),
    )
    if with_resolution_artifact:
        resolved_plan = dict(plan_content if plan_content is not None else BOUND_PLAN)
        content = {
            "action": "re_research", "decision_id": decision_id, "gate_id": gate_id,
            "plan": resolved_plan,
            "plan_hash": plan_hash if plan_hash is not None else digest(resolved_plan),
            "run_id": run_id, "version": "decision-set-v1",
        }
        revision = StateStore(connection).add_revision(
            run_id, "gate_resolution", content, schema_version="decision-set-v1",
        )
        event_id = "te_" + digest({"seeded_resolution": gate_id, "at": now})[:20]
        connection.execute(
            "INSERT INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                event_id, run_id, "inventor", "decision_required", "research_running",
                "seeded re_research resolution", "[]", revision.revision_id, now,
            ),
        )
        connection.execute(
            "INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)",
            (
                run_id, f"gate.resolve:{gate_id}", "seeded-decision-input", event_id,
                revision.revision_id, "research_running", now,
            ),
        )
    connection.execute(
        "UPDATE runs SET state='research_running', state_version=state_version+1, updated_at=? WHERE run_id=?",
        (now, run_id),
    )
    return gate_id, decision_id


class CredentialStubAdapter:
    """Credential-surface stand-in whose per-term results are injectable."""

    name = "kipris"
    version = "plus-xml-v1"
    credential_name = "KIPRIS_PLUS_API_KEY"
    requires_credential = True

    def __init__(self, *, present, results=None):
        self.credential_present = present
        self.results = results or {}
        self.calls = []

    def search(self, envelope):
        term = envelope.query_projection["word"]
        self.calls.append(term)
        return self.results[term]


class ReentryAnchorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.store = ready(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_anchor_derives_gate_decision_plan_and_hash_from_state(self):
        gate_id, decision_id = seed_re_research_reentry(self.connection, "run")
        anchor = re_research_reentry_anchor(self.connection, "run")
        self.assertEqual(
            (anchor.gate_id, anchor.decision_id, anchor.stale, anchor.plan, anchor.plan_hash),
            (gate_id, decision_id, False, BOUND_PLAN, digest(BOUND_PLAN)),
        )
        self.assertIsNotNone(validated_reentry_anchor(self.connection, "run"))

    def test_anchor_is_none_after_a_later_research_complete(self):
        seed_re_research_reentry(self.connection, "run")
        self.connection.execute(
            "INSERT INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "te_cycle_back_publish", "run", "research-cli", "research_running",
                RunState.RESEARCH_COMPLETE.value, "second pass published", "[]", None, utc_now(),
            ),
        )
        self.assertIsNone(re_research_reentry_anchor(self.connection, "run"))
        self.assertIsNone(validated_reentry_anchor(self.connection, "run"))

    def test_stale_binding_is_refused_with_the_repurposed_code(self):
        seed_re_research_reentry(self.connection, "run", stale=1)
        with self.assertRaises(LiveResearchReentryRefusedError) as caught:
            validated_reentry_anchor(self.connection, "run")
        self.assertEqual(caught.exception.code, "live_research_reentry_refused_issue_48")

    def test_missing_resolution_artifact_is_plan_unbound(self):
        seed_re_research_reentry(self.connection, "run", with_resolution_artifact=False)
        with self.assertRaises(LiveResearchReentryPlanUnboundError) as caught:
            validated_reentry_anchor(self.connection, "run")
        self.assertEqual(caught.exception.code, "live_research_reentry_plan_unbound_issue_48")

    def test_empty_valued_plan_is_plan_unbound(self):
        # {"needed_research": []} is the shape deleting the TODO entry but
        # keeping the key produces — bounded only by a bare-truthiness bug.
        seed_re_research_reentry(self.connection, "run", plan_content={"needed_research": []})
        with self.assertRaises(LiveResearchReentryPlanUnboundError):
            validated_reentry_anchor(self.connection, "run")

    def test_plan_not_reproducing_its_hash_is_a_mismatch(self):
        seed_re_research_reentry(self.connection, "run", plan_hash="0" * 64)
        with self.assertRaises(LiveResearchReentryPlanMismatchError) as caught:
            validated_reentry_anchor(self.connection, "run")
        self.assertEqual(caught.exception.code, "live_research_reentry_plan_mismatch_issue_48")

    def test_salt_is_deterministic_and_idempotent_across_derived_keys(self):
        gate_id, decision_id = seed_re_research_reentry(self.connection, "run")
        anchor = re_research_reentry_anchor(self.connection, "run")
        salted = salted_reentry_key("serpapi-abc123", anchor)
        self.assertEqual(salted, f"serpapi-abc123:re_research:{decision_id}")
        # Applying the salt again — directly or after a -rN retry suffix was
        # appended downstream — must not stack a second salt.
        self.assertEqual(salted_reentry_key(salted, anchor), salted)
        self.assertEqual(salted_reentry_key(f"{salted}-r2", anchor), f"{salted}-r2")


class ForceGateAndSaltTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.store = ready(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def batch(self, adapter, key, **kwargs):
        return run_research_batch(
            self.connection, run_root=self.root, run_id="run", adapter=adapter,
            queries=plan(), idempotency_key=key, retrieved_at=RETRIEVED_AT, **kwargs,
        )

    def approve(self, gate):
        decision, _ = self.store.decide_gate(
            gate.gate_id, action="configure_and_verify", actor="user", reason="approved",
            subject_revision_hash=gate.subject_revision_hash,
            approval_scope=dict(gate.approval_scope),
            suspended_operation=gate.suspended_operation,
            return_state=gate.return_state,
        )
        return decision

    def gated_first_pass(self, key="collision-batch"):
        """Suspend -> approve -> resume one credentialed pass-1; return (D1, run)."""

        missing = CredentialStubAdapter(present=False)
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(missing, key)
        self.assertEqual(missing.calls, [])
        decision = self.approve(captured.exception.gate)
        configured = CredentialStubAdapter(present=True, results={
            "센서": success(), "감지기": success(), "sensor": success(),
        })
        completed = self.batch(configured, key, credential_decision_id=decision.decision_id)
        self.assertEqual(completed.next_state, RunState.RESEARCH_COMPLETE.value)
        return decision, completed

    def test_env_credential_second_pass_is_suspended_not_executed(self):
        """The Option-X mutation lock (pre-mortem #4): reverting the force-gate
        to the `if not credential_present`-only suspend FAILS here — the key is
        in env and the pass must still suspend, executing nothing."""

        seed_re_research_reentry(self.connection, "run")
        adapter = CredentialStubAdapter(present=True)
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(adapter, "second-pass")
        self.assertEqual(adapter.calls, [])
        gate = captured.exception.gate
        self.assertEqual(gate.approval_scope["plan_hash"], digest(BOUND_PLAN))
        self.assertEqual(gate.approval_scope["needed_research"], BOUND_PLAN["needed_research"])
        self.assertEqual(gate.approval_scope["second_pass_terms"], ["센서", "감지기", "sensor"])
        self.assertEqual(gate.approval_scope["re_research_decision_id"], "gd_seeded_re_research")
        self.assertEqual(self.store.snapshot("run").state, RunState.CREDENTIAL_REQUIRED)
        # The suspended operation carries the SALTED key: the finish/consume
        # coordinate is shifted before the operation is derived.
        self.assertEqual(
            gate.suspended_operation,
            "research.execute:second-pass:re_research:gd_seeded_re_research",
        )

    def test_first_pass_credential_scope_is_byte_identical_to_pre_48(self):
        missing = CredentialStubAdapter(present=False)
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(missing, "first-pass")
        scope = captured.exception.gate.approval_scope
        self.assertEqual(
            sorted(scope),
            [
                "adapter", "adapter_version", "allowed_host", "auth_attempt", "capability",
                "credential_name", "effective_pages", "max_requests", "query_count",
                "request_fingerprint", "result_budget",
            ],
        )
        self.assertEqual(
            captured.exception.gate.suspended_operation, "research.execute:first-pass",
        )

    def test_finish_collision_same_term_pass_two_publishes_its_own_bundle(self):
        """BLOCKING salt-mutation guard (pre-mortem #5): remove the salt and a
        gated pass-1 + force-gated same-term pass-2 share the bare-key finish
        coordinate, so pass-2's finish would `_published_replay` pass-1's
        bundle — same artifact revision back, D2 consumed-but-unused. Every
        assertion after the resume fails in that world."""

        _first_decision, first = self.gated_first_pass()
        seed_re_research_reentry(self.connection, "run")

        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(CredentialStubAdapter(present=True), "collision-batch")
        second_decision = self.approve(captured.exception.gate)
        second_adapter = CredentialStubAdapter(present=True, results={
            "센서": success("10-2026-7770001"), "감지기": success("10-2026-7770002"),
            "sensor": success("10-2026-7770003"),
        })
        second = self.batch(
            second_adapter, "collision-batch",
            credential_decision_id=second_decision.decision_id,
        )
        self.assertEqual(second.next_state, RunState.RESEARCH_COMPLETE.value)
        self.assertFalse(second.replayed)
        self.assertNotEqual(second.artifact_revision_id, first.artifact_revision_id)
        self.assertNotEqual(second.transition_event_ids[-1], first.transition_event_ids[-1])
        # Pass-2 actually fetched fresh evidence (credential spent AND used),
        # and its own bundle carries it.
        self.assertEqual(second_adapter.calls, ["센서", "감지기", "sensor"])
        second_locators = {item["source_locator"] for item in second.bundle["evidence"]}
        self.assertIn("kr-patent:1020267770001", second_locators)
        used = self.connection.execute(
            "SELECT used_at, consumed_by_event_id FROM gate_decisions WHERE decision_id=?",
            (second_decision.decision_id,),
        ).fetchone()
        self.assertTrue(used["used_at"])
        self.assertEqual(used["consumed_by_event_id"], second.transition_event_ids[-1])
        # Both finish coordinates persist side by side: the bare pass-1 key and
        # the salted pass-2 key — the store keys carry the salt AND the
        # `:credential:` suffix.
        salted = "collision-batch:re_research:gd_seeded_re_research"
        finish_rows = {
            (row["operation"], row["idempotency_key"])
            for row in self.connection.execute(
                "SELECT operation, idempotency_key FROM idempotency_records "
                "WHERE operation LIKE 'research.execute:%'",
            )
        }
        self.assertEqual(finish_rows, {
            ("research.execute:collision-batch", "collision-batch"),
            (f"research.execute:{salted}", salted),
        })
        store_keys = [
            row[0] for row in self.connection.execute(
                "SELECT idempotency_key FROM research_operations WHERE idempotency_key LIKE ?",
                (f"{salted}:credential:%",),
            )
        ]
        self.assertEqual(
            sorted(store_keys),
            [f"{salted}:credential:{second_decision.decision_id}:q{index:02d}" for index in range(3)],
        )

    def test_suspend_approve_resume_replays_with_zero_transport(self):
        """Crash-after-execute, before-publish: the re-resume replays every
        recorded page with zero transport and lands the finish on the same
        salted coordinate — the resume is idempotent across the store AND
        finish coordinates."""

        seed_re_research_reentry(self.connection, "run")
        with self.assertRaises(CredentialRequiredError) as captured:
            self.batch(CredentialStubAdapter(present=True), "resume-batch")
        decision = self.approve(captured.exception.gate)
        adapter = CredentialStubAdapter(present=True, results={
            "센서": success(), "감지기": success(), "sensor": success(),
        })
        with self.assertRaises(InjectedFailure):
            self.batch(
                adapter, "resume-batch",
                credential_decision_id=decision.decision_id, fault_at="before_export",
            )
        self.assertEqual(adapter.calls, ["센서", "감지기", "sensor"])
        self.assertEqual(self.store.snapshot("run").state, RunState.RESEARCH_RUNNING)

        resumed = self.batch(
            adapter, "resume-batch", credential_decision_id=decision.decision_id,
        )
        # Zero new transport: all three pages replayed from research_operations
        # under the salted `:credential:` store keys.
        self.assertEqual(adapter.calls, ["센서", "감지기", "sensor"])
        self.assertEqual(resumed.next_state, RunState.RESEARCH_COMPLETE.value)
        salted = "resume-batch:re_research:gd_seeded_re_research"
        finish = self.connection.execute(
            "SELECT idempotency_key FROM idempotency_records WHERE operation=?",
            (f"research.execute:{salted}",),
        ).fetchone()
        self.assertEqual(finish["idempotency_key"], salted)

    def test_offline_second_pass_is_not_force_gated_and_publishes(self):
        """The shipped #12 offline behavior survives: the guard lives inside
        the requires_credential branch, so an offline re-entry runs to
        publish with no gate and no salt."""

        seed_re_research_reentry(self.connection, "run")
        record = {
            "canonical_url": "https://example.test/offline-second-pass",
            "identifier": "offline-second-pass-1", "title": "Offline second-pass reference",
            "content_hash": digest("offline second pass content"), "language": "en",
            "provenance": "reviewed_import",
        }
        envelope = QueryEnvelope(
            run_id="run", adapter="manual_web", adapter_version="import-v1", capability="import",
            allowed_scheme="https", allowed_host="example.test", deadline_seconds=1,
            page=1, page_cap=1, result_budget=10, byte_budget=10_000, retry_budget=0,
            retry_ownership="research_runner",
            query_projection={"content_type": "application/json", "records": [record]},
        )
        result = run_research(
            self.connection, run_root=self.root, run_id="run",
            adapter=ManualWebAdapter(("example.test",)), query=envelope,
            idempotency_key="offline-second-pass", retrieved_at=RETRIEVED_AT,
        )
        self.assertEqual(result.next_state, RunState.RESEARCH_COMPLETE.value)
        salted_rows = self.connection.execute(
            "SELECT count(*) FROM research_operations WHERE idempotency_key LIKE '%:re_research:%'"
        ).fetchone()[0]
        self.assertEqual(salted_rows, 0)


if __name__ == "__main__":
    unittest.main()
