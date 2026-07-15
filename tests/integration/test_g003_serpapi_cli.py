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

    def _response_fixture(self) -> Path:
        response = self.documents / "serp.json"
        response.write_bytes((ROOT / "tests/fixtures/google_patents/organic-results-v1.json").read_bytes())
        return response

    def _account_fixture(self, name: str) -> Path:
        account = self.documents / f"{name}.json"
        account.write_bytes((ROOT / f"tests/fixtures/serpapi/{name}.json").read_bytes())
        return account

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

    def test_quota_exhausted_emits_template_and_spends_no_search(self):
        run_root = self.workspace / "serp-quota"
        prepare_run(run_root, "serp-quota")
        account = self._account_fixture("account-exhausted")
        result = run_cli(
            *self.common(run_root, "serp-quota"),
            "--fixture-account", self.relative(account),
            environment={"SERPAPI_API_KEY": "SERP-CANARY-SECRET"},
        )
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "quota_exhausted")
        self.assertEqual(output["searches_left"], 0)
        template = self.workspace / "requests" / "manual-web-template.json"
        self.assertTrue(template.is_file())
        payload = json.loads(template.read_text())
        self.assertIn("records", payload)
        self.assertEqual(payload["records"][0]["provenance"], "google_patents_manual")
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM adapter_events").fetchone()[0], 0)

    def test_missing_credential_requires_gate(self):
        run_root = self.workspace / "serp-nocred"
        prepare_run(run_root, "serp-nocred")
        response = self._response_fixture()
        result = run_cli(
            *self.common(run_root, "serp-nocred"),
            "--fixture-response", self.relative(response),
            environment={"SERPAPI_API_KEY": ""},
        )
        self.assertEqual(result.returncode, 13, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "credential_required")


if __name__ == "__main__":
    unittest.main()
