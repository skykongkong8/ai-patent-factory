import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import (
    InjectedFailure, SCHEMA_VERSION, connect_database, ingest,
    profile_conflict_snapshot, profile_payload, resolve_profile_conflicts,
)
from patent_factory.profile import IncomingFact
from patent_factory.provenance import Claim, EpistemicLabel


def fact(field, value, source):
    return IncomingFact(field, value, Claim(EpistemicLabel.USER_STATEMENT, source_id=source))


class G006ProfileConflictTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "profile.sqlite3"
        self.connection = connect_database(self.path)
        ingest(self.connection, "interview", [fact("name", "A", "initial"), fact("technical_domain", "old", "initial")])
        conflict = ingest(self.connection, "interview", [fact("name", "B", "changed"), fact("technical_domain", "new", "changed")])
        self.batch_id = conflict.batch_id
        self.snapshot = profile_conflict_snapshot(self.connection, self.batch_id)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def _decision(self, action, choices=None, subject_hash=None):
        return {
            "action": action, "actor": "user", "batch_id": self.batch_id,
            "choices": choices or [], "reason": "reviewed exact conflict batch",
            "schema_version": "profile-conflict-decision-v1",
            "subject_hash": subject_hash or self.snapshot["subject_hash"],
        }

    def test_stale_subject_and_partial_choice_are_rejected_without_mutation(self):
        before = profile_payload(self.connection)
        choices = [{"conflict_id": self.snapshot["conflicts"][0]["conflict_id"], "selected": "incoming"}]
        with self.assertRaisesRegex(ValueError, "stale"):
            resolve_profile_conflicts(self.connection, self._decision("choose_value", choices, "f" * 64))
        with self.assertRaisesRegex(ValueError, "exactly one choice"):
            resolve_profile_conflicts(self.connection, self._decision("choose_value", choices))
        self.assertEqual(profile_payload(self.connection), before)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM profile_conflict_resolutions").fetchone()[0], 0)

    def test_multi_conflict_mixed_existing_and_incoming_choices_are_exact(self):
        choices = [{
            "conflict_id": item["conflict_id"],
            "selected": "existing" if item["field"] == "name" else "incoming",
        } for item in self.snapshot["conflicts"]]
        result = resolve_profile_conflicts(self.connection, self._decision("choose_source", choices))
        payload = profile_payload(self.connection)
        self.assertEqual((payload["facts"]["name"]["value"], payload["facts"]["technical_domain"]["value"]), ("A", "new"))
        self.assertEqual((result["status"], len(payload["conflicts"])), ("profile_ready", 0))

    def test_retain_unresolved_preserves_canonical_values_and_replays(self):
        request = self._decision("retain_unresolved")
        first = resolve_profile_conflicts(self.connection, request)
        payload = profile_payload(self.connection)
        self.assertEqual((payload["facts"]["name"]["value"], payload["facts"]["technical_domain"]["value"]), ("A", "old"))
        self.assertEqual((first["status"], len(payload["conflicts"])), ("conflict_resolution_required", 2))
        replay = resolve_profile_conflicts(self.connection, request)
        self.assertTrue(replay["replayed"])

    def test_stop_is_terminal_marker_and_preserves_unresolved_history(self):
        result = resolve_profile_conflicts(self.connection, self._decision("stop"))
        payload = profile_payload(self.connection)
        self.assertEqual((result["status"], payload["state"], len(payload["conflicts"])), ("stopped", "stopped", 2))
        with self.assertRaisesRegex(ValueError, "stopped"):
            ingest(self.connection, "interview", [fact("name", "C", "after-stop")])
        self.assertEqual(profile_payload(self.connection), payload)

    def test_conflict_application_fault_rolls_back_values_decision_and_status(self):
        choices = [{"conflict_id": item["conflict_id"], "selected": "incoming"} for item in self.snapshot["conflicts"]]
        before = profile_payload(self.connection)
        with self.assertRaises(InjectedFailure):
            resolve_profile_conflicts(self.connection, self._decision("choose_value", choices), fault_at="after_conflict_application")
        self.assertEqual(profile_payload(self.connection), before)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM profile_conflict_resolutions").fetchone()[0], 0)

    def test_v6_to_v7_migration_fault_rolls_back_then_repairs(self):
        self.connection.close()
        raw = sqlite3.connect(self.path)
        raw.execute("DROP TABLE profile_conflict_resolutions")
        raw.execute("PRAGMA user_version=6")
        raw.commit()
        raw.close()
        with self.assertRaises(InjectedFailure):
            connect_database(self.path, fault_at="migration_v7")
        raw = sqlite3.connect(self.path)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name='profile_conflict_resolutions'").fetchone())
        raw.close()
        self.connection = connect_database(self.path)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='profile_conflict_resolutions'").fetchone())


if __name__ == "__main__":
    unittest.main()
