import json
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database

from tests.integration.test_g003_research_cli import FIXED_TIME, ROOT, prepare_run, run_cli


class SerpApiCliTests(unittest.TestCase):
    def setUp(self):
        self.documents_context = tempfile.TemporaryDirectory(dir=ROOT / "documents")
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.documents = Path(self.documents_context.name)
        self.workspace = Path(self.workspace_context.name)

    def tearDown(self):
        self.workspace_context.cleanup()
        self.documents_context.cleanup()

    def relative(self, path: Path) -> Path:
        return path.relative_to(ROOT)

    def common(self, run_root: Path, run_id: str):
        return (
            "research", "serpapi",
            "--run", self.relative(run_root), "--run-id", run_id,
            "--query", "Vision Language Action", "--retrieved-at", FIXED_TIME,
            "--documents-root", self.relative(self.documents),
            "--workspace-root", self.relative(self.workspace),
        )

    def _response_fixture(self, source: str = "organic-results-v1") -> Path:
        response = self.documents / "serp.json"
        response.write_bytes((ROOT / f"tests/fixtures/google_patents/{source}.json").read_bytes())
        return response

    def _account_fixture(self, name: str) -> Path:
        account = self.documents / f"{name}.json"
        account.write_bytes((ROOT / f"tests/fixtures/serpapi/{name}.json").read_bytes())
        return account

    def _template_path(self) -> Path:
        return self.documents / "requests" / "manual-web-template.json"

    def test_live_serpapi_persists_gpatent_evidence_without_leaking_key(self):
        run_root = self.workspace / "serp-run"
        prepare_run(run_root, "serp-run")
        response = self._response_fixture()
        account = self._account_fixture("account-ok")
        result = run_cli(
            *self.common(run_root, "serp-run"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["next_state"], "research_complete")
        self.assertNotIn("SERP-CANARY-SECRET", result.stdout + result.stderr)
        self.assertNotIn(b"SERP-CANARY-SECRET", (run_root / "factory.sqlite3").read_bytes())
        with connect_database(run_root / "factory.sqlite3") as connection:
            rows = connection.execute(
                "SELECT source_type,source_locator FROM evidence_records ORDER BY source_locator"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["source_type"] == "google_patent" for row in rows))
            self.assertTrue(all(row["source_locator"].startswith("gpatent:") for row in rows))

    def test_success_replay_spends_no_second_search(self):
        run_root = self.workspace / "serp-replay"
        prepare_run(run_root, "serp-replay")
        response = self._response_fixture()
        account = self._account_fixture("account-ok")
        arguments = (
            *self.common(run_root, "serp-replay"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
        )
        first = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(
            json.loads(first.stdout)["query_id"], json.loads(second.stdout)["query_id"],
        )
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 1,
            )

    def test_quota_exhausted_emits_template_and_spends_no_search(self):
        run_root = self.workspace / "serp-quota"
        prepare_run(run_root, "serp-quota")
        response = self._response_fixture()
        account = self._account_fixture("account-exhausted")
        result = run_cli(
            *self.common(run_root, "serp-quota"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "quota_exhausted")
        self.assertEqual(output["searches_left"], 0)
        self.assertFalse(output["template_preserved"])
        # The template lands under the documents root so the advertised
        # `research manual` fallback command can consume it directly.
        template = self._template_path()
        self.assertTrue(template.is_file())
        self.assertIn(output["fallback_template"], result.stdout)
        # The advertised fallback command must carry the configured roots so it
        # works verbatim with non-default --documents-root/--workspace-root.
        self.assertIn("--documents-root", output["message"])
        self.assertIn("--workspace-root", output["message"])
        payload = json.loads(template.read_text())
        self.assertIn("records", payload)
        self.assertEqual(payload["records"][0]["provenance"], "google_patents_manual")
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 0)
            # The stop is recorded in the authoritative run database.
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM artifact_revisions WHERE kind='research_quota_stop'"
                ).fetchone()[0],
                1,
            )

    def test_quota_fallback_template_is_rejected_until_edited(self):
        run_root = self.workspace / "serp-sentinel"
        prepare_run(run_root, "serp-sentinel")
        response = self._response_fixture()
        account = self._account_fixture("account-exhausted")
        result = run_cli(
            *self.common(run_root, "serp-sentinel"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        imported = run_cli(
            "research", "manual", self.relative(self._template_path()),
            "--run", self.relative(run_root), "--run-id", "serp-sentinel",
            "--query", "Vision Language Action", "--retrieved-at", FIXED_TIME,
            "--documents-root", self.relative(self.documents),
            "--workspace-root", self.relative(self.workspace),
            "--allow-host", "patents.google.com",
        )
        self.assertEqual(imported.returncode, 2, imported.stdout + imported.stderr)
        self.assertIn("placeholder", imported.stdout)
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM evidence_records").fetchone()[0], 0,
            )

    def test_quota_fallback_preserves_user_edited_template(self):
        run_root = self.workspace / "serp-preserve"
        prepare_run(run_root, "serp-preserve")
        response = self._response_fixture()
        account = self._account_fixture("account-exhausted")
        arguments = (
            *self.common(run_root, "serp-preserve"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
        )
        first = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(first.returncode, 12, first.stdout + first.stderr)
        edited = json.dumps({"records": [{"identifier": "KR102000001B1", "title": "edited"}]})
        self._template_path().write_text(edited, encoding="utf-8")
        second = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(second.returncode, 12, second.stdout + second.stderr)
        self.assertTrue(json.loads(second.stdout)["template_preserved"])
        self.assertEqual(self._template_path().read_text(encoding="utf-8"), edited)

    def test_transient_rate_limit_is_not_reported_as_quota_exhaustion(self):
        # The search reports a rate_limit failure while the free account endpoint
        # still shows quota available: this must surface as an incomplete research
        # attempt, never as a fabricated quota_exhausted stop.
        run_root = self.workspace / "serp-throttle"
        prepare_run(run_root, "serp-throttle")
        response = self._response_fixture("error-rate-limit")
        account = self._account_fixture("account-ok")
        result = run_cli(
            *self.common(run_root, "serp-throttle"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "incomplete")
        self.assertEqual(output["adapter_status"]["failure_kind"], "rate_limit")
        self.assertIn("rate_limit_note", output)
        self.assertNotIn("searches_left", output)
        self.assertFalse(self._template_path().exists())

    def test_failed_attempt_is_retried_under_a_fresh_key(self):
        run_root = self.workspace / "serp-retry"
        prepare_run(run_root, "serp-retry")
        response = self._response_fixture("error-rate-limit")
        account = self._account_fixture("account-ok")
        arguments = (
            *self.common(run_root, "serp-retry"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
        )
        first = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(first.returncode, 4, first.stdout + first.stderr)
        # The identical command must not replay the stored failure once the
        # upstream condition clears.
        response.write_bytes((ROOT / "tests/fixtures/google_patents/organic-results-v1.json").read_bytes())
        second = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        output = json.loads(second.stdout)
        self.assertEqual(output["next_state"], "research_complete")
        self.assertEqual(output["adapter_status"]["status"], "success")

    def test_replayed_failure_never_requeries_account_or_converts_to_quota(self):
        # An explicit-key replay of a stored rate-limited attempt must report the
        # stored result as-is even when the account endpoint now reports
        # exhaustion: no template, no quota conversion, no second search.
        run_root = self.workspace / "serp-replayfail"
        prepare_run(run_root, "serp-replayfail")
        response = self._response_fixture("error-rate-limit")
        account = self._account_fixture("account-ok")
        first = run_cli(
            *self.common(run_root, "serp-replayfail"),
            "--idempotency-key", "pinned-key",
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(first.returncode, 4, first.stdout + first.stderr)
        exhausted = self._account_fixture("account-exhausted")
        second = run_cli(
            *self.common(run_root, "serp-replayfail"),
            "--idempotency-key", "pinned-key",
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(exhausted),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(second.returncode, 4, second.stdout + second.stderr)
        output = json.loads(second.stdout)
        self.assertEqual(output["status"], "incomplete")
        self.assertIn("replayed a stored rate-limited attempt", output["rate_limit_note"])
        self.assertFalse(self._template_path().exists())
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 1,
            )

    def test_research_refused_from_completed_state_before_quota_stop(self):
        # A fresh attempt from a state that forbids research is refused before the
        # preflight: no quota stop, no template, no artifact mutation.
        run_root = self.workspace / "serp-refused"
        prepare_run(run_root, "serp-refused")
        response = self._response_fixture()
        account = self._account_fixture("account-ok")
        first = run_cli(
            *self.common(run_root, "serp-refused"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        exhausted = self._account_fixture("account-exhausted")
        second = run_cli(
            "research", "serpapi",
            "--run", self.relative(run_root), "--run-id", "serp-refused",
            "--query", "another different query", "--retrieved-at", FIXED_TIME,
            "--documents-root", self.relative(self.documents),
            "--workspace-root", self.relative(self.workspace),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(exhausted),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
        self.assertIn("not permitted from run state research_complete", json.loads(second.stdout)["error"])
        self.assertFalse(self._template_path().exists())
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM artifact_revisions WHERE kind='research_quota_stop'"
                ).fetchone()[0],
                0,
            )

    def test_credentialed_failure_recovers_without_decision_under_fresh_key(self):
        # Gate dance: missing key suspends the gate; the decision-bound retry
        # fails transiently; the follow-up WITHOUT a decision must advance to a
        # fresh key and succeed instead of clashing with the stored
        # decision-suffixed failure.
        run_root = self.workspace / "serp-cred"
        prepare_run(run_root, "serp-cred")
        response = self._response_fixture("error-rate-limit")
        account = self._account_fixture("account-ok")
        arguments = (
            *self.common(run_root, "serp-cred"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
        )
        gated = run_cli(*arguments, environment={"SERPAPI_API_KEY": ""})
        self.assertEqual(gated.returncode, 13, gated.stdout + gated.stderr)
        gate_id = json.loads(gated.stdout)["gate_id"]
        common_gate = (
            "--run", self.relative(run_root), "--run-id", "serp-cred",
            "--gate-id", gate_id, "--workspace-root", self.relative(self.workspace),
        )
        inspected = run_cli("gate", "inspect", *common_gate)
        self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
        gate = json.loads(inspected.stdout)
        request = {
            "action": "configure_and_verify", "actor": "user",
            "approval_scope": gate["approval_scope"], "decisions": [],
            "gate_id": gate_id, "plan": {},
            "reason": "credential configured", "schema_version": "gate-decision-input-v1",
            "subject_revision_hash": gate["subject_revision_hash"],
        }
        decision_file = self.workspace / "decision.json"
        decision_file.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        decided = run_cli("gate", "decide", *common_gate, "--input", self.relative(decision_file))
        self.assertEqual(decided.returncode, 0, decided.stdout + decided.stderr)
        decision_id = json.loads(decided.stdout)["decision_id"]
        resumed = run_cli(
            *arguments, "--decision-id", decision_id,
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(resumed.returncode, 4, resumed.stdout + resumed.stderr)
        response.write_bytes((ROOT / "tests/fixtures/google_patents/organic-results-v1.json").read_bytes())
        retried = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(json.loads(retried.stdout)["next_state"], "research_complete")

    def test_non_utf8_edited_template_counts_as_preserved(self):
        run_root = self.workspace / "serp-euckr"
        prepare_run(run_root, "serp-euckr")
        response = self._response_fixture()
        account = self._account_fixture("account-exhausted")
        arguments = (
            *self.common(run_root, "serp-euckr"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
        )
        first = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(first.returncode, 12, first.stdout + first.stderr)
        edited = '{"records": [{"title": "센서 특허"}]}'.encode("euc-kr")
        self._template_path().write_bytes(edited)
        second = run_cli(*arguments, environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"})
        self.assertEqual(second.returncode, 12, second.stdout + second.stderr)
        self.assertTrue(json.loads(second.stdout)["template_preserved"])
        self.assertEqual(self._template_path().read_bytes(), edited)

    def test_pending_gate_state_is_refused_before_quota_stop(self):
        # research_running is reachable from a gate state, but only via a gate
        # decision: a fresh attempt must be refused before any egress or template.
        run_root = self.workspace / "serp-gate"
        prepare_run(run_root, "serp-gate")
        with connect_database(run_root / "factory.sqlite3") as connection:
            connection.execute(
                "UPDATE runs SET state='domain_pivot_required' WHERE run_id='serp-gate'"
            )
            connection.commit()
        response = self._response_fixture()
        account = self._account_fixture("account-exhausted")
        result = run_cli(
            *self.common(run_root, "serp-gate"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("requires a gate decision", json.loads(result.stdout)["error"])
        self.assertFalse(self._template_path().exists())

    def test_unknown_decision_is_rejected_even_with_explicit_key(self):
        run_root = self.workspace / "serp-baddec"
        prepare_run(run_root, "serp-baddec")
        response = self._response_fixture()
        account = self._account_fixture("account-exhausted")
        result = run_cli(
            *self.common(run_root, "serp-baddec"),
            "--idempotency-key", "pinned-key", "--decision-id", "no-such-decision",
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("credential decision is unavailable", json.loads(result.stdout)["error"])
        self.assertFalse(self._template_path().exists())

    def test_decision_bound_to_another_key_is_refused_before_quota_stop(self):
        # A fresh, valid decision bound to a different --idempotency-key must be
        # refused locally: quota state must not decide whether an unauthorized
        # request is rejected or handed a quota stop.
        run_root = self.workspace / "serp-rekey"
        prepare_run(run_root, "serp-rekey")
        response = self._response_fixture()
        account = self._account_fixture("account-ok")
        seams = (
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
        )
        gated = run_cli(
            *self.common(run_root, "serp-rekey"), "--idempotency-key", "attempt-1", *seams,
            environment={"SERPAPI_API_KEY": ""},
        )
        self.assertEqual(gated.returncode, 13, gated.stdout + gated.stderr)
        gate_id = json.loads(gated.stdout)["gate_id"]
        common_gate = (
            "--run", self.relative(run_root), "--run-id", "serp-rekey",
            "--gate-id", gate_id, "--workspace-root", self.relative(self.workspace),
        )
        gate = json.loads(run_cli("gate", "inspect", *common_gate).stdout)
        decision_file = self.workspace / "rekey-decision.json"
        decision_file.write_text(json.dumps({
            "action": "configure_and_verify", "actor": "user",
            "approval_scope": gate["approval_scope"], "decisions": [],
            "gate_id": gate_id, "plan": {}, "reason": "credential configured",
            "schema_version": "gate-decision-input-v1",
            "subject_revision_hash": gate["subject_revision_hash"],
        }, ensure_ascii=False), encoding="utf-8")
        decided = run_cli("gate", "decide", *common_gate, "--input", self.relative(decision_file))
        self.assertEqual(decided.returncode, 0, decided.stdout + decided.stderr)
        decision_id = json.loads(decided.stdout)["decision_id"]

        # Same unauthorized request under both quota conditions: identical refusal.
        for fixture in ("account-ok", "account-exhausted"):
            with self.subTest(account=fixture):
                result = run_cli(
                    *self.common(run_root, "serp-rekey"),
                    "--idempotency-key", "attempt-2", "--decision-id", decision_id,
                    "--fixture-response", self.relative(response),
                    "--fixture-account", self.relative(self._account_fixture(fixture)),
                    environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(
                    "credential decision does not match", json.loads(result.stdout)["error"],
                )
                self.assertFalse(self._template_path().exists())
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM artifact_revisions WHERE kind='research_quota_stop'"
                ).fetchone()[0],
                0,
            )

    def test_half_configured_fixture_seams_are_rejected(self):
        run_root = self.workspace / "serp-seam"
        prepare_run(run_root, "serp-seam")
        response = self._response_fixture()
        result = run_cli(
            *self.common(run_root, "serp-seam"),
            "--fixture-response", self.relative(response),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("must be supplied together", json.loads(result.stdout)["error"])

    def test_unknown_run_is_rejected_before_any_preflight(self):
        run_root = self.workspace / "serp-known"
        prepare_run(run_root, "serp-known")
        response = self._response_fixture()
        account = self._account_fixture("account-exhausted")
        result = run_cli(
            *self.common(run_root, "serp-unknown"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("not registered", json.loads(result.stdout)["error"])
        # The refused operation produced no fallback artifacts.
        self.assertFalse(self._template_path().exists())

    def test_missing_credential_requires_gate(self):
        run_root = self.workspace / "serp-nocred"
        prepare_run(run_root, "serp-nocred")
        response = self._response_fixture()
        account = self._account_fixture("account-ok")
        result = run_cli(
            *self.common(run_root, "serp-nocred"),
            "--fixture-response", self.relative(response),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": ""},
        )
        self.assertEqual(result.returncode, 13, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "credential_required")


if __name__ == "__main__":
    unittest.main()
