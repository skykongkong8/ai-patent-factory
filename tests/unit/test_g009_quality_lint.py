import unittest

from patent_factory.lint import MIN_CORPUS_RECORDS, audit_advisories, shortlist_advisories


def finalist(identity, scores):
    return {
        "finalist_id": identity,
        "axes": [
            {"axis": axis, "score": score}
            for axis, score in zip(
                ("differentiation", "technical_feasibility", "utility_significance"), scores,
            )
        ],
    }


class ShortlistAdvisoryTests(unittest.TestCase):
    def test_monotone_and_near_identical_vectors_are_flagged(self):
        advisories = shortlist_advisories([
            finalist("fi_1", (81, 81, 81)),
            finalist("fi_2", (80, 80, 80)),
            finalist("fi_3", (79, 79, 79)),
        ])
        codes = [item["code"] for item in advisories]
        self.assertEqual(codes.count("flat_axis_scores"), 3)
        self.assertEqual(codes.count("near_identical_finalists"), 3)
        subjects = {tuple(item["subjects"]) for item in advisories if item["code"] == "near_identical_finalists"}
        self.assertIn(("fi_1", "fi_2"), subjects)

    def test_genuinely_distinct_finalists_produce_no_advisories(self):
        advisories = shortlist_advisories([
            finalist("fi_1", (85, 70, 60)),
            finalist("fi_2", (60, 88, 75)),
            finalist("fi_3", (72, 61, 90)),
        ])
        self.assertEqual(advisories, [])


class AuditAdvisoryTests(unittest.TestCase):
    def test_thin_corpora_and_shared_closest_reference_are_flagged(self):
        corpus_set = {"corpora": [
            {"finalist_id": "fi_1", "retained_count": 1},
            {"finalist_id": "fi_2", "retained_count": MIN_CORPUS_RECORDS},
            {"finalist_id": "fi_3", "retained_count": 2},
        ]}
        audit = {"results": [
            {"finalist_id": "fi_1", "closest_reference_id": "ev_a"},
            {"finalist_id": "fi_2", "closest_reference_id": "ev_a"},
            {"finalist_id": "fi_3", "closest_reference_id": "ev_b"},
        ]}
        advisories = audit_advisories(corpus_set, audit)
        codes = [item["code"] for item in advisories]
        self.assertEqual(codes.count("thin_corpus"), 2)
        self.assertEqual(codes.count("shared_closest_reference"), 1)
        shared = next(item for item in advisories if item["code"] == "shared_closest_reference")
        self.assertEqual(shared["subjects"], ["fi_1", "fi_2"])

    def test_healthy_audit_produces_no_advisories(self):
        corpus_set = {"corpora": [{"finalist_id": "fi_1", "retained_count": 25}]}
        audit = {"results": [{"finalist_id": "fi_1", "closest_reference_id": "ev_a"}]}
        self.assertEqual(audit_advisories(corpus_set, audit), [])


if __name__ == "__main__":
    unittest.main()
