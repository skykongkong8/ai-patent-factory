import io
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from patent_factory import cli
from patent_factory.config import load_evaluation_config
from patent_factory.database import connect_database
from patent_factory.decisions import resolve_gate
from patent_factory.ideation import run_ideation
from patent_factory.ideation_workbench import (
    BRIEF_SCHEMA,
    CLUSTER_SCHEMA,
    IDEA_SCHEMA,
    LINEAGE_SCHEMA,
    RELATION_SCHEMA,
    initialize_workbench,
    scaffold_ideation_brief,
)
from patent_factory.provenance import digest
from patent_factory.scaffold import scaffold_candidate_input
from patent_factory.state import StateStore
from tests.integration.test_g004_ideation_and_shortlist import ready_profile, ready_research
from tests.integration.test_g009_scaffolds import filled, filled_shortlist
from tests.integration.test_g010_checkpoint import CheckpointFixture as _CheckpointFixture

ROOT = Path(__file__).resolve().parents[2]


class IdeationWorkbenchCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.workspace = Path(self.temporary.name)
        self.workspace_rel = self.workspace.relative_to(ROOT)
        self.run_root = self.workspace / "run"
        self.run_root.mkdir(mode=0o700)
        self.connection = connect_database(self.run_root / "factory.sqlite3")
        self.profile_connection, self.profile = ready_profile(self.workspace / "profile.sqlite3")
        self.evidence, self.span, self.research = ready_research(self.connection, self.run_root)
        self.config = load_evaluation_config()
        self.workbench = self.workspace / "requests" / "ideation" / "run"
        self.brief_path = self.workbench / "brief-v1.json"
        payload, code = self.invoke(
            "scaffold", "ideation-workbench", "--run", self.run_root.relative_to(ROOT),
            "--run-id", "run", "--profile-database", self.workspace_rel / "profile.sqlite3",
            "--out", self.brief_path.relative_to(ROOT), "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, payload)
        self.brief = json.loads(self.brief_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self.connection.close()
        self.profile_connection.close()
        self.temporary.cleanup()

    def invoke(self, *argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main([str(item) for item in argv])
        return json.loads(stream.getvalue()), code

    def write_session(self, name="s1", *, lens="contradiction", count=2):
        session = self.workbench / "sessions" / name
        session.mkdir(mode=0o700, parents=True, exist_ok=True)
        rows = []
        for index in range(count):
            rows.append({
                "brief_hash": digest(self.brief),
                "creative_status": "creative_suggestion",
                "evidence_ids": ["ev_fixture"],
                "idea_id": f"id_{name}_{index}",
                "inputs": [f"input {name} {index}"],
                "lens": lens,
                "limitations": ["public fixture limitation"],
                "outputs": [f"output {name} {index}"],
                "profile_references": [],
                "rough_mechanism": f"mechanism {name} {index}",
                "schema_version": IDEA_SCHEMA,
                "session_id": name,
                "technical_problem": f"problem {name} {index}",
                "title": f"idea {name} {index}",
                "transformations": [f"transformation {name} {index}"],
                "validation_approach": f"validation {name} {index}",
            })
        (session / "ideas.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
        os.chmod(session / "ideas.jsonl", 0o600)
        return rows

    def write_entanglement(self):
        s1 = self.write_session("s1", lens="contradiction", count=2)
        s2 = self.write_session("s2", lens="far_analogy", count=2)
        relation = {
            "brief_hash": digest(self.brief),
            "rationale": "combines two session directions",
            "relation_id": "rel_cross",
            "schema_version": RELATION_SCHEMA,
            "session_id": "s1",
            "source_idea_ids": [s1[0]["idea_id"], s2[0]["idea_id"]],
            "target_idea_ids": [s1[1]["idea_id"]],
            "type": "combines",
        }
        path = self.workbench / "sessions" / "s1" / "relations.jsonl"
        path.write_text(json.dumps(relation, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        clusters = {
            "brief_hash": digest(self.brief),
            "session_id": "s1",
            "clusters": [{
                "cluster_id": "cl_cross",
                "idea_ids": [s1[0]["idea_id"], s2[0]["idea_id"]],
                "rationale": "shared mechanism boundary",
            }],
            "schema_version": CLUSTER_SCHEMA,
        }
        cluster_path = self.workbench / "sessions" / "s1" / "clusters-v1.json"
        cluster_path.write_text(json.dumps(clusters, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(cluster_path, 0o600)
        return s1, s2

    def write_promoted(self, count=3):
        draft = scaffold_candidate_input(self.connection, self.profile_connection, run_id="run", count=count)
        candidate_input = filled(draft)
        promoted = self.workbench / "promoted"
        candidate_path = promoted / "candidate-input-v1.json"
        candidate_path.write_text(json.dumps(candidate_input, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(candidate_path, 0o600)
        lineage = {
            "candidate_input_hash": digest(candidate_input),
            "records": [
                {
                    "candidate_index": index,
                    "rationale": f"candidate {index} promoted from public fixture ideas",
                    "relation_ids": ["rel_cross"],
                    "source_idea_ids": ["id_s1_0", "id_s2_0"],
                    "source_session_ids": ["s1", "s2"],
                }
                for index in range(count)
            ],
            "schema_version": LINEAGE_SCHEMA,
        }
        lineage_path = promoted / "lineage-v1.json"
        lineage_path.write_text(json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(lineage_path, 0o600)
        return candidate_input

    def validate(self, stage, *extra):
        return self.invoke(
            "scaffold", "ideation-workbench", "--run", self.run_root.relative_to(ROOT),
            "--run-id", "run", "--profile-database", self.workspace_rel / "profile.sqlite3",
            "--validate", self.workbench.relative_to(ROOT), "--stage", stage,
            "--workspace-root", self.workspace_rel, *extra,
        )

    def file_snapshot(self):
        snapshot = {}
        for path in sorted(self.workspace.rglob("*")):
            if path.is_file() and not path.is_symlink():
                stat_result = path.stat()
                snapshot[str(path.relative_to(self.workspace))] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    stat_result.st_mode & 0o777,
                    stat_result.st_mtime_ns,
                    stat_result.st_size,
                )
        return snapshot

    def test_scaffold_initializes_private_layout_with_hash_bound_brief(self):
        self.assertEqual(self.brief["version"], BRIEF_SCHEMA)
        self.assertTrue((self.workbench / "sessions").is_dir())
        self.assertTrue((self.workbench / "promoted").is_dir())
        self.assertFalse((self.workbench / "history").exists())
        self.assertEqual((self.brief_path.stat().st_mode & 0o777), 0o600)
        encoded = self.brief_path.read_text(encoding="utf-8")
        self.assertNotIn("센서 오차를 줄여야 한다", encoded)
        self.assertNotIn("공개 기술 자료", encoded)
        self.assertNotIn("redacted fixture", encoded)
        self.assertFalse(self.brief["egress"]["allowed"])
        self.assertFalse(self.brief["egress"]["semantic_content_available"])
        self.assertTrue(all(card["semantic_status"] == "hash_only_private_boundary" for card in self.brief["evidence_cards"]))

    def test_diverge_validation_is_read_only_and_uses_public_counts(self):
        self.write_session(count=2)
        before_files = self.file_snapshot()
        before_state = StateStore(self.connection).snapshot("run")
        before_counts = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("artifact_revisions", "current_artifacts", "gate_envelopes", "transition_events")
        }
        payload, code = self.validate("diverge")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "workbench_valid")
        self.assertEqual(payload["counts"]["ideas"], 2)
        self.assertEqual(payload["brief_hash"], digest(self.brief))
        self.assertEqual(before_files, self.file_snapshot())
        self.assertEqual(before_state, StateStore(self.connection).snapshot("run"))
        self.assertEqual(before_counts, {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in before_counts
        })

    def test_four_independent_lenses_retain_sixteen_raw_ideas(self):
        for session, lens in (
            ("contradiction", "contradiction"),
            ("morphology", "morphology"),
            ("far-analogy", "far_analogy"),
            ("inversion", "inversion"),
        ):
            self.write_session(session, lens=lens, count=4)
        payload, code = self.validate("diverge")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["counts"]["ideas"], 16)
        self.assertNotIn("single_lens_workbench", {item["code"] for item in payload["advisories"]})

    def test_same_brief_replay_does_not_rewrite_brief(self):
        before = self.brief_path.stat().st_mtime_ns
        payload, code = self.invoke(
            "scaffold", "ideation-workbench", "--run", self.run_root.relative_to(ROOT),
            "--run-id", "run", "--profile-database", self.workspace_rel / "profile.sqlite3",
            "--out", self.brief_path.relative_to(ROOT), "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(before, self.brief_path.stat().st_mtime_ns)

    def test_changed_brief_archives_history_and_old_sessions_remain_valid(self):
        self.write_session("old", count=1)
        old_brief = self.brief
        changed = scaffold_ideation_brief(
            self.connection, self.profile_connection, run_id="run", config=self.config,
            reideate_seed={"public_fixture_revision": 2},
        )
        initialize_workbench(self.brief_path, changed)
        history = self.workbench / "history" / f"brief-{digest(old_brief)}.json"
        self.assertTrue(history.is_file())
        self.brief = changed
        self.write_session("current", count=1)
        payload, code = self.validate("diverge")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["counts"]["ideas"], 2)

    def test_diverge_validation_rejects_missing_idea_schema_version(self):
        rows = self.write_session(count=1)
        rows[0].pop("schema_version")
        path = self.workbench / "sessions" / "s1" / "ideas.jsonl"
        path.write_text(json.dumps(rows[0], sort_keys=True) + "\n", encoding="utf-8")
        payload, code = self.validate("diverge")
        self.assertEqual(code, 2)
        self.assertIn("schema_version", payload["error"])

    def test_diverge_validation_rejects_unknown_idea_field(self):
        rows = self.write_session(count=1)
        rows[0]["score"] = 99
        path = self.workbench / "sessions" / "s1" / "ideas.jsonl"
        path.write_text(json.dumps(rows[0], sort_keys=True) + "\n", encoding="utf-8")
        payload, code = self.validate("diverge")
        self.assertEqual(code, 2)
        self.assertIn("unknown fields", payload["error"])

    def test_diverge_validation_rejects_symlinked_session(self):
        target = self.workbench / "sessions" / "real"
        target.mkdir(mode=0o700)
        (self.workbench / "sessions" / "link").symlink_to(target, target_is_directory=True)
        payload, code = self.validate("diverge")
        self.assertEqual(code, 2)
        self.assertIn("session", payload["error"])

    def test_entangle_validation_rejects_relation_cycles(self):
        self.write_session(count=2)
        relation_path = self.workbench / "sessions" / "s1" / "relations.jsonl"
        rows = [
            {"brief_hash": digest(self.brief), "schema_version": RELATION_SCHEMA, "session_id": "s1", "relation_id": "r1", "type": "derives", "source_idea_ids": ["id_s1_0"], "target_idea_ids": ["id_s1_1"], "rationale": "forward"},
            {"brief_hash": digest(self.brief), "schema_version": RELATION_SCHEMA, "session_id": "s1", "relation_id": "r2", "type": "revises", "source_idea_ids": ["id_s1_1"], "target_idea_ids": ["id_s1_0"], "rationale": "back"},
        ]
        relation_path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
        os.chmod(relation_path, 0o600)
        cluster_path = self.workbench / "sessions" / "s1" / "clusters-v1.json"
        cluster_path.write_text(json.dumps({"brief_hash": digest(self.brief), "session_id": "s1", "schema_version": CLUSTER_SCHEMA, "clusters": [{"cluster_id": "cl", "idea_ids": ["id_s1_0"], "rationale": "cluster"}]}) + "\n", encoding="utf-8")
        os.chmod(cluster_path, 0o600)
        payload, code = self.validate("entangle")
        self.assertEqual(code, 2)
        self.assertIn("acyclic", payload["error"])

    def test_promote_validation_accepts_three_candidates_and_preserves_ideate_publication(self):
        self.write_entanglement()
        candidate_input = self.write_promoted(count=3)
        payload, code = self.validate("promote")
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(payload["candidate_ids_by_index"]), 3)
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.run_root,
            run_id="run", profile=self.profile, candidate_input=candidate_input, config=self.config,
        )
        self.assertEqual(ideation.next_state, "candidates_ready")

    def test_promote_validation_accepts_workbench_bounds_one_and_twelve(self):
        self.write_entanglement()
        for count in (1, 12):
            with self.subTest(count=count):
                self.write_promoted(count=count)
                payload, code = self.validate("promote")
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload["counts"]["candidates"], count)

    def test_promote_validation_rejects_duplicate_lineage_index(self):
        self.write_entanglement()
        self.write_promoted(count=2)
        path = self.workbench / "promoted" / "lineage-v1.json"
        lineage = json.loads(path.read_text(encoding="utf-8"))
        lineage["records"][1]["candidate_index"] = 0
        path.write_text(json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        payload, code = self.validate("promote")
        self.assertEqual(code, 2)
        self.assertIn("duplicate candidate index", payload["error"])

    def test_promote_validation_rejects_wrong_source_session_binding(self):
        self.write_entanglement()
        self.write_promoted(count=1)
        path = self.workbench / "promoted" / "lineage-v1.json"
        lineage = json.loads(path.read_text(encoding="utf-8"))
        lineage["records"][0]["source_session_ids"] = ["s1"]
        path.write_text(json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        payload, code = self.validate("promote")
        self.assertEqual(code, 2)
        self.assertIn("source_session_ids", payload["error"])

    def test_promote_validation_rejects_todo_anywhere_in_candidate_input(self):
        self.write_entanglement()
        candidate_input = self.write_promoted(count=1)
        candidate_input["candidates"][0]["title"] = "TODO(agent): title"
        path = self.workbench / "promoted" / "candidate-input-v1.json"
        path.write_text(json.dumps(candidate_input, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        payload, code = self.validate("promote")
        self.assertEqual(code, 2)
        self.assertIn("TODO(agent) placeholder", payload["error"])

    def test_promote_validation_rejects_more_than_twelve_candidates(self):
        self.write_entanglement()
        candidate_input = self.write_promoted(count=12)
        candidate_input["candidates"].append(dict(candidate_input["candidates"][0], title="thirteenth candidate"))
        path = self.workbench / "promoted" / "candidate-input-v1.json"
        path.write_text(json.dumps(candidate_input, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        lineage_path = self.workbench / "promoted" / "lineage-v1.json"
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
        lineage["candidate_input_hash"] = digest(candidate_input)
        lineage["records"].append(dict(lineage["records"][0], candidate_index=12, rationale="extra"))
        lineage_path.write_text(json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        payload, code = self.validate("promote")
        self.assertEqual(code, 2)
        self.assertIn("at most 12", payload["error"])

    def test_parser_enforces_workbench_mode_and_stage_pairing(self):
        payload, code = self.invoke(
            "scaffold", "ideation-workbench", "--run", self.run_root.relative_to(ROOT),
            "--run-id", "run", "--profile-database", self.workspace_rel / "profile.sqlite3",
            "--validate", self.workbench.relative_to(ROOT), "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 2)
        self.assertIn("requires --stage", payload["error"])
        payload, code = self.invoke(
            "scaffold", "ideation-workbench", "--run", self.run_root.relative_to(ROOT),
            "--run-id", "run", "--profile-database", self.workspace_rel / "profile.sqlite3",
            "--out", self.brief_path.relative_to(ROOT), "--stage", "diverge",
            "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 2)
        self.assertIn("only valid with --validate", payload["error"])

    def test_shortlist_scaffold_for_five_candidates_selects_three_and_excludes_rest(self):
        draft = scaffold_candidate_input(self.connection, self.profile_connection, run_id="run", count=5)
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.run_root,
            run_id="run", profile=self.profile, candidate_input=filled(draft), config=self.config,
        )
        shortlist_payload, code = self.invoke(
            "scaffold", "shortlist", "--run", self.run_root.relative_to(ROOT), "--run-id", "run",
            "--out", self.workspace_rel / "requests" / "shortlist-input-v1.json", "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, shortlist_payload)
        request = json.loads((self.workspace / "requests" / "shortlist-input-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(request["finalists"]), 3)
        self.assertEqual(len(request["exclusions"]), 2)
        self.assertEqual({item["reason_codes"][0] for item in request["exclusions"]}, {"not_selected"})
        self.assertEqual(set(ideation.candidate_ids), {item["candidate_id"] for item in request["finalists"]} | {item["candidate_id"] for item in request["exclusions"]})

    def test_shortlist_scaffold_for_two_candidates_emits_insufficiency_request(self):
        draft = scaffold_candidate_input(self.connection, self.profile_connection, run_id="run", count=2)
        run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.run_root,
            run_id="run", profile=self.profile, candidate_input=filled(draft), config=self.config,
        )
        payload, code = self.invoke(
            "scaffold", "shortlist", "--run", self.run_root.relative_to(ROOT), "--run-id", "run",
            "--out", self.workspace_rel / "requests" / "shortlist-input-v1.json", "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, payload)
        request = json.loads((self.workspace / "requests" / "shortlist-input-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(request["finalists"]), 2)
        self.assertIsNotNone(request["insufficiency"])
        self.assertEqual(len(request["insufficiency"]["eligible_candidate_ids"]), 2)


class ReIdeateWorkbenchSeedTests(_CheckpointFixture):
    def test_reideate_brief_carries_only_hash_bound_feedback_and_public_ids(self):
        self._run_audit(["matched", "different", "different"])
        gate_id = self._pending_gate_id()
        request = self._decide_input(gate_id, action="re_ideate")
        resolved = resolve_gate(
            self.connection, run_root=self.run_root, run_id="run", decision_input=request,
        )
        brief = scaffold_ideation_brief(
            self.connection, self.profile_connection, run_id="run",
            config=load_evaluation_config(),
        )
        seed = brief["reideate_seed"]
        self.assertEqual(seed["action"], "re_ideate")
        self.assertEqual(seed["resolution_revision_id"], resolved.artifact_revision_id)
        self.assertEqual(
            {item["finalist_id"] for item in seed["feedback_bindings"]},
            {item["finalist_id"] for item in request["feedback"]},
        )
        encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True)
        for feedback in request["feedback"]:
            self.assertNotIn(feedback["interesting"], encoded)
            self.assertNotIn(feedback["boring"], encoded)
        self.assertTrue(all(item["feedback_hash"] for item in seed["feedback_bindings"]))
        self.assertEqual(
            {item["candidate_id"] for item in seed["finalist_bindings"]},
            {item["candidate_id"] for item in request["approval_scope"]["finalist_bindings"]},
        )


if __name__ == "__main__":
    unittest.main()
