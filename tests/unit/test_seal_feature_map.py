"""`seal_feature_map_input` — re-derives map_id, never edits judgment.

`map_id` digests the canonicalized *filled* map, so it cannot be pre-filled by a
scaffold and cannot be authored by hand. Sealing is the clerical last step; these
tests pin the boundary between "clerical" and "judgment".
"""

import json
import unittest

from patent_factory.audit import feature_map_id
from patent_factory.scaffold import ScaffoldError, seal_feature_map_input
from tests.unit.test_g005_similarity import feature_map

# Reuse the real map shape the audit validator accepts, rather than inventing one.
BASE_MAP = feature_map()


def payload(**overrides):
    base = {
        "schema_version": "feature-map-set-input-v1",
        "finalist_set_hash": "a" * 64,
        "corpus_set_hash": "b" * 64,
        "maps": [{"feature_map": BASE_MAP, "finalist_id": "fi_1", "map_id": "fm_wrong"}],
    }
    return {**base, **overrides}


class SealFeatureMapTests(unittest.TestCase):
    def test_seal_derives_the_identity_audit_will_demand(self):
        sealed = seal_feature_map_input(payload())
        self.assertEqual(sealed["maps"][0]["map_id"], feature_map_id("fi_1", BASE_MAP))

    def test_seal_is_idempotent(self):
        once = seal_feature_map_input(payload())
        self.assertEqual(seal_feature_map_input(once), once)

    def test_seal_preserves_every_judgment_field_and_sibling_key(self):
        sealed = seal_feature_map_input(payload())
        self.assertEqual(sealed["maps"][0]["feature_map"], BASE_MAP)
        self.assertEqual(sealed["finalist_set_hash"], "a" * 64)
        self.assertEqual(sealed["corpus_set_hash"], "b" * 64)

    def test_identity_tracks_content_so_an_edit_changes_the_seal(self):
        edited = feature_map(status="different")
        first = seal_feature_map_input(payload())["maps"][0]["map_id"]
        second = seal_feature_map_input(payload(
            maps=[{"feature_map": edited, "finalist_id": "fi_1", "map_id": "fm_wrong"}],
        ))["maps"][0]["map_id"]
        self.assertNotEqual(first, second)

    def test_unfilled_map_is_refused_rather_than_sealed(self):
        # Sealing an unfilled map would mint a valid identity for placeholder text,
        # which is exactly how a 0/0/0 finalist set reached the committed golden.
        unfilled = json.loads(json.dumps(BASE_MAP))
        unfilled["reference_maps"][0]["decisions"][0]["rationale"] = "TODO(agent): why it matches"
        with self.assertRaisesRegex(ScaffoldError, "TODO"):
            seal_feature_map_input(payload(
                maps=[{"feature_map": unfilled, "finalist_id": "fi_1", "map_id": "fm_wrong"}],
            ))

    def test_malformed_inputs_are_refused(self):
        with self.assertRaisesRegex(ScaffoldError, "schema_version"):
            seal_feature_map_input(payload(schema_version="feature-map-set-v1"))
        with self.assertRaisesRegex(ScaffoldError, "non-empty list"):
            seal_feature_map_input(payload(maps=[]))
        with self.assertRaisesRegex(ScaffoldError, "finalist_id"):
            seal_feature_map_input(payload(maps=[{"feature_map": BASE_MAP}]))
        with self.assertRaisesRegex(ScaffoldError, "unexpected fields"):
            seal_feature_map_input(payload(maps=[
                {"feature_map": BASE_MAP, "finalist_id": "fi_1", "smuggled": 1},
            ]))


if __name__ == "__main__":
    unittest.main()
