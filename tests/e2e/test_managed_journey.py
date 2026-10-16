"""Public fixtures traverse managed inventor/topic scope through the real CLI."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from patent_factory.audit import feature_map_id
from patent_factory.database import connect_database
from patent_factory.provenance import digest
from tests.e2e.test_full_journey import EXAMPLES, RETRIEVED_AT, REVIEW_CHECKS, ROOT, run_cli
from tests.integration.test_g005_audit import kipris_xml
from tests.integration.test_g009_scaffolds import filled, filled_shortlist
from tests.unit.test_g005_similarity import feature_map


class ManagedFullJourneyTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.documents_context = tempfile.TemporaryDirectory(dir=ROOT / "documents")
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.addCleanup(self.documents_context.cleanup)
        self.addCleanup(self.workspace_context.cleanup)
        self.documents = Path(self.documents_context.name)
        self.workspace = Path(self.workspace_context.name)
        self.docs_rel = self.documents.relative_to(ROOT)
        self.ws_rel = self.workspace.relative_to(ROOT)
        result = run_cli("init", "--documents", self.docs_rel, "--workspace", self.ws_rel)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shutil.copy(EXAMPLES / "background.md", self.documents / "background.md")
        shutil.copy(EXAMPLES / "web-rows.json", self.documents / "web-rows.json")
        (self.documents / "topic.md").write_text(
            "observed_problem: runtime thermal limits change the inference budget\n"
            "constraints: operate with bounded device memory\n", encoding="utf-8",
        )
        self.step(
            "research", "normalize-web", self.docs_rel / "web-rows.json",
            "--out", self.docs_rel / "normalized.json", "--allow-host", "arxiv.org",
            "--allow-host", "patents.google.com", "--source-type", "web",
        )

    def step(self, *args: object, code: int = 0) -> dict:
        result = run_cli(
            *args, "--documents-root", self.docs_rel, "--workspace-root", self.ws_rel,
        )
        self.assertEqual(result.returncode, code, f"step {args}:\n{result.stdout}\n{result.stderr}")
        value = json.loads(result.stdout)
        self.assertEqual(value["schema_version"], "cli-result-v1")
        self.assertEqual(value["envelope_version"], "cli-envelope-v1")
        return value

    def write(self, relative: Path, value: dict) -> None:
        (ROOT / relative).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def fill(self, relative: Path, *, shortlist: bool = False) -> dict:
        draft = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        value = filled_shortlist(draft) if shortlist else filled(draft)
        self.write(relative, value)
        return value

    def current(self, run: Path, run_id: str, kind: str) -> dict:
        # These databases contain only this test's synthetic public fixtures.
        with connect_database(ROOT / run / "factory.sqlite3") as connection:
            row = connection.execute(
                "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca "
                "ON ca.revision_id=ar.revision_id WHERE ca.run_id=? AND ca.kind=?",
                (run_id, kind),
            ).fetchone()
            self.assertIsNotNone(row, f"missing {kind} for {run_id}")
            return dict(row)

    def topic(self, inventor: str, name: str) -> tuple[str, dict]:
        topic = self.step(
            "topic", "create", "--inventor", inventor, "--name", name, "--tag", "edge AI",
        )["topic_id"]
        version = self.step(
            "topic", "document", self.docs_rel / "topic.md", "--inventor", inventor,
            "--topic", topic,
        )
        return topic, version

    def complete(self, inventor: str, topic: str, profile: dict, context: dict, run_id: str) -> Path:
        started = self.step(
            "run", "start", "--inventor", inventor, "--topic", topic, "--run-id", run_id,
        )
        run = Path(started["run"])
        self.assertEqual(
            run, self.ws_rel / "inventors" / inventor / "topics" / topic / "runs" / run_id,
        )
        self.assertEqual(started["next_state"], "research_ready")
        pinned = self.current(run, run_id, "profile_context")
        pinned_content = json.loads(pinned["content_json"])
        self.assertEqual(pinned_content["version"], "profile-context-v2")
        self.assertEqual(pinned_content["binding"], {
            "inventor_id": inventor, "topic_id": topic,
            "profile_version_id": profile["version_id"], "topic_version_id": context["version_id"],
        })

        imported = self.step(
            "research", "manual", self.docs_rel / "normalized.json", "--run", run,
            "--run-id", run_id, "--query", "on-device inference kv-cache",
            "--allow-host", "arxiv.org", "--allow-host", "patents.google.com",
            "--retrieved-at", RETRIEVED_AT,
        )
        self.assertEqual(imported["next_state"], "research_complete")
        self.assertEqual(imported["evidence_count"], 2)

        # Replacement changes future runs while this run keeps its exact context.
        before = self.step("run", "status", "--run-id", run_id)
        replacement_source = self.documents / (run_id + "-replacement.md")
        replacement_source.write_text(
            "expertise: synthetic optical sensor controls\n"
            "technical_domain: optical sensing\nproject_summary: track synthetic light pulses\n",
            encoding="utf-8",
        )
        replacement = self.step(
            "profile", "document", replacement_source.relative_to(ROOT), "--inventor", inventor,
            "--replace",
        )
        self.assertNotEqual(profile["version_id"], replacement["version_id"])
        after = self.step("run", "status", "--run", run, "--run-id", run_id)
        self.assertEqual(before["artifacts"], after["artifacts"])
        self.assertEqual(after["profile_version_id"], profile["version_id"])
        self.assertEqual(after["topic_version_id"], context["version_id"])
        self.assertEqual(self.current(run, run_id, "profile_context"), pinned)

        requests = run / "requests"
        candidate_path = requests / "candidate-input-v1.json"
        self.step("scaffold", "candidate", "--run-id", run_id, "--out", candidate_path)
        self.fill(candidate_path)
        result = self.step("ideate", "--run-id", run_id, "--input", candidate_path)
        self.assertEqual(result["next_state"], "candidates_ready")
        self.assertEqual(self.current(run, run_id, "profile_context"), pinned)

        shortlist_path = requests / "shortlist-input-v1.json"
        self.step("scaffold", "shortlist", "--run-id", run_id, "--out", shortlist_path)
        self.fill(shortlist_path, shortlist=True)
        result = self.step("shortlist", "--run-id", run_id, "--input", shortlist_path)
        self.assertEqual(result["next_state"], "finalists_ready")

        query_path = requests / "audit-query-input-v1.json"
        self.step("scaffold", "audit-query", "--run-id", run_id, "--out", query_path)
        query = self.fill(query_path)
        fixture = self.documents / (run_id + "-kipris.xml")
        fixture.write_bytes(kipris_xml("10-2026-0011111"))
        manifest_path = self.docs_rel / (run_id + "-manifest.json")
        self.write(manifest_path, {
            "schema_version": "audit-fixture-manifest-v1",
            "responses": [
                {"finalist_id": group["finalist_id"], "page": 1,
                 "source": str(fixture.relative_to(ROOT)), "term": item["term"]}
                for group in query["groups"] for item in group["queries"]
            ],
        })
        result = self.step(
            "audit", "retrieve", "--run-id", run_id, "--query-input", query_path,
            "--fixture-manifest", manifest_path, "--retrieved-at", RETRIEVED_AT,
        )
        self.assertEqual(result["status"], "audit_running")

        corpus_row = self.current(run, run_id, "corpus_set")
        finalist_row = self.current(run, run_id, "finalist_set")
        candidates = {
            item["candidate_id"]: item
            for item in json.loads(self.current(run, run_id, "candidate_set")["content_json"])["candidates"]
        }
        finalists = {
            item["finalist_id"]: item for item in json.loads(finalist_row["content_json"])["finalists"]
        }
        fields = {
            "problem": "technical_problem", "inputs": "required_inputs", "mechanism": "mechanism",
            "transformations": "transformations", "outputs": "outputs", "technical_effects": "expected_effects",
        }
        maps = []
        for corpus in json.loads(corpus_row["content_json"])["corpora"]:
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
        feature_path = requests / "feature-map-set-input-v1.json"
        self.write(feature_path, {
            "schema_version": "feature-map-set-input-v1", "maps": maps,
            "finalist_set_hash": finalist_row["content_hash"], "corpus_set_hash": corpus_row["content_hash"],
        })
        scored = self.step(
            "audit", "score", "--run-id", run_id, "--feature-input", feature_path, code=8,
        )
        self.assertEqual(scored["status"], "decision_required")
        gate_id = scored["gate_id"]
        inspected = self.step("gate", "inspect", "--run-id", run_id, "--gate-id", gate_id)
        self.assertEqual(inspected["kind"], "post_audit_checkpoint")
        decision_path = requests / "gate-decision-input-v2.json"
        self.step(
            "scaffold", "gate-decision", "--run-id", run_id, "--gate-id", gate_id,
            "--out", decision_path,
        )
        decision = json.loads((ROOT / decision_path).read_text(encoding="utf-8"))
        decision.update({
            "action": "approve", "actor": "inventor", "decisions": [], "plan": {},
            "reason": "synthetic fixture inventor reviewed the clean audit dossier",
            "feedback": [{
                "finalist_id": item["finalist_id"],
                "boring": "the input arrangement is a familiar fixture",
                "interesting": "the adaptive mechanism merits the fixture report",
            } for item in decision["feedback"]],
        })
        self.write(decision_path, decision)
        approved = self.step(
            "gate", "decide", "--run-id", run_id, "--gate-id", gate_id, "--input", decision_path,
        )
        self.assertEqual(approved["next_state"], "audit_approved")

        report_path = requests / "report-input-v2.json"
        self.step(
            "scaffold", "report", "--run", run, "--run-id", run_id,
            "--language", "en", "--out", report_path,
        )
        self.fill(report_path)
        drafted = self.step("draft", "--run-id", run_id, "--input", report_path)
        self.assertEqual(drafted["next_state"], "draft_ready")
        self.assertEqual(drafted["language"], "en")
        markdown = Path(drafted["export_path"]).read_text(encoding="utf-8")
        self.assertIn("# Korean Patent Proposal Review Report", markdown)
        self.assertIn("runtime-adaptive mechanism control", markdown)
        self.assertNotIn("synthetic optical sensor controls", markdown)

        review_path = requests / "review-input-v1.json"
        self.write(review_path, {
            "checks": [{"details": "independent managed fixture review", "name": name, "status": "pass"}
                       for name in REVIEW_CHECKS],
            "decision_gate_verification": {
                "audit_hash": self.current(run, run_id, "audit_batch")["content_hash"],
                "covered_finalist_ids": [], "status": "pass",
            },
            "disposition": "approved", "evidence_corrections": [], "findings": [],
            "prohibited_language_findings": [], "report_hash": drafted["report_hash"],
            "reviewer": {"id": "managed-independent-reviewer", "pass_id": run_id + "-review", "type": "agent"},
            "schema_version": "review-input-v1",
        })
        reviewed = self.step("review", "--run-id", run_id, "--input", review_path)
        self.assertEqual(reviewed["next_state"], "reviewed")
        validated = self.step("validate", "--run-id", run_id)
        self.assertEqual(validated["next_state"], "complete")
        self.assertEqual(self.current(run, run_id, "profile_context"), pinned)
        return run

    def test_two_inventors_complete_independent_journeys_with_pinned_versions(self):
        inventors, topics, runs = [], [], []
        for index in range(2):
            inventor = self.step("inventor", "create", "--name", "Synthetic inventor")["inventor_id"]
            profile = self.step(
                "profile", "document", self.docs_rel / "background.md", "--inventor", inventor,
            )
            topic, context = self.topic(inventor, "Thermal inference")
            run = self.complete(inventor, topic, profile, context, f"managed-{index}")
            inventors.append(inventor)
            topics.append(topic)
            runs.append(run)

        self.assertNotEqual(inventors[0], inventors[1])
        self.assertNotEqual(topics[0], topics[1])
        self.assertNotEqual(runs[0], runs[1])
        another_topic, context = self.topic(inventors[0], "Optical budget")
        extra = self.step(
            "run", "start", "--inventor", inventors[0], "--topic", another_topic,
            "--run-id", "managed-another-topic",
        )
        status = self.step("run", "status", "--run-id", "managed-another-topic")
        self.assertEqual(extra["next_state"], "research_ready")
        self.assertEqual(status["topic_version_id"], context["version_id"])
        new_context = json.loads(self.current(Path(extra["run"]), "managed-another-topic", "profile_context")["content_json"])
        self.assertEqual(new_context["base_profile"]["facts"]["expertise"]["value"], "synthetic optical sensor controls")
        self.assertEqual(new_context["binding"]["inventor_id"], inventors[0])
        listed = self.step("inventor", "list", "--name", "Synthetic inventor")
        self.assertEqual(set(listed["matches"]), set(inventors))


if __name__ == "__main__":
    unittest.main()
