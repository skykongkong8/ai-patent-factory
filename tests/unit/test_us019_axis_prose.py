"""US-019/US-020 renderer contract — axis prose replaces the numeric axis matrix.

Axis scores are validated pass-throughs (`evaluation.EvaluationAxis`): never
computed by this tool and never used to order candidates (`evaluation.py` sorts
finalists on `(priority, candidate_id)`). An axis literally named
`differentiation` rendered as a bare `81` — in a ranked comparison table or as
`81 pts` — reads as a novelty assessment regardless of who authored the number,
which is the CLAUDE.md section 6 surface these tests close.

The renderer is exercised through `_section_bodies` directly: it is the unit
that changed, and a hand-built fixture pins the rendered text far more sharply
than a full publish round-trip would.
"""

import unittest

from patent_factory.report import LEXICON, _section_bodies, load_report_policy


EV_RESEARCH = "ev_" + "a" * 16
EV_CLOSEST = "ev_" + "b" * 16
# Distinctive scores: none of these digit strings occurs anywhere else in the
# fixture, so "81 is absent from the body" is a sound assertion.
SCORES = {"differentiation": 81, "technical_feasibility": 74, "utility_significance": 68}


def axis(name):
    return {
        "axis": name,
        "score": SCORES[name],
        "confidence": "medium",
        "rationale": f"why the {name} axis reads as it does",
        "coverage_assessment": f"what the corpus covers for {name}",
        "coverage_limitations": [f"known {name} coverage limit"],
        "gaps": [f"open {name} question"],
        "supporting_evidence_references": [{"evidence_id": EV_RESEARCH}],
        "contrary_evidence_references": [],
    }


def candidate():
    return {
        "candidate_id": "ca_1",
        "title": "Sample candidate",
        "technical_problem": "the stated problem",
        "mechanism": "the proposed mechanism",
        "components": ["component one"],
        "interactions": ["interaction one"],
        "expected_effects": ["effect one"],
        "domain": "the domain",
        "implementation_example": "an implementation",
        "measurable_validation": "a validation",
        "unresolved_questions": ["an open question"],
        "evidence_references": [{"evidence_id": EV_RESEARCH}],
        "claims": [],
        "synthesis_trace": {
            "method": "combine",
            "narrative": "combined two retrieved mechanisms",
            "evidence_ids": [EV_RESEARCH],
        },
    }


def evidence_entry(evidence_id, title, identifier, source_type):
    return {
        "canonical_url": None, "content_hash": "c" * 64, "evidence_id": evidence_id,
        "identifier": identifier, "limitations": [], "observation_date": "2026-07-20",
        "record": {}, "source_type": source_type, "title": title,
    }


def bodies(language="en"):
    return _section_bodies(
        policy=load_report_policy(language),
        report_input={
            "profile_fields": ["domain"], "report_date": "2026-07-20",
            "handoff_questions": ["a handoff question"],
            "recommended_investigations": ["a follow-up"],
        },
        profile={"profile": {"facts": {"domain": {
            "value": "the domain", "claims": [{"label": "user_statement", "source_id": "src_1"}],
        }}}},
        research={"evidence": [], "coverage_limitations": [], "adapter_events": [], "queries": []},
        candidates=[candidate()],
        finalists=[{
            "finalist_id": "fi_1", "candidate_id": "ca_1", "rank": 1,
            "axes": [axis(name) for name in SCORES],
        }],
        corpus={"corpora": []},
        audit={"results": [{
            "finalist_id": "fi_1", "candidate_id": "ca_1", "closest_reference_id": EV_CLOSEST,
            "upper_bound_reference_id": EV_CLOSEST, "r_obs": 0, "r_hi": 0, "coverage": 100,
            "outcome": "audit_approved", "counterargument": "a counterargument",
            "pair_scores": [{
                "evidence_id": EV_CLOSEST, "version": "simrisk-v1.0.0", "T": 0, "F": 0,
                "C": 100, "D": 100, "Q": 100, "r_obs": 0, "r_hi": 0,
                "matched_feature_ids": [], "differentiated_feature_ids": ["feat_1"],
            }],
        }]},
        decision=None,
        evidence={
            EV_RESEARCH: evidence_entry(EV_RESEARCH, "Research source", "10-2026-0000001", "kipris_patent"),
            EV_CLOSEST: evidence_entry(EV_CLOSEST, "Closest reference", "10-2026-0011111", "kipris_patent"),
        },
        cited_ids=sorted([EV_RESEARCH, EV_CLOSEST]),
        scorer={"config": {"version": "simrisk-v1.0.0"}},
        language=language,
        feature_descriptions={"fi_1": {"feat_1": "runtime-adaptive mechanism control"}},
    )


