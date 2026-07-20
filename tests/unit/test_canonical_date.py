"""`canonical_date` — issue #41, fixed against recorded evidence.

The same patent (1020160062884) is returned by KIPRIS `word_search` with
`applicationDate` `20160523` and by `bibliography_summary` as `2016.05.23`.
Google Patents supplies a third form, `2020-11-20`. All three feed
`digest(normalized_record)`, so a formatting difference alone produced different
`content_hash` values for one reference.

Two things this file is careful about:

* A digits-only normalization would be a **no-op across the entire pre-existing
  suite**, because every fixture date was already 8 digits — the suite would
  have given a false all-clear. The dotted form is therefore asserted against
  the RECORDED bibliography response, not against a hand-written string.
* Canonicalizing the date does NOT make the two capabilities' hashes equal, and
  this file says so explicitly. `bibliography_summary` genuinely omits abstract,
  applicant and classifications, so the records differ in content, not just in
  formatting. Claiming #41 "fully resolved" would overstate the fix.
"""

import unittest
from pathlib import Path

from patent_factory.adapters.base import TransportResponse, canonical_date
from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.models import QueryEnvelope

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kipris"


def envelope(capability, projection):
    return QueryEnvelope(
        run_id="r", adapter="kipris", adapter_version="plus-xml-v1", capability=capability,
        allowed_scheme="https", allowed_host="plus.kipris.or.kr", deadline_seconds=10,
        page=1, page_cap=5, result_budget=10, byte_budget=1_000_000, retry_budget=0,
        retry_ownership="t", query_projection=projection,
    )


def parse(fixture, capability, projection):
    body = (FIXTURES / fixture).read_bytes()
    adapter = KiprisAdapter("k", transport=lambda *_: TransportResponse(200, {}, body))
    return adapter.search(envelope(capability, projection))


class CanonicalDateTests(unittest.TestCase):
    def test_every_observed_service_format_maps_to_one_canonical_form(self):
        for raw in ("20160523", "2016.05.23", "2016-05-23", "2016/05/23", " 20160523 "):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_date(raw), "2016-05-23")

    def test_unrecognizable_values_are_returned_untouched_not_guessed(self):
        # A wrong canonicalization would silently merge two distinct references,
        # which is worse than leaving one unnormalized.
        for raw in ("", "unknown", "2016", "201605", "9999999999999", "abcdefgh"):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_date(raw), raw)
        self.assertIsNone(canonical_date(None))

    def test_implausible_components_are_left_alone(self):
        for raw in ("20161345", "20160599", "99160523"):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_date(raw), raw)


class RecordedDivergenceTests(unittest.TestCase):
    """Bound to real recorded bytes from both capabilities — the #41 oracle."""

    def records(self):
        word = parse(
            "word-search-live-v1.xml", "word_search",
            {"word": "x", "year": 0, "patent": True, "utility": True},
        )
        bibliography = parse(
            "bibliography-summary-live-v1.xml", "bibliography_summary",
            {"application_number": "1020160062884"},
        )
        same = [
            record for record in word.records
            if record.original_identifier.replace("-", "") == "1020160062884"
        ]
        return same[0], bibliography.records[0]

    def test_the_recorded_responses_really_do_use_different_date_formats(self):
        # Guards the oracle itself: if a re-record ever makes both formats
        # identical, this fix loses the evidence that motivated it.
        word_bytes = (FIXTURES / "word-search-live-v1.xml").read_bytes()
        bibliography_bytes = (FIXTURES / "bibliography-summary-live-v1.xml").read_bytes()
        self.assertIn(b"<applicationDate>20160523</applicationDate>", word_bytes)
        self.assertIn(b"<applicationDate>2016.05.23</applicationDate>", bibliography_bytes)

    def test_both_capabilities_now_agree_on_the_filing_date(self):
        word_record, bibliography_record = self.records()
        self.assertEqual(word_record.filing_date, "2016-05-23")
        self.assertEqual(bibliography_record.filing_date, "2016-05-23")

    def test_adopted_status_dates_are_canonicalized_too(self):
        # #41 asks for the same treatment on openDate/registerDate/publicationDate
        # "if they are ever adopted". They were, so they are.
        word_record, bibliography_record = self.records()
        self.assertEqual(word_record.register_date, "2018-12-06")
        self.assertEqual(bibliography_record.register_date, "2018-12-06")
        self.assertEqual(bibliography_record.open_date, "2017-12-04")

    def test_identity_agrees_across_capabilities(self):
        word_record, bibliography_record = self.records()
        self.assertEqual(word_record.source_locator, bibliography_record.source_locator)

    def test_residual_hash_difference_is_real_content_not_formatting(self):
        """Honest scope: the fix removes the SPURIOUS difference, not every one.

        `bibliography_summary` omits abstract, applicant and classifications
        entirely, so the two records carry genuinely different content and
        therefore still hash differently. That is correct behaviour, not a
        surviving bug — but it does mean retrieving one patent through both
        capabilities yields two corpus entries, which is why corpus dedup keying
        on (identity, content_hash) is worth revisiting if both capabilities are
        ever routed into the same audit corpus.
        """

        word_record, bibliography_record = self.records()
        self.assertNotEqual(word_record.content_hash, bibliography_record.content_hash)
        self.assertIsNotNone(word_record.abstract)
        self.assertIsNone(bibliography_record.abstract)
        self.assertIsNone(bibliography_record.applicant)
        self.assertEqual(bibliography_record.classifications, ())


if __name__ == "__main__":
    unittest.main()
