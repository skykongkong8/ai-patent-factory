"""PROBE: does the shipped COVERAGE-`expand` research re-entry actually work?

Drives one real offline run through the CLI all the way to a raised COVERAGE
gate (init -> profile -> run start -> normalize-web/manual research -> ideate ->
shortlist -> audit retrieve (fixture manifest) -> audit score), resolves that
gate with `action: "expand"` plus a bounded plan, imports a SECOND batch of
research records, and then tries to drive the run forward again.

The coverage gate is reached by construction, not by luck: `summarize_candidate`
returns `coverage_insufficient` whenever `upper_exact["Q"] < coverage`
(similarity.py:364-366). With simrisk-v1.0.0,

    Q = 0.25 * q_text + 0.60 * q_features + 0.15 * q_classification

and the coverage threshold is 0.80. Marking the `mechanism` (0.30) and
`transformations` (0.20) feature decisions `unavailable` drops q_features to
0.50, so Q = 0.25 + 0.30 + 0.15 = 0.70 < 0.80 while r_hi stays far below the
0.75 excessive threshold, i.e. coverage_insufficient rather than
decision_required.

Every assertion below records what the shipped core actually does; nothing here
is a plan.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import os
import tempfile
import unittest
from pathlib import Path

from patent_factory.audit import feature_map_id
from patent_factory.database import connect_database
from patent_factory.provenance import digest
from tests.integration.test_g005_audit import kipris_xml
from tests.integration.test_g009_scaffolds import filled, filled_shortlist
from tests.unit.test_g005_similarity import feature_map

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "justin"
RETRIEVED_AT = "2026-07-19T00:00:00Z"
RUN_ID = "reentry"

# mechanism (0.30) + transformations (0.20) => q_features 0.50 => Q 0.70 < 0.80.
STARVED_FEATURES = ("feature-mechanism", "feature-transformations")

# The widening the COVERAGE gate is supposed to buy: new, distinct sources.
EXPANSION_ROWS = {
    "schema_version": "web-rows-v1",
    "rows": [
        {
            "url": "https://arxiv.org/abs/2502.09876",
            "title": "Layer-wise KV-Cache Eviction Policies for Edge Transformers",
            "identifier": "arXiv:2502.09876",
            "abstract": "A layer-wise eviction policy that bounds edge transformer cache growth under a fixed memory budget.",
            "excerpts": ["Layer-wise eviction keeps the resident cache under a fixed budget."],
            "limitations": ["arXiv preprint metadata only"],
            "language": "en",
        },
        {
            "url": "https://patents.google.com/patent/KR102026000002",
            "title": "Runtime cache precision switching device for neural accelerators",
            "identifier": "KR-10-2026-0000002",
            "abstract": "A device that switches cache element precision at runtime according to an accelerator load signal.",
            "limitations": ["public bibliographic metadata only"],
            "language": "en",
        },
    ],
}

CANDIDATE_FIELDS = {
    "problem": "technical_problem", "inputs": "required_inputs", "mechanism": "mechanism",
    "transformations": "transformations", "outputs": "outputs",
    "technical_effects": "expected_effects",
}


def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "patent_factory", *map(str, args)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )


class CoverageExpandReentryTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.documents_context = tempfile.TemporaryDirectory(dir=ROOT / "documents")
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.documents = Path(self.documents_context.name)
        self.workspace = Path(self.workspace_context.name)
        self.observations: dict[str, object] = {}
        self.exit_codes: list[tuple[object, int]] = []

    def tearDown(self):
        self.documents_context.cleanup()
        self.workspace_context.cleanup()
        # Quiet by default so the suite stays readable; set REENTRY_PROBE_VERBOSE=1
        # to dump the measured re-entry trace.
        if self.observations and os.environ.get("REENTRY_PROBE_VERBOSE"):
            print("\n=== PROBE OBSERVATIONS ===")
            print(json.dumps(self.observations, ensure_ascii=False, indent=2, sort_keys=True))

    def rel(self, path: Path) -> Path:
        return path.relative_to(ROOT)

    def step(self, *args: object, allowed: frozenset[int] = frozenset({0})) -> dict:
        """Run one CLI verb. A raised gate is exit code 7, not a failure."""
        result = run_cli(*args)
        self.assertIn(
            result.returncode, allowed,
            f"step {args} failed:\n{result.stdout}\n{result.stderr}",
        )
        self.exit_codes.append((" ".join(str(item) for item in args[:2]), result.returncode))
        return json.loads(result.stdout)

    def attempt(self, *args: object) -> tuple[int, dict]:
        """Run one CLI verb that is expected to be refused; return code + payload."""
        result = run_cli(*args)
        return result.returncode, json.loads(result.stdout)

    def fill(self, relative: Path, *, shortlist: bool = False) -> dict:
        path = ROOT / relative
        draft = json.loads(path.read_text(encoding="utf-8"))
        value = filled_shortlist(draft) if shortlist else filled(draft)
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return value

    def current(self, connection, kind: str):
        return connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id=? AND ca.kind=?",
            (RUN_ID, kind),
        ).fetchone()

    # ---------------- pipeline helpers (each drives the real CLI) -------------

    def import_web_evidence(self, rows: dict, *, name: str, query: str) -> dict:
        docs_rel, ws_rel = self.rel(self.documents), self.rel(self.workspace)
        (self.documents / f"{name}-rows.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8",
        )
        self.step(
            "research", "normalize-web", docs_rel / f"{name}-rows.json",
            "--out", docs_rel / f"{name}-normalized.json",
            "--allow-host", "arxiv.org", "--allow-host", "patents.google.com",
            "--source-type", "web",
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        return self.step(
            "research", "manual", docs_rel / f"{name}-normalized.json",
            "--run", self.run_rel, "--run-id", RUN_ID, "--query", query,
            "--allow-host", "arxiv.org", "--allow-host", "patents.google.com",
            "--retrieved-at", RETRIEVED_AT,
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )

    def ideate_and_shortlist(self) -> None:
        ws_rel = self.rel(self.workspace)
        candidate_path = ws_rel / "requests" / "candidate-input-v1.json"
        self.step(
            "scaffold", "candidate", "--run", self.run_rel, "--run-id", RUN_ID,
            "--out", candidate_path, "--workspace-root", ws_rel,
        )
        self.fill(candidate_path)
        payload = self.step(
            "ideate", "--run", self.run_rel, "--run-id", RUN_ID,
            "--profile", ws_rel / "profile.json",
            "--profile-database", ws_rel / "profile.sqlite3",
            "--input", candidate_path, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "candidates_ready")
        shortlist_path = ws_rel / "requests" / "shortlist-input-v1.json"
        self.step(
            "scaffold", "shortlist", "--run", self.run_rel, "--run-id", RUN_ID,
            "--out", shortlist_path, "--workspace-root", ws_rel,
        )
        self.fill(shortlist_path, shortlist=True)
        payload = self.step(
            "shortlist", "--run", self.run_rel, "--run-id", RUN_ID,
            "--input", shortlist_path, "--workspace-root", ws_rel,
        )
        self.assertEqual(payload["next_state"], "finalists_ready")

    def audit_retrieve(self) -> None:
        docs_rel, ws_rel = self.rel(self.documents), self.rel(self.workspace)
        query_path = ws_rel / "requests" / "audit-query-input-v1.json"
        self.step(
            "scaffold", "audit-query", "--run", self.run_rel, "--run-id", RUN_ID,
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
            "audit", "retrieve", "--run", self.run_rel, "--run-id", RUN_ID,
            "--query-input", query_path, "--fixture-manifest", docs_rel / "manifest.json",
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
            "--retrieved-at", RETRIEVED_AT,
        )
        self.assertEqual(payload["status"], "audit_running")

    def audit_score(self, *, starve: bool) -> dict:
        """Build one frozen feature map per finalist and score it.

        With starve=True two feature decisions are `unavailable`, which is what
        pushes Q under the coverage threshold and raises the COVERAGE gate.
        """
        ws_rel = self.rel(self.workspace)
        with connect_database(self.run_root / "factory.sqlite3") as connection:
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
        maps = []
        for corpus in corpus_set["corpora"]:
            record = corpus["records"][0]
            mapping = feature_map(record["evidence_id"], status="different")
            candidate = candidates[finalists[corpus["finalist_id"]]["candidate_id"]]
            for feature in mapping["features"]:
                field = CANDIDATE_FIELDS[feature["category"]]
                raw = candidate[field]
                value = raw[0] if isinstance(raw, list) else raw
                feature["candidate_span_hashes"] = [digest({"field": field, "text": value})]
                feature["description"] = f"runtime-adaptive {feature['category'].replace('_', ' ')} control"
            span = record["record"]["field_span_hashes"]["abstract"]
            for decision in mapping["reference_maps"][0]["decisions"]:
                if starve and decision["feature_id"] in STARVED_FEATURES:
                    decision["status"] = "unavailable"
                    decision["reference_span_hashes"] = []
                    decision["rationale"] = "not inspectable in the retrieved corpus"
                else:
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
        return self.step(
            "audit", "score", "--run", self.run_rel, "--run-id", RUN_ID,
            "--feature-input", feature_path, "--workspace-root", ws_rel,
            allowed=frozenset({0, 7}),
        )

    # ------------------------------- the probe -------------------------------

    def test_coverage_expand_reentry(self):
        docs_rel, ws_rel = self.rel(self.documents), self.rel(self.workspace)
        self.step("init", "--documents", docs_rel, "--workspace", ws_rel)
        shutil.copy(EXAMPLES / "background.md", self.documents / "background.md")
        self.step(
            "profile", "document", docs_rel / "background.md",
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        self.run_root = self.workspace / "run"
        self.run_rel = self.rel(self.run_root)
        self.step(
            "run", "start", "--run", self.run_rel, "--run-id", RUN_ID,
            "--profile", ws_rel / "profile.json",
            "--profile-database", ws_rel / "profile.sqlite3",
            "--workspace-root", ws_rel,
        )

        first = self.import_web_evidence(
            json.loads((EXAMPLES / "web-rows.json").read_text(encoding="utf-8")),
            name="first", query="on-device inference kv-cache",
        )
        self.assertEqual(first["next_state"], "research_complete")
        self.ideate_and_shortlist()
        self.audit_retrieve()
        scored = self.audit_score(starve=True)
        self.observations["audit_score_1"] = {
            "status": scored["status"], "next_state": scored.get("next_state"),
        }
        self.assertEqual(scored["status"], "coverage_insufficient")
        gate_id = scored["gate_id"]

        gate = self.step(
            "gate", "inspect", "--run", self.run_rel, "--run-id", RUN_ID, "--gate-id", gate_id,
        )
        self.assertEqual(gate["kind"], "coverage")
        self.assertIn("expand", gate["actions"])

        # ---- ASSERTION 1: resolve_gate lands the run in research_running -----
        decision_path = ws_rel / "requests" / "gate-decision-input-v1.json"
        plan = {
            "added_queries": ["kv cache eviction policy", "runtime precision switching"],
            "rationale": "coverage under threshold: mechanism and transformation features were "
                         "not inspectable in the retrieved corpus",
            "target_finalist_ids": sorted(gate["approval_scope"]["affected_finalist_ids"]),
        }
        (ROOT / decision_path).write_text(json.dumps({
            "action": "expand", "actor": "probe-operator",
            "approval_scope": gate["approval_scope"], "decisions": [],
            "gate_id": gate_id, "plan": plan,
            "reason": "widen the corpus before re-scoring",
            "schema_version": "gate-decision-input-v1",
            "subject_revision_hash": gate["subject_revision_hash"],
        }, ensure_ascii=False), encoding="utf-8")
        resolved = self.step(
            "gate", "decide", "--run", self.run_rel, "--run-id", RUN_ID,
            "--gate-id", gate_id, "--input", decision_path, "--workspace-root", ws_rel,
        )
        self.observations["assert_1_resolve_gate_next_state"] = resolved["next_state"]
        self.assertEqual(resolved["next_state"], "research_running")

        # Snapshot every revision the widening should invalidate.
        with connect_database(self.run_root / "factory.sqlite3") as connection:
            before = {
                kind: self.current(connection, kind)["revision_id"]
                for kind in ("research_bundle", "candidate_set", "finalist_set",
                             "corpus_set", "feature_map_set", "audit_batch",
                             "audit_query_set", "gate_resolution")
            }
            resolution = json.loads(connection.execute(
                "SELECT content_json FROM artifact_revisions WHERE revision_id=?",
                (before["gate_resolution"],),
            ).fetchone()["content_json"])

        # ---- ASSERTION 5 (write side): the gate binds a plan_hash ------------
        self.assertEqual(resolution["plan"], plan)
        self.assertEqual(resolution["plan_hash"], digest(plan))
        self.observations["assert_5_plan_hash_bound"] = resolution["plan_hash"]

        # ---- ASSERTION 2: the SECOND research publishes research_complete,   ----
        # ---- scoped to the research stage, not re-inflated by the audit ----
        second = self.import_web_evidence(
            EXPANSION_ROWS, name="second", query="kv-cache eviction precision switching",
        )
        self.observations["assert_2_second_research"] = {
            "prior_state": second.get("prior_state"), "next_state": second["next_state"],
            "evidence_count": second.get("evidence_count"),
        }
        self.assertEqual(second["next_state"], "research_complete")
        # `evidence_count` here is this single manual-import call's own new
        # records (research.py ResearchRun.as_dict), not the bundle union — both
        # EXPANSION_ROWS URLs are new, so both must land.
        self.assertEqual(second["evidence_count"], len(EXPANSION_ROWS["rows"]))

        # By this point `audit_retrieve` (above) has already written its own
        # `audit_ko`/`audit_en`-tagged queries and evidence through the same
        # ResearchStore the research stage uses (audit.py:306). Before J1
        # (ResearchStore.manifest stage-scoping), the second research_bundle's
        # unfiltered manifest() re-absorbed every one of those rows, so this is
        # the regression test for that fix: the republished bundle must carry
        # none of the audit stage's queries, edges, or evidence.
        with connect_database(self.run_root / "factory.sqlite3") as connection:
            bundle = json.loads(self.current(connection, "research_bundle")["content_json"])
            audit_query_ids = {
                row["query_id"] for row in connection.execute(
                    "SELECT query_id,plan_json FROM research_queries WHERE run_id=?", (RUN_ID,),
                )
                if json.loads(row["plan_json"]).get("term_kind", "").startswith("audit_")
            }
        self.assertTrue(audit_query_ids, "audit_retrieve should have tagged its own queries audit_* by now")
        bundle_term_kinds = sorted({
            json.loads(item["plan_json"]).get("term_kind", "") for item in bundle["queries"]
        })
        self.observations["assert_2_republished_bundle_term_kinds"] = bundle_term_kinds
        self.assertTrue(
            all(not kind.startswith("audit_") for kind in bundle_term_kinds),
            f"republished research_bundle leaked audit-stage term_kinds: {bundle_term_kinds}",
        )
        self.assertFalse(
            {item["query_id"] for item in bundle["queries"]} & audit_query_ids,
            "republished research_bundle retained an audit-tagged query",
        )
        self.assertFalse(
            {item["query_id"] for item in bundle["edges"]} & audit_query_ids,
            "republished research_bundle's edges retained an audit-tagged query",
        )

        # ---- ASSERTION 3: downstream revisions are stale, pointers dropped ---
        with connect_database(self.run_root / "factory.sqlite3") as connection:
            stale = {
                kind: connection.execute(
                    "SELECT stale FROM artifact_revisions WHERE revision_id=?", (revision_id,),
                ).fetchone()["stale"]
                for kind, revision_id in before.items()
            }
            pointers = {
                row["kind"]: row["revision_id"]
                for row in connection.execute(
                    "SELECT kind,revision_id FROM current_artifacts WHERE run_id=?", (RUN_ID,),
                )
            }
            decision_stale = connection.execute(
                "SELECT stale FROM gate_decisions WHERE run_id=? AND gate_id=?",
                (RUN_ID, gate_id),
            ).fetchone()["stale"]
        self.observations["assert_3_stale"] = stale
        self.observations["assert_3_current_pointers"] = sorted(pointers)
        self.observations["assert_3_expand_decision_stale"] = decision_stale
        for kind in ("candidate_set", "finalist_set", "corpus_set", "feature_map_set",
                     "audit_batch", "audit_query_set", "gate_resolution"):
            self.assertEqual(stale[kind], 1, f"{kind} should be stale")
            self.assertNotEqual(
                pointers.get(kind), before[kind], f"{kind} pointer should be dropped/replaced",
            )

        # ---- ASSERTION 4: how much must be redone to get forward again ------
        # First measure the floor: nothing downstream can be resumed in place,
        # because the finalist set the audit needs no longer exists.
        skip_code, skip_payload = self.attempt(
            "scaffold", "audit-query", "--run", self.run_rel, "--run-id", RUN_ID,
            "--out", ws_rel / "requests" / "skip-audit-query.json", "--workspace-root", ws_rel,
        )
        self.observations["assert_4_skip_ahead_refused"] = {
            "exit_code": skip_code,
            "failure_code": skip_payload.get("failure_code"),
            "message": skip_payload.get("message") or skip_payload.get("error"),
        }
        self.assertNotEqual(skip_code, 0)

        redone: list[str] = ["research (manual import)"]
        self.ideate_and_shortlist()
        redone.extend(["scaffold candidate + ideate", "scaffold shortlist + shortlist"])
        self.audit_retrieve()
        redone.append("scaffold audit-query + audit retrieve")
        rescored = self.audit_score(starve=False)
        redone.append("feature maps + audit score")
        self.observations["assert_4_stages_redone"] = redone
        self.observations["assert_4_final_status"] = rescored["status"]
        self.assertEqual(rescored["status"], "audit_approved")

        # ---- ASSERTION 5 (read side): nothing downstream carries plan_hash ---
        with connect_database(self.run_root / "factory.sqlite3") as connection:
            carriers = sorted({
                row["kind"] for row in connection.execute(
                    "SELECT kind,content_json FROM artifact_revisions WHERE run_id=?", (RUN_ID,),
                ) if "plan_hash" in row["content_json"]
            })
            plan_hash_references = sorted({
                row["kind"] for row in connection.execute(
                    "SELECT kind,content_json FROM artifact_revisions WHERE run_id=?", (RUN_ID,),
                ) if resolution["plan_hash"] in row["content_json"]
                and row["kind"] != "gate_resolution"
            })
        self.observations["assert_5_kinds_containing_plan_hash_field"] = carriers
        self.observations["assert_5_other_kinds_referencing_the_value"] = plan_hash_references
        self.assertEqual(carriers, ["gate_resolution"])
        self.assertEqual(plan_hash_references, [])
        self.observations["cli_exit_codes"] = self.exit_codes


if __name__ == "__main__":
    unittest.main()
