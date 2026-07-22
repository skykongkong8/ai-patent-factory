"""Integration tests for the unified `post_audit_checkpoint` gate.

Covers Steps 2-7 and Step 10 of `.omc/plans/ralplan-hitl-checkpoint.md`: the
always-raised checkpoint gate (AC-1), its four resolution branches — approve
(clean and breaching, AC-2), re_ideate (AC-3), re_research (AC-4), stop — the
`gate-decision-input-v2` schema and its core sentinel enforcement (AC-9,
RF#5), the report-binding dispatch fix (RF#1), and the exit-8 breaking change
(D6). Drives the real core pipeline (ideation -> shortlist -> audit
retrieval/scoring -> gate inspect/decide -> draft), never fabricated state.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.adapters.manual_web import ManualWebAdapter
from patent_factory.audit import feature_map_id, run_audit_retrieval, run_audit_scoring
from patent_factory.config import load_evaluation_config, load_similarity_config
from patent_factory.database import connect_database
from patent_factory.decisions import inspect_gate, resolve_gate
from patent_factory.evaluation import run_shortlist
from patent_factory.ideation import run_ideation
from patent_factory.models import QueryEnvelope, RunState
from patent_factory.provenance import digest
from patent_factory.report import publish_report
from patent_factory.research import run_research
from patent_factory.scaffold import count_todos, gate_decision_dossier, scaffold_gate_decision_input
from patent_factory.state import StateError, StateStore
from tests.integration.test_g004_ideation_and_shortlist import (
    candidate, candidate_input, ready_profile, shortlist_input,
)
from tests.integration.test_g005_audit import kipris_xml
from tests.integration.test_g009_scaffolds import filled, filled_shortlist
from tests.unit.test_g005_similarity import feature_map

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "justin"
RETRIEVED_AT = "2026-07-19T00:00:00Z"


def _ready_research(connection, run_root: Path, run_id: str = "run"):
    """Like test_g004_ideation_and_shortlist.ready_research, but citation-shaped
    AND routed through the real `ManualWebAdapter` pipeline.

    That fixture hardcodes ``evidence_id="ev_fixture"`` directly into the
    research_bundle JSON (never through `ResearchStore`), which trips two
    problems here: (1) "ev_fixture" fails report.CITATION_RE
    (``ev_[0-9a-f]{16}``) the moment a candidate cites it — silently, deep
    inside `publish_report`; (2) since it never lands in the `evidence_records`
    table, any LATER research op (the re_research second pass) rebuilds the
    bundle from that table via `ResearchStore.manifest()` and drops it,
    breaking re-ideation with "unknown evidence revision". Running the first
    pass through the same `run_research`/`ManualWebAdapter` path the second
    pass uses avoids both.
    """
    exports = run_root / "research-exports"
    exports.mkdir(mode=0o700)
    store = StateStore(connection, export_directories=(exports,))
    store.create_run(run_id)
    store.transition(run_id, RunState.PROFILE_PENDING, actor="test", reason="start",
                      operation="prepare.profile", idempotency_key="1")
    store.transition(run_id, RunState.PROFILE_READY, actor="test", reason="ready",
                      operation="prepare.profile-ready", idempotency_key="1")
    store.transition(run_id, RunState.RESEARCH_READY, actor="test", reason="research",
                      operation="prepare.research-ready", idempotency_key="1")
    span = digest("bounded public excerpt")
    record = {
        "canonical_url": "https://example.test/public/1",
        "content_hash": digest("bounded public record"),
        "excerpt_hashes": [span],
        "identifier": "public-1",
        "language": "ko",
        "limitations": ["redacted fixture"],
        "provenance": "user_import",
        "title": "공개 기술 자료",
    }
    envelope = QueryEnvelope(
        run_id=run_id, adapter="manual_web", adapter_version="import-v1", capability="import",
        allowed_scheme="https", allowed_host="example.test", deadline_seconds=1,
        page=1, page_cap=1, result_budget=10, byte_budget=10_000, retry_budget=0,
        retry_ownership="research_runner",
        query_projection={"content_type": "application/json", "records": [record]},
    )
    result = run_research(
        connection, run_root=run_root, run_id=run_id,
        adapter=ManualWebAdapter(("example.test",)), query=envelope,
        idempotency_key="checkpoint-fixture-pass1", retrieved_at="2026-07-13T00:00:00Z",
    )
    evidence = {"content_hash": record["content_hash"], "evidence_id": result.execution.evidence_ids[0]}
    return evidence, span, result.artifact_revision_id


class CheckpointFixture(unittest.TestCase):
    """FINALISTS_READY, reached through the real core (no fabricated state)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        self.connection = connect_database(self.run_root / "factory.sqlite3")
        self.profile_connection, self.profile = ready_profile(self.run_root / "profile.sqlite3")
        self.evidence, self.span, _research = _ready_research(self.connection, self.run_root)
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.run_root,
            run_id="run", profile=self.profile, candidate_input=candidate_input(3, self.evidence, self.span),
            config=load_evaluation_config(),
        )
        run_shortlist(
            self.connection, run_root=self.run_root, run_id="run",
            shortlist_input=shortlist_input(ideation.candidate_ids, self.evidence, self.span),
            config=load_evaluation_config(),
        )

    def tearDown(self):
        self.connection.close()
        self.profile_connection.close()
        self.temporary.cleanup()

    def _finalists(self):
        row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='finalist_set'"
        ).fetchone()
        return row, json.loads(row["content_json"])["finalists"]

    def _current(self, kind):
        return self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind=?", (kind,),
        ).fetchone()

    def _run_audit(self, statuses):
        """Retrieve + score one audit pass.

        ``statuses`` is one similarity status per finalist (index-aligned
        with the sorted finalist_id order, which is how both
        `run_audit_retrieval`'s query groups and its resulting `corpus_set`
        are ordered): 'matched' breaches (-> decision_required), 'different'
        stays clean (-> audit_approved, still checkpoint-gated post-D6).
        """
        finalist_row, finalists = self._finalists()

        def factory(query, page, finalist_id):
            del query, page
            number = "10-2026-" + str(int(finalist_id[-4:], 16) % 10_000_000).zfill(7)
            body = kipris_xml(number)
            return KiprisAdapter(
                "fixture", credential_required=False,
                transport=lambda url, timeout, byte_budget: TransportResponse(200, {}, body),
            )

        query_input = {
            "schema_version": "audit-query-input-v1", "finalist_set_hash": finalist_row["content_hash"],
            "groups": [{
                "finalist_id": finalist["finalist_id"],
                "queries": [{"language": "ko", "term": "동일 검색어"}, {"language": "en", "term": "same query"}],
            } for finalist in finalists],
        }
        run_audit_retrieval(
            self.connection, run_root=self.run_root, run_id="run", query_input=query_input,
            config=load_similarity_config(), adapter_factory=factory,
        )
        corpus_row = self._current("corpus_set")
        corpus_set = json.loads(corpus_row["content_json"])
        candidate_row = self._current("candidate_set")
        candidates = {item["candidate_id"]: item for item in json.loads(candidate_row["content_json"])["candidates"]}
        finalist_by_id = {item["finalist_id"]: item for item in finalists}
        fields = {
            "problem": "technical_problem", "inputs": "required_inputs", "mechanism": "mechanism",
            "transformations": "transformations", "outputs": "outputs", "technical_effects": "expected_effects",
        }
        status_by_finalist = dict(zip(sorted(finalist_by_id), statuses))
        maps = []
        for corpus in corpus_set["corpora"]:
            record = corpus["records"][0]
            mapping = feature_map(record["evidence_id"], status=status_by_finalist[corpus["finalist_id"]])
            candidate_item = candidates[finalist_by_id[corpus["finalist_id"]]["candidate_id"]]
            for feature in mapping["features"]:
                field = fields[feature["category"]]
                raw = candidate_item[field]
                value = raw[0] if isinstance(raw, list) else raw
                feature["candidate_span_hashes"] = [digest({"field": field, "text": value})]
            span = record["record"]["field_span_hashes"]["abstract"]
            for decision in mapping["reference_maps"][0]["decisions"]:
                decision["reference_span_hashes"] = [span]
            maps.append({
                "feature_map": mapping, "finalist_id": corpus["finalist_id"],
                "map_id": feature_map_id(corpus["finalist_id"], mapping),
            })
        return run_audit_scoring(
            self.connection, run_root=self.run_root, run_id="run",
            feature_input={
                "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                "corpus_set_hash": corpus_row["content_hash"], "maps": maps,
            }, config=load_similarity_config(),
        )

    def _pending_gate_id(self):
        rows = self.connection.execute(
            "SELECT gate_id FROM gate_envelopes WHERE run_id='run' AND status='pending'"
        ).fetchall()
        self.assertEqual(len(rows), 1, "exactly one pending gate expected")
        return rows[0]["gate_id"]

    def _decide_input(self, gate_id, *, action, decisions=None, feedback=None, plan=None, reason="checkpoint decision"):
        envelope = inspect_gate(self.connection, "run", gate_id)
        if feedback is None:
            _row, finalists = self._finalists()
            feedback = [
                {
                    "boring": f"{item['finalist_id']} felt like a narrow variant",
                    "finalist_id": item["finalist_id"],
                    "interesting": f"{item['finalist_id']} mechanism is worth pursuing further",
                }
                for item in finalists
            ]
        return {
            "action": action, "actor": "inventor", "approval_scope": envelope["approval_scope"],
            "decisions": decisions or [], "feedback": feedback, "gate_id": gate_id,
            "plan": plan or {}, "reason": reason, "schema_version": "gate-decision-input-v2",
            "subject_revision_hash": envelope["subject_revision_hash"],
        }

    def _draft_input(self):
        return {
            "drafter": {"id": "drafter", "pass_id": "draft-pass", "type": "agent"},
            "handoff_questions": ["Does the retained scope need attorney review?"],
            "language": "en",
            "profile_fields": ["expertise", "project_summary", "technical_domain"],
            "recommended_investigations": ["Confirm additional embodiments"],
            "report_date": "2026-07-21",
            "revision": None, "schema_version": "report-input-v2",
            "sensitive_disclosures": [],
        }