class AxisMatrixIsDroppedTests(unittest.TestCase):
    def test_section_7_renders_no_numeric_comparison_table(self):
        for language in ("en", "ko"):
            with self.subTest(language=language):
                section7 = bodies(language)[6]
                self.assertNotIn("|---:|", section7)
                self.assertNotIn("| Rank |", section7)
                self.assertNotIn("| 순위 |", section7)

    def test_no_axis_score_renders_in_any_section(self):
        # Section 6 rendered "differentiation: 81 pts" and section 7 rendered a
        # bare "| 81 |" cell. Both read as an assessment of the named axis.
        for language in ("en", "ko"):
            for index, body in enumerate(bodies(language), start=1):
                for name, score in SCORES.items():
                    with self.subTest(language=language, section=index, axis=name):
                        self.assertNotIn(str(score), body)

    def test_section_7_states_that_ranking_follows_priority_not_score(self):
        self.assertIn("priority", bodies("en")[6])
        self.assertIn("우선순위", bodies("ko")[6])

    def test_section_7_carries_axis_prose_from_the_existing_axis_fields(self):
        section7 = bodies("en")[6]
        for name in SCORES:
            with self.subTest(axis=name):
                self.assertIn(f"why the {name} axis reads as it does", section7)
                self.assertIn(f"what the corpus covers for {name}", section7)
                self.assertIn(f"known {name} coverage limit", section7)
                self.assertIn(f"open {name} question", section7)

    def test_section_7_has_enough_lines_for_canonical_line_mutation_checks(self):
        # test_g007 mutates section 7 by swapping lines 2/3 and dropping line 3;
        # those mutations must remain distinguishable from the canonical body.
        for language in ("en", "ko"):
            with self.subTest(language=language):
                lines = bodies(language)[6].splitlines()
                self.assertGreaterEqual(len(lines), 4)
                self.assertNotEqual(lines[2], lines[3])


class DeltaNarrativeTests(unittest.TestCase):
    def test_each_differentiated_feature_renders_description_operation_axis_and_citation(self):
        for language in ("en", "ko"):
            with self.subTest(language=language):
                section7 = bodies(language)[6]
                line = next(
                    (item for item in section7.splitlines()
                     if "runtime-adaptive mechanism control" in item),
                    None,
                )
                self.assertIsNotNone(line, "no delta narrative line for the differentiated feature")
                self.assertIn("combine", line)
                self.assertIn("differentiation", line)
                self.assertIn(f"[@{EV_CLOSEST}]", line)

    def test_the_delta_narrative_makes_no_novelty_or_inventive_step_claim(self):
        for language in ("en", "ko"):
            body = bodies(language)[6].casefold()
            for phrase in ("novel", "novelty", "inventive step", "patentab", "신규성", "진보성", "특허 가능"):
                with self.subTest(language=language, phrase=phrase):
                    self.assertNotIn(phrase, body)


class LanguageAgnosticStringTests(unittest.TestCase):
    def test_the_matrix_header_lexicon_entry_is_gone_from_both_languages(self):
        for language in ("en", "ko"):
            with self.subTest(language=language):
                self.assertNotIn("matrix_header", LEXICON[language])


if __name__ == "__main__":
    unittest.main()
