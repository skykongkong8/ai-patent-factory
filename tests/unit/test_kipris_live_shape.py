"""Regression tests bound to a RECORDED live KIPRIS response.

The hand-authored fixture in tests/fixtures/kipris/word-search-v1.xml nests the
pagination elements inside <body>, a shape the live service never returns. Every
adapter test passed against that invented structure while the live path failed
100% of the time. These tests are pinned to a real recorded response
(word-search-live-v1.xml, key-scrubbed, responseTime pinned) so that the mocked
suite and reality cannot drift apart again.
"""
import unittest
from pathlib import Path

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.config import load_similarity_config
from patent_factory.similarity import _classification_similarity

from .test_g003_adapters import envelope

LIVE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kipris" / "word-search-live-v1.xml"


def live_adapter():
    body = LIVE_FIXTURE.read_bytes()
    return KiprisAdapter("secret", transport=lambda *_: TransportResponse(200, {}, body))


class LiveResponseShapeTests(unittest.TestCase):
    def test_pagination_outside_body_still_parses(self):
        """The live service puts <count> as a SIBLING of <body>, not inside it."""
        result = live_adapter().search(envelope())
        self.assertIsNone(result.failure)
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.coverage["total_count"], 151001)

    def test_recorded_fixture_really_has_count_outside_body(self):
        """Guards the fixture itself: if it is ever 'fixed' to the invented shape
        by nesting <count> in <body>, the regression oracle is lost."""
        import xml.etree.ElementTree as ET

        root = ET.fromstring(LIVE_FIXTURE.read_bytes())
        self.assertEqual([child.tag for child in root], ["header", "body", "count"])
        self.assertIsNone(root.find("body").find(".//totalCount"))
        self.assertIsNotNone(root.find("count").find("totalCount"))

    def test_pipe_delimited_classifications_are_split_into_individual_codes(self):
        """Live ipcNumber packs several codes into one element with '|'."""
        record = live_adapter().search(envelope()).records[0]
        self.assertEqual(
            record.classifications,
            ("G01S 13/02", "G01S 13/62", "G01S 13/88", "G06M 1/10"),
        )
        self.assertTrue(all("|" not in value for value in record.classifications))

    def test_exact_subgroup_match_is_not_lost_to_an_unsplit_blob(self):
        """Split codes score an exact IPC subgroup hit at 1.0. An unsplit blob
        never does: the regex strips '|', so the candidate is compared against
        one run-on token and the result depends on which code happens to sit at
        the front of the blob -- 'main_group' if the prefixes collide by luck,
        'unrelated' otherwise. Both are wrong, and both bias the score low."""
        config = load_similarity_config()
        records = live_adapter().search(envelope()).records

        for candidate, record in (("G01S 13/62", records[0]), ("H03K 17/687", records[1])):
            with self.subTest(candidate=candidate):
                score, available = _classification_similarity((candidate,), record.classifications, config)
                self.assertTrue(available)
                self.assertEqual(score, 1, "split codes must score an exact subgroup match")

        # Same data unsplit: never 1.0, and the error depends on blob ordering.
        lucky_prefix, _ = _classification_similarity(
            ("G01S 13/62",), ("G01S 13/88|G01S 13/62|G01S 13/02|G06M 1/10",), config)
        no_prefix, _ = _classification_similarity(
            ("H03K 17/687",), ("G01R 31/12|H03K 17/687|H02M 7/06",), config)
        self.assertNotEqual(lucky_prefix, 1)
        self.assertEqual(no_prefix, 0)


if __name__ == "__main__":
    unittest.main()
