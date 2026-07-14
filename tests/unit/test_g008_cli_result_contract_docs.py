import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CliResultContractDocumentationTests(unittest.TestCase):
    def test_agent_surfaces_bind_both_common_result_identifiers(self):
        for relative in ("AGENTS.md", ".codex/README.md"):
            content = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(surface=relative):
                self.assertIn("cli-result-v1", content)
                self.assertIn("cli-envelope-v1", content)

    def test_cleanup_mapping_uses_the_exact_safe_cli_contract(self):
        content = (ROOT / ".codex/README.md").read_text(encoding="utf-8")
        self.assertIn(
            "python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace",
            content,
        )
        self.assertIn("run-id", content)
        self.assertIn("직접 삭제하지 않는다", content)

    def test_audit_fixture_and_plaintext_help_exemption_match_cli_boundaries(self):
        content = (ROOT / ".codex/README.md").read_text(encoding="utf-8")
        self.assertIn("--fixture-manifest documents/requests/audit-fixture-manifest-v1.json", content)
        self.assertIn("help/version", content)
        self.assertIn("plain text", content)


if __name__ == "__main__":
    unittest.main()
