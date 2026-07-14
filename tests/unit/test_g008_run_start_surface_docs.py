import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class RunStartSurfaceDocumentationTests(unittest.TestCase):
    def test_claude_and_codex_map_run_bootstrap_to_the_core_cli(self):
        for relative in (".claude/commands/research.md", ".codex/README.md"):
            content = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(surface=relative):
                self.assertIn("python3 -m patent_factory run start", content)
                self.assertIn("--run", content)
                self.assertIn("--run-id", content)
                self.assertIn("--profile", content)
                self.assertIn("--profile-database", content)
                self.assertIn("research_ready", content)

    def test_codex_cleanup_mapping_matches_the_core_cli(self):
        content = (ROOT / ".codex/README.md").read_text(encoding="utf-8")
        self.assertIn(
            "python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace",
            content,
        )
        self.assertIn("run-id", content)
        self.assertIn("cli-result-v1", content)
        self.assertIn("cli-envelope-v1", content)


if __name__ == "__main__":
    unittest.main()
