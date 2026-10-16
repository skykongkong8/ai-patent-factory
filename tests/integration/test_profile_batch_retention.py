import sqlite3
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import (
    InjectedFailure, connect_database, ingest, profile_conflict_snapshot, profile_payload,
    resolve_profile_conflicts,
)
from patent_factory.profile import IncomingFact
from patent_factory.provenance import Claim, EpistemicLabel


def source_fact(field, value, source):
    return IncomingFact(
        field, value,
        Claim(EpistemicLabel.SOURCE_FACT, source, "fixture-content", f"span-{field}-{value}"),
    )


class ProfileBatchRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "profile.sqlite3"
        self.connection = connect_database(self.path)
        ingest(self.connection, "document", [
            source_fact("name", "Old name", "initial"),
            source_fact("expertise", "Robotics", "initial"),
        ])
        self.incoming = [
            source_fact("name", "New name", "replacement"),
            source_fact("expertise", "Robotics", "replacement"),
            source_fact("project_summary", "Sensor project", "replacement"),
        ]
        self.batch_id = ingest(self.connection, "document", self.incoming).batch_id

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def decision(self, selected="incoming"):
        snapshot = profile_conflict_snapshot(self.connection, self.batch_id)
        return {
            "schema_version": "profile-conflict-decision-v1",
            "action": "choose_source", "actor": "user",
            "batch_id": self.batch_id, "reason": "Reviewed the fixture batch",
            "subject_hash": snapshot["subject_hash"],
            "choices": [
                {"conflict_id": item["conflict_id"], "selected": selected}
                for item in snapshot["conflicts"]
            ],
        }

    def test_resolved_batch_applies_new_fields_and_original_source_claims_after_reopen(self):
        self.assertNotIn("project_summary", profile_payload(self.connection)["facts"])
        self.connection.close()
        self.connection = connect_database(self.path)
        result = resolve_profile_conflicts(self.connection, self.decision())
        facts = profile_payload(self.connection)["facts"]
        self.assertEqual(facts.get("project_summary", {}).get("value"), "Sensor project")
        for incoming in self.incoming:
            self.assertIn(incoming.claim.as_dict(), facts[incoming.field]["claims"])
        self.assertEqual(result["status"], "profile_ready")
        self.assertTrue(any(
            claim["label"] == "user_statement"
            and claim["source_id"] == result["resolution_id"]
            for claim in facts["name"]["claims"]
        ))

    def test_choosing_existing_applies_compatible_fields_without_rejected_source_claim(self):
        resolve_profile_conflicts(self.connection, self.decision("existing"))
        facts = profile_payload(self.connection)["facts"]
        self.assertEqual(facts["name"]["value"], "Old name")
        self.assertEqual(facts.get("project_summary", {}).get("value"), "Sensor project")
        self.assertNotIn(self.incoming[0].claim.as_dict(), facts["name"]["claims"])
        self.assertIn(self.incoming[1].claim.as_dict(), facts["expertise"]["claims"])

    def test_invalid_decisions_preserve_all_input_for_later_resolution(self):
        before = profile_payload(self.connection)
        request = self.decision()
        request["subject_hash"] = "outdated-subject"
        with self.assertRaisesRegex(ValueError, "stale"):
            resolve_profile_conflicts(self.connection, request)
        request = self.decision()
        request["choices"] = []
        with self.assertRaisesRegex(ValueError, "exactly one choice"):
            resolve_profile_conflicts(self.connection, request)
        self.assertEqual(profile_payload(self.connection), before)
        resolve_profile_conflicts(self.connection, self.decision())
        self.assertEqual(
            profile_payload(self.connection)["facts"].get("project_summary", {}).get("value"),
            "Sensor project",
        )

    def test_faults_roll_back_complete_batch_and_retry_replays_without_duplicate_claims(self):
        before = profile_payload(self.connection)
        request = self.decision()
        for boundary in ("after_conflict_application", "after_conflict_decision"):
            with self.subTest(boundary=boundary):
                with self.assertRaises(InjectedFailure):
                    resolve_profile_conflicts(self.connection, request, fault_at=boundary)
                self.assertEqual(profile_payload(self.connection), before)
                self.assertEqual(self.connection.execute(
                    "SELECT count(*) FROM profile_conflict_resolutions"
                ).fetchone()[0], 0)
        first = resolve_profile_conflicts(self.connection, request)
        complete = profile_payload(self.connection)
        replay = resolve_profile_conflicts(self.connection, request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["resolution_id"], first["resolution_id"])
        self.assertEqual(profile_payload(self.connection), complete)
        self.assertIn("project_summary", complete["facts"])
        for incoming in self.incoming:
            self.assertEqual(
                complete["facts"][incoming.field]["claims"].count(incoming.claim.as_dict()), 1,
            )

    def test_legacy_v7_conflict_without_retained_input_still_resolves(self):
        self.connection.execute("DROP TABLE profile_batch_inputs")
        self.connection.execute("PRAGMA user_version=7")
        self.connection.close()
        self.connection = connect_database(self.path)
        result = resolve_profile_conflicts(self.connection, self.decision())
        payload = profile_payload(self.connection)
        self.assertEqual(payload["facts"]["name"]["value"], "New name")
        self.assertEqual(result["status"], "profile_ready")
        self.assertNotIn("project_summary", payload["facts"])

    def test_v8_migration_failure_preserves_v7_profile_then_retries(self):
        before = profile_payload(self.connection)
        self.connection.execute("DROP TABLE profile_batch_inputs")
        self.connection.execute("PRAGMA user_version=7")
        self.connection.close()
        with self.assertRaises(InjectedFailure):
            connect_database(self.path, fault_at="migration_v8")
        with sqlite3.connect(self.path) as raw:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertIsNone(raw.execute(
                "SELECT 1 FROM sqlite_master WHERE name='profile_batch_inputs'"
            ).fetchone())
        self.connection = connect_database(self.path)
        self.assertEqual(profile_payload(self.connection), before)

    def test_intra_batch_conflict_only_applies_claims_for_selected_value(self):
        first = source_fact("new_field", "First", "first-source")
        second = source_fact("new_field", "Second", "second-source")
        result = ingest(self.connection, "folder", [first, second])
        self.batch_id = result.batch_id
        resolve_profile_conflicts(self.connection, self.decision("incoming"))
        field = profile_payload(self.connection)["facts"]["new_field"]
        self.assertEqual(field["value"], "Second")
        self.assertIn(second.claim.as_dict(), field["claims"])
        self.assertNotIn(first.claim.as_dict(), field["claims"])


if __name__ == "__main__":
    unittest.main()
