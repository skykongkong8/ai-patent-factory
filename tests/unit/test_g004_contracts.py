import json
import unittest
from pathlib import Path

from patent_factory.config import load_evaluation_config
from patent_factory.evaluation import REQUIRED_AXES


ROOT = Path(__file__).resolve().parents[2]


class G004ContractTests(unittest.TestCase):
    def test_defaults_are_exactly_three_preliminary_independent_rubrics(self):
        config = load_evaluation_config(ROOT / "config/defaults.json")
        self.assertEqual(config.minimum_finalists, 3)
        self.assertEqual(set(config.rubrics), set(REQUIRED_AXES))
        self.assertFalse(config.rubrics["differentiation"].startswith("simrisk-"))
        self.assertEqual(len(config.content_hash), 64)

    def test_documented_candidate_and_finalist_required_fields_match_runtime_outputs(self):
        candidate = json.loads((ROOT / "schemas/candidate.schema.json").read_text(encoding="utf-8"))
        finalist = json.loads((ROOT / "schemas/finalist.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(candidate["required"]),
            {
                "candidate_id", "claims", "components", "domain", "evaluation_config_hash",
                "evidence_references", "expected_effects", "implementation_example",
                "interactions", "mechanism", "measurable_validation", "outputs",
                "profile_references", "profile_revision_hash", "required_inputs",
                "research_revision_hash", "synthesis_trace", "technical_problem", "title",
                "transformations", "unresolved_dependencies", "unresolved_questions",
            },
        )
        self.assertEqual(
            set(finalist["required"]),
            {
                "axes", "candidate_id", "candidate_revision_hash", "finalist_id", "rank",
                "selection_priority", "selection_rationale",
            },
        )


if __name__ == "__main__":
    unittest.main()
