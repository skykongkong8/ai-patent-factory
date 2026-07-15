import unittest

from patent_factory.privacy import KNOWN_CREDENTIAL_NAMES, credential_canaries


class CredentialCanariesTests(unittest.TestCase):
    def test_returns_present_values_for_all_known_credentials(self):
        env = {"KIPRIS_PLUS_API_KEY": "KA", "SERPAPI_API_KEY": "SB", "UNRELATED": "x"}
        self.assertEqual(set(credential_canaries(env)), {"KA", "SB"})

    def test_skips_absent_and_empty_values(self):
        self.assertEqual(credential_canaries({"SERPAPI_API_KEY": "SB"}), ("SB",))
        self.assertEqual(credential_canaries({}), ())
        self.assertEqual(credential_canaries({"SERPAPI_API_KEY": ""}), ())

    def test_both_credentials_are_known(self):
        self.assertIn("KIPRIS_PLUS_API_KEY", KNOWN_CREDENTIAL_NAMES)
        self.assertIn("SERPAPI_API_KEY", KNOWN_CREDENTIAL_NAMES)


if __name__ == "__main__":
    unittest.main()
