import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from patent_factory import cli
from patent_factory.config import load_evaluation_config
from patent_factory.database import connect_database
from patent_factory.evaluation import EvaluationAxis, run_shortlist
from patent_factory.ideation import run_ideation
from patent_factory.report import validate_report_input
from patent_factory.scaffold import (
    ScaffoldError,
    TODO,
    count_todos,
    scaffold_audit_query_input,
    scaffold_candidate_input,
    scaffold_report_input,
    scaffold_shortlist_input,
)
from tests.integration.test_g004_ideation_and_shortlist import ready_profile, ready_research

ROOT = Path(__file__).resolve().parents[2]


# Axis scores per finalist. Deliberately spread WELL beyond lint.SCORE_EPSILON
# (2) both within a finalist and across finalists: identical or near-identical
# vectors legitimately trip `flat_axis_scores` / `near_identical_finalists`, and
# a golden that permanently renders those advisories teaches readers to ignore
# them. Distinct numbers also keep the comparison surface honest — the committed
# golden used to show 0/0/0 for every finalist.
_AXIS_SCORES = ((81, 74, 68), (63, 55, 71), (49, 88, 57))


def filled(value, date="2026-07-19", scores=None):
    """Replace every TODO(agent) marker with plausible agent-authored prose."""

    if isinstance(value, str):
        if value.startswith(TODO):
            if "YYYY-MM-DD" in value:
                return date
            if "integer 0-100" in value:
                # Unreachable for a shortlist (handled per-axis below); this keeps
                # a stray numeric placeholder from silently becoming prose.
                return 50
            return "agent-completed " + value[len(TODO):][:48]
        return value
    if isinstance(value, dict):
        filled_dict = {key: filled(item, date, scores) for key, item in value.items()}
        if scores is not None and "axes" in filled_dict and isinstance(filled_dict["axes"], list):
            for axis_index, axis in enumerate(filled_dict["axes"]):
                if isinstance(axis, dict) and "score" in axis:
                    axis["score"] = scores[axis_index % len(scores)]
        return filled_dict
    if isinstance(value, list):
        return [filled(item, date, scores) for item in value]
    return value


def filled_shortlist(draft, date="2026-07-19"):
    """Fill a shortlist scaffold, giving each finalist a distinct score vector."""

    result = filled(draft, date)
    for index, finalist in enumerate(result.get("finalists", [])):
        for axis_index, axis in enumerate(finalist.get("axes", [])):
            axis["score"] = _AXIS_SCORES[index % len(_AXIS_SCORES)][axis_index % 3]
    return result


class ScaffoldRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.profile_connection, self.profile = ready_profile(self.root / "profile.sqlite3")
        ready_research(self.connection, self.root)

    def tearDown(self):
        self.connection.close()
        self.profile_connection.close()
        self.temporary.cleanup()

    def test_all_four_scaffolds_round_trip_through_the_real_verbs(self):
        candidate_draft = scaffold_candidate_input(
            self.connection, self.profile_connection, run_id="run",
        )
        self.assertEqual(len(candidate_draft["candidates"]), 3)
        self.assertGreater(count_todos(candidate_draft), 0)
        for candidate in candidate_draft["candidates"]:
            for reference in candidate["evidence_references"]:
                self.assertEqual(reference["evidence_id"], "ev_fixture")
                self.assertTrue(reference["span_hash"])
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.root,
            run_id="run", profile=self.profile, candidate_input=filled(candidate_draft),
            config=load_evaluation_config(),
        )
        self.assertEqual(ideation.next_state, "candidates_ready")

        shortlist_draft = scaffold_shortlist_input(
            self.connection, run_id="run", config=load_evaluation_config(),
        )
        self.assertEqual(len(shortlist_draft["finalists"]), 3)
        shortlisted = run_shortlist(
            self.connection, run_root=self.root, run_id="run",
            shortlist_input=filled_shortlist(shortlist_draft), config=load_evaluation_config(),
        )
        self.assertEqual(shortlisted.next_state, "finalists_ready")

        audit_draft = scaffold_audit_query_input(self.connection, run_id="run")
        current = self.connection.execute(
            "SELECT ar.content_hash FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='finalist_set'",
        ).fetchone()[0]
        self.assertEqual(audit_draft["finalist_set_hash"], current)
        self.assertEqual(len(audit_draft["groups"]), 3)
        for group in audit_draft["groups"]:
            self.assertEqual(
                {query["language"] for query in group["queries"]}, {"ko", "en"},
            )

        report_draft = scaffold_report_input(self.profile_connection, language="en")
        request = validate_report_input(filled(report_draft))
        self.assertEqual(request["language"], "en")
        self.assertIn("expertise", request["profile_fields"])

    def test_scaffold_without_upstream_state_raises_actionable_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            bare = connect_database(Path(directory) / "factory.sqlite3")
            try:
                from patent_factory.state import StateStore

                StateStore(bare).create_run("empty")
                with self.assertRaises(Exception):
                    scaffold_candidate_input(bare, self.profile_connection, run_id="empty")
                with self.assertRaises(Exception):
                    scaffold_shortlist_input(bare, run_id="empty", config=load_evaluation_config())
                with self.assertRaises(Exception):
                    scaffold_audit_query_input(bare, run_id="empty")
            finally:
                bare.close()
        with self.assertRaises(ScaffoldError):
            scaffold_candidate_input(self.connection, self.profile_connection, run_id="run", count=0)
        with self.assertRaises(ScaffoldError):
            scaffold_report_input(self.profile_connection, language="de")


class ScaffoldCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.workspace = Path(self.temporary.name)
        self.workspace_rel = self.workspace.relative_to(ROOT)
        self.run_root = self.workspace / "run"
        self.run_root.mkdir(mode=0o700)
        connection = connect_database(self.run_root / "factory.sqlite3")
        try:
            ready_research(connection, self.run_root)
        finally:
            connection.close()
        profile_connection, _profile = ready_profile(self.workspace / "profile.sqlite3")
        profile_connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main([str(item) for item in argv])
        return json.loads(stream.getvalue()), code

    def test_cli_scaffold_candidate_writes_draft_and_binding_table(self):
        payload, code = self.invoke(
            "scaffold", "candidate", "--run", self.run_root.relative_to(ROOT),
            "--run-id", "run", "--out", self.workspace_rel / "requests" / "candidate-input-v1.draft.json",
            "--profile-database", self.workspace_rel / "profile.sqlite3",
            "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "scaffolded")
        self.assertGreater(payload["todo_count"], 0)
        self.assertEqual(payload["evidence"][0]["evidence_id"], "ev_fixture")
        draft_path = self.workspace / "requests" / "candidate-input-v1.draft.json"
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertEqual(draft["schema_version"], "candidate-input-v1")
        self.assertEqual(len(draft["candidates"]), 3)


if __name__ == "__main__":
    unittest.main()


class UnfilledScaffoldFailsLoudlyTests(unittest.TestCase):
    """A scaffold placeholder must be impossible to leave in silently.

    `scaffold shortlist` used to emit a bare `"score": 0`. Fill helpers rewrite
    only `TODO(agent):` STRINGS, so the numeric placeholder passed through
    untouched and `evaluation.py` accepted 0 as a real score. That is how the
    committed golden came to render a `| 0 | 0 | 0 |` comparison matrix — a
    published artifact whose headline table was pure placeholder.
    """

    def test_axis_score_placeholder_is_a_rejectable_sentinel_not_a_number(self):
        config = load_evaluation_config()
        draft = {
            "axes": [{
                "axis": "differentiation", "confidence": "medium",
                "contrary_evidence_references": [], "coverage_assessment": "covered",
                "coverage_limitations": [], "gaps": [],
                "rationale": "why", "rubric_version": config.rubrics["differentiation"],
                "score": TODO + "integer 0-100 for this axis",
                "supporting_evidence_references": [],
            }],
        }
        with self.assertRaisesRegex(ValueError, "unfilled scaffold placeholder"):
            EvaluationAxis.from_dict(
                draft["axes"][0], "finalists[0].axes[0]",
                expected_axis="differentiation",
                expected_rubric=config.rubrics["differentiation"],
                evidence={}, candidate_evidence_ids=set(),
            )

    def test_the_scaffold_really_emits_the_sentinel(self):
        # Guards the pairing: if the scaffold reverts to a bare 0, the sentinel
        # check above becomes dead code and the 0/0/0 matrix can return.
        source = (ROOT / "src" / "patent_factory" / "scaffold.py").read_text(encoding="utf-8")
        self.assertIn('"score": TODO + "integer 0-100 for this axis"', source)
        self.assertNotIn('"score": 0,', source)
