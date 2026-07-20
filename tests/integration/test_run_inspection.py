"""`run status` / `run show` — the read surfaces that make inputs authorable.

Downstream stage inputs bind to artifact hashes and to retained-corpus content.
Before these verbs existed the only way to obtain either was to open the run
SQLite directly, which the golden e2e does (`test_full_journey.py:190-202`,
`:255-256`) and which a CLI-driven agent cannot. These tests assert the same
values are reachable through stdout alone.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from patent_factory import cli
from patent_factory.database import connect_database
from tests.integration.test_g009_research_batch import ready

ROOT = Path(__file__).resolve().parents[2]


class RunInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        base = Path(self.temporary.name)
        self.workspace = base / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.workspace_rel = self.workspace.relative_to(ROOT)
        self.run_root = self.workspace / "run"
        self.run_root.mkdir(mode=0o700)
        self.run_rel = self.run_root.relative_to(ROOT)
        connection = connect_database(self.run_root / "factory.sqlite3")
        try:
            store = ready(connection)
            store.transition(
                "run", "research_running", actor="test", reason="seed",
                operation="seed.state", idempotency_key="seed-1",
            )
            store.add_revision(
                "run", "corpus_set",
                {"corpora": [{"finalist_id": "fi_1"}], "version": "corpus-set-v1"},
                schema_version="corpus-set-v1",
            )
            # audit_batch must be reachable too: `review-input` needs its
            # content_hash as `audit_hash`, and the golden e2e reads it from
            # SQLite — the surface `run show`/`run status` exist to replace.
            store.add_revision(
                "run", "audit_batch",
                {"finalists": [{"finalist_id": "fi_1", "outcome": "audit_approved"}],
                 "version": "audit-batch-v1"},
                schema_version="audit-batch-v1",
            )
        finally:
            connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main([str(item) for item in argv])
        return json.loads(stream.getvalue()), code

    def test_status_lists_every_current_artifact_with_its_hash(self):
        payload, code = self.invoke(
            "run", "status", "--run", self.run_rel, "--run-id", "run",
            "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["state"], "research_running")
        kinds = {item["kind"] for item in payload["artifacts"]}
        self.assertIn("corpus_set", kinds)
        for artifact in payload["artifacts"]:
            # A bare id is not enough: inputs bind to the *hash*.
            self.assertEqual(len(artifact["content_hash"]), 64)
            self.assertTrue(artifact["revision_id"])

    def test_show_returns_artifact_body_not_just_its_hash(self):
        payload, code = self.invoke(
            "run", "show", "--run", self.run_rel, "--run-id", "run",
            "--kind", "corpus_set", "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["kind"], "corpus_set")
        self.assertEqual(payload["content"]["corpora"], [{"finalist_id": "fi_1"}])

    def test_show_hash_matches_status_hash(self):
        status, _ = self.invoke(
            "run", "status", "--run", self.run_rel, "--run-id", "run",
            "--workspace-root", self.workspace_rel,
        )
        shown, _ = self.invoke(
            "run", "show", "--run", self.run_rel, "--run-id", "run",
            "--kind", "corpus_set", "--workspace-root", self.workspace_rel,
        )
        listed = next(item for item in status["artifacts"] if item["kind"] == "corpus_set")
        self.assertEqual(listed["content_hash"], shown["content_hash"])

    def test_audit_batch_content_and_hash_are_reachable_for_review_input(self):
        # review-input's audit_hash is this artifact's content_hash. Prove both
        # the body (run show) and the hash (run status) come from stdout, since
        # the golden e2e still reaches into SQLite for exactly this value.
        status, _ = self.invoke(
            "run", "status", "--run", self.run_rel, "--run-id", "run",
            "--workspace-root", self.workspace_rel,
        )
        listed = next(a for a in status["artifacts"] if a["kind"] == "audit_batch")
        shown, code = self.invoke(
            "run", "show", "--run", self.run_rel, "--run-id", "run",
            "--kind", "audit_batch", "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["content"]["finalists"][0]["outcome"], "audit_approved")
        self.assertEqual(shown["content_hash"], listed["content_hash"])

    def test_show_names_the_available_kinds_when_asked_for_a_missing_one(self):
        payload, code = self.invoke(
            "run", "show", "--run", self.run_rel, "--run-id", "run",
            "--kind", "finalist_set", "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 2, payload)
        # The error has to be actionable — an agent needs to know what it *can* ask for.
        self.assertIn("finalist_set", payload["error"])
        self.assertIn("corpus_set", payload["error"])

    def test_unknown_run_is_rejected(self):
        payload, code = self.invoke(
            "run", "status", "--run", self.run_rel, "--run-id", "no-such-run",
            "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 2, payload)
        self.assertIn("run_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
