"""`bibliography_summary` planning — the capability that had no caller.

Every production envelope pinned `capability="word_search"`, so this declared
capability was unreachable from anywhere in `src/`. Its only test fed the adapter
hand-authored XML, which is why it could sit broken and uncovered indefinitely.
These tests exercise the planner and the operation mapping that the CLI now uses.
"""

import unittest

from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.research import ResearchBudget, plan_bibliography_queries, plan_keyword_queries


class PlanBibliographyQueriesTests(unittest.TestCase):
    def test_one_query_per_application_number(self):
        planned = plan_bibliography_queries(
            run_id="run", application_numbers=("1020160062884", "1020150178699"),
        )
        self.assertEqual(len(planned), 2)
        self.assertEqual(
            [query.envelope.query_projection["application_number"] for query in planned],
            ["1020160062884", "1020150178699"],
        )

    def test_capability_and_projection_match_what_the_adapter_expects(self):
        query = plan_bibliography_queries(run_id="run", application_numbers=("1020160062884",))[0]
        self.assertEqual(query.envelope.capability, "bibliography_summary")
        # The adapter demands exactly this projection key set.
        self.assertEqual(set(query.envelope.query_projection), {"application_number"})
        operation, params = KiprisAdapter("k")._parameters(query.envelope)
        self.assertEqual(operation, "getBibliographySumryInfoSearch")
        self.assertEqual(params, {"applicationNumber": "1020160062884"})

    def test_word_search_planner_is_unchanged_and_still_the_default_shape(self):
        query = plan_keyword_queries(run_id="run", origin_query="온디바이스 추론")[0]
        self.assertEqual(query.envelope.capability, "word_search")
        self.assertIn("word", query.envelope.query_projection)

    def test_duplicates_are_collapsed(self):
        planned = plan_bibliography_queries(
            run_id="run", application_numbers=("1020160062884", " 1020160062884 ", "1020150178699"),
        )
        self.assertEqual(len(planned), 2)

    def test_call_budget_is_respected(self):
        planned = plan_bibliography_queries(
            run_id="run", application_numbers=tuple(str(n) for n in range(20)),
            budget=ResearchBudget(max_calls=3),
        )
        self.assertEqual(len(planned), 3)

    def test_empty_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one value required"):
            plan_bibliography_queries(run_id="run", application_numbers=())
        with self.assertRaisesRegex(ValueError, "at least one value required"):
            plan_bibliography_queries(run_id="run", application_numbers=("", "   "))


if __name__ == "__main__":
    unittest.main()