class CheckpointCleanApproveTests(CheckpointFixture):
    def test_clean_audit_raises_unified_checkpoint_gate_for_all_three_finalists(self):
        scored = self._run_audit(["different"] * 3)
        self.assertEqual(scored.state, RunState.DECISION_REQUIRED.value)
        self.assertIsNotNone(scored.gate_id)
        gate_id = self._pending_gate_id()
        self.assertEqual(gate_id, scored.gate_id)
        envelope = inspect_gate(self.connection, "run", gate_id)
        self.assertEqual(envelope["kind"], "post_audit_checkpoint")
        self.assertEqual(envelope["actions"], sorted({"approve", "re_ideate", "re_research", "stop"}))
        self.assertEqual(envelope["approval_scope"]["affected_finalist_ids"], [])
        _row, finalists = self._finalists()
        self.assertEqual(
            sorted(item["finalist_id"] for item in envelope["approval_scope"]["finalist_bindings"]),
            sorted(item["finalist_id"] for item in finalists),
        )

    def test_clean_approve_reaches_audit_approved_consumes_decision_and_binds_report(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        request = self._decide_input(gate_id, action="approve")
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        self.assertEqual(resolved.next_state, RunState.AUDIT_APPROVED.value)
        # RF#2 / consumed-decision assertion: approve is non-authorizing, so
        # publish_gate_resolution stamps used_at/consumed_by_event_id at
        # resolution time — never left dangling for `draft` to choke on.
        row = self.connection.execute(
            "SELECT used_at, consumed_by_event_id FROM gate_decisions WHERE decision_id=?", (resolved.decision_id,),
        ).fetchone()
        self.assertIsNotNone(row["used_at"])
        self.assertIsNotNone(row["consumed_by_event_id"])
        # AC-5 determinism: replaying the identical decision_input reproduces
        # the exact same persisted artifact, byte for byte.
        replay = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.artifact_revision_id, resolved.artifact_revision_id)
        report = publish_report(self.connection, run_root=self.run_root, run_id="run", report_input=self._draft_input())
        self.assertEqual(report.next_state, RunState.DRAFT_READY.value)
        self.assertIn("checkpoint_gate_resolution", report.artifact.content["bindings"])
        self.assertNotIn("excessive_gate_resolution", report.artifact.content["bindings"])
        content = report.artifact.content
        self.assertIn("Checkpoint decision: approve", content["sections"][8]["body"])


