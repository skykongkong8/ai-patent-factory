"""RALPLAN-DR P0: the byte-identity baselines four acceptance criteria diff against.

AC-1c, AC-2, AC-6 and AC-8b all compare recorded pre-change values, and the
plan makes that a merge gate — so the values are captured here as a committed
fixture rather than as numbers in a review comment. Recorded at `0d0aedc`
(issue-48 as shipped), before any RALPLAN-DR source edit.

The three observables and why each is load-bearing:

* **first-pass credential gate** — the control for every "the guard must not
  touch a first pass" claim. Its `gate_id` folds in `return_state`
  (`state.py`'s digest), so this is also the AC-1c lock: P1 changes
  `return_state` only on paths that CRASH today, and `research_ready` skips
  the state-check transition entirely, so this id must not move.
* **second-pass attempt 1** — the salted keys, suspended operation and store
  coordinates shipped by issue #48. `salted_reentry_key` gets zero diff in
  this plan, so these must not move either.
* **serpapi derived keys** — AC-8b is output equivalence, not code-path
  equivalence: PR-B deliberately changes WHERE the key is derived, so the test
  is that the same inputs still yield the same `-rN` ladder and the same
  decision-bound resume key. Derived by calling the key functions against a
  bare table, which is exactly the surface AC-8b names.

The credential decision id is minted with a timestamp, so every coordinate
carrying it is normalised to the literal `{credential_decision_id}` — the
shape is the invariant, not the nonce.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from patent_factory.cli import _serpapi_decision_key, _serpapi_idempotency_key
from patent_factory.database import connect_database
from patent_factory.models import RunState
from patent_factory.research import CredentialRequiredError, run_research_batch
from tests.integration.test_g009_research_batch import plan, ready, success
from tests.integration.test_research_reentry_gate import (
    CredentialStubAdapter,
    seed_re_research_reentry,
)

BASELINE = Path(__file__).resolve().parent / "baselines" / "reentry_byte_identity.json"
RETRIEVED_AT = "2026-01-01T00:00:00Z"
ALL_SUCCEED = {"센서": success(), "감지기": success(), "sensor": success()}


def _batch(root, connection, adapter, key, **kwargs):
    return run_research_batch(
        connection, run_root=root, run_id="run", adapter=adapter, queries=plan(),
        idempotency_key=key, retrieved_at=RETRIEVED_AT, **kwargs,
    )


def _gate_record(gate):
    return {
        "approval_scope_hash": gate.approval_scope_hash,
        "gate_id": gate.gate_id,
        "return_state": gate.return_state.value,
        "scope_keys": sorted(gate.approval_scope),
        "suspended_operation": gate.suspended_operation,
        "suspended_state": gate.suspended_state.value,
    }


def capture_first_pass(root, connection):
    """An ordinary first pass suspended for a missing credential."""

    adapter = CredentialStubAdapter(present=False)
    try:
        _batch(root, connection, adapter, "first-pass")
    except CredentialRequiredError as error:
        assert adapter.calls == []
        return _gate_record(error.gate)
    raise AssertionError("a first pass with no credential must suspend")


def capture_second_pass_attempt_one(root, connection):
    """Attempt 1 of an authorized second pass: force-gate, approve, resume."""

    store = ready(connection)
    seed_re_research_reentry(connection, "run")
    try:
        _batch(root, connection, CredentialStubAdapter(present=True), "attempt-one")
    except CredentialRequiredError as error:
        gate = error.gate
    else:
        raise AssertionError("an authorized second pass must force-gate")
    decision, _ = store.decide_gate(
        gate.gate_id, action="configure_and_verify", actor="user", reason="approved",
        subject_revision_hash=gate.subject_revision_hash,
        approval_scope=dict(gate.approval_scope),
        suspended_operation=gate.suspended_operation, return_state=gate.return_state,
    )
    resumed = _batch(
        root, connection, CredentialStubAdapter(present=True, results=ALL_SUCCEED),
        "attempt-one", credential_decision_id=decision.decision_id,
    )
    assert resumed.next_state == RunState.RESEARCH_COMPLETE.value
    nonce = decision.decision_id

    def normalise(value):
        return value.replace(nonce, "{credential_decision_id}")

    record = _gate_record(gate)
    record["store_keys"] = sorted(
        normalise(row[0]) for row in connection.execute(
            "SELECT idempotency_key FROM research_operations WHERE run_id='run'"
        )
    )
    record["finish_coordinates"] = sorted(
        [normalise(row[0]), normalise(row[1])] for row in connection.execute(
            "SELECT operation, idempotency_key FROM idempotency_records "
            "WHERE run_id='run' AND operation LIKE 'research.%'"
        )
    )
    return record


def capture_serpapi_keys():
    """The `-rN` ladder and the decision-bound resume key, derived not executed."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE research_operations (run_id TEXT, idempotency_key TEXT, result_json TEXT)"
    )
    base = "serpapi-0123456789abcdef0123"
    ladder = []
    for attempt in range(3):
        candidate, stored = _serpapi_idempotency_key(connection, "run", base)
        ladder.append(candidate)
        assert stored is None
        connection.execute(
            "INSERT INTO research_operations VALUES(?,?,?)",
            ("run", candidate, json.dumps({"status": "failed"})),
        )
    resume_key, stored = _serpapi_decision_key(
        connection, "run", "gd_fixture_decision", f"research.execute:{base}:re_research:gd_seeded",
    )
    assert stored is None
    connection.close()
    return {"decision_bound_resume": resume_key, "fresh_ladder": ladder}


def capture_baseline():
    with tempfile.TemporaryDirectory() as first_name, tempfile.TemporaryDirectory() as second_name:
        first_root, second_root = Path(first_name), Path(second_name)
        first = connect_database(first_root / "factory.sqlite3")
        second = connect_database(second_root / "factory.sqlite3")
        try:
            ready(first)
            return {
                "first_pass_credential_gate": capture_first_pass(first_root, first),
                "recorded_at_commit": "0d0aedc",
                "second_pass_attempt_one": capture_second_pass_attempt_one(second_root, second),
                "serpapi_derived_keys": capture_serpapi_keys(),
            }
        finally:
            first.close()
            second.close()


class ByteIdentityBaselineTests(unittest.TestCase):
    def test_recorded_observables_are_byte_identical_to_the_baseline(self):
        self.assertEqual(capture_baseline(), json.loads(BASELINE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    print(json.dumps(capture_baseline(), ensure_ascii=False, indent=2, sort_keys=True))
