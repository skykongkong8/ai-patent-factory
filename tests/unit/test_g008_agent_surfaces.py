import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMANDS = {
    "setup": ROOT / ".claude/commands/setup.md",
    "research": ROOT / ".claude/commands/research.md",
    "ideate": ROOT / ".claude/commands/ideate.md",
    "shortlist": ROOT / ".claude/commands/shortlist.md",
    "audit": ROOT / ".claude/commands/audit.md",
    "draft": ROOT / ".claude/commands/draft.md",
    "review": ROOT / ".claude/commands/review.md",
}
SKILLS = {
    "profile": ROOT / ".claude/skills/profile/SKILL.md",
    "research": ROOT / ".claude/skills/research/SKILL.md",
    "ideation": ROOT / ".claude/skills/ideation/SKILL.md",
    "patent-review": ROOT / ".claude/skills/patent-review/SKILL.md",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def shell_blocks(markdown: str) -> str:
    return "\n".join(re.findall(r"```bash\n(.*?)```", markdown, flags=re.DOTALL))


class AgentSurfaceConformanceTests(unittest.TestCase):
    def test_required_repository_surfaces_exist(self):
        for path in (ROOT / "AGENTS.md", ROOT / ".codex/README.md", *COMMANDS.values(), *SKILLS.values()):
            self.assertTrue(path.is_file(), path)

    def test_claude_commands_are_thin_cli_wrappers(self):
        forbidden = ("sqlite3 ", "UPDATE ", "INSERT ", "DELETE FROM", "workspace/*.json", "> workspace/")
        for name, path in COMMANDS.items():
            with self.subTest(command=name):
                content = read(path)
                shell = shell_blocks(content)
                self.assertIn("python3 -m patent_factory", shell)
                self.assertTrue("JSON" in content or "json" in content)
                self.assertFalse(any(token in shell for token in forbidden), shell)
                self.assertNotRegex(shell, r"(?:^|\n)\s*(?:cp|mv|rm)\s")

    def test_all_workflow_verbs_map_to_the_same_portable_cli(self):
        corpus = "\n".join(read(path) for path in COMMANDS.values())
        for invocation in (
            "patent_factory init",
            "patent_factory profile",
            "patent_factory research",
            "patent_factory ideate",
            "patent_factory shortlist",
            "patent_factory audit retrieve",
            "patent_factory audit score",
            "patent_factory draft",
            "patent_factory review",
            "patent_factory validate",
            "patent_factory share",
        ):
            self.assertIn(invocation, corpus)
        for version in (
            "candidate-input-v1",
            "shortlist-input-v1",
            "audit-query-input-v1",
            "audit-fixture-manifest-v1",
            "feature-map-set-input-v1",
            "report-input-v1",
            "review-input-v1",
            "external-report-share-v1",
            "gate-decision-input-v1",
        ):
            self.assertIn(version, corpus)

    def test_wrappers_stop_at_core_owned_gates(self):
        corpus = "\n".join(read(path) for path in (*COMMANDS.values(), *SKILLS.values()))
        for state in (
            "conflict_resolution_required",
            "credential_required",
            "domain_pivot_required",
            "coverage_insufficient",
            "decision_required",
            "revision_required",
            "sensitive_disclosure_required",
            "insufficient_evidence",
        ):
            self.assertIn(state, corpus)
        self.assertIn("R_hi < 75", corpus)
        self.assertIn("user", corpus)

    def test_hosted_egress_is_never_authorized_by_a_wrapper(self):
        corpus = "\n".join(read(path) for path in (ROOT / "AGENTS.md", ROOT / ".codex/README.md", *COMMANDS.values(), *SKILLS.values()))
        for required in ("hosted", "external transfer", "exact", "approval", "egress manifest", "authorize"):
            self.assertIn(required, corpus)
        self.assertIn("creates such an approval", read(ROOT / ".codex/README.md"))

    def test_codex_documents_gate_share_cleanup_and_ux_limits(self):
        content = read(ROOT / ".codex/README.md")
        for required in (
            "gate inspect",
            "gate decide",
            "validate",
            "external-report-share-v1",
            "delete-run",
            "SQLite",
            "UX differences",
            "best-effort",
        ):
            self.assertIn(required, content)
        self.assertIn("python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace", content)
        self.assertIn("cli-envelope-v1", content)
        self.assertIn("directly delete", content)
        self.assertIn("directly modify", content)


if __name__ == "__main__":
    unittest.main()