class CheckpointBreachingApproveTests(CheckpointFixture):
    def test_breaching_approve_renders_report_without_raising(self):
        # Catches RF#1: the unconditional _excessive_decision call used to
        # raise StateError here because no excessive_similarity resolution
        # exists for a checkpoint-breaching run.
        self._run_audit(["matched", "different", "different"])
        gate_id = self._pending_gate_id()
        envelope = inspect_gate(self.connection, "run", gate_id)
        affected = envelope["approval_scope"]["affected_finalist_ids"]
        self.assertEqual(len(affected), 1)
        decisions = [
            {"action": "retain_with_warning", "finalist_id": finalist_id, "reason": "retained despite the flagged similarity"}
            for finalist_id in affected
        ]
        request = self._decide_input(gate_id, action="approve", decisions=decisions)
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        self.assertEqual(resolved.next_state, RunState.AUDIT_APPROVED.value)
        row = self.connection.execute(
            "SELECT used_at, consumed_by_event_id FROM gate_decisions WHERE decision_id=?", (resolved.decision_id,),
        ).fetchone()
        self.assertIsNotNone(row["used_at"])
        self.assertIsNotNone(row["consumed_by_event_id"])
        report = publish_report(self.connection, run_root=self.run_root, run_id="run", report_input=self._draft_input())
        self.assertIn("checkpoint_gate_resolution", report.artifact.content["bindings"])
        self.assertNotIn("excessive_gate_resolution", report.artifact.content["bindings"])
        body = report.artifact.content["sections"][8]["body"]
        self.assertIn(affected[0], body)
        self.assertIn("retain_with_warning", body)
        self.assertIn("Retained despite excessive provisional similarity risk", body)

    def test_approve_rejects_a_refine_entry_and_a_partial_breach_set(self):
        self._run_audit(["matched", "different", "different"])
        gate_id = self._pending_gate_id()
        envelope = inspect_gate(self.connection, "run", gate_id)
        affected = envelope["approval_scope"]["affected_finalist_ids"]
        wrong_action = self._decide_input(
            gate_id, action="approve",
            decisions=[{"action": "refine", "finalist_id": affected[0], "reason": "should be rejected"}],
        )
        with self.assertRaises(ValueError):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=wrong_action)
        empty = self._decide_input(gate_id, action="approve", decisions=[])
        with self.assertRaises(ValueError):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=empty)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)


