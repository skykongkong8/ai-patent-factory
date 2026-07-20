import json
import unittest

from patent_factory.config import load_similarity_config
from patent_factory.provenance import digest
from patent_factory.report import publish_report
from patent_factory.similarity import canonical_feature_map, validate_feature_map
from tests.integration.test_g007_report_review_validation import G007Fixture
from tests.unit.test_g005_similarity import feature_map


class FeatureDescriptionContractTests(unittest.TestCase):
    def test_optional_description_is_canonical_hash_bound_and_validated(self):
        base = feature_map()
        base["features"][0]["description"] = "runtime entropy-adaptive precision control"
        canonical = canonical_feature_map(base)
        feature_id = "feature-problem"
        self.assertEqual(
            canonical["features"][feature_id]["description"],
            "runtime entropy-adaptive precision control",
        )
        without = canonical_feature_map(feature_map())
        self.assertNotIn("description", without["features"][feature_id])
        self.assertNotEqual(digest(canonical), digest(without))
        validate_feature_map(canonical, load_similarity_config())
        validate_feature_map(without, load_similarity_config())

    def test_blank_description_and_unknown_fields_are_rejected(self):
        blank = feature_map()
        blank["features"][0]["description"] = "  "
        with self.assertRaisesRegex(ValueError, "description"):
            validate_feature_map(canonical_feature_map(blank), load_similarity_config())
        unknown = feature_map()
        unknown["features"][0]["label"] = "not-allowed"
        with self.assertRaisesRegex(ValueError, "exact fields"):
            canonical_feature_map(unknown)


class FeatureDescriptionReportTests(G007Fixture):
    def _describe_features(self):
        feature_row = self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='feature_map_set'",
        ).fetchone()
        feature_content = json.loads(feature_row["content_json"])
        for index, entry in enumerate(feature_content["maps"], start=1):
            entry["feature_map"] = {"features": {
                f"df_{index}": {
                    "candidate_span_hashes": ["span"], "category": "mechanism",
                    "description": f"runtime-signal control loop {index}", "essential": True,
                    "weight": "0.30",
                },
                f"mf_{index}": {
                    "candidate_span_hashes": ["span"], "category": "problem",
                    "essential": True, "weight": "0.10",
                },
            }}
        audit_row = self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='audit_batch'",
        ).fetchone()
        audit_content = json.loads(audit_row["content_json"])
        new_feature = self.store.add_revision(
            "run", "feature_map_set", feature_content, schema_version="feature-map-set-v1",
        )
        audit_content["feature_map_set_hash"] = new_feature.content_hash
        self.audit = self.store.add_revision(
            "run", "audit_batch", audit_content, schema_version="audit-batch-v1",
        )

    def test_described_features_replace_ids_and_undamaged_ids_fall_back(self):
        self._describe_features()
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run",
            report_input=self.draft_input(),
        )
        markdown = report.artifact.content["markdown"]
        self.assertIn("- 차별화 특징: runtime-signal control loop 1", markdown)
        self.assertIn("차이=runtime-signal control loop 1", markdown)
        # mf_1 has no description — the stable ID remains as the fallback.
        self.assertIn("일치=mf_1", markdown)
        self.assertNotIn("차이=df_1", markdown)


if __name__ == "__main__":
    unittest.main()
