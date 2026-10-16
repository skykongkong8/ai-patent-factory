import importlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from patent_factory.config import load_evaluation_config
from patent_factory.database import connect_database, ingest, profile_payload
from patent_factory.ideation import _profile_claim_categories, run_ideation
from patent_factory.profile import IncomingFact, interview_facts
from patent_factory.provenance import Claim, EpistemicLabel, canonical_json, digest
from patent_factory.runs import PreparedRunProfile, start_run
from patent_factory.state import StateStore
from tests.integration.test_g004_ideation_and_shortlist import (
    candidate_input, ready_profile, ready_research,
)


class PinnedContextsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "base.sqlite3"
        self.topic = self.root / "topic.sqlite3"
        self.effective = self.root / "effective.sqlite3"
        self.export = self.root / "effective.json"
        connection, self.base_profile = ready_profile(self.base)
        connection.close()
        with connect_database(self.topic) as connection:
            ingest(connection, "interview", interview_facts({
                "project_summary": "Public fixture topic problem",
                "technical_domain": "Public fixture topic domain",
                "constraints": "Public fixture bounded memory",
            }))
            self.topic_profile = profile_payload(connection)
        self.binding = {
            "inventor_id": "inv_fixture", "topic_id": "topic_fixture",
            "profile_version_id": "pv_fixture", "topic_version_id": "tv_fixture",
        }

    def contexts(self):
        if importlib.util.find_spec("patent_factory.contexts") is None:
            self.fail("managed pinned profile context implementation is missing")
        return importlib.import_module("patent_factory.contexts")

    def prepare(self, **changes):
        arguments = {
            "profile_database": self.base, "topic_database": self.topic,
            "output_database": self.effective, "output_export": self.export,
            "binding": self.binding,
        }
        arguments.update(changes)
        return self.contexts().prepare_effective_profile(**arguments)

    def test_effective_profile_preserves_common_and_namespaces_topic_claims(self):
        prepared = self.prepare()
        self.assertEqual(prepared.context["version"], "profile-context-v2")
        self.assertEqual(prepared.context["binding"], self.binding)
        self.assertEqual(prepared.context["base_profile"], self.base_profile)
        self.assertEqual(prepared.context["topic_context"], self.topic_profile)
        with connect_database(self.effective) as connection:
            effective = profile_payload(connection)
        expected_facts = dict(self.base_profile["facts"])
        expected_facts.update({"topic." + field: entry
                               for field, entry in self.topic_profile["facts"].items()})
        self.assertEqual(effective["facts"], expected_facts)
        self.assertEqual(prepared.context["profile"], effective)
        self.assertEqual(json.loads(self.export.read_text()), effective)
        self.assertEqual(prepared.content_hash, digest(prepared.context))
        self.assertEqual(stat.S_IMODE(self.effective.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.export.stat().st_mode), 0o600)

    def test_effective_profile_replays_and_restores_only_missing_export(self):
        first = self.prepare()
        database_bytes = self.effective.read_bytes()
        self.export.unlink()
        self.assertEqual(self.prepare(), first)
        self.assertEqual(self.effective.read_bytes(), database_bytes)
        self.assertEqual(json.loads(self.export.read_text()), first.context["profile"])

    def test_different_existing_effective_database_is_rejected_without_overwrite(self):
        with connect_database(self.effective) as connection:
            ingest(connection, "interview", interview_facts({"expertise": "Other owner"}))
            before = profile_payload(connection)
        with self.assertRaisesRegex(ValueError, "effective profile.*different"):
            self.prepare()
        with connect_database(self.effective) as connection:
            self.assertEqual(profile_payload(connection), before)
        self.assertFalse(self.export.exists())

    def test_different_existing_export_is_rejected_without_overwrite(self):
        self.export.write_text('{"profile_version":"forged"}')
        with self.assertRaisesRegex(ValueError, "effective profile.*export"):
            self.prepare()
        self.assertEqual(self.export.read_text(), '{"profile_version":"forged"}')

    def test_conflicting_topic_is_rejected_before_effective_database_created(self):
        with connect_database(self.topic) as connection:
            ingest(connection, "interview", interview_facts({"constraints": "Other constraint"}))
        with self.assertRaisesRegex(ValueError, "profile_ready"):
            self.prepare()
        self.assertFalse(self.effective.exists())
        self.assertFalse(self.export.exists())

    def test_common_topic_namespace_collision_is_rejected(self):
        with connect_database(self.base) as connection:
            ingest(connection, "interview", interview_facts({"topic.constraints": "Common constraint"}))
        with self.assertRaisesRegex(ValueError, "reserved.*topic"):
            self.prepare()
        self.assertFalse(self.effective.exists())

    def test_owner_binding_changes_context_identity_even_with_identical_facts(self):
        first = self.prepare()
        second = self.prepare(binding={**self.binding, "inventor_id": "inv_another"})
        self.assertEqual(first.revision, second.revision)
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_topic_project_summary_claim_is_a_problem_reference(self):
        claim = Claim(EpistemicLabel.USER_STATEMENT, "topic-public-fixture").as_dict()
        profile = {**self.base_profile, "facts": {
            **self.base_profile["facts"],
            "topic.project_summary": {"value": "Public fixture", "claims": [claim]},
        }}
        categories = _profile_claim_categories(profile)
        self.assertEqual(categories.get(("topic.project_summary", claim["claim_id"])),
                         frozenset({"problem"}))

    def test_start_run_publishes_v2_context_and_resolver_rejects_other_database(self):
        prepared = self.prepare()
        run_root = self.root / "run"
        run_root.mkdir(mode=0o700)
        with connect_database(run_root / "factory.sqlite3") as connection, \
                connect_database(self.effective) as profile_connection:
            result = start_run(
                connection, profile_connection=profile_connection, run_root=run_root,
                run_id="run", profile=prepared.context["profile"], prepared_profile=prepared,
            )
            self.assertEqual(result.artifact.schema_version, "profile-context-v2")
            self.assertEqual(result.artifact.content, prepared.context)
            resolved = self.contexts().resolve_pinned_profile(connection, profile_connection, "run")
            self.assertEqual(resolved, prepared.context["profile"])
            with connect_database(self.base) as other:
                with self.assertRaisesRegex(ValueError, "pinned.*profile"):
                    self.contexts().resolve_pinned_profile(connection, other, "run")

    def test_start_run_rejects_prepared_context_for_another_database_before_writes(self):
        prepared = self.prepare()
        run_root = self.root / "run"
        run_root.mkdir(mode=0o700)
        with connect_database(run_root / "factory.sqlite3") as connection, \
                connect_database(self.base) as profile_connection:
            with self.assertRaisesRegex(ValueError, "prepared.*profile"):
                start_run(
                    connection, profile_connection=profile_connection, run_root=run_root,
                    run_id="run", profile=self.base_profile, prepared_profile=prepared,
                )
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_start_run_rejects_inconsistent_prepared_context_hash_before_writes(self):
        prepared = self.prepare()
        forged = PreparedRunProfile(prepared.revision, digest("forged"), prepared.context)
        run_root = self.root / "run"
        run_root.mkdir(mode=0o700)
        with connect_database(run_root / "factory.sqlite3") as connection, \
                connect_database(self.effective) as profile_connection:
            with self.assertRaisesRegex(ValueError, "prepared.*hash"):
                start_run(
                    connection, profile_connection=profile_connection, run_root=run_root,
                    run_id="run", profile=prepared.context["profile"], prepared_profile=forged,
                )
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_resolver_legacy_context_keeps_current_profile_selection(self):
        run_root = self.root / "run"
        run_root.mkdir(mode=0o700)
        with connect_database(run_root / "factory.sqlite3") as connection, \
                connect_database(self.base) as profile_connection:
            result = start_run(connection, profile_connection=profile_connection,
                               run_root=run_root, run_id="run", profile=self.base_profile)
            self.assertEqual(result.artifact.schema_version, "profile-context-v1")
            resolved = self.contexts().resolve_pinned_profile(connection, profile_connection, "run")
            self.assertEqual(resolved, self.base_profile)

    def test_ideation_retains_exact_v2_binding_with_topic_references(self):
        prepared = self.prepare()
        run_root = self.root / "run"
        run_root.mkdir(mode=0o700)
        with connect_database(run_root / "factory.sqlite3") as connection, \
                connect_database(self.effective) as profile_connection:
            evidence, span, _research = ready_research(connection, run_root)
            store = StateStore(connection)
            pinned = store.add_revision("run", "profile_context", prepared.context,
                                        schema_version="profile-context-v2")
            request = candidate_input(3, evidence, span)
            topic_claim = prepared.context["profile"]["facts"]["topic.project_summary"]["claims"][0]
            for candidate in request["candidates"]:
                candidate["profile_references"][0] = {
                    "field": "topic.project_summary", "kind": "problem",
                    "claim_id": topic_claim["claim_id"],
                }
            result = run_ideation(
                connection, profile_connection=profile_connection, run_root=run_root,
                run_id="run", profile=prepared.context["profile"], candidate_input=request,
                config=load_evaluation_config(),
            )
            self.assertEqual(result.next_state, "candidates_ready")
            self.assertEqual(store.snapshot("run").current_revisions["profile_context"],
                             pinned.revision_id)
            row = connection.execute("SELECT schema_version,content_json FROM artifact_revisions "
                                     "WHERE revision_id=?", (pinned.revision_id,)).fetchone()
            self.assertEqual(row["schema_version"], "profile-context-v2")
            self.assertEqual(canonical_json(json.loads(row["content_json"])),
                             canonical_json(prepared.context))


if __name__ == "__main__":
    unittest.main()