class CheckpointMixedBatchApproveTests(CheckpointFixture):
    """Review finding #1: a coverage_insufficient rider inside a
    decision_required batch must reject `approve`, not strand the run."""

    def test_approve_rejects_a_batch_carrying_a_coverage_insufficient_finalist(self):
        # decision_required takes gate-routing precedence over
        # coverage_insufficient (audit.py), so "unavailable" features on the
        # third finalist produce a coverage_insufficient result that rides
        # along in the same POST_AUDIT_CHECKPOINT batch as the "matched"
        # breach — exactly the shape review finding #1 reproduced.
        self._run_audit(["matched", "different", "unavailable"])
        gate_id = self._pending_gate_id()
        envelope = inspect_gate(self.connection, "run", gate_id)
        affected = envelope["approval_scope"]["affected_finalist_ids"]
        self.assertEqual(len(affected), 1)
        decisions = [
            {"action": "retain_with_warning", "finalist_id": finalist_id, "reason": "retained despite the flagged similarity"}
            for finalist_id in affected
        ]
        request = self._decide_input(gate_id, action="approve", decisions=decisions)
        with self.assertRaisesRegex(ValueError, "coverage_insufficient"):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        # The run must still be decidable afterward (re_ideate stays open).
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)
        reideate = self._decide_input(gate_id, action="re_ideate")
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=reideate)
        self.assertEqual(resolved.next_state, RunState.IDEATION_RUNNING.value)


class CheckpointLegalLanguageScreenTests(CheckpointFixture):
    """Review finding #2: checkpoint prose can never be re-authored once
    persisted, so the legal-language screen must run at `gate decide`."""

    def test_offending_reason_is_rejected_at_decide_and_gate_stays_pending(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        offending = self._decide_input(gate_id, action="approve", reason="이 기술은 특허 가능하다")
        with self.assertRaisesRegex(ValueError, "validation.legal_language"):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=offending)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)
        envelope = inspect_gate(self.connection, "run", gate_id)
        self.assertEqual(envelope["status"], "pending")
        corrected = self._decide_input(gate_id, action="approve", reason="clean audit reviewed; approving for draft")
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=corrected)
        self.assertEqual(resolved.next_state, RunState.AUDIT_APPROVED.value)

    def test_offending_feedback_interesting_is_rejected_at_decide(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        _row, finalists = self._finalists()
        feedback = [
            {
                "boring": "nothing stood out",
                "finalist_id": item["finalist_id"],
                "interesting": "특허 가능하다" if index == 0 else "worth pursuing further",
            }
            for index, item in enumerate(finalists)
        ]
        offending = self._decide_input(gate_id, action="approve", feedback=feedback)
        with self.assertRaisesRegex(ValueError, "validation.legal_language"):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=offending)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)


