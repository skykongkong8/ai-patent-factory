"""Pins the `feature-map-set-input-v1.json` example documented in
`workspace/README.md` against the real input contract.

`schemas/feature-map.schema.json` describes the persisted output artifact, not
the input `/audit score` accepts — the authoritative input contract is
`similarity.validate_feature_map` (per-map fields, category weights, decision
shape). This test loads the exact JSON shown in the README and asserts the
real validator accepts it, so the doc cannot silently drift back into a
rejected shape.
"""

import json
import unittest
from pathlib import Path

from patent_factory.config import load_similarity_config
from patent_factory.similarity import canonical_feature_map, validate_feature_map

ROOT = Path(__file__).resolve().parents[2]
README_PATH = ROOT / "workspace" / "README.md"
START_MARKER = "<!-- feature-map-example:start -->"
END_MARKER = "<!-- feature-map-example:end -->"


def _extract_feature_map_example(readme_text: str) -> dict:
    start = readme_text.index(START_MARKER) + len(START_MARKER)
    end = readme_text.index(END_MARKER, start)
    block = readme_text[start:end]
    fence_start = block.index("```json") + len("```json")
    fence_end = block.index("```", fence_start)
    return json.loads(block[fence_start:fence_end])


class DocumentedFeatureMapContractTests(unittest.TestCase):
    def setUp(self):
        self.config = load_similarity_config()
        readme_text = README_PATH.read_text(encoding="utf-8")
        self.example = _extract_feature_map_example(readme_text)

    def test_example_is_the_documented_wrapper_shape(self):
        self.assertEqual(self.example["schema_version"], "feature-map-set-input-v1")
        self.assertIn("finalist_set_hash", self.example)
        self.assertIn("corpus_set_hash", self.example)
        maps = self.example["maps"]
        self.assertTrue(maps)
        for item in maps:
            self.assertEqual(set(item), {"feature_map", "finalist_id", "map_id"})

    def test_every_documented_feature_map_is_accepted_by_the_input_validator(self):
        for item in self.example["maps"]:
            canonical = canonical_feature_map(item["feature_map"])
            validate_feature_map(canonical, self.config)  # must not raise


if __name__ == "__main__":
    unittest.main()
