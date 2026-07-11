import unittest

from patent_factory.provenance import Claim, EpistemicLabel, claim_from_dict


class ProvenanceTests(unittest.TestCase):
    def test_source_fact_requires_evidence_binding(self):
        with self.assertRaisesRegex(ValueError, "content_hash"):
            Claim(EpistemicLabel.SOURCE_FACT, source_id="src_x").validate("fact.x")

    def test_agent_inference_requires_rationale(self):
        with self.assertRaisesRegex(ValueError, "rationale"):
            Claim(EpistemicLabel.AGENT_INFERENCE).validate("fact.x")
        claim = claim_from_dict({"label": "agent_inference", "rationale": "두 사용자 진술의 공통점"})
        self.assertEqual(claim.as_dict()["label"], "agent_inference")

    def test_user_statement_is_distinct(self):
        claim = Claim(EpistemicLabel.USER_STATEMENT, source_id="interview-v1").as_dict()
        self.assertEqual(claim["label"], "user_statement")


if __name__ == "__main__":
    unittest.main()
