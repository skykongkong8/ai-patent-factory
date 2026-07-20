"""The `as_dict()` serialization boundary, pinned.

There are two independent hash chains hanging off an `AdapterRecord`:

1. `content_hash` / `evidence_id` — `digest(normalized_record)`, computed inside
   the adapter. Drives dedup and `UNIQUE(run_id, source_locator, content_hash)`.
2. `corpus_set` — `as_dict()` -> `canonical_json` -> `record_json`
   (`research.py`) -> embedded verbatim as `"record"` in each retained entry
   (`corpus.py`) -> `add_revision("corpus_set")` -> `digest`. From there it
   cascades to `feature_map_set` -> `audit_batch` -> report bindings.

A field can miss chain 1 and still hit chain 2. The legal-status fields are
carried on the dataclass but kept out of `as_dict()` so they change neither.

`as_dict()` is safe only because it enumerates its keys as a literal. A refactor
to `{f.name: getattr(self, f.name) for f in fields(self)}` would silently pull
every dataclass field into the corpus hash. That is what this file exists to
catch.
"""

import unittest

from patent_factory.models import AdapterRecord
from patent_factory.provenance import canonical_json

# The 16 original keys plus the five #42 legal-status fields, which entered
# `as_dict()` as a DECLARED change inside the input-schema batch (the golden is
# regenerated once alongside it). Adding a key here re-mints every corpus_set
# hash and everything bound to it, so a change to this set must be deliberate
# and batched — never incidental.
FROZEN_AS_DICT_KEYS = {
    "abstract", "applicant", "canonical_url", "classifications", "content_hash",
    "excerpt_hashes", "field_span_hashes", "filing_date", "interpretations",
    "language", "limitations", "original_identifier", "provenance",
    "source_locator", "source_type", "title",
    "open_date", "publication_date", "register_date", "register_number", "register_status",
}

# corpus._patent_identity resolves dedup identity in this order. `as_dict()`
# emits none of the first two, so identity currently falls through to
# `original_identifier`. A field named to collide with either would move the
# dedup KEY, not merely its hash — a strictly larger blast radius.
IDENTITY_PREFERRED_KEYS = ("application_number", "publication_number")


def record(**overrides):
    base = {
        "source_type": "kipris_patent", "source_locator": "kr-patent:1020160062884",
        "original_identifier": "1020160062884", "title": "제목", "content_hash": "c" * 64,
        "language": "ko",
    }
    return AdapterRecord(**{**base, **overrides})


LEGAL_STATUS = {
    "register_status": "소멸", "register_date": "20181206", "register_number": "1019284170000",
    "open_date": "20171204", "publication_date": "20181213",
}


class AdapterRecordHashBoundaryTests(unittest.TestCase):
    def test_as_dict_key_set_is_frozen(self):
        # Asserted against a FULLY populated record: legal_status_metadata()
        # omits absent values, so the maximal key set is the invariant.
        #
        # If this fails, decide deliberately — adding a key re-mints every
        # corpus_set hash and must land inside a declared hash batch. It is safe
        # only because as_dict() enumerates keys literally; a refactor to
        # {f.name: getattr(self, f.name) for f in fields(self)} would dissolve
        # the boundary silently, which is exactly what this test prevents.
        self.assertEqual(set(record(**LEGAL_STATUS).as_dict()), FROZEN_AS_DICT_KEYS)

    def test_legal_status_fields_reach_the_corpus_serialization_by_design(self):
        # Carried so the report appendix can state a reference's source-reported
        # status. That IS a hashed surface, which is why it landed inside the
        # declared batch rather than as a drive-by addition.
        annotated = record(**LEGAL_STATUS)
        for name, value in LEGAL_STATUS.items():
            self.assertEqual(annotated.as_dict()[name], value)

    def test_a_record_without_status_metadata_serializes_exactly_as_before(self):
        # Unrelated corpora must not churn just because the fields now exist.
        self.assertEqual(set(record().as_dict()), FROZEN_AS_DICT_KEYS - set(LEGAL_STATUS))

    def test_status_fields_never_reach_content_hash(self):
        # The invariant that actually matters: register_status is mutable
        # (등록 -> 소멸), so it must stay OUT of the adapter's normalized_record.
        # Hashing it would re-mint evidence_id whenever an upstream status
        # changed, breaking dedup, idempotency and replay.
        plain, annotated = record(), record(**LEGAL_STATUS)
        self.assertEqual(plain.content_hash, annotated.content_hash)

    def test_legal_status_fields_are_still_reachable_on_the_dataclass(self):
        annotated = record(**LEGAL_STATUS)
        self.assertEqual(annotated.register_status, "소멸")
        self.assertEqual(annotated.legal_status_metadata(), LEGAL_STATUS)

    def test_legal_status_metadata_omits_absent_values(self):
        self.assertEqual(record().legal_status_metadata(), {})
        self.assertEqual(
            record(register_status="등록").legal_status_metadata(), {"register_status": "등록"},
        )

    def test_no_as_dict_key_collides_with_corpus_identity_resolution(self):
        for name in IDENTITY_PREFERRED_KEYS:
            self.assertNotIn(name, record(**LEGAL_STATUS).as_dict())

    def test_status_tokens_are_carried_verbatim_and_never_translated(self):
        # CLAUDE.md section 6: reporting the retrieved token is in scope;
        # rendering it as "expired"/"invalid"/"lapsed" is a legal conclusion.
        annotated = record(register_status="소멸")
        self.assertEqual(annotated.legal_status_metadata()["register_status"], "소멸")


if __name__ == "__main__":
    unittest.main()
