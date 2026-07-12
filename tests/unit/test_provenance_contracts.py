import unittest

from patent_factory.provenance import (
    Claim,
    EpistemicLabel,
    SourceRepresentation,
    claim_from_dict,
    evidence_revision_id,
)


class ProvenanceContractTests(unittest.TestCase):
    def test_all_six_labels_have_valid_contracts(self):
        claims = (
            Claim(EpistemicLabel.SOURCE_FACT, "ev_fact", "content", "span"),
            Claim(EpistemicLabel.USER_STATEMENT, "interview-v1"),
            Claim(EpistemicLabel.SOURCE_INFERENCE, "ev_source", "content", "span", "summary rationale"),
            Claim(EpistemicLabel.AGENT_INFERENCE, rationale="comparison rationale"),
            Claim(EpistemicLabel.HYPOTHESIS),
            Claim(EpistemicLabel.CREATIVE_SUGGESTION),
        )
        self.assertEqual({claim.as_dict()["label"] for claim in claims}, {label.value for label in EpistemicLabel})

    def test_source_binding_and_inference_paths_are_actionable(self):
        with self.assertRaisesRegex(ValueError, r"artifact.claim: source_fact requires content_hash, span_hash"):
            Claim(EpistemicLabel.SOURCE_FACT, source_id="ev_x").validate("artifact.claim")
        with self.assertRaisesRegex(ValueError, r"candidate.claim: source_inference requires rationale"):
            Claim(EpistemicLabel.SOURCE_INFERENCE, "ev_x", "content", "span").validate("candidate.claim")

    def test_interpretation_cannot_masquerade_as_quote(self):
        claim = Claim(
            EpistemicLabel.SOURCE_INFERENCE,
            "ev_x",
            "content",
            "span",
            "source summary",
            SourceRepresentation.QUOTE,
        )
        with self.assertRaisesRegex(ValueError, r"claim\.representation"):
            claim.validate()
        serialized = Claim(
            EpistemicLabel.SOURCE_INFERENCE,
            "ev_x",
            "content",
            "span",
            "source summary",
        ).as_dict()
        self.assertEqual(serialized["representation"], "interpretation")
        self.assertEqual(claim_from_dict(serialized).resolved_representation(), SourceRepresentation.INTERPRETATION)

    def test_evidence_revision_identity_excludes_retrieval_time(self):
        first = evidence_revision_id("https://example.test/patent/1", "sha256-content")
        second = evidence_revision_id("https://example.test/patent/1", "sha256-content")
        changed = evidence_revision_id("https://example.test/patent/2", "sha256-content")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("ev_"))
        self.assertNotEqual(first, changed)

    def test_claim_id_changes_with_quote_interpretation_contract(self):
        quoted = Claim(EpistemicLabel.SOURCE_FACT, "ev_x", "content", "span").as_dict()
        interpreted = Claim(
            EpistemicLabel.SOURCE_FACT,
            "ev_x",
            "content",
            "span",
            representation=SourceRepresentation.INTERPRETATION,
        ).as_dict()
        self.assertNotEqual(quoted["claim_id"], interpreted["claim_id"])


if __name__ == "__main__":
    unittest.main()
