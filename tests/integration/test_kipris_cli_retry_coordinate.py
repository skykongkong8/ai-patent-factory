"""The `research kipris` CLI's own wiring of the retry convention (PR-B).

`attempt_coordinate` is locked at the runner, but the runner is not what an
operator invokes. Its sole production caller is one argument at the kipris call
site — `advance_spent_key=not args.idempotency_key` — and that argument was
sensitive to nothing: flipping it either way left the whole suite green.

Both directions matter, and they fail differently:

* forced to False, PR-B silently reverts for kipris and the honest same-term
  retry after an incomplete second pass goes back to being refused, sending the
  operator back to manual key arithmetic;
* forced to True, an operator who pinned `--idempotency-key` to a specific
  coordinate silently gets a DIFFERENT one — a fresh egress where they asked
  for a replay, which is the more dangerous direction.

The credential is deliberately absent, so every case here suspends before any
egress: the observable is which coordinate the raised gate is bound to, not
anything the adapter did.
"""
import json
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database, utc_now
from patent_factory.provenance import digest
from patent_factory.research import ResearchBudget, plan_keyword_queries

from tests.integration.test_g003_research_cli import ROOT, prepare_run, run_cli
from tests.integration.test_research_reentry_gate import seed_re_research_reentry

SALT = ":re_research:gd_seeded_re_research"


class KiprisRetryCoordinateTests(unittest.TestCase):
    def setUp(self):
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.workspace = Path(self.workspace_context.name)
        self.run_root = self.workspace / "run"
        prepare_run(self.run_root, "kipris-retry")

    def tearDown(self):
        self.workspace_context.cleanup()

    def base_key(self):
        """The key the CLI derives when the operator supplies none.

        Mirrors `_research_kipris`: a digest over the planned envelopes'
        request fingerprints. Recomputed here rather than hardcoded, so a
        change to the derivation surfaces as a mismatch instead of a silent
        pass against a stale literal.
        """

        planned = plan_keyword_queries(
            run_id="kipris-retry", origin_query="센서", budget=ResearchBudget(max_calls=3),
        )
        return "research-kipris-" + digest({
            "fingerprints": [query.envelope.request_fingerprint for query in planned],
        })[:20]

    def seed_published_attempt(self, key, *, state="research_running"):
        """Mark one attempt coordinate as already having published a finish.

        `state` matters for more than realism: the `research.start` state-check
        transition is SKIPPED from `research_ready`/`research_running`, so a run
        parked there cannot observe anything about where the refusal sits
        relative to it. `research_incomplete` — the state an incomplete second
        pass actually leaves the run in — is the one that can.
        """

        with connect_database(self.run_root / "factory.sqlite3") as connection:
            seed_re_research_reentry(connection, "kipris-retry")
            now = utc_now()
            connection.execute(
                "INSERT INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)",
                ("te_seeded_finish", "kipris-retry", "research-cli", "research_running",
                 "research_incomplete", "seeded attempt 1 finish", "[]", None, now),
            )
            connection.execute(
                "INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)",
                ("kipris-retry", f"research.execute:{key}", key, "te_seeded_finish", None,
                 "research_incomplete", now),
            )
            connection.execute(
                "UPDATE runs SET state=? WHERE run_id='kipris-retry'", (state,),
            )

    def research(self, *extra):
        return run_cli(
            "research", "kipris",
            "--run", self.run_root.relative_to(ROOT), "--run-id", "kipris-retry",
            "--query", "센서", "--workspace-root", self.workspace.relative_to(ROOT),
            *extra, environment={"KIPRIS_PLUS_API_KEY": ""},
        )

    def pending_gate_operation(self):
        with connect_database(self.run_root / "factory.sqlite3") as connection:
            row = connection.execute(
                "SELECT suspended_operation FROM gate_envelopes "
                "WHERE run_id='kipris-retry' AND status='pending'",
            ).fetchone()
        return row["suspended_operation"] if row else None

    def test_a_derived_key_advances_past_a_spent_coordinate(self):
        """No `--idempotency-key`: the CLI must let the convention advance.

        The key is recomputed from the request fingerprint on every
        invocation, so without the advance this retry lands on exactly the
        coordinate the previous attempt published and is refused.
        """

        salted = f"{self.base_key()}{SALT}"
        self.seed_published_attempt(salted)
        result = self.research()
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "credential_required")
        self.assertEqual(
            self.pending_gate_operation(), f"research.execute:{salted}-r2",
        )

    def test_an_explicit_key_pins_the_coordinate_and_is_refused_when_spent(self):
        """With `--idempotency-key`: the operator chose the coordinate.

        Advancing here would hand back a different attempt than the one asked
        for. The convention must stand aside and let the spent-coordinate
        refusal speak.
        """

        pinned = "operator-pinned-attempt"
        self.seed_published_attempt(f"{pinned}{SALT}")
        result = self.research("--idempotency-key", pinned)
        self.assertNotEqual(result.returncode, 5, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["failure_code"], "live_research_reentry_spent_coordinate_issue_48",
        )
        self.assertIsNone(self.pending_gate_operation())
        # The message has to move with the behaviour, and it is the only thing
        # the operator actually reads. Before the advance existed the advice was
        # "pass --idempotency-key"; that is now the flag that DISABLES the
        # advance, so repeating it here would send this exact operator straight
        # back to the state they are already in.
        message = payload["error"]
        self.assertIn("WITHOUT --idempotency-key", message)
        self.assertIn("has not already used", message)

    def test_a_refused_attempt_leaves_the_run_exactly_as_it_found_it(self):
        """Refused before any egress AND before any state mutation.

        The refusal sits above the `research.start` state-check transition and
        above the request revision, so a turned-away attempt is not merely
        egress-free: it writes nothing. Otherwise every refused retry would
        quietly advance the run and accumulate revisions.
        """

        pinned = "operator-pinned-sideeffect"
        self.seed_published_attempt(f"{pinned}{SALT}", state="research_incomplete")
        with connect_database(self.run_root / "factory.sqlite3") as connection:
            before = connection.execute(
                "SELECT state, state_version FROM runs WHERE run_id='kipris-retry'",
            ).fetchone()
            revisions_before = connection.execute(
                "SELECT count(*) FROM artifact_revisions WHERE run_id='kipris-retry'",
            ).fetchone()[0]

        result = self.research("--idempotency-key", pinned)
        self.assertEqual(
            json.loads(result.stdout)["failure_code"],
            "live_research_reentry_spent_coordinate_issue_48",
        )
        with connect_database(self.run_root / "factory.sqlite3") as connection:
            after = connection.execute(
                "SELECT state, state_version FROM runs WHERE run_id='kipris-retry'",
            ).fetchone()
            self.assertEqual(tuple(after), tuple(before))
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM artifact_revisions WHERE run_id='kipris-retry'",
                ).fetchone()[0],
                revisions_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM idempotency_records WHERE run_id='kipris-retry' "
                    "AND operation='research.start'",
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
