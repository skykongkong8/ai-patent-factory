import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database
from patent_factory.state import StateStore


class InvalidationDagTests(unittest.TestCase):
    def _graph(self, directory):
        connection = connect_database(Path(directory) / "factory.sqlite3")
        store = StateStore(connection)
        store.create_run("run")
        revisions = {}
        revisions["profile"] = store.add_revision("run", "profile", {"version": 1})
        revisions["query"] = store.add_revision("run", "query", {"version": 1}, dependencies=(revisions["profile"].revision_id,))
        revisions["evidence"] = store.add_revision("run", "evidence", {"version": 1}, dependencies=(revisions["query"].revision_id,))
        revisions["candidate"] = store.add_revision("run", "candidate", {"version": 1}, dependencies=(revisions["evidence"].revision_id,))
        revisions["finalist"] = store.add_revision("run", "finalist", {"version": 1}, dependencies=(revisions["candidate"].revision_id,))
        revisions["corpus"] = store.add_revision("run", "corpus", {"version": 1}, dependencies=(revisions["evidence"].revision_id, revisions["finalist"].revision_id))
        revisions["feature_map"] = store.add_revision("run", "feature_map", {"version": 1}, dependencies=(revisions["corpus"].revision_id, revisions["finalist"].revision_id))
        revisions["scorer_version"] = store.add_revision("run", "scorer_version", {"version": "simrisk-v1.0.0"})
        revisions["audit"] = store.add_revision("run", "audit", {"version": 1}, dependencies=(revisions["corpus"].revision_id, revisions["feature_map"].revision_id, revisions["scorer_version"].revision_id))
        revisions["decision"] = store.add_revision("run", "decision", {"version": 1}, dependencies=(revisions["audit"].revision_id,))
        revisions["draft"] = store.add_revision("run", "draft", {"version": 1}, dependencies=(revisions["decision"].revision_id,))
        revisions["review"] = store.add_revision("run", "review", {"version": 1}, dependencies=(revisions["draft"].revision_id,))
        revisions["validation"] = store.add_revision("run", "validation", {"version": 1}, dependencies=(revisions["review"].revision_id,))
        return connection, store, revisions

    def test_each_required_upstream_change_stales_all_dependent_gates(self):
        mutation_dependencies = {
            "profile": (),
            "evidence": ("query",),
            "finalist": ("candidate",),
            "corpus": ("evidence", "finalist"),
            "feature_map": ("corpus", "finalist"),
            "scorer_version": (),
        }
        required_stale = ("decision", "draft", "review", "validation")
        for kind, dependency_kinds in mutation_dependencies.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                connection, store, revisions = self._graph(temporary)
                store.add_revision(
                    "run",
                    kind,
                    {"version": 2},
                    dependencies=tuple(revisions[name].revision_id for name in dependency_kinds),
                )
                stale = dict(connection.execute("SELECT revision_id,stale FROM artifact_revisions"))
                self.assertTrue(all(stale[revisions[name].revision_id] for name in required_stale))
                pointers = dict(connection.execute("SELECT kind,revision_id FROM current_artifacts WHERE run_id='run'"))
                self.assertTrue(all(name not in pointers for name in required_stale))
                connection.close()


if __name__ == "__main__":
    unittest.main()
