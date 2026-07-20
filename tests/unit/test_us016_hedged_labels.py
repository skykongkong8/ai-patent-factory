import unittest

from patent_factory.report import HEDGED_LABELS, HEDGED_LINE_KEYS, LEXICON


class HedgedLabelContractTests(unittest.TestCase):
    def test_every_documented_hedged_label_exists_in_both_lexicons(self):
        for language in ("ko", "en"):
            rendered = "\n".join(LEXICON[language].values())
            present = [label for label in HEDGED_LABELS if label in rendered]
            self.assertEqual(len(present), 4, f"{language}: expected exactly 4 hedged labels, got {present}")
        self.assertEqual(len(HEDGED_LABELS), 8)

    def test_hedged_line_keys_match_the_labelled_templates_in_both_languages(self):
        for language in ("ko", "en"):
            labelled = {
                key for key, template in LEXICON[language].items()
                if any(label in template for label in HEDGED_LABELS)
            }
            self.assertEqual(
                labelled, set(HEDGED_LINE_KEYS),
                f"{language}: HEDGED_LINE_KEYS drifted from the labelled lexicon templates",
            )

    def test_no_hedged_label_is_a_substring_of_another(self):
        for label in HEDGED_LABELS:
            others = [item for item in HEDGED_LABELS if item != label and label in item]
            self.assertEqual(others, [], f"{label} is a substring of {others}")


if __name__ == "__main__":
    unittest.main()