class CheckpointFinalistBindingsLockstepTests(CheckpointFixture):
    """Review finding #6: `finalist_bindings` must carry `coverage` and
    `upper_bound_reference_id`, lockstep across the two independent
    derivation sites (audit.py scope builder, decisions.py staleness
    mirror), and the checkpoint dossier must surface both — especially for
    a coverage_insufficient finalist, where a null `closest_reference_id`
    is not "nothing found"."""

    def test_finalist_bindings_carries_coverage_and_upper_bound_reference_id(self):
        scored = self._run_audit(["matched", "different", "unavailable"])
        audit_row = self._current("audit_batch")
        audit = json.loads(audit_row["content_json"])
        results_by_id = {item["finalist_id"]: item for item in audit["results"]}
        gate_id = self._pending_gate_id()
        self.assertEqual(gate_id, scored.gate_id)
        envelope = inspect_gate(self.connection, "run", gate_id)
        bindings = envelope["approval_scope"]["finalist_bindings"]
        self.assertEqual(len(bindings), 3)
        for binding in bindings:
            expected = results_by_id[binding["finalist_id"]]
            self.assertEqual(binding["coverage"], expected["coverage"])
            self.assertEqual(binding["upper_bound_reference_id"], expected["upper_bound_reference_id"])
        insufficient = next(item for item in results_by_id.values() if item["outcome"] == "coverage_insufficient")
        self.assertIsNotNone(insufficient["upper_bound_reference_id"])

    def test_lockstep_walk_succeeds_with_zero_gate_mismatch_on_clean_and_breaching_audits(self):
        # Both approve walks must complete with no GateMismatchError — the
        # staleness mirror at decisions.py must byte-match the scope
        # builder at audit.py, or every gate decide would brick.
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        clean = self._decide_input(gate_id, action="approve")
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=clean)
        self.assertEqual(resolved.next_state, RunState.AUDIT_APPROVED.value)

    def test_gate_decision_dossier_surfaces_coverage_and_upper_bound_for_every_finalist(self):
        self._run_audit(["matched", "different", "unavailable"])
        gate_id = self._pending_gate_id()
        draft = scaffold_gate_decision_input(self.connection, run_id="run", gate_id=gate_id)
        dossier = gate_decision_dossier(draft["approval_scope"])
        self.assertEqual(len(dossier), 3)
        by_outcome = {item["outcome"]: item for item in dossier}
        self.assertIn("coverage_insufficient", by_outcome)
        self.assertIn("decision_required", by_outcome)
        insufficient = by_outcome["coverage_insufficient"]
        self.assertIsNotNone(insufficient["coverage"])
        self.assertIsNotNone(insufficient["upper_bound_reference_id"])
        # The dossier is CLI-response-only: it must never leak into the
        # decision-input draft file itself (exact top-level key set).
        self.assertNotIn("dossier", draft)


class CheckpointReIdeateTests(CheckpointFixture):
    def test_re_ideate_completes_and_stales_old_artifacts_with_a_new_content_hash(self):
        self._run_audit(["matched", "different", "different"])
        gate_id = self._pending_gate_id()
        stale_candidate = self._current("candidate_set")
        stale_finalist = self._current("finalist_set")
        stale_audit = self._current("audit_batch")
        request = self._decide_input(gate_id, action="re_ideate")
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        self.assertEqual(resolved.next_state, RunState.IDEATION_RUNNING.value)
        # Vary title/mechanism only — a re-authored candidate that changes
        # `domain` trips DomainPivotRequiredError instead of completing.
        reauthored = {
            "schema_version": "candidate-input-v1",
            "candidates": [candidate(f"후보 {index}-refined", self.evidence, self.span) for index in range(3)],
        }
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.run_root,
            run_id="run", profile=self.profile, candidate_input=reauthored, config=load_evaluation_config(),
        )
        self.assertEqual(ideation.next_state, RunState.CANDIDATES_READY.value)
        fresh_candidate = self._current("candidate_set")
        self.assertNotEqual(fresh_candidate["content_hash"], stale_candidate["content_hash"])
        for row in (stale_candidate, stale_finalist, stale_audit):
            current_stale = self.connection.execute(
                "SELECT stale FROM artifact_revisions WHERE revision_id=?", (row["revision_id"],),
            ).fetchone()[0]
            self.assertEqual(current_stale, 1)

    def test_byte_identical_reideate_replay_raises_state_error_not_false_success(self):
        # Review finding #3: after `re_ideate` the run sits at
        # ideation_running. Resubmitting the BYTE-IDENTICAL candidate_input
        # (same as the original, pre-checkpoint ideate call) hits the
        # idempotency replay for both `ideation.start` and `ideation.publish`
        # — but the run's actual current state is still ideation_running, so
        # a hardcoded "candidates_ready" status would misreport success over
        # a stalled run.
        self._run_audit(["matched", "different", "different"])
        gate_id = self._pending_gate_id()
        request = self._decide_input(gate_id, action="re_ideate")
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        self.assertEqual(resolved.next_state, RunState.IDEATION_RUNNING.value)
        with self.assertRaises(StateError):
            run_ideation(
                self.connection, profile_connection=self.profile_connection, run_root=self.run_root,
                run_id="run", profile=self.profile, candidate_input=candidate_input(3, self.evidence, self.span),
                config=load_evaluation_config(),
            )

    def test_re_ideate_rejects_any_non_empty_decisions(self):
        self._run_audit(["matched", "different", "different"])
        gate_id = self._pending_gate_id()
        envelope = inspect_gate(self.connection, "run", gate_id)
        affected = envelope["approval_scope"]["affected_finalist_ids"]
        request = self._decide_input(
            gate_id, action="re_ideate",
            decisions=[{"action": "retain_with_warning", "finalist_id": affected[0], "reason": "should be rejected"}],
        )
        with self.assertRaises(ValueError):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)


