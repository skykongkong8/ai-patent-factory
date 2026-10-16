import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from patent_factory.database import connect_database, export_profile, ingest, profile_payload
from patent_factory.profile import interview_facts
from patent_factory.runs import start_run


class LegacyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.documents = self.root / "documents"
        self.documents.mkdir(mode=0o700)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with connect_database(self.workspace / "profile.sqlite3") as connection:
            ingest(connection, "interview", interview_facts({"name": "Private Fixture", "expertise": "sensor"}))
            self.profile = export_profile(connection, self.workspace / "profile.json")

    def _run(self):
        run = self.workspace / "runs" / "legacy"
        run.mkdir(parents=True, mode=0o700)
        with connect_database(self.workspace / "profile.sqlite3") as profile_connection:
            with connect_database(run / "factory.sqlite3") as connection:
                start_run(connection, profile_connection=profile_connection, run_root=run,
                          run_id="legacy", profile=profile_payload(profile_connection))
        return run

    def _hashes(self):
        return {str(path.relative_to(self.workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.workspace.rglob("*") if path.is_file()}

    def _catalog(self):
        from patent_factory.catalog import Catalog

        catalog = Catalog(Path("workspace"), Path("documents"), create=True)
        self.addCleanup(catalog.close)
        return catalog

    def test_inspect_is_metadata_only_and_preserves_all_existing_bytes(self):
        from patent_factory.legacy import inspect_legacy

        run = self._run()
        before = self._hashes()
        result = inspect_legacy(self.workspace, run=run, run_id="legacy")
        self.assertEqual(result["status"], "reported")
        self.assertEqual(result["profile"]["fact_count"], 2)
        self.assertEqual(result["profile"]["state"], "profile_ready")
        self.assertEqual(result["runs"][0]["run_id"], "legacy")
        self.assertEqual(result["runs"][0]["state"], "research_ready")
        self.assertTrue(result["runs"][0]["artifacts"])
        self.assertNotIn("Private Fixture", json.dumps(result))
        self.assertNotIn("sensor", json.dumps(result))
        self.assertEqual(self._hashes(), before)

    def test_inspect_does_not_enumerate_unselected_run_tree(self):
        from patent_factory.legacy import inspect_legacy

        run = self.workspace / "runs" / "unselected"
        run.mkdir(parents=True)
        (run / "factory.sqlite3").write_text("not a database")
        result = inspect_legacy(self.workspace)
        self.assertEqual(result["runs"], [])

    def test_inspect_wrong_run_id_fails_without_writing(self):
        from patent_factory.legacy import inspect_legacy

        run = self._run()
        before = self._hashes()
        with self.assertRaisesRegex(ValueError, "run|requested"):
            inspect_legacy(self.workspace, run=run, run_id="other")
        self.assertEqual(self._hashes(), before)

    def test_inspect_rejects_symlink_before_reading(self):
        from patent_factory.legacy import inspect_legacy

        run = self._run()
        linked = self.workspace / "linked"
        linked.symlink_to(run, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            inspect_legacy(self.workspace, run=linked, run_id="legacy")

    def test_inspect_rejects_symlink_profile_database(self):
        from patent_factory.legacy import inspect_legacy

        database = self.workspace / "profile.sqlite3"
        target = self.workspace / "original.sqlite3"
        database.rename(target)
        database.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            inspect_legacy(self.workspace)

    def test_inspect_missing_profile_does_not_create_it(self):
        from patent_factory.legacy import inspect_legacy

        empty = self.root / "empty"
        empty.mkdir()
        result = inspect_legacy(empty)
        self.assertIsNone(result["profile"])
        self.assertEqual(list(empty.iterdir()), [])

    def test_adopt_profile_preserves_source_and_activates_exact_new_version(self):
        from patent_factory.legacy import adopt_legacy

        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        before = (self.workspace / "profile.sqlite3").read_bytes()
        result = adopt_legacy(catalog, inventor_id=inventor["inventor_id"], profile=True)
        self.assertEqual(result["status"], "adopted")
        self.assertEqual((self.workspace / "profile.sqlite3").read_bytes(), before)
        self.assertNotIn("Private Fixture", json.dumps(result))
        with connect_database(Path(result["profile"]["database"])) as connection:
            self.assertEqual(profile_payload(connection), self.profile)

    def test_adopt_profile_rejects_pending_conflicts_before_creating_version(self):
        from patent_factory.legacy import adopt_legacy

        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        with connect_database(self.workspace / "profile.sqlite3") as connection:
            ingest(connection, "interview", interview_facts({"name": "Conflicting Fixture"}))
        before = self._hashes()
        with self.assertRaisesRegex(ValueError, "conflict|profile_ready"):
            adopt_legacy(catalog, inventor_id=inventor["inventor_id"], profile=True)
        self.assertEqual(self._hashes(), before)

    def test_adopt_profile_retry_returns_same_version_without_writing(self):
        from patent_factory.legacy import adopt_legacy

        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        first = adopt_legacy(catalog, inventor_id=inventor["inventor_id"], profile=True)
        before = self._hashes()
        second = adopt_legacy(catalog, inventor_id=inventor["inventor_id"], profile=True)
        self.assertEqual(second["profile"]["version_id"], first["profile"]["version_id"])
        self.assertEqual(self._hashes(), before)

    def test_adopt_profile_rejects_reassignment_to_another_owner(self):
        from patent_factory.legacy import adopt_legacy

        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        adopt_legacy(catalog, inventor_id=inventor["inventor_id"], profile=True)
        other = catalog.create_inventor("Other")
        before = self._hashes()
        with self.assertRaisesRegex(ValueError, "owner|bound|belong"):
            adopt_legacy(catalog, inventor_id=other["inventor_id"], profile=True)
        self.assertEqual(self._hashes(), before)

    def test_adopt_profile_recovers_candidate_after_publication_failure(self):
        from patent_factory.legacy import adopt_legacy

        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        original = (self.workspace / "profile.sqlite3").read_bytes()
        with patch.object(catalog, "finish_version", side_effect=OSError("fixture export failure")):
            with self.assertRaisesRegex(OSError, "export failure"):
                adopt_legacy(catalog, inventor_id=inventor["inventor_id"], profile=True)
        claim = catalog.legacy_source(Path("workspace/profile.sqlite3"))
        result = adopt_legacy(catalog, inventor_id=inventor["inventor_id"], profile=True)
        self.assertEqual(result["profile"]["version_id"], claim["version_id"])
        self.assertEqual(len(catalog.list_versions(inventor["inventor_id"])), 1)
        self.assertEqual((self.workspace / "profile.sqlite3").read_bytes(), original)

    def test_v7_inspect_and_adopt_never_migrate_source(self):
        from patent_factory.legacy import adopt_legacy, inspect_legacy

        run = self._run()
        for path in (self.workspace / "profile.sqlite3", run / "factory.sqlite3"):
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA user_version=7")
        profile_bytes = (self.workspace / "profile.sqlite3").read_bytes()
        run_bytes = (run / "factory.sqlite3").read_bytes()
        inspect_legacy(self.workspace, run=run, run_id="legacy")
        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        topic = catalog.create_topic(inventor["inventor_id"], "Fixture topic", [])
        adopt_legacy(catalog, inventor_id=inventor["inventor_id"], profile=True)
        adopt_legacy(catalog, inventor_id=inventor["inventor_id"], topic_id=topic["topic_id"], run=run, run_id="legacy")
        self.assertEqual((self.workspace / "profile.sqlite3").read_bytes(), profile_bytes)
        self.assertEqual((run / "factory.sqlite3").read_bytes(), run_bytes)

    def test_adopt_run_preserves_bytes_and_uses_its_historical_profile(self):
        from patent_factory.legacy import adopt_legacy

        run = self._run()
        original_run = {str(path.relative_to(run)): path.read_bytes() for path in run.rglob("*") if path.is_file()}
        with connect_database(self.workspace / "profile.sqlite3") as connection:
            ingest(connection, "interview", interview_facts({"later_fact": "newer"}))
        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        topic = catalog.create_topic(inventor["inventor_id"], "Fixture topic", [])
        result = adopt_legacy(catalog, inventor_id=inventor["inventor_id"],
                              topic_id=topic["topic_id"], run=run, run_id="legacy")
        self.assertEqual(result["status"], "adopted")
        binding = result["run"]
        self.assertTrue(binding["legacy"])
        with connect_database(Path(binding["legacy_profile_database"])) as connection:
            self.assertEqual(profile_payload(connection), self.profile)
        self.assertEqual({str(path.relative_to(run)): path.read_bytes() for path in run.rglob("*") if path.is_file()}, original_run)
        self.assertNotIn("Private Fixture", json.dumps(result))

    def test_adopt_run_rejects_different_owner_without_altering_original(self):
        from patent_factory.legacy import adopt_legacy

        run = self._run()
        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        topic = catalog.create_topic(inventor["inventor_id"], "Fixture topic", [])
        adopt_legacy(catalog, inventor_id=inventor["inventor_id"], topic_id=topic["topic_id"], run=run, run_id="legacy")
        other = catalog.create_inventor("Other")
        other_topic = catalog.create_topic(other["inventor_id"], "Other topic", [])
        before = self._hashes()
        with self.assertRaisesRegex(ValueError, "owner|bound|belong"):
            adopt_legacy(catalog, inventor_id=other["inventor_id"], topic_id=other_topic["topic_id"], run=run, run_id="legacy")
        self.assertEqual(self._hashes(), before)

    def test_adopt_run_retry_does_not_republish_historical_snapshot(self):
        from patent_factory.legacy import adopt_legacy

        run = self._run()
        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        topic = catalog.create_topic(inventor["inventor_id"], "Fixture topic", [])
        first = adopt_legacy(catalog, inventor_id=inventor["inventor_id"], topic_id=topic["topic_id"], run=run, run_id="legacy")
        snapshot = Path(first["run"]["legacy_profile_export"])
        before = snapshot.stat().st_mtime_ns
        second = adopt_legacy(catalog, inventor_id=inventor["inventor_id"], topic_id=topic["topic_id"], run=run, run_id="legacy")
        self.assertEqual(second["run"], first["run"])
        self.assertEqual(snapshot.stat().st_mtime_ns, before)

    def test_adopt_run_requires_topic_and_exact_id_before_mutation(self):
        from patent_factory.legacy import adopt_legacy

        run = self._run()
        catalog = self._catalog()
        inventor = catalog.create_inventor("Fixture")
        topic = catalog.create_topic(inventor["inventor_id"], "Fixture topic", [])
        before = self._hashes()
        with self.assertRaisesRegex(ValueError, "topic"):
            adopt_legacy(catalog, inventor_id=inventor["inventor_id"], run=run, run_id="legacy")
        with self.assertRaisesRegex(ValueError, "run|requested"):
            adopt_legacy(catalog, inventor_id=inventor["inventor_id"], topic_id=topic["topic_id"], run=run, run_id="wrong")
        self.assertEqual(self._hashes(), before)


if __name__ == "__main__":
    unittest.main()
