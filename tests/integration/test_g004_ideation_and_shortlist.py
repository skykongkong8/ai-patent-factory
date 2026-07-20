import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from patent_factory.config import load_evaluation_config
from patent_factory.database import InjectedFailure, connect_database, ingest, profile_payload
from patent_factory.evaluation import run_shortlist
from patent_factory.ideation import DomainPivotRequiredError, run_ideation
from patent_factory.models import RunState
from patent_factory.provenance import Claim, EpistemicLabel, canonical_json, digest
from patent_factory.profile import IncomingFact, interview_facts
from patent_factory.state import GateMismatchError, StateError, StateStore


def ready_research(connection, run_root: Path, run_id: str = "run"):
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
    store.transition(run_id, RunState.RESEARCH_RUNNING, actor="test", reason="running",
                     operation="prepare.research-running", idempotency_key="1")
    span = digest("bounded public excerpt")
    content_hash = digest("bounded public record")
    evidence = {
        "canonical_url": "https://example.test/public/1",
        "content_hash": content_hash,
        "created_at": "2026-07-13T00:00:00Z",
        "evidence_id": "ev_fixture",
        "language": "ko",
        "original_identifier": "public-1",
        "provenance": "user_import",
        "record_json": canonical_json({
            "excerpt_hashes": [span], "limitations": ["redacted fixture"],
            "title": "공개 기술 자료",
        }),
        "run_id": run_id,
        "source_locator": "https://example.test/public/1",
        "source_type": "manual_web",
        "title": "공개 기술 자료",
    }
    research, _ = store.publish_transition(
        run_id, RunState.RESEARCH_COMPLETE, actor="test", reason="fixture research",
        operation="prepare.research-finish", idempotency_key="1",
        artifact_kind="research_bundle",
        artifact_content={
            "adapter_events": [], "coverage_limitations": [], "edges": [],
            "evidence": [evidence], "observations": [], "queries": [],
            "run_id": run_id, "version": "research-bundle-v1",
        },
        artifact_schema_version="research-bundle-v1", export_directory=exports,
    )
    return evidence, span, research.artifact


def profile():
    problem = Claim(EpistemicLabel.USER_STATEMENT, "interview-problem").as_dict()
    capability = Claim(EpistemicLabel.USER_STATEMENT, "interview-capability").as_dict()
    return {
        "conflicts": [],
        "facts": {
            "project_summary": {"claims": [problem], "value": "센서 오차를 줄여야 한다"},
            "expertise": {"claims": [capability], "value": "센서 제어 구현"},
            "technical_domain": {
                "claims": [Claim(EpistemicLabel.USER_STATEMENT, "interview-domain").as_dict()],
                "value": "센서",
            },
        },
        "profile_revision": "pr_fixture",
        "profile_version": "profile-v1",
        "state": "profile_ready",
    }


def ready_profile(path: Path):
    connection = connect_database(path)
    facts = profile()["facts"]
    incoming = [
        IncomingFact(
            field,
            entry["value"],
            Claim(
                EpistemicLabel(entry["claims"][0]["label"]),
                source_id=entry["claims"][0].get("source_id"),
            ),
        )
        for field, entry in facts.items()
    ]
    ingest(connection, "interview", incoming)
    return connection, profile_payload(connection)


def evidence_ref(evidence, span):
    return {
        "content_hash": evidence["content_hash"], "evidence_id": evidence["evidence_id"],
        "limitation": None, "span_hash": span,
    }