class CheckpointReResearchTests(CheckpointFixture):
    def test_re_research_offline_second_pass_raises_a_fresh_single_checkpoint_gate(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        stale_research = self._current("research_bundle")
        request = self._decide_input(
            gate_id, action="re_research",
            plan={"needed_research": ["broader prior-art sweep for the sensor mechanism"]},
        )
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        self.assertEqual(resolved.next_state, RunState.RESEARCH_RUNNING.value)

        # Offline second pass (manual import path — no networked verb).
        record = {
            "canonical_url": "https://example.test/second-pass",
            "identifier": "second-pass-1",
            "title": "Second-pass reference",
            "content_hash": digest("second pass unique content"),
            "language": "en",
            "provenance": "reviewed_import",
        }
        envelope = QueryEnvelope(
            run_id="run", adapter="manual_web", adapter_version="import-v1", capability="import",
            allowed_scheme="https", allowed_host="example.test", deadline_seconds=1,
            page=1, page_cap=1, result_budget=10, byte_budget=10_000, retry_budget=0,
            retry_ownership="research_runner",
            query_projection={"content_type": "application/json", "records": [record]},
        )
        second_pass = run_research(
            self.connection, run_root=self.run_root, run_id="run",
            adapter=ManualWebAdapter(("example.test",)), query=envelope,
            idempotency_key="checkpoint-re-research-pass2", retrieved_at=RETRIEVED_AT,
        )
        self.assertEqual(second_pass.next_state, RunState.RESEARCH_COMPLETE.value)
        fresh_research = self._current("research_bundle")
        self.assertNotEqual(fresh_research["content_hash"], stale_research["content_hash"])

        reauthored = {
            "schema_version": "candidate-input-v1",
            "candidates": [candidate(f"후보 {index}-rescoped", self.evidence, self.span) for index in range(3)],
        }
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.run_root,
            run_id="run", profile=self.profile, candidate_input=reauthored, config=load_evaluation_config(),
        )
        run_shortlist(
            self.connection, run_root=self.run_root, run_id="run",
            shortlist_input=shortlist_input(ideation.candidate_ids, self.evidence, self.span),
            config=load_evaluation_config(),
        )
        scored = self._run_audit(["different"] * 3)
        self.assertEqual(scored.state, RunState.DECISION_REQUIRED.value)
        self.assertNotEqual(scored.gate_id, gate_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM gate_envelopes WHERE run_id='run' AND status='pending'"
            ).fetchone()[0],
            1,
        )

    def test_re_research_requires_a_bounded_plan(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        request = self._decide_input(gate_id, action="re_research", plan={})
        with self.assertRaises(ValueError):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)


