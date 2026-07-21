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

    def test_legal_status_metadata_is_carried_without_disturbing_content_hash(self):
        """#42 fields ride the dataclass; they must not touch either hash chain.

        These pins moved exactly ONCE, deliberately, when #41's date
        canonicalization landed (filing_date 20160523 -> 2016-05-23). That was a
        declared hash change inside the input batch, with the golden regenerated
        alongside it.

        Adopting the #42 legal-status fields did NOT move them, which is the
        point of keeping those fields out of `as_dict()`: verified by stashing
        the change and re-parsing this fixture. If these values move again
        without a declared batch, evidence_id churns and dedup, idempotency and
        replay all break.
        """
        records = live_adapter().search(envelope()).records
        self.assertEqual(
            [record.content_hash for record in records],
            [
                "8bbc4f42532ed33787d86ad1b2b67c5250fac9373359d2ae5b9e71a928b6767b",
                "d15d49665c93b4b98a4a4f77847c58748332c1cf97cf8662e03b2885a298dfe5",
            ],
        )
        # Real recorded values, including a mutable status — the reason these
        # fields are kept out of the hash in the first place.
        self.assertEqual(records[0].legal_status_metadata(), {
            "open_date": "2017-12-04", "publication_date": "2018-12-13",
            "register_date": "2018-12-06", "register_number": "1019284170000",
            "register_status": "소멸",
        })
        self.assertEqual(records[1].register_status, "등록")

    def test_recorded_fixture_carries_two_distinct_register_statuses(self):
        """Load-bearing for the status_variant coverage predicate.

        Both records are required: one 소멸, one 등록. If a re-record or a
        key-scrub drops either, status_variant silently loses its only witness.
        """
        records = live_adapter().search(envelope()).records
        self.assertEqual({record.register_status for record in records}, {"소멸", "등록"})

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
