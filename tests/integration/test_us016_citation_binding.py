import unittest

from patent_factory.report import (
    CITATION_RE,
    HEDGED_LABELS,
    _field_reference_tokens,
    publish_report,
)
from patent_factory.validation import _hedged_citation_check
from tests.integration.test_g007_report_review_validation import G007Fixture


def hedged_lines(markdown):
    return [line for line in markdown.splitlines() if any(label in line for label in HEDGED_LABELS)]


class FieldReferenceTokenTests(unittest.TestCase):
    def candidate(self):
        return {
            "claims": [
                {
                    "claim": {"label": "user_statement", "source_id": "interview-problem"},
                    "evidence_references": [
                        {"evidence_id": "ev_00000000000000b0"},
                        {"evidence_id": "ev_00000000000000a0"},
                        {"evidence_id": "ev_00000000000000a0"},
                    ],
                    "field": "technical_problem",
                },
                {
                    "claim": {"label": "creative_suggestion"},
                    "evidence_references": [],
                    "field": "mechanism",
                },
            ],
        }

    def test_tokens_are_sorted_and_deduplicated_per_field(self):
        self.assertEqual(
            _field_reference_tokens(self.candidate(), "technical_problem"),
            "[@ev_00000000000000a0] [@ev_00000000000000b0]",
        )

    def test_a_field_with_no_references_renders_no_token(self):
        self.assertEqual(_field_reference_tokens(self.candidate(), "mechanism"), "")

    def test_an_unclaimed_field_renders_no_token(self):
        self.assertEqual(_field_reference_tokens(self.candidate(), "expected_effects"), "")

    def test_a_candidate_without_claims_renders_no_token(self):
        self.assertEqual(_field_reference_tokens({}, "technical_problem"), "")


class HedgedCitationCheckTests(unittest.TestCase):
    def test_the_validator_rejects_a_citation_on_a_hedged_line(self):
        for label in HEDGED_LABELS:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    _hedged_citation_check({"markdown": f"- {label} something [@ev_0123456789abcdef]"})

    def test_the_validator_accepts_a_hedged_line_without_a_citation(self):
        _hedged_citation_check({"markdown": "- [hypothesis] something\n- Problem: x [@ev_0123456789abcdef]"})


class ReportCitationBindingTests(G007Fixture):
    def report_input(self, language):
        return {
            "drafter": {"id": "drafter", "pass_id": "draft-pass", "type": "agent"},
            "handoff_questions": ["Can the calibration step be claimed independently?"],
            "language": language,
            "profile_fields": ["expertise", "project_summary", "technical_domain"],
            "recommended_investigations": ["Confirm the published calibration specification"],
            "report_date": "2026-07-19",
            "revision": None,
            "schema_version": "report-input-v2",
            "sensitive_disclosures": [],
        }

    def assert_no_hedged_citation(self, language):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run",
            report_input=self.report_input(language),
        )
        lines = hedged_lines(report.artifact.content["markdown"])
        # All four labels of the declared language must actually render, so the
        # assertion cannot pass by rendering nothing.
        self.assertEqual(len({label for line in lines for label in HEDGED_LABELS if label in line}), 4)
        offenders = [line for line in lines if CITATION_RE.search(line)]
        self.assertEqual(offenders, [], f"{language}: hedged lines carry citations: {offenders}")

    def test_no_hedged_korean_line_carries_a_prior_art_citation(self):
        self.assert_no_hedged_citation("ko")

    def test_no_hedged_english_line_carries_a_prior_art_citation(self):
        self.assert_no_hedged_citation("en")

    def test_unhedged_bullets_cite_only_their_own_field_references(self):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run",
            report_input=self.report_input("en"),
        )
        markdown = report.artifact.content["markdown"]
        problem_lines = [line for line in markdown.splitlines() if line.startswith("- Problem: ")]
        mechanism_lines = [line for line in markdown.splitlines() if line.startswith("- Proposed mechanism: ")]
        self.assertEqual(len(problem_lines), 3)
        self.assertEqual(len(mechanism_lines), 3)
        # The fixture binds one evidence revision to technical_problem and none to mechanism.
        for line in problem_lines:
            self.assertEqual(len(CITATION_RE.findall(line)), 1, line)
        for line in mechanism_lines:
            self.assertEqual(CITATION_RE.findall(line), [], line)


if __name__ == "__main__":
    unittest.main()
