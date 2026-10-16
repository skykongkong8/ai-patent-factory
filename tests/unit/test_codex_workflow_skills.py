"""Check the repository artifacts Codex consumes, without invoking private runs."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STAGES = ("setup", "research", "ideate", "shortlist", "audit", "checkpoint", "draft", "review")


class CodexWorkflowSkillTests(unittest.TestCase):
    def test_all_eight_public_commands_have_unique_discoverable_names(self):
        names = []
        for stage in STAGES:
            with self.subTest(stage=stage):
                path = ROOT / f".agents/skills/patent-{stage}/SKILL.md"
                self.assertTrue(path.is_file(), f"Missing Codex entrypoint: {path}")
                content = path.read_text(encoding="utf-8")
                header = re.match(r"\A---\n(.*?)\n---\n", content, re.DOTALL)
                self.assertIsNotNone(header, path)
                metadata = dict(line.split(":", 1) for line in header[1].splitlines())
                name = metadata["name"].strip().strip('"')
                self.assertEqual(name, f"patent-{stage}")
                self.assertTrue(metadata["description"].strip())
                names.append(name)
        self.assertEqual(len(set(names)), 8)

    def test_all_public_commands_disable_implicit_invocation(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                path = ROOT / f".agents/skills/patent-{stage}/agents/openai.yaml"
                self.assertTrue(path.is_file(), f"Missing explicit invocation policy: {path}")
                content = path.read_text(encoding="utf-8")
                self.assertRegex(content, r"(?m)^policy:\n +allow_implicit_invocation: false\s*$")

    def test_stage_entrypoints_resolve_to_shared_procedures_in_a_fresh_clone(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                path = ROOT / f".agents/skills/patent-{stage}/SKILL.md"
                self.assertTrue(path.is_file(), path)
                links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8"))
                targets = {(path.parent / link).resolve() for link in links}
                self.assertIn(ROOT / ".codex/workflow.md", targets)
                self.assertIn(ROOT / f".claude/commands/{stage}.md", targets)
                for target in targets:
                    self.assertTrue(target.is_file(), f"Broken skill dependency: {target}")
                    self.assertTrue(target.is_relative_to(ROOT), target)

    def test_common_contract_dependencies_resolve_without_local_migration_helpers(self):
        path = ROOT / ".codex/workflow.md"
        self.assertTrue(path.is_file(), path)
        links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8"))
        targets = {(path.parent / link).resolve() for link in links}
        for helper in ("profile", "research", "ideation", "checkpoint", "patent-review"):
            self.assertIn(ROOT / f".claude/skills/{helper}/SKILL.md", targets)
        for target in targets:
            self.assertTrue(target.is_file(), f"Broken common dependency: {target}")


if __name__ == "__main__":
    unittest.main()
