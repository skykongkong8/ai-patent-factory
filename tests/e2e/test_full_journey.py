"""The golden Justin journey: every stage through the REAL CLI, no fabricated state.

Closes the end-to-end gap called out in issue #22: previous suites exercised each
stage against synthesized upstream state; this test drives
init → profile → run start → research (normalize-web + manual) → scaffold/ideate
→ scaffold/shortlist → scaffold/audit retrieve → audit score → scaffold/draft(en)
→ review → validate, asserting the final English report byte-matches the
committed golden. Set JUSTIN_GOLDEN_REGENERATE=1 to refresh the golden after an
intentional renderer change.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from patent_factory.audit import feature_map_id
from patent_factory.database import connect_database
from patent_factory.provenance import digest
from tests.integration.test_g005_audit import kipris_xml
from tests.integration.test_g009_scaffolds import filled
from tests.unit.test_g005_similarity import feature_map

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "justin"
GOLDEN = EXAMPLES / "expected-report-en.md"
RETRIEVED_AT = "2026-07-19T00:00:00Z"

REVIEW_CHECKS = [
    "citation_integrity", "decision_gate_coverage", "factual_grounding", "internal_consistency",
    "legal_language", "schema_completeness", "source_coverage",
]


def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "patent_factory", *map(str, args)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )


class JustinFullJourneyTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.documents_context = tempfile.TemporaryDirectory(dir=ROOT / "documents")
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.documents = Path(self.documents_context.name)
        self.workspace = Path(self.workspace_context.name)

    def tearDown(self):
        self.documents_context.cleanup()
        self.workspace_context.cleanup()

    def rel(self, path: Path) -> Path:
        return path.relative_to(ROOT)

    def step(self, *args: object) -> dict:
        result = run_cli(*args)
        self.assertEqual(
            result.returncode, 0,
            f"step {args} failed:\n{result.stdout}\n{result.stderr}",
        )
        return json.loads(result.stdout)

    def fill(self, relative: Path) -> dict:
        path = ROOT / relative
        value = filled(json.loads(path.read_text(encoding="utf-8")))
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return value

    def current(self, connection, kind: str):
        return connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='justin' AND ca.kind=?",
            (kind,),
        ).fetchone()

    def test_full_pipeline_from_init_to_complete_matches_golden(self):
        docs_rel, ws_rel = self.rel(self.documents), self.rel(self.workspace)

        # 1. init — also scaffolds the requests/ directories.
        self.step("init", "--documents", docs_rel, "--workspace", ws_rel)
        self.assertTrue((self.workspace / "requests" / "README.md").is_file())
        self.assertTrue((self.documents / "requests").is_dir())

        # 2. profile from the committed Justin background document.
        shutil.copy(EXAMPLES / "background.md", self.documents / "background.md")
        payload = self.step(
            "profile", "document", docs_rel / "background.md",
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["status"], "profile_ready")

        # 3. bind the profile into a fresh run.
        run_root = self.workspace / "run"
        run_rel = self.rel(run_root)
        payload = self.step(
            "run", "start", "--run", run_rel, "--run-id", "justin",
            "--profile", ws_rel / "profile.json",
            "--profile-database", ws_rel / "profile.sqlite3",
            "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "research_ready")

        # 4. web evidence: agent-gathered rows → normalize-web → manual import.
        shutil.copy(EXAMPLES / "web-rows.json", self.documents / "web-rows.json")
        self.step(
            "research", "normalize-web", docs_rel / "web-rows.json",
            "--out", docs_rel / "normalized.json",
            "--allow-host", "arxiv.org", "--allow-host", "patents.google.com",
            "--source-type", "web",
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        payload = self.step(
            "research", "manual", docs_rel / "normalized.json",
            "--run", run_rel, "--run-id", "justin",
            "--query", "on-device inference kv-cache",
            "--allow-host", "arxiv.org", "--allow-host", "patents.google.com",
            "--retrieved-at", RETRIEVED_AT,
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "research_complete")
        self.assertEqual(payload["evidence_count"], 2)

        # 5. candidates: scaffold → fill TODOs → ideate.
        candidate_path = ws_rel / "requests" / "candidate-input-v1.json"
        self.step(
            "scaffold", "candidate", "--run", run_rel, "--run-id", "justin",
            "--out", candidate_path, "--workspace-root", ws_rel,
        )
        self.fill(candidate_path)
        payload = self.step(
            "ideate", "--run", run_rel, "--run-id", "justin",
            "--profile", ws_rel / "profile.json",
            "--profile-database", ws_rel / "profile.sqlite3",
            "--input", candidate_path, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "candidates_ready")

        # 6. finalists: scaffold → fill → shortlist.
        shortlist_path = ws_rel / "requests" / "shortlist-input-v1.json"
        self.step(
            "scaffold", "shortlist", "--run", run_rel, "--run-id", "justin",
            "--out", shortlist_path, "--workspace-root", ws_rel,
        )
        self.fill(shortlist_path)
        payload = self.step(
            "shortlist", "--run", run_rel, "--run-id", "justin",
            "--input", shortlist_path, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "finalists_ready")

        # 7. audit retrieval: scaffold query → fill terms → fixture manifest → retrieve.
        query_path = ws_rel / "requests" / "audit-query-input-v1.json"
        self.step(
            "scaffold", "audit-query", "--run", run_rel, "--run-id", "justin",
            "--out", query_path, "--workspace-root", ws_rel,
        )
        query = self.fill(query_path)
        fixture = self.documents / "kipris-fixture.xml"
        fixture.write_bytes(kipris_xml("10-2026-0011111"))
        manifest = {
            "schema_version": "audit-fixture-manifest-v1",
            "responses": [
                {"finalist_id": group["finalist_id"], "page": 1,
                 "source": str(self.rel(fixture)), "term": item["term"]}
                for group in query["groups"] for item in group["queries"]
            ],
        }
        (self.documents / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
        )
        payload = self.step(
            "audit", "retrieve", "--run", run_rel, "--run-id", "justin",
            "--query-input", query_path, "--fixture-manifest", docs_rel / "manifest.json",
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["status"], "audit_running")

        # 8. feature maps with human-readable descriptions → score.
        with connect_database(run_root / "factory.sqlite3") as connection:
            corpus_row = self.current(connection, "corpus_set")
            corpus_set = json.loads(corpus_row["content_json"])
            candidate_row = self.current(connection, "candidate_set")
            candidates = {
                item["candidate_id"]: item
                for item in json.loads(candidate_row["content_json"])["candidates"]
            }
            finalist_row = self.current(connection, "finalist_set")
            finalists = {
                item["finalist_id"]: item
                for item in json.loads(finalist_row["content_json"])["finalists"]
            }
        fields = {
            "problem": "technical_problem", "inputs": "required_inputs", "mechanism": "mechanism",
            "transformations": "transformations", "outputs": "outputs",
            "technical_effects": "expected_effects",
        }
        maps = []
        for corpus in corpus_set["corpora"]:
            record = corpus["records"][0]
            mapping = feature_map(record["evidence_id"], status="different")
            candidate = candidates[finalists[corpus["finalist_id"]]["candidate_id"]]
            for feature in mapping["features"]:
                field = fields[feature["category"]]
                raw = candidate[field]
                value = raw[0] if isinstance(raw, list) else raw
                feature["candidate_span_hashes"] = [digest({"field": field, "text": value})]
                feature["description"] = f"runtime-adaptive {feature['category'].replace('_', ' ')} control"
            span = record["record"]["field_span_hashes"]["abstract"]
            for decision in mapping["reference_maps"][0]["decisions"]:
                decision["reference_span_hashes"] = [span]
            maps.append({
                "feature_map": mapping, "finalist_id": corpus["finalist_id"],
                "map_id": feature_map_id(corpus["finalist_id"], mapping),
            })
        feature_path = ws_rel / "requests" / "feature-map-set-input-v1.json"
        (ROOT / feature_path).write_text(json.dumps({
            "schema_version": "feature-map-set-input-v1",
            "finalist_set_hash": finalist_row["content_hash"],
            "corpus_set_hash": corpus_row["content_hash"], "maps": maps,
        }, ensure_ascii=False), encoding="utf-8")
        payload = self.step(
            "audit", "score", "--run", run_rel, "--run-id", "justin",
            "--feature-input", feature_path, "--workspace-root", ws_rel,
        )
        self.assertNotIn(payload["status"], {"decision_required", "coverage_insufficient"})

        # 9. English report: scaffold → fill → draft.
        report_path = ws_rel / "requests" / "report-input-v2.json"
        self.step(
            "scaffold", "report", "--language", "en", "--out", report_path,
            "--profile-database", ws_rel / "profile.sqlite3", "--workspace-root", ws_rel,
        )
        self.fill(report_path)
        payload = self.step(
            "draft", "--run", run_rel, "--run-id", "justin",
            "--input", report_path, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "draft_ready")
        self.assertEqual(payload["language"], "en")
        report_hash = payload["report_hash"]
        markdown = Path(payload["export_path"]).read_text(encoding="utf-8")

        # 10. independent review → validate → complete.
        with connect_database(run_root / "factory.sqlite3") as connection:
            audit_hash = self.current(connection, "audit_batch")["content_hash"]
        review_path = ws_rel / "requests" / "review-input-v1.json"
        (ROOT / review_path).write_text(json.dumps({
            "checks": [
                {"details": "independent journey review", "name": name, "status": "pass"}
                for name in REVIEW_CHECKS
            ],
            "decision_gate_verification": {
                "audit_hash": audit_hash, "covered_finalist_ids": [], "status": "pass",
            },
            "disposition": "approved", "evidence_corrections": [], "findings": [],
            "prohibited_language_findings": [], "report_hash": report_hash,
            "reviewer": {"id": "journey-reviewer", "pass_id": "journey-review-pass", "type": "agent"},
            "schema_version": "review-input-v1",
        }, ensure_ascii=False), encoding="utf-8")
        payload = self.step(
            "review", "--run", run_rel, "--run-id", "justin",
            "--input", review_path, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "reviewed")
        payload = self.step(
            "validate", "--run", run_rel, "--run-id", "justin", "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "complete")

        # 11. the committed golden.
        self.assertIn("# Korean Patent Proposal Review Report", markdown)
        self.assertIn("runtime-adaptive mechanism control", markdown)
        if os.environ.get("JUSTIN_GOLDEN_REGENERATE"):
            GOLDEN.write_text(markdown, encoding="utf-8")
        self.assertEqual(markdown, GOLDEN.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
