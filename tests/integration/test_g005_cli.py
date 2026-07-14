import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from patent_factory.config import load_evaluation_config
from patent_factory.database import connect_database
from patent_factory.evaluation import run_shortlist
from patent_factory.ideation import run_ideation
from patent_factory.models import RunState
from patent_factory.state import StateStore
from tests.integration.test_g004_ideation_and_shortlist import (
    candidate_input, ready_profile, ready_research, shortlist_input,
)
from tests.integration.test_g005_audit import kipris_xml


ROOT = Path(__file__).resolve().parents[2]


class G005CliTests(unittest.TestCase):
    def setUp(self):
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.documents_context = tempfile.TemporaryDirectory(dir=ROOT / "documents")
        self.workspace, self.documents = Path(self.workspace_context.name), Path(self.documents_context.name)

    def tearDown(self):
        self.workspace_context.cleanup()
        self.documents_context.cleanup()

    def relative(self, path):
        return path.relative_to(ROOT)

    def prepare(self, name):
        run_root = self.workspace / name
        run_root.mkdir(mode=0o700)
        connection = connect_database(run_root / "factory.sqlite3")
        profile_connection, profile = ready_profile(self.workspace / f"{name}-profile.sqlite3")
        evidence, span, _ = ready_research(connection, run_root, name)
        ideation = run_ideation(
            connection, profile_connection=profile_connection, run_root=run_root, run_id=name,
            profile=profile, candidate_input=candidate_input(3, evidence, span), config=load_evaluation_config(),
        )
        run_shortlist(
            connection, run_root=run_root, run_id=name,
            shortlist_input=shortlist_input(ideation.candidate_ids, evidence, span), config=load_evaluation_config(),
        )
        row = connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id=? AND ca.kind='finalist_set'", (name,),
        ).fetchone()
        finalists = json.loads(row["content_json"])["finalists"]
        profile_connection.close()
        connection.close()
        return run_root, row["content_hash"], finalists

    def inputs(self, name, finalist_hash, finalists, term="동일 검색어"):
        query = {
            "schema_version": "audit-query-input-v1", "finalist_set_hash": finalist_hash,
            "groups": [{"finalist_id": item["finalist_id"], "queries": [
                {"language": "ko", "term": term}, {"language": "en", "term": "same query"},
            ]} for item in finalists],
        }
        query_path = self.workspace / f"{name}-queries.json"
        query_path.write_text(json.dumps(query, ensure_ascii=False), encoding="utf-8")
        fixture = self.documents / f"{name}.xml"
        fixture.write_bytes(kipris_xml("10-2026-0012345"))
        manifest = {
            "schema_version": "audit-fixture-manifest-v1", "responses": [
                {"finalist_id": item["finalist_id"], "page": 1, "source": str(self.relative(fixture)), "term": query_term}
                for item in finalists for query_term in (term, "same query")
            ],
        }
        manifest_path = self.documents / f"{name}-manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return query_path, manifest_path

    def invoke(self, run_root, name, query_path, manifest_path, **environment):
        env = os.environ.copy()
        env.update(environment)
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run([
            sys.executable, "-m", "patent_factory", "audit", "retrieve",
            "--run", str(self.relative(run_root)), "--run-id", name,
            "--query-input", str(self.relative(query_path)),
            "--fixture-manifest", str(self.relative(manifest_path)),
            "--workspace-root", str(self.relative(self.workspace)),
            "--documents-root", str(self.relative(self.documents)),
        ], cwd=ROOT, env=env, text=True, capture_output=True, check=False)

    def invoke_score(self, run_root, name, feature_path):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run([
            sys.executable, "-m", "patent_factory", "audit", "score",
            "--run", str(self.relative(run_root)), "--run-id", name,
            "--feature-input", str(self.relative(feature_path)),
            "--workspace-root", str(self.relative(self.workspace)),
        ], cwd=ROOT, env=env, text=True, capture_output=True, check=False)

    def test_fixture_cli_runs_three_separate_finalist_groups(self):
        run_root, finalist_hash, finalists = self.prepare("happy")
        query_path, manifest_path = self.inputs("happy", finalist_hash, finalists)
        result = self.invoke(run_root, "happy", query_path, manifest_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["status"], len(payload["corpus_hashes"])), ("audit_running", 3))
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM research_queries WHERE envelope_json LIKE '%final_similarity_audit%'"
            ).fetchone()[0], 6)

    def test_query_canary_is_rejected_before_audit_state_or_persistence(self):
        secret = "G005-KIPRIS-CANARY"
        run_root, finalist_hash, finalists = self.prepare("private")
        query_path, manifest_path = self.inputs("private", finalist_hash, finalists, term=secret)
        result = self.invoke(run_root, "private", query_path, manifest_path, KIPRIS_PLUS_API_KEY=secret)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        with connect_database(run_root / "factory.sqlite3") as connection:
            snapshot = StateStore(connection).snapshot("private")
            self.assertEqual(snapshot.state, RunState.FINALISTS_READY)
            self.assertNotIn("audit_query_set", snapshot.current_revisions)
        self.assertNotIn(secret.encode(), (run_root / "factory.sqlite3").read_bytes())

    def test_duplicate_keyed_decision_is_rejected_redacted_before_any_write(self):
        run_root, finalist_hash, _finalists = self.prepare("duplicate-key")
        feature_path = self.workspace / "duplicate-key-features.json"
        corpus_hash = "a" * 64
        feature_path.write_text(
            '{"schema_version":"feature-map-set-input-v1",'
            f'"finalist_set_hash":"{finalist_hash}","corpus_set_hash":"{corpus_hash}",'
            '"maps":[{"feature_map":{"reference_maps":[{"decisions":{'
            '"feature-problem":{"status":"matched"},'
            '"feature-problem":{"status":"different"}}}]}}]}',
            encoding="utf-8",
        )
        result = self.invoke_score(run_root, "duplicate-key", feature_path)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("duplicate JSON object key", result.stdout)
        self.assertNotIn("feature-problem", result.stdout + result.stderr)
        with connect_database(run_root / "factory.sqlite3") as connection:
            snapshot = StateStore(connection).snapshot("duplicate-key")
            self.assertEqual(snapshot.state, RunState.FINALISTS_READY)
            self.assertNotIn("feature_map_set", snapshot.current_revisions)
            self.assertNotIn("audit_batch", snapshot.current_revisions)


if __name__ == "__main__":
    unittest.main()
