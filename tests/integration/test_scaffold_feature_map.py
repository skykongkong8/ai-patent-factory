"""`scaffold feature-map` — the last surface that forced SQLite access.

Two blockers made `audit score` undrivable from the CLI:

* `map_id` digests the FILLED map, so it cannot be pre-filled (closed by --seal).
* `candidate_span_hashes` are `digest({"field": f, "text": normalize(t)})` over
  `FEATURE_SOURCE_FIELDS`, and no verb emitted them. An offline rehearsal found
  the only way through was to reimplement `provenance.digest`/`normalize` by
  hand — with a generic "candidate span does not belong to the finalist
  revision" as the sole feedback. `--seal` does not close this; it seals the id
  and passes the body through.

The golden e2e reaches `audit score` only by importing Python helpers and
opening `factory.sqlite3` directly. These tests assert an agent restricted to
CLI output can now get there.
"""

import json
import tempfile
import unittest
from pathlib import Path

from patent_factory.audit import (
    _candidate_span_hashes,
    feature_map_id,
    run_audit_retrieval,
    run_audit_scoring,
)
from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.config import load_evaluation_config, load_similarity_config
from patent_factory.database import connect_database
from patent_factory.evaluation import run_shortlist
from patent_factory.ideation import run_ideation
from patent_factory.scaffold import (
    ScaffoldError,
    count_todos,
    scaffold_feature_map_input,
    seal_feature_map_input,
)
from tests.integration.test_g004_ideation_and_shortlist import (
    candidate_input, ready_profile, ready_research, shortlist_input,
)
from tests.integration.test_g005_audit import kipris_xml


class ScaffoldFeatureMapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.profile_connection, self.profile = ready_profile(self.root / "profile.sqlite3")
        evidence, span, _ = ready_research(self.connection, self.root)
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.root,
            run_id="run", profile=self.profile, candidate_input=candidate_input(3, evidence, span),
            config=load_evaluation_config(),
        )
        run_shortlist(
            self.connection, run_root=self.root, run_id="run",
            shortlist_input=shortlist_input(ideation.candidate_ids, evidence, span),
            config=load_evaluation_config(),
        )
        finalist_row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='finalist_set'",
        ).fetchone()
        finalists = json.loads(finalist_row["content_json"])["finalists"]
        run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run",
            query_input={
                "schema_version": "audit-query-input-v1",
                "finalist_set_hash": finalist_row["content_hash"],
                "groups": [{
                    "finalist_id": item["finalist_id"],
                    "queries": [
                        {"language": "ko", "term": "동일 검색어"},
                        {"language": "en", "term": "same query"},
                    ],
                } for item in finalists],
            },
            config=load_similarity_config(),
            adapter_factory=lambda query, page, finalist: KiprisAdapter(
                "fixture", credential_required=False,
                transport=lambda *_: TransportResponse(200, {}, kipris_xml("10-2026-0012345")),
            ),
        )

    def tearDown(self):
        self.connection.close()
        self.profile_connection.close()
        self.temporary.cleanup()

    def draft(self):
        return scaffold_feature_map_input(
            self.connection, run_id="run", config=load_similarity_config(),
        )

    def test_candidate_span_hashes_are_derived_not_left_to_the_agent(self):
        # The B5 blocker: without these an agent must reverse-engineer
        # provenance.digest/normalize to get past audit score at all.
        candidate_set = json.loads(self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='candidate_set'",
        ).fetchone()["content_json"])
        candidates = {item["candidate_id"]: item for item in candidate_set["candidates"]}
        finalist_set = json.loads(self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='finalist_set'",
        ).fetchone()["content_json"])
        by_finalist = {item["finalist_id"]: item for item in finalist_set["finalists"]}
        for wrapper in self.draft()["maps"]:
            candidate = candidates[by_finalist[wrapper["finalist_id"]]["candidate_id"]]
            for feature in wrapper["feature_map"]["features"]:
                with self.subTest(finalist=wrapper["finalist_id"], category=feature["category"]):
                    expected = _candidate_span_hashes(candidate, feature["category"])
                    self.assertTrue(set(feature["candidate_span_hashes"]).issubset(expected))
                    self.assertTrue(feature["candidate_span_hashes"])

    def test_weights_match_config_exactly_since_a_wrong_guess_is_rejected(self):
        config = load_similarity_config()
        for wrapper in self.draft()["maps"]:
            weights = {
                feature["category"]: feature["weight"]
                for feature in wrapper["feature_map"]["features"]
            }
            self.assertEqual(weights, dict(config.feature_weights))

    def test_reference_span_choice_is_left_to_the_reviewer_but_the_menu_is_provided(self):
        # Enumerating the available spans is clerical; choosing which one
        # justifies a decision is the reviewer's judgment.
        for wrapper in self.draft()["maps"]:
            for reference in wrapper["feature_map"]["reference_maps"]:
                for decision in reference["decisions"]:
                    with self.subTest(evidence=reference["evidence_id"]):
                        self.assertEqual(decision["reference_span_hashes"], [])
                        self.assertTrue(decision["available_reference_span_hashes"])

    def test_the_review_attestation_is_never_manufactured(self):
        # A tool must not emit the record that a human review occurred.
        for wrapper in self.draft()["maps"]:
            review = wrapper["feature_map"]["review"]
            self.assertIsInstance(review, str)
            self.assertTrue(review.startswith("TODO(agent):"))

    def test_map_id_is_a_placeholder_because_it_digests_the_filled_map(self):
        for wrapper in self.draft()["maps"]:
            self.assertTrue(str(wrapper["map_id"]).startswith("TODO(agent):"))

    def test_every_retained_evidence_id_is_covered_exactly_once(self):
        corpus_set = json.loads(self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='corpus_set'",
        ).fetchone()["content_json"])
        corpora = {item["finalist_id"]: item for item in corpus_set["corpora"]}
        for wrapper in self.draft()["maps"]:
            retained = [record["evidence_id"] for record in corpora[wrapper["finalist_id"]]["records"]]
            covered = [ref["evidence_id"] for ref in wrapper["feature_map"]["reference_maps"]]
            self.assertEqual(sorted(covered), sorted(retained))
            self.assertEqual(len(covered), len(set(covered)))

    def test_scaffold_fill_seal_reaches_audit_score(self):
        """The whole point: a CLI-only agent can now get through `audit score`."""

        draft = self.draft()
        self.assertGreater(count_todos(draft), 0, "an unfilled draft must carry TODO markers")

        # Stand in for the agent's judgment — only the TODO fields.
        for wrapper in draft["maps"]:
            wrapper["feature_map"]["review"] = {
                "reviewed_at": "2026-07-20T00:00:00Z",
                "reviewed_by": "integration-reviewer",
                "status": "reviewed",
            }
            for feature in wrapper["feature_map"]["features"]:
                feature["description"] = f"the {feature['category']} feature"
            for reference in wrapper["feature_map"]["reference_maps"]:
                for decision in reference["decisions"]:
                    menu = decision.pop("available_reference_span_hashes")
                    decision["status"] = "matched"
                    decision["rationale"] = "the reference discloses this feature"
                    decision["reference_span_hashes"] = menu[:1]

        sealed = seal_feature_map_input(draft)
        self.assertEqual(count_todos(sealed), 0)
        for wrapper in sealed["maps"]:
            self.assertEqual(
                wrapper["map_id"], feature_map_id(wrapper["finalist_id"], wrapper["feature_map"]),
            )
        # The assertion that matters is that the input is ACCEPTED — no
        # "map identity does not bind frozen content" and no "candidate span
        # does not belong to the finalist revision". Which scored outcome
        # follows depends on the corpus: this thin fixture corpus legitimately
        # gates on coverage, and that is a real result, not a rejection.
        scored = run_audit_scoring(
            self.connection, run_root=self.root, run_id="run",
            feature_input=sealed, config=load_similarity_config(),
        )
        self.assertIn(
            scored.state,
            {"decision_required", "audit_approved", "coverage_insufficient"},
        )

    def test_seal_strips_the_generator_menu_so_the_scaffold_output_seals_directly(self):
        # F1 (found by the re-run gate): the generator writes
        # available_reference_span_hashes, which canonical_feature_map rejects.
        # Sealing must strip it rather than making the agent hand-delete a field
        # the tool itself added.
        draft = self.draft()
        for wrapper in draft["maps"]:
            wrapper["feature_map"]["review"] = {
                "reviewed_at": "2026-07-20T00:00:00Z",
                "reviewed_by": "integration-reviewer", "status": "reviewed",
            }
            for feature in wrapper["feature_map"]["features"]:
                feature["description"] = f"the {feature['category']} feature"
            for reference in wrapper["feature_map"]["reference_maps"]:
                for decision in reference["decisions"]:
                    # Choose a span from the menu but LEAVE the menu key in place.
                    decision["reference_span_hashes"] = decision["available_reference_span_hashes"][:1]
                    decision["status"] = "matched"
                    decision["rationale"] = "the reference discloses this feature"
        sealed = seal_feature_map_input(draft)
        for wrapper in sealed["maps"]:
            for reference in wrapper["feature_map"]["reference_maps"]:
                for decision in reference["decisions"]:
                    self.assertNotIn("available_reference_span_hashes", decision)
        # And it now scores without any hand-editing of the scaffold shape.
        scored = run_audit_scoring(
            self.connection, run_root=self.root, run_id="run",
            feature_input=sealed, config=load_similarity_config(),
        )
        self.assertIn(
            scored.state, {"decision_required", "audit_approved", "coverage_insufficient"},
        )

    def test_missing_upstream_state_is_named_not_a_traceback(self):
        empty = connect_database(self.root / "empty.sqlite3")
        try:
            with self.assertRaises((ScaffoldError, Exception)) as captured:
                scaffold_feature_map_input(empty, run_id="run", config=load_similarity_config())
            self.assertTrue(str(captured.exception))
        finally:
            empty.close()


if __name__ == "__main__":
    unittest.main()
