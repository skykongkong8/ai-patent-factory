import sqlite3
import tempfile
import unittest
from pathlib import Path

from patent_factory.artifacts import ArtifactError
from patent_factory.database import consistent_snapshot, connect_database
from patent_factory.models import RunState
from patent_factory.state import StateStore


class SnapshotAndIntegrityTests(unittest.TestCase):
    def test_consistent_snapshot_does_not_mix_concurrent_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            setup = connect_database(path)
            setup.execute("PRAGMA journal_mode=WAL")
            StateStore(setup).create_run("run")
            setup.close()
            reader = connect_database(path)
            writer = connect_database(path)
            with consistent_snapshot(reader):
                before = reader.execute("SELECT state_version FROM runs WHERE run_id='run'").fetchone()[0]
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE runs SET state_version=state_version+1 WHERE run_id='run'")
                writer.commit()
                after = reader.execute("SELECT state_version FROM runs WHERE run_id='run'").fetchone()[0]
            self.assertEqual((before,after),(0,0))
            self.assertEqual(reader.execute("SELECT state_version FROM runs WHERE run_id='run'").fetchone()[0],1)
            reader.close()
            writer.close()

    def test_corrupt_database_is_refused_without_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "factory.sqlite3"
            original = b"not a sqlite database\x00private"
            path.write_bytes(original)
            with self.assertRaises(sqlite3.DatabaseError):
                connect_database(path)
            self.assertEqual(path.read_bytes(),original)

    def test_state_store_startup_recovers_only_interrupted_export_temporaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exports = root / "exports"
            exports.mkdir()
            interrupted = exports / ".artifact-interrupted.tmp"
            interrupted.write_bytes(b"partial")
            published = exports / "published.json"
            published.write_bytes(b"complete")
            connection = connect_database(root / "factory.sqlite3")
            StateStore(connection,export_directories=(exports,))
            self.assertFalse(interrupted.exists())
            self.assertEqual(published.read_bytes(),b"complete")
            connection.close()

    def test_startup_reconciles_orphans_and_refuses_tampered_registered_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exports = root / "exports"
            exports.mkdir()
            connection = connect_database(root / "factory.sqlite3")
            store = StateStore(connection, export_directories=(exports,))
            store.create_run("run")
            result, exported = store.publish_transition(
                "run",
                RunState.PROFILE_PENDING,
                actor="tester",
                reason="publish",
                operation="publish",
                idempotency_key="key",
                artifact_kind="profile",
                artifact_content={"name": "홍길동"},
                export_directory=exports,
            )
            self.assertEqual(result.snapshot.state, RunState.PROFILE_PENDING)
            orphan = exports / "ar_orphan.json"
            orphan.write_text("{}\n", encoding="utf-8")
            StateStore(connection, export_directories=(exports,))
            self.assertFalse(orphan.exists())

            Path(exported.path).write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "registered export mismatch"):
                StateStore(connection, export_directories=(exports,))
            connection.close()

    def test_startup_refuses_missing_or_out_of_root_registered_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exports = root / "exports"
            other = root / "other"
            exports.mkdir()
            other.mkdir()
            connection = connect_database(root / "factory.sqlite3")
            store = StateStore(connection, export_directories=(exports,))
            store.create_run("run")
            _, exported = store.publish_transition(
                "run",
                RunState.PROFILE_PENDING,
                actor="tester",
                reason="publish",
                operation="publish",
                idempotency_key="key",
                artifact_kind="profile",
                artifact_content={"name": "홍길동"},
                export_directory=exports,
            )
            with self.assertRaisesRegex(Exception, "outside configured export directories"):
                StateStore(connection, export_directories=(other,))
            Path(exported.path).unlink()
            with self.assertRaisesRegex(ArtifactError, "registered export missing"):
                StateStore(connection, export_directories=(exports,))
            connection.close()


if __name__ == "__main__":
    unittest.main()
