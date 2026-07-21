"""US-020 — language-agnostic renderer strings and documented synthesis discipline.

English is the default report language, so a string that hard-codes "Korean"
while describing a language-agnostic fact is simply wrong on the English path.

The SKILL.md assertions exist because the creative-delta discipline was
implemented (`ideation.SynthesisTrace`) but never documented: the skill file
never named `synthesis_trace`, and the "~10-30% delta" guidance survived only
inside a `scaffold.py` TODO string, where no ideating agent would read it.
"""

import re
import unittest
from pathlib import Path

from patent_factory.ideation import SYNTHESIS_METHODS


ROOT = Path(__file__).resolve().parents[2]
REPORT_SOURCE = (ROOT / "src" / "patent_factory" / "report.py").read_text(encoding="utf-8")
SKILL = (ROOT / ".claude" / "skills" / "ideation" / "SKILL.md").read_text(encoding="utf-8")


class LanguageAgnosticStringTests(unittest.TestCase):
    def test_the_policy_binding_error_does_not_claim_to_be_korean_only(self):
        # Raised on the English path too, where "Korean" is a lie.
        self.assertNotIn("Korean policy binding mismatch", REPORT_SOURCE)
        self.assertIn("report_artifact: policy binding mismatch", REPORT_SOURCE)

    def test_the_publish_transition_reason_does_not_claim_a_korean_render(self):
        self.assertNotIn("Korean report rendered from approved artifacts", REPORT_SOURCE)
        self.assertIn('reason="report rendered from approved artifacts"', REPORT_SOURCE)

    def test_no_remaining_korean_mention_sits_in_a_language_agnostic_runtime_string(self):
        # Only comments may still say "Korean", and only where the statement is
        # genuinely about the ko lexicon or ko-pinning tests.
        offenders = [
            line.strip() for line in REPORT_SOURCE.splitlines()
            if "Korean" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(offenders, [])


class SynthesisTraceIsDocumentedTests(unittest.TestCase):
    def test_the_skill_names_synthesis_trace(self):
        self.assertIn("synthesis_trace", SKILL)

    def test_the_skill_lists_every_supported_synthesis_method(self):
        for method in SYNTHESIS_METHODS:
            with self.subTest(method=method):
                self.assertIn(f"`{method}`", SKILL)

    def test_the_skill_carries_the_creative_delta_guidance(self):
        self.assertIsNotNone(
            re.search(r"10\s*[-–]\s*30%", SKILL),
            "the ~10-30% creative delta guidance is missing from the ideation skill",
        )

    def test_the_delta_guidance_is_marked_a_heuristic_not_a_measured_novelty_claim(self):
        # CLAUDE.md section 6: the percentage must never read as a measured
        # property of the invention.
        self.assertIn("heuristic", SKILL.casefold())
        self.assertIn("not a measurement", SKILL.casefold())

    def test_the_skill_records_that_the_trace_cannot_bind_the_audit_closest_reference(self):
        # The ordering constraint is the reason no closest-prior-art binding is
        # enforced on synthesis_trace; if that rationale is dropped from the docs
        # the gap looks like an oversight and invites a schema change.
        self.assertIn("closest", SKILL.casefold())


if __name__ == "__main__":
    unittest.main()