def candidate(title, evidence, span, *, domain="센서"):
    facts = profile()["facts"]
    problem_id = facts["project_summary"]["claims"][0]["claim_id"]
    capability_id = facts["expertise"]["claims"][0]["claim_id"]
    creative = Claim(EpistemicLabel.CREATIVE_SUGGESTION).as_dict()
    hypothesis = Claim(EpistemicLabel.HYPOTHESIS).as_dict()
    user = Claim(EpistemicLabel.USER_STATEMENT, "interview-problem").as_dict()
    return {
        "claims": [
            {"field": "technical_problem", "claim": user, "evidence_references": [evidence_ref(evidence, span)]},
            {"field": "mechanism", "claim": creative, "evidence_references": []},
            {"field": "expected_effects", "claim": hypothesis, "evidence_references": []},
            {"field": "synthesis_trace", "claim": creative, "evidence_references": []},
        ],
        "components": ["센서", "보정기"],
        "domain": domain,
        "evidence_references": [evidence_ref(evidence, span)],
        "expected_effects": ["오차 감소"],
        "implementation_example": "센서 출력에 보정기를 연결한다",
        "interactions": ["보정기가 센서 출력을 조정한다"],
        "mechanism": f"{title}의 보정 메커니즘",
        "measurable_validation": "평균 절대 오차를 비교한다",
        "outputs": ["보정 출력"],
        "profile_references": [
            {"claim_id": problem_id, "field": "project_summary", "kind": "problem"},
            {"claim_id": capability_id, "field": "expertise", "kind": "capability"},
        ],
        "required_inputs": ["센서 출력"],
        "synthesis_trace": {
            "evidence_ids": [evidence["evidence_id"]], "method": "adapt",
            "narrative": "공개 메커니즘을 사용자 제약에 맞게 조정한 휴리스틱 기여",
        },
        "technical_problem": "센서 오차",
        "title": title,
        "transformations": ["오차 보정"],
        "unresolved_dependencies": [],
        "unresolved_questions": ["현장 잡음 분포"],
    }


def candidate_input(count, evidence, span, *, domain="센서"):
    return {
        "schema_version": "candidate-input-v1",
        "candidates": [candidate(f"후보 {index}", evidence, span, domain=domain) for index in range(count)],
    }


def axis(name, evidence, span):
    return {
        "axis": name,
        "confidence": "medium",
        "contrary_evidence_references": [],
        "coverage_assessment": "현재 검색된 공개 자료 범위에서만 평가",
        "coverage_limitations": ["최종 KIPRIS 재검색 전의 예비 평가"],
        "gaps": [],
        "rationale": f"{name} 구조적 근거",
        "rubric_version": load_evaluation_config().rubrics[name],
        "score": 60,
        "supporting_evidence_references": [evidence_ref(evidence, span)],
    }


def shortlist_input(candidate_ids, evidence, span, all_candidate_ids=None):
    selected = list(candidate_ids)
    all_ids = list(all_candidate_ids or candidate_ids)
    return {
        "exclusions": [
            {"candidate_id": candidate_id, "rationale": "현재 선택 우선순위 밖", "reason_codes": ["not_selected"]}
            for candidate_id in all_ids if candidate_id not in selected
        ],
        "finalists": [
            {
                "axes": [axis(name, evidence, span) for name in (
                    "differentiation", "technical_feasibility", "utility_significance"
                )],
                "candidate_id": candidate_id,
                "priority": 1,
                "selection_rationale": "세 축의 구조적 근거가 완전하다",
            }
            for candidate_id in selected
        ],
        "insufficiency": None,
        "schema_version": "shortlist-input-v1",
    }


