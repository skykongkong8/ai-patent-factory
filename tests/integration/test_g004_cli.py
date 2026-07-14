import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database
from patent_factory.state import StateStore
from tests.integration.test_g004_ideation_and_shortlist import (
    candidate_input,
    profile,
    ready_profile,
    ready_research,
    shortlist_input,
)


ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "patent_factory", *map(str, args)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )


class G004CliTests(unittest.TestCase):
    def setUp(self):
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.workspace = Path(self.workspace_context.name)

    def tearDown(self):
        self.workspace_context.cleanup()

    def relative(self, path: Path) -> Path:
        return path.relative_to(ROOT)

    def prepare(self, name="run"):
        run_root = self.workspace / name
        run_root.mkdir(mode=0o700)
        connection = connect_database(run_root / "factory.sqlite3")
        evidence, span, _research = ready_research(connection, run_root, name)
        connection.close()
        profile_database = self.workspace / f"{name}-profile.sqlite3"
        profile_connection, current_profile = ready_profile(profile_database)
        profile_connection.close()
        profile_path = self.workspace / f"{name}-profile.json"
        profile_path.write_text(json.dumps(current_profile, ensure_ascii=False), encoding="utf-8")
        return run_root, profile_path, profile_database, evidence, span

    def test_cli_happy_path_is_private_deterministic_and_redacted(self):
        run_root, profile_path, profile_database, evidence, span = self.prepare("happy")
        candidates_path = self.workspace / "candidates.json"
        candidates_path.write_text(
            json.dumps(candidate_input(3, evidence, span), ensure_ascii=False), encoding="utf-8"
        )
        ideate_args = (
            "ideate", "--run", self.relative(run_root), "--run-id", "happy",
            "--profile", self.relative(profile_path), "--input", self.relative(candidates_path),
            "--profile-database", self.relative(profile_database),
            "--workspace-root", self.relative(self.workspace),
        )
        first = run_cli(*ideate_args)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        ideation = json.loads(first.stdout)
        self.assertEqual(ideation["status"], "candidates_ready")
        replay = run_cli(*ideate_args)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertTrue(json.loads(replay.stdout)["replayed"])

        shortlist_path = self.workspace / "shortlist.json"
        shortlist_path.write_text(json.dumps(
            shortlist_input(ideation["candidate_ids"], evidence, span), ensure_ascii=False
        ), encoding="utf-8")
        selected = run_cli(
            "shortlist", "--run", self.relative(run_root), "--run-id", "happy",
            "--input", self.relative(shortlist_path), "--workspace-root", self.relative(self.workspace),
        )
        self.assertEqual(selected.returncode, 0, selected.stdout + selected.stderr)
        self.assertEqual(json.loads(selected.stdout)["status"], "finalists_ready")
        exports = run_root / "ideation-exports"
        self.assertEqual(stat.S_IMODE(exports.stat().st_mode), 0o700)
        self.assertTrue(tuple(exports.glob("ar_*.json")))
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in exports.iterdir()))

    def test_unknown_private_field_is_rejected_without_canary_persistence(self):
        run_root, profile_path, profile_database, evidence, span = self.prepare("private")
        payload = candidate_input(3, evidence, span)
        payload["candidates"][0]["raw_document"] = "G004-PRIVATE-CANARY"
        input_path = self.workspace / "private-candidates.json"
        input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = run_cli(
            "ideate", "--run", self.relative(run_root), "--run-id", "private",
            "--profile", self.relative(profile_path), "--input", self.relative(input_path),
            "--profile-database", self.relative(profile_database),
            "--workspace-root", self.relative(self.workspace),
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("G004-PRIVATE-CANARY", result.stdout + result.stderr)
        self.assertNotIn(b"G004-PRIVATE-CANARY", (run_root / "factory.sqlite3").read_bytes())
        with connect_database(run_root / "factory.sqlite3") as connection:
            self.assertNotIn("candidate_set", StateStore(connection).snapshot("private").current_revisions)


if __name__ == "__main__":
    unittest.main()
