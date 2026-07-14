import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args, extra_environment=None):
    environment = os.environ.copy()
    environment.update(extra_environment or {})
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "patent_factory", *map(str, args)], cwd=ROOT, env=environment, text=True, capture_output=True, check=False)


class CliProfilePathTests(unittest.TestCase):
    def test_three_paths_and_deterministic_json(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "documents") as documents_temporary, tempfile.TemporaryDirectory(dir=ROOT / "workspace") as workspace_temporary:
            documents = Path(documents_temporary)
            workspace = Path(workspace_temporary)
            document = documents / "facts.json"
            values = {"expertise": "분산 시스템", "name": "홍길동", "project_summary": "지연 감소", "technical_domain": "산업 데이터"}
            document.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            responses = documents / "responses.json"
            responses.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            outputs = []
            for mode, source in (("folder", documents), ("document", document), ("interview", responses)):
                profile = workspace / f"{mode}.json"
                database = workspace / f"{mode}.sqlite3"
                common = ("--documents-root", documents.relative_to(ROOT), "--workspace-root", workspace.relative_to(ROOT), "--database", database.relative_to(ROOT), "--profile", profile.relative_to(ROOT))
                args = ("profile", mode, source.relative_to(ROOT), *common) if mode != "interview" else ("profile", "interview", "--responses", responses.relative_to(ROOT), *common)
                result = run_cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                outputs.append(json.loads(result.stdout))
                saved = json.loads(profile.read_text(encoding="utf-8"))
                labels = {entry["claims"][0]["label"] for entry in saved["facts"].values()}
                self.assertEqual(labels, {"user_statement"} if mode == "interview" else {"source_fact"})
            self.assertTrue(all(item["fact_count"] == 4 for item in outputs))

    def test_cli_conflict_is_nonzero_and_does_not_write_partial_batch(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "documents") as documents_temporary, tempfile.TemporaryDirectory(dir=ROOT / "workspace") as workspace_temporary:
            documents = Path(documents_temporary)
            workspace = Path(workspace_temporary)
            profile = workspace / "profile.json"
            database = workspace / "profile.sqlite3"
            first = documents / "first.json"
            second = documents / "second.json"
            first.write_text('{"name":"A"}', encoding="utf-8")
            second.write_text('{"name":"B","technical_domain":"secret"}', encoding="utf-8")
            common = ("--documents-root", documents.relative_to(ROOT), "--workspace-root", workspace.relative_to(ROOT), "--database", database.relative_to(ROOT), "--profile", profile.relative_to(ROOT))
            self.assertEqual(run_cli("profile", "document", first.relative_to(ROOT), *common).returncode, 0)
            result = run_cli("profile", "document", second.relative_to(ROOT), *common)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(json.loads(result.stdout)["status"], "conflict_resolution_required")
            self.assertNotIn("secret", result.stdout)
            exported = json.loads(profile.read_text(encoding="utf-8"))
            self.assertEqual(exported["facts"]["name"]["value"], "A")
            self.assertNotIn("technical_domain", exported["facts"])
            self.assertEqual(exported["state"], "conflict_resolution_required")
            with sqlite3.connect(database) as connection:
                counts = connection.execute(
                    "SELECT (SELECT count(*) FROM profile_facts), "
                    "(SELECT count(*) FROM profile_claims), "
                    "(SELECT count(*) FROM ingestion_batches), "
                    "(SELECT count(*) FROM profile_conflicts)"
                ).fetchone()
            self.assertEqual(counts, (1, 1, 2, 1))

            rerun = run_cli("profile", "document", second.relative_to(ROOT), *common)
            self.assertEqual(rerun.returncode, 3)
            self.assertEqual(json.loads(rerun.stdout).get("changes", 0), 0)
            with sqlite3.connect(database) as connection:
                after = connection.execute(
                    "SELECT (SELECT count(*) FROM profile_facts), "
                    "(SELECT count(*) FROM profile_claims), "
                    "(SELECT count(*) FROM ingestion_batches), "
                    "(SELECT count(*) FROM profile_conflicts)"
                ).fetchone()
            self.assertEqual(after, counts)

            batch_id = json.loads(result.stdout)["batch_id"]
            inspected = run_cli(
                "profile", "conflict-inspect", "--batch-id", batch_id,
                "--database", database.relative_to(ROOT), "--workspace-root", workspace.relative_to(ROOT),
            )
            self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
            pending = json.loads(inspected.stdout)
            self.assertEqual((pending["status"], len(pending["conflicts"])), ("pending", 1))
            decision_path = workspace / "conflict-decision.json"
            decision = {
                "action": "choose_value", "actor": "user", "batch_id": batch_id,
                "choices": [], "reason": "select reviewed incoming value",
                "schema_version": "profile-conflict-decision-v1", "subject_hash": pending["subject_hash"],
            }
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            canary = "G006-PROFILE-CONFLICT-CANARY"
            canary_decision = dict(decision)
            canary_decision["reason"] = canary
            canary_decision["choices"] = [{"conflict_id": pending["conflicts"][0]["conflict_id"], "selected": "incoming"}]
            decision_path.write_text(json.dumps(canary_decision), encoding="utf-8")
            blocked_canary = run_cli(
                "profile", "conflict-decide", "--batch-id", batch_id,
                "--input", decision_path.relative_to(ROOT), "--database", database.relative_to(ROOT),
                "--profile", profile.relative_to(ROOT), "--workspace-root", workspace.relative_to(ROOT),
                extra_environment={"KIPRIS_PLUS_API_KEY": canary},
            )
            self.assertEqual(blocked_canary.returncode, 2, blocked_canary.stdout + blocked_canary.stderr)
            self.assertNotIn(canary, blocked_canary.stdout + blocked_canary.stderr)
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            incomplete = run_cli(
                "profile", "conflict-decide", "--batch-id", batch_id,
                "--input", decision_path.relative_to(ROOT), "--database", database.relative_to(ROOT),
                "--profile", profile.relative_to(ROOT), "--workspace-root", workspace.relative_to(ROOT),
            )
            self.assertEqual(incomplete.returncode, 2, incomplete.stdout + incomplete.stderr)
            decision["choices"] = [{"conflict_id": pending["conflicts"][0]["conflict_id"], "selected": "incoming"}]
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            decided = run_cli(
                "profile", "conflict-decide", "--batch-id", batch_id,
                "--input", decision_path.relative_to(ROOT), "--database", database.relative_to(ROOT),
                "--profile", profile.relative_to(ROOT), "--workspace-root", workspace.relative_to(ROOT),
            )
            self.assertEqual(decided.returncode, 0, decided.stdout + decided.stderr)
            self.assertEqual(json.loads(profile.read_text(encoding="utf-8"))["facts"]["name"]["value"], "B")
            replay = run_cli(
                "profile", "conflict-decide", "--batch-id", batch_id,
                "--input", decision_path.relative_to(ROOT), "--database", database.relative_to(ROOT),
                "--profile", profile.relative_to(ROOT), "--workspace-root", workspace.relative_to(ROOT),
            )
            self.assertTrue(json.loads(replay.stdout)["replayed"])

    def test_sqlite_is_authoritative_and_exact_success_rerun_has_no_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "documents") as documents_temporary, tempfile.TemporaryDirectory(dir=ROOT / "workspace") as workspace_temporary:
            documents = Path(documents_temporary)
            workspace = Path(workspace_temporary)
            source = documents / "facts.json"
            source.write_text('{"name":"canonical"}', encoding="utf-8")
            common = (
                "--documents-root", documents.relative_to(ROOT),
                "--workspace-root", workspace.relative_to(ROOT),
            )
            first = run_cli("profile", "document", source.relative_to(ROOT), *common)
            self.assertEqual(json.loads(first.stdout)["changes"], 1)
            export = workspace / "profile.json"
            export.write_text('{"facts":{"name":{"value":"tampered"}}}', encoding="utf-8")
            second = run_cli("profile", "document", source.relative_to(ROOT), *common)
            self.assertEqual(second.returncode, 0, second.stdout)
            self.assertEqual(json.loads(second.stdout)["changes"], 0)
            restored = json.loads(export.read_text(encoding="utf-8"))
            self.assertEqual(restored["facts"]["name"]["value"], "canonical")
            with sqlite3.connect(workspace / "profile.sqlite3") as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM profile_claims").fetchone()[0], 1)

    def test_containment_rejects_absolute_parent_symlink_and_oversize(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "documents") as documents_temporary, tempfile.TemporaryDirectory(dir=ROOT / "workspace") as workspace_temporary:
            documents = Path(documents_temporary)
            workspace = Path(workspace_temporary)
            safe = documents / "safe.json"
            safe.write_text('{"name":"A"}', encoding="utf-8")
            common = ("--documents-root", documents.relative_to(ROOT), "--workspace-root", workspace.relative_to(ROOT))
            absolute = run_cli("profile", "document", safe, *common)
            self.assertEqual(absolute.returncode, 2)
            self.assertIn("absolute", absolute.stdout)
            escaped = run_cli("profile", "document", documents.relative_to(ROOT) / ".." / "outside.json", *common)
            self.assertEqual(escaped.returncode, 2)
            self.assertIn("parent traversal", escaped.stdout)
            link = documents / "link.json"
            try:
                link.symlink_to(safe)
            except OSError:
                self.skipTest("symlinks unavailable")
            linked = run_cli("profile", "document", link.relative_to(ROOT), *common)
            self.assertEqual(linked.returncode, 2)
            self.assertIn("symbolic link", linked.stdout)
            real = documents / "real"
            real.mkdir()
            (real / "safe.json").write_text('{"name":"A"}', encoding="utf-8")
            ancestor_link = documents / "ancestor-link"
            ancestor_link.symlink_to(real, target_is_directory=True)
            ancestor = run_cli("profile", "document", (ancestor_link / "safe.json").relative_to(ROOT), *common)
            self.assertEqual(ancestor.returncode, 2)
            self.assertIn("symbolic link", ancestor.stdout)
            root_link = documents / "root-link"
            root_link.symlink_to(real, target_is_directory=True)
            root_symlink = run_cli(
                "profile", "document", (root_link / "safe.json").relative_to(ROOT),
                "--documents-root", root_link.relative_to(ROOT),
                "--workspace-root", workspace.relative_to(ROOT),
            )
            self.assertEqual(root_symlink.returncode, 2)
            self.assertIn("symbolic link", root_symlink.stdout)
            oversized = documents / "large.json"
            oversized.write_bytes(b" " * (2_000_001))
            large = run_cli("profile", "document", oversized.relative_to(ROOT), *common)
            self.assertEqual(large.returncode, 2)
            self.assertIn("too large", large.stdout)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_nonregular_output_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "documents") as documents_temporary, tempfile.TemporaryDirectory(dir=ROOT / "workspace") as workspace_temporary:
            documents = Path(documents_temporary)
            workspace = Path(workspace_temporary)
            source = documents / "safe.json"
            source.write_text('{"name":"A"}', encoding="utf-8")
            fifo = workspace / "profile.json"
            os.mkfifo(fifo)
            result = run_cli(
                "profile", "document", source.relative_to(ROOT),
                "--documents-root", documents.relative_to(ROOT),
                "--workspace-root", workspace.relative_to(ROOT),
                "--profile", fifo.relative_to(ROOT),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("regular file", result.stdout)

    def test_non_tty_interview_requires_script(self):
        result = run_cli("profile", "interview")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--responses", json.loads(result.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