class G004IdeationAndShortlistTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        self.connection = connect_database(self.run_root / "factory.sqlite3")
        self.profile_connection, self.profile = ready_profile(self.run_root / "profile.sqlite3")
        self.evidence, self.span, self.research = ready_research(self.connection, self.run_root)
        self.config = load_evaluation_config()

    def tearDown(self):
        self.connection.close()
        self.profile_connection.close()
        self.temporary.cleanup()

    def ideate(self, count=3, **changes):
        payload = candidate_input(count, self.evidence, self.span, **changes)
        return run_ideation(
            self.connection, profile_connection=self.profile_connection,
            run_root=self.run_root, run_id="run", profile=self.profile,
            candidate_input=payload, config=self.config,
        )

    def write_snapshot(self):
        state = StateStore(self.connection).snapshot("run")
        tables = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "artifact_revisions", "current_artifacts", "transition_events",
                "idempotency_records", "gate_envelopes",
            )
        }
        exports = tuple(sorted(
            path.relative_to(self.run_root).as_posix()
            for path in self.run_root.glob("*-exports/*")
        ))
        return state.state, state.state_version, tuple(sorted(state.current_revisions.items())), tables, exports

    def test_forged_profile_export_is_rejected_before_profile_context_or_any_write(self):
        forged = json.loads(json.dumps(self.profile))
        forged["profile_revision"] = "pr_forged"
        forged["facts"]["technical_domain"]["value"] = "위조 분야"
        before = self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "does not match authoritative profile database"):
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=forged,
                candidate_input=candidate_input(3, self.evidence, self.span), config=self.config,
            )
        self.assertEqual(self.write_snapshot(), before)
        self.assertNotIn("profile_context", StateStore(self.connection).snapshot("run").current_revisions)

    def test_cross_run_or_stale_profile_context_is_rejected_before_any_new_write(self):
        StateStore(self.connection).add_revision(
            "run", "profile_context",
            {
                "profile": {"profile_revision": "pr_other_run"},
                "profile_revision_hash": digest("other"),
                "profile_revision_id": "pr_other_run",
                "version": "profile-context-v1",
            },
            schema_version="profile-context-v1",
        )
        before = self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "bound to a different profile revision"):
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=candidate_input(3, self.evidence, self.span), config=self.config,
            )
        self.assertEqual(self.write_snapshot(), before)

    def test_source_fact_must_match_exact_current_evidence_content_and_span_before_write(self):
        payload = candidate_input(3, self.evidence, self.span)
        payload["candidates"][0]["claims"][1]["claim"] = {
            "content_hash": "forged-content",
            "label": "source_fact",
            "representation": "quote",
            "source_id": self.evidence["evidence_id"],
            "span_hash": "forged-span",
        }
        before = self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "exact evidence revision and span"):
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=payload, config=self.config,
            )
        self.assertEqual(self.write_snapshot(), before)

    def test_credential_canary_is_rejected_across_candidate_evaluation_and_insufficiency_payloads(self):
        secret = "G004-KIPRIS-CREDENTIAL-CANARY"
        candidate_payload = candidate_input(3, self.evidence, self.span)
        candidate_payload["candidates"][0]["title"] = f"허용된 제목 {secret}"
        before = self.write_snapshot()
        with patch.dict("os.environ", {"KIPRIS_PLUS_API_KEY": secret}):
            with self.assertRaisesRegex(ValueError, "candidate_input: canary_detected") as captured:
                run_ideation(
                    self.connection, profile_connection=self.profile_connection,
                    run_root=self.run_root, run_id="run", profile=self.profile,
                    candidate_input=candidate_payload, config=self.config,
                )
        self.assertNotIn(secret, str(captured.exception))
        self.assertEqual(self.write_snapshot(), before)
        self.assertNotIn(secret.encode(), (self.run_root / "factory.sqlite3").read_bytes())

        ideation = self.ideate(3)
        evaluation_payload = shortlist_input(ideation.candidate_ids, self.evidence, self.span)
        evaluation_payload["finalists"][0]["axes"][0]["rationale"] = secret
        before = self.write_snapshot()
        with patch.dict("os.environ", {"KIPRIS_PLUS_API_KEY": secret}):
            with self.assertRaisesRegex(ValueError, "shortlist_input: canary_detected"):
                run_shortlist(
                    self.connection, run_root=self.run_root, run_id="run",
                    shortlist_input=evaluation_payload, config=self.config,
                )
        self.assertEqual(self.write_snapshot(), before)
        self.assertTrue(all(
            secret.encode() not in path.read_bytes()
            for path in self.run_root.glob("*-exports/*")
        ))

        selected = ideation.candidate_ids[:2]
        insufficiency_payload = shortlist_input(
            selected, self.evidence, self.span, all_candidate_ids=ideation.candidate_ids
        )
        rejected = [candidate_id for candidate_id in ideation.candidate_ids if candidate_id not in selected]
        insufficiency_payload["insufficiency"] = {
            "eligible_candidate_ids": list(selected),
            "limitations": [secret],
            "missing_evidence": ["추가 근거"],
            "reason_codes": ["fewer_than_three_defensible"],
            "recommended_research": ["추가 검색"],
            "rejected_candidate_ids": rejected,
            "unresolved_questions": ["추가 질문"],
        }
        with patch.dict("os.environ", {"KIPRIS_PLUS_API_KEY": secret}):
            with self.assertRaisesRegex(ValueError, "shortlist_input: canary_detected"):
                run_shortlist(
                    self.connection, run_root=self.run_root, run_id="run",
                    shortlist_input=insufficiency_payload, config=self.config,
                )
        self.assertEqual(self.write_snapshot(), before)
        self.assertNotIn(secret.encode(), (self.run_root / "factory.sqlite3").read_bytes())
        self.assertTrue(all(
            secret.encode() not in path.read_bytes()
            for path in self.run_root.glob("*-exports/*")
        ))

    def test_authoritative_profile_credential_canary_is_rejected_before_run_write(self):
        secret = "G004-PROFILE-CREDENTIAL-CANARY"
        ingest(
            self.profile_connection,
            "interview",
            [IncomingFact("name", secret, Claim(EpistemicLabel.USER_STATEMENT, "interview-name"))],
        )
        current_profile = profile_payload(self.profile_connection)
        before = self.write_snapshot()
        with patch.dict("os.environ", {"KIPRIS_PLUS_API_KEY": secret}):
            with self.assertRaisesRegex(ValueError, "profile_context: canary_detected") as captured:
                run_ideation(
                    self.connection, profile_connection=self.profile_connection,
                    run_root=self.run_root, run_id="run", profile=current_profile,
                    candidate_input=candidate_input(3, self.evidence, self.span), config=self.config,
                )
        self.assertNotIn(secret, str(captured.exception))
        self.assertEqual(self.write_snapshot(), before)
        self.assertNotIn(secret.encode(), (self.run_root / "factory.sqlite3").read_bytes())
        self.assertTrue(all(
            secret.encode() not in path.read_bytes()
            for path in self.run_root.glob("*-exports/*")
        ))

    def test_profile_problem_and_capability_refs_require_distinct_correctly_typed_claims(self):
        duplicated = candidate_input(3, self.evidence, self.span)
        shared = duplicated["candidates"][0]["profile_references"][0]["claim_id"]
        duplicated["candidates"][0]["profile_references"][1]["claim_id"] = shared
        duplicated["candidates"][0]["profile_references"][1]["field"] = "project_summary"
        before = self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "distinct authoritative fact references"):
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=duplicated, config=self.config,
            )
        self.assertEqual(self.write_snapshot(), before)

        mistyped = candidate_input(3, self.evidence, self.span)
        mistyped["candidates"][0]["profile_references"][0]["kind"] = "capability"
        mistyped["candidates"][0]["profile_references"][1]["kind"] = "problem"
        with self.assertRaisesRegex(ValueError, "reference kind does not match authoritative profile category"):
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=mistyped, config=self.config,
            )
        self.assertEqual(self.write_snapshot(), before)

    def test_actual_interview_profile_allows_field_distinct_facts_with_shared_provenance_claim(self):
        actual_connection = connect_database(self.run_root / "actual-interview.sqlite3")
        try:
            ingest(
                actual_connection,
                "interview",
                interview_facts({
                    "expertise": "센서 제어 구현",
                    "project_summary": "센서 오차를 줄여야 한다",
                    "technical_domain": "센서",
                }),
            )
            actual_profile = profile_payload(actual_connection)
            shared_problem = actual_profile["facts"]["project_summary"]["claims"][0]["claim_id"]
            shared_capability = actual_profile["facts"]["expertise"]["claims"][0]["claim_id"]
            self.assertEqual(shared_problem, shared_capability)
            payload = candidate_input(3, self.evidence, self.span)
            for item in payload["candidates"]:
                item["profile_references"] = [
                    {"claim_id": shared_problem, "field": "project_summary", "kind": "problem"},
                    {"claim_id": shared_capability, "field": "expertise", "kind": "capability"},
                ]
            result = run_ideation(
                self.connection, profile_connection=actual_connection,
                run_root=self.run_root, run_id="run", profile=actual_profile,
                candidate_input=payload, config=self.config,
            )
        finally:
            actual_connection.close()
        self.assertEqual(result.next_state, "candidates_ready")
        self.assertIn("profile_context", StateStore(self.connection).snapshot("run").current_revisions)

    def test_domain_pivot_approval_is_bound_to_exact_candidate_request_and_context(self):
        first_payload = candidate_input(3, self.evidence, self.span, domain="다른 분야")
        with self.assertRaises(DomainPivotRequiredError) as captured:
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=first_payload, config=self.config,
            )
        first_gate = captured.exception.gate
        pending_snapshot = self.write_snapshot()

        changed_payload = json.loads(json.dumps(first_payload))
        changed_payload["candidates"][0]["title"] = "같은 피벗 분야의 변경된 후보"
        with self.assertRaises(StateError):
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=changed_payload, config=self.config,
            )
        self.assertEqual(self.write_snapshot(), pending_snapshot)

        decision, _result = StateStore(self.connection).decide_gate(
            first_gate.gate_id, action="approve", actor="test", reason="approve exact request",
            subject_revision_hash=first_gate.subject_revision_hash,
            approval_scope=first_gate.approval_scope,
            suspended_operation=first_gate.suspended_operation,
            return_state=first_gate.return_state,
        )
        approved_snapshot = self.write_snapshot()
        changed_scope = dict(first_gate.approval_scope)
        changed_scope["request_fingerprint"] = "ir_changed"
        with self.assertRaises(GateMismatchError):
            StateStore(self.connection).consume_decision(
                decision.decision_id, suspended_operation="ideation.publish:ir_changed",
                subject_revision_hash=first_gate.subject_revision_hash,
                approval_scope=changed_scope,
            )
        self.assertEqual(self.write_snapshot(), approved_snapshot)

        with self.assertRaises(DomainPivotRequiredError) as changed:
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=changed_payload, config=self.config,
            )
        self.assertNotEqual(changed.exception.gate.subject_revision_hash, first_gate.subject_revision_hash)
        stale_snapshot = self.write_snapshot()
        with self.assertRaises(GateMismatchError):
            StateStore(self.connection).consume_decision(
                decision.decision_id, suspended_operation=first_gate.suspended_operation,
                subject_revision_hash=first_gate.subject_revision_hash,
                approval_scope=first_gate.approval_scope,
            )
        self.assertEqual(self.write_snapshot(), stale_snapshot)
        self.assertEqual(
            self.connection.execute(
                "SELECT stale FROM gate_decisions WHERE decision_id=?", (decision.decision_id,)
            ).fetchone()["stale"],
            1,
        )

    def test_domain_pivot_approval_resumes_only_exact_fingerprinted_publish_once(self):
        payload = candidate_input(3, self.evidence, self.span, domain="승인된 새 분야")
        with self.assertRaises(DomainPivotRequiredError) as captured:
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=payload, config=self.config,
            )
        gate = captured.exception.gate
        decision, _ = StateStore(self.connection).decide_gate(
            gate.gate_id, action="approve", actor="test", reason="approve exact pivot",
            subject_revision_hash=gate.subject_revision_hash, approval_scope=gate.approval_scope,
        )
        completed = run_ideation(
            self.connection, profile_connection=self.profile_connection,
            run_root=self.run_root, run_id="run", profile=self.profile,
            candidate_input=payload, config=self.config, domain_decision_id=decision.decision_id,
        )
        self.assertEqual(completed.next_state, "candidates_ready")
        claimed = self.connection.execute(
            "SELECT used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?",
            (decision.decision_id,),
        ).fetchone()
        self.assertTrue(claimed["used_at"] and claimed["consumed_by_event_id"])
        replayed = run_ideation(
            self.connection, profile_connection=self.profile_connection,
            run_root=self.run_root, run_id="run", profile=self.profile,
            candidate_input=payload, config=self.config, domain_decision_id=decision.decision_id,
        )
        self.assertTrue(replayed.replayed)

    def test_candidate_and_finalist_publication_is_stable_ranked_and_idempotent(self):
        first = self.ideate(3)
        self.assertEqual(first.next_state, "candidates_ready")
        self.assertEqual(first.candidate_ids, tuple(sorted(first.candidate_ids)))
        replay = self.ideate(3)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.artifact.revision_id, first.artifact.revision_id)
        current = StateStore(self.connection).snapshot("run").current_revisions
        self.assertIn("profile_context", current)

        shortlist = shortlist_input(reversed(first.candidate_ids), self.evidence, self.span)
        selected = run_shortlist(
            self.connection, run_root=self.run_root, run_id="run",
            shortlist_input=shortlist, config=self.config,
        )
        self.assertEqual(selected.next_state, "finalists_ready")
        content = selected.artifact.content
        self.assertEqual([item["rank"] for item in content["finalists"]], [1, 2, 3])
        self.assertEqual(
            [item["candidate_id"] for item in content["finalists"]],
            sorted(first.candidate_ids),
        )
        self.assertTrue(all(len(item["axes"]) == 3 for item in content["finalists"]))
        replayed = run_shortlist(
            self.connection, run_root=self.run_root, run_id="run",
            shortlist_input=shortlist, config=self.config,
        )
        self.assertTrue(replayed.replayed)
        dependencies = {
            tuple(row) for row in self.connection.execute(
                "SELECT upstream_revision_id,downstream_revision_id FROM artifact_dependencies"
            )
        }
        self.assertIn((self.research.revision_id, first.artifact.revision_id), dependencies)
        self.assertIn((current["profile_context"], first.artifact.revision_id), dependencies)
        self.assertIn((first.artifact.revision_id, selected.artifact.revision_id), dependencies)

    def test_less_than_three_records_explicit_insufficiency_without_finalist_pointer(self):
        ideation = self.ideate(2)
        request = shortlist_input(ideation.candidate_ids, self.evidence, self.span)
        request["insufficiency"] = {
            "eligible_candidate_ids": list(ideation.candidate_ids),
            "limitations": ["세 번째 방어 가능한 후보 없음"],
            "missing_evidence": ["추가 메커니즘 근거"],
            "reason_codes": ["fewer_than_three_defensible"],
            "recommended_research": ["대체 메커니즘 검색"],
            "rejected_candidate_ids": [],
            "unresolved_questions": ["추가 구현 경로"],
        }
        result = run_shortlist(
            self.connection, run_root=self.run_root, run_id="run",
            shortlist_input=request, config=self.config,
        )
        self.assertEqual(result.next_state, "insufficient_evidence")
        self.assertEqual(result.finalist_ids, ())
        pointers = dict(self.connection.execute(
            "SELECT kind,revision_id FROM current_artifacts WHERE run_id='run'"
        ))
        self.assertNotIn("finalist_set", pointers)
        self.assertIn("insufficiency", pointers)
        self.assertEqual(result.artifact.content["finalist_count"], 0)

    def test_incomplete_axis_is_rejected_without_state_or_artifact_advance(self):
        ideation = self.ideate(3)
        request = shortlist_input(ideation.candidate_ids, self.evidence, self.span)
        request["finalists"][0]["axes"].pop()
        before = StateStore(self.connection).snapshot("run").state_version
        with self.assertRaisesRegex(ValueError, "exactly three independent axes"):
            run_shortlist(
                self.connection, run_root=self.run_root, run_id="run",
                shortlist_input=request, config=self.config,
            )
        snapshot = StateStore(self.connection).snapshot("run")
        self.assertEqual((snapshot.state, snapshot.state_version), (RunState.CANDIDATES_READY, before))
        self.assertNotIn("finalist_set", snapshot.current_revisions)

    def test_domain_pivot_suspends_before_candidate_persistence(self):
        with self.assertRaises(DomainPivotRequiredError):
            self.ideate(3, domain="다른 분야")
        snapshot = StateStore(self.connection).snapshot("run")
        self.assertEqual(snapshot.state, RunState.DOMAIN_PIVOT_REQUIRED)
        self.assertNotIn("candidate_set", snapshot.current_revisions)
        envelope = self.connection.execute(
            "SELECT suspended_operation,approval_scope_json FROM gate_envelopes WHERE status='pending'"
        ).fetchone()
        self.assertTrue(envelope["suspended_operation"].startswith("ideation.publish:ir_"))
        scope = json.loads(envelope["approval_scope_json"])
        self.assertIn("old_domain_hash", scope)
        self.assertEqual(len(scope["new_domain_hashes"]), 1)
        self.assertTrue(scope["request_fingerprint"].startswith("ir_"))
        self.assertEqual(scope["profile_revision_hash"], digest(self.profile))
        self.assertEqual(scope["research_revision_hash"], self.research.content_hash)
        self.assertEqual(scope["evaluation_config_hash"], self.config.content_hash)

    def test_publish_crash_leaves_resumable_state_and_recovery_removes_orphan(self):
        with self.assertRaises(InjectedFailure):
            run_ideation(
                self.connection, profile_connection=self.profile_connection,
                run_root=self.run_root, run_id="run", profile=self.profile,
                candidate_input=candidate_input(3, self.evidence, self.span),
                config=self.config, fault_at="after_export_publish",
            )
        self.assertEqual(StateStore(self.connection).snapshot("run").state, RunState.IDEATION_RUNNING)
        exports = self.run_root / "ideation-exports"
        self.assertTrue(tuple(exports.glob("ar_*.json")))
        StateStore(self.connection, export_directories=(self.run_root / "research-exports", exports))
        self.assertEqual(tuple(exports.glob("ar_*.json")), ())
        resumed = self.ideate(3)
        self.assertEqual(resumed.next_state, "candidates_ready")


if __name__ == "__main__":
    unittest.main()
