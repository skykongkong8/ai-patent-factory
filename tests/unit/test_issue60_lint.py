import unittest

from patent_factory.lint import candidate_advisories, workbench_advisories


class Issue60LintTests(unittest.TestCase):
    def test_candidate_advisories_are_nonblocking_and_do_not_echo_titles(self):
        candidates = [
            {
                "title": "PRIVATE semantic title A",
                "synthesis_trace": {"method": "combine"},
                "evidence_references": [{"evidence_id": "ev_1"}],
                "profile_references": [{"field": "expertise", "claim_id": "cl_1", "kind": "capability"}],
            },
            {
                "title": "PRIVATE semantic title B",
                "synthesis_trace": {"method": "combine"},
                "evidence_references": [{"evidence_id": "ev_1"}],
                "profile_references": [{"field": "expertise", "claim_id": "cl_1", "kind": "capability"}],
            },
        ]
        advisories = candidate_advisories(candidates)
        codes = {item["code"] for item in advisories}
        self.assertEqual(codes, {"identical_evidence_sets", "identical_profile_reference_sets", "single_synthesis_method"})
        encoded = repr(advisories)
        self.assertNotIn("PRIVATE", encoded)
        self.assertIn("candidate[0]", encoded)

    def test_workbench_advisories_use_source_and_target_relation_keys(self):
        ideas = [
            {"idea_id": "idea_a", "session_id": "s1", "lens": "same"},
            {"idea_id": "idea_b", "session_id": "s2", "lens": "same"},
        ]
        self.assertFalse(any(
            item["code"] == "no_cross_session_relation"
            for item in workbench_advisories(
                ideas,
                [{"source_idea_ids": ["idea_a"], "target_idea_ids": ["idea_b"]}],
            )
        ))


if __name__ == "__main__":
    unittest.main()
