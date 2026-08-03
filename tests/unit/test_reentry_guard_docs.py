"""AC-10: the re-entry guard's operator contract is documented, asserted mechanically.

A prose acceptance criterion ("the docs explain the refusal") is unfalsifiable
— it passes on the day it is written and never fails again. These assertions
name the exact strings an operator has to be able to find, so a future edit
that quietly drops the recovery path breaks a test instead of stranding
somebody at a refusal with no way forward.

Two of the three claims are about strings the CODE emits, so they are read from
the code rather than retyped: a refusal code that drifts from its documentation
is precisely the failure this guards against.

This follows the pattern of `test_g008_cli_result_contract_docs.py:24` and
`test_us020_language_and_synthesis_docs.py:21` — read the doc, assert on its
content. It is the first test in the repo to read SETUP.md.
"""
import unittest
from pathlib import Path

from patent_factory.research import (
    LiveResearchReentryRefusedError,
    LiveResearchReentrySpentCoordinateError,
)

ROOT = Path(__file__).resolve().parents[2]
SETUP = (ROOT / "SETUP.md").read_text(encoding="utf-8")
RESEARCH_SKILL = (ROOT / ".claude/skills/research/SKILL.md").read_text(encoding="utf-8")
CHECKPOINT_SKILL = (ROOT / ".claude/skills/checkpoint/SKILL.md").read_text(encoding="utf-8")


class ReentryGuardDocsTests(unittest.TestCase):
    def test_setup_documents_the_spent_coordinate_refusal_and_its_recovery(self):
        self.assertIn(LiveResearchReentrySpentCoordinateError.code, SETUP)
        self.assertIn("--idempotency-key", SETUP)
        self.assertIn("fresh attempt key", SETUP)

    def test_setup_documents_the_offline_publish_escape_hatch(self):
        self.assertIn(LiveResearchReentryRefusedError.code, SETUP)
        recovery = SETUP.split("Recovering from a refusal")[1][:900]
        self.assertIn("offline pass", recovery)
        self.assertIn("research_complete", recovery)
        self.assertIn("quiets the anchor", recovery)

    def test_setup_states_that_retries_stay_inside_the_force_gate(self):
        self.assertIn("retries included", SETUP)
        self.assertIn("research_incomplete", SETUP)
        # Whitespace-normalised: this is a claim about what the document says,
        # not about where the paragraph happens to wrap.
        self.assertIn("not on the run's current state", " ".join(SETUP.split()))

    def test_both_skills_carry_the_same_retry_contract(self):
        for name, content in (
            ("research", RESEARCH_SKILL), ("checkpoint", CHECKPOINT_SKILL),
        ):
            with self.subTest(skill=name):
                self.assertIn("retries included", content)
                self.assertIn(LiveResearchReentrySpentCoordinateError.code, content)
                self.assertIn("--idempotency-key", content)

    def test_no_sentence_describes_a_retry_without_saying_it_is_guarded(self):
        """The negative half — as an invariant, not a blacklist.

        A list of forbidden phrases is unfalsifiable theatre: none of the
        obvious wordings ever appeared in these documents, so the assertion
        passes in every world including one where SETUP.md says a retry runs
        without a gate. The invariant that actually holds is structural — in
        the second-pass section, no sentence may mention retrying without also
        saying what happens to it. A new "you can simply rerun the command"
        sentence fails this; a blacklist would wave it through.
        """

        section = SETUP.split("The second: the post-audit")[1].split("## Full pipeline verbs")[0]
        guarded = ("gate", "refus", "salt", "approv", "fresh attempt key")
        # "rerun" does not contain "retr" — filtering on that substring alone
        # let through the exact sentence shape this docstring names.
        mentions_retry = ("retr", "rerun", "run it again", "run the command again")
        for sentence in section.replace("\n", " ").split(". "):
            lowered = sentence.lower()
            if not any(word in lowered for word in mentions_retry):
                continue
            with self.subTest(sentence=sentence.strip()[:70]):
                self.assertTrue(
                    any(word in lowered for word in guarded),
                    f"a retry is described with no mention of what guards it: {sentence.strip()!r}",
                )


if __name__ == "__main__":
    unittest.main()