class CheckpointStopTests(CheckpointFixture):
    def test_stop_is_terminal(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        request = self._decide_input(gate_id, action="stop")
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        self.assertEqual(resolved.next_state, RunState.STOPPED.value)


class CheckpointSentinelAndSchemaTests(CheckpointFixture):
    def test_unedited_scaffold_is_rejected_and_a_completed_one_resolves(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        draft = scaffold_gate_decision_input(self.connection, run_id="run", gate_id=gate_id)
        self.assertGreater(count_todos(draft), 0)
        with self.assertRaisesRegex(ValueError, r"TODO\(agent\)"):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=draft)
        completed = dict(draft)
        completed["action"] = "approve"
        completed["actor"] = "inventor"
        completed["reason"] = "clean audit reviewed; approving for draft"
        completed["decisions"] = []
        completed["plan"] = {}
        completed["feedback"] = [
            {"boring": "nothing stood out", "finalist_id": item["finalist_id"], "interesting": "worth pursuing"}
            for item in draft["feedback"]
        ]
        self.assertEqual(count_todos(completed), 0)
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=completed)
        self.assertEqual(resolved.next_state, RunState.AUDIT_APPROVED.value)

    def test_v1_payload_is_rejected_by_a_checkpoint_gate(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        envelope = inspect_gate(self.connection, "run", gate_id)
        v1_payload = {
            "action": "approve", "actor": "inventor", "approval_scope": envelope["approval_scope"],
            "decisions": [], "gate_id": gate_id, "plan": {}, "reason": "approving",
            "schema_version": "gate-decision-input-v1", "subject_revision_hash": envelope["subject_revision_hash"],
        }
        with self.assertRaisesRegex(ValueError, "gate-decision-input-v2"):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=v1_payload)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)

    def test_feedback_must_cover_exactly_the_current_finalists(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        _row, finalists = self._finalists()
        incomplete = self._decide_input(
            gate_id, action="approve",
            feedback=[{
                "boring": "x", "finalist_id": finalists[0]["finalist_id"], "interesting": "y",
            }],
        )
        with self.assertRaises(ValueError):
            resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=incomplete)

    def test_decision_set_schema_valid_and_carries_scrubbed_feedback_and_plan(self):
        self._run_audit(["different"] * 3)
        gate_id = self._pending_gate_id()
        request = self._decide_input(
            gate_id, action="re_research",
            plan={"needed_research": ["confirm the sensor mechanism prior art"]},
        )
        resolved = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=request)
        row = self.connection.execute(
            "SELECT content_json FROM artifact_revisions WHERE revision_id=?", (resolved.artifact_revision_id,),
        ).fetchone()
        content = json.loads(row["content_json"])
        self.assertEqual(len(content["feedback"]), 3)
        self.assertEqual(
            [item["finalist_id"] for item in content["feedback"]], sorted(item["finalist_id"] for item in content["feedback"]),
        )
        self.assertTrue(content["plan"])
        self.assertIn("plan_hash", content)
        if Draft202012Validator is not None:
            schema = json.loads((ROOT / "schemas/decision.schema.json").read_text(encoding="utf-8"))
            Draft202012Validator(schema).validate(content)


def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "patent_factory", *map(str, args)],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )


class CheckpointExitCodeCliTests(unittest.TestCase):
    """CLI-level: a CLEAN `audit score` now exits 8 with a pending checkpoint gate (D6, AC-1)."""

    def setUp(self):
        self.documents_context = tempfile.TemporaryDirectory(dir=ROOT / "documents")
        self.workspace_context = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.documents = Path(self.documents_context.name)
        self.workspace = Path(self.workspace_context.name)

    def tearDown(self):
        self.documents_context.cleanup()
        self.workspace_context.cleanup()

    def rel(self, path: Path) -> Path:
        return path.relative_to(ROOT)

    def step(self, *args: object) -> dict:
        result = run_cli(*args)
        self.assertEqual(result.returncode, 0, f"step {args} failed:\n{result.stdout}\n{result.stderr}")
        return json.loads(result.stdout)

    def fill(self, relative: Path, *, shortlist: bool = False) -> dict:
        path = ROOT / relative
        draft = json.loads(path.read_text(encoding="utf-8"))
        value = filled_shortlist(draft) if shortlist else filled(draft)
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return value

    def current(self, connection, kind: str):
        return connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='justin' AND ca.kind=?",
            (kind,),
        ).fetchone()

    def test_clean_audit_score_exits_8_with_pending_checkpoint_gate(self):
        docs_rel, ws_rel = self.rel(self.documents), self.rel(self.workspace)
        self.step("init", "--documents", docs_rel, "--workspace", ws_rel)
        shutil.copy(EXAMPLES / "background.md", self.documents / "background.md")
        self.step(
            "profile", "document", docs_rel / "background.md",
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        run_root = self.workspace / "run"
        run_rel = self.rel(run_root)
        self.step(
            "run", "start", "--run", run_rel, "--run-id", "justin",
            "--profile", ws_rel / "profile.json", "--profile-database", ws_rel / "profile.sqlite3",
            "--workspace-root", ws_rel,
        )
        shutil.copy(EXAMPLES / "web-rows.json", self.documents / "web-rows.json")
        self.step(
            "research", "normalize-web", docs_rel / "web-rows.json", "--out", docs_rel / "normalized.json",
            "--allow-host", "arxiv.org", "--allow-host", "patents.google.com", "--source-type", "web",
            "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        self.step(
            "research", "manual", docs_rel / "normalized.json", "--run", run_rel, "--run-id", "justin",
            "--query", "on-device inference kv-cache",
            "--allow-host", "arxiv.org", "--allow-host", "patents.google.com",
            "--retrieved-at", RETRIEVED_AT, "--documents-root", docs_rel, "--workspace-root", ws_rel,
        )
        candidate_path = ws_rel / "requests" / "candidate-input-v1.json"
        self.step(
            "scaffold", "candidate", "--run", run_rel, "--run-id", "justin",
            "--out", candidate_path, "--workspace-root", ws_rel,
        )
        self.fill(candidate_path)
        self.step(
            "ideate", "--run", run_rel, "--run-id", "justin", "--profile", ws_rel / "profile.json",
            "--profile-database", ws_rel / "profile.sqlite3", "--input", candidate_path, "--workspace-root", ws_rel,
        )
        shortlist_path = ws_rel / "requests" / "shortlist-input-v1.json"
        self.step(
            "scaffold", "shortlist", "--run", run_rel, "--run-id", "justin",
            "--out", shortlist_path, "--workspace-root", ws_rel,
        )
        self.fill(shortlist_path, shortlist=True)
        self.step(
            "shortlist", "--run", run_rel, "--run-id", "justin",
            "--input", shortlist_path, "--workspace-root", ws_rel,
        )
        query_path = ws_rel / "requests" / "audit-query-input-v1.json"
        self.step(
            "scaffold", "audit-query", "--run", run_rel, "--run-id", "justin",
            "--out", query_path, "--workspace-root", ws_rel,
        )
        query = self.fill(query_path)
        fixture = self.documents / "kipris-fixture.xml"
        fixture.write_bytes(kipris_xml("10-2026-0022222"))
        manifest = {
            "schema_version": "audit-fixture-manifest-v1",
            "responses": [
                {"finalist_id": group["finalist_id"], "page": 1, "source": str(self.rel(fixture)), "term": item["term"]}
                for group in query["groups"] for item in group["queries"]
            ],
        }
        (self.documents / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.step(
            "audit", "retrieve", "--run", run_rel, "--run-id", "justin", "--query-input", query_path,
            "--fixture-manifest", docs_rel / "manifest.json", "--documents-root", docs_rel,
            "--workspace-root", ws_rel, "--retrieved-at", RETRIEVED_AT,
        )
        with connect_database(run_root / "factory.sqlite3") as connection:
            corpus_row = self.current(connection, "corpus_set")
            corpus_set = json.loads(corpus_row["content_json"])
            candidate_row = self.current(connection, "candidate_set")
            candidates = {
                item["candidate_id"]: item for item in json.loads(candidate_row["content_json"])["candidates"]
            }
            finalist_row = self.current(connection, "finalist_set")
            finalists = {
                item["finalist_id"]: item for item in json.loads(finalist_row["content_json"])["finalists"]
            }
        fields = {
            "problem": "technical_problem", "inputs": "required_inputs", "mechanism": "mechanism",
            "transformations": "transformations", "outputs": "outputs", "technical_effects": "expected_effects",
        }
        maps = []
        for corpus in corpus_set["corpora"]:
            record = corpus["records"][0]
            # "different" — a CLEAN audit; the D6 exit-8 contract is the point
            # here, so no result should be decision_required or coverage_insufficient.
            mapping = feature_map(record["evidence_id"], status="different")
            candidate_item = candidates[finalists[corpus["finalist_id"]]["candidate_id"]]
            for feature in mapping["features"]:
                field = fields[feature["category"]]
                raw = candidate_item[field]
                value = raw[0] if isinstance(raw, list) else raw
                feature["candidate_span_hashes"] = [digest({"field": field, "text": value})]
            span = record["record"]["field_span_hashes"]["abstract"]
            for decision in mapping["reference_maps"][0]["decisions"]:
                decision["reference_span_hashes"] = [span]
            maps.append({
                "feature_map": mapping, "finalist_id": corpus["finalist_id"],
                "map_id": feature_map_id(corpus["finalist_id"], mapping),
            })
        feature_path = ws_rel / "requests" / "feature-map-set-input-v1.json"
        (ROOT / feature_path).write_text(json.dumps({
            "schema_version": "feature-map-set-input-v1",
            "finalist_set_hash": finalist_row["content_hash"],
            "corpus_set_hash": corpus_row["content_hash"], "maps": maps,
        }, ensure_ascii=False), encoding="utf-8")
        result = run_cli(
            "audit", "score", "--run", run_rel, "--run-id", "justin",
            "--feature-input", feature_path, "--workspace-root", ws_rel,
        )
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "decision_required")
        with connect_database(run_root / "factory.sqlite3") as connection:
            gate_row = connection.execute(
                "SELECT kind FROM gate_envelopes WHERE run_id='justin' AND status='pending'"
            ).fetchone()
        self.assertEqual(gate_row["kind"], "post_audit_checkpoint")


if __name__ == "__main__":
    unittest.main()
