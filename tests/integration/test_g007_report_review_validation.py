import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from patent_factory.artifacts import ArtifactError
from patent_factory.cli import build_parser
from patent_factory.config import load_similarity_config
from patent_factory.database import connect_database
from patent_factory.database import InjectedFailure
from patent_factory.decisions import DecisionRun, resolve_gate
from patent_factory.models import RunState
from patent_factory.provenance import canonical_json, digest
from patent_factory.report import (
    REPORT_DISCLAIMER, SECTION_HEADINGS, SIMILARITY_DISCLAIMER, load_report_policy,
    publish_report, render_report_markdown, validate_report_artifact,
)
from patent_factory.review import run_review, validate_review_artifact
from patent_factory.sharing import SensitiveDisclosureRequiredError, share_report
from patent_factory.state import (
    GateMismatchError, StateError, StateStore, workspace_export_directories,
)
from patent_factory.validation import (
    _legal_language_check, _semantic_check, validate_and_complete,
    validate_validation_artifact,
)

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "patent_factory", *map(str, args)], cwd=ROOT,
        env=environment, text=True, capture_output=True, check=False,
    )


class G007Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        self.workspace = Path(self.temporary.name) / "workspace"
        self.run_root = self.workspace / "run"
        self.run_root.mkdir(parents=True, mode=0o700)
        self.connection = connect_database(self.run_root / "factory.sqlite3")
        self.store = StateStore(self.connection)
        self.store.create_run("run")
        self.connection.execute("UPDATE runs SET state='audit_approved' WHERE run_id='run'")
        self.evidence = []
        for index in range(1, 4):
            evidence_id = f"ev_{index:016x}"
            record = {
                "excerpt_hashes": [digest({"span": index})], "limitations": [f"한계 {index}"],
                "title": f"선행기술 {index}",
            }
            self.evidence.append({
                "canonical_url": f"https://example.test/source/{index}",
                "content_hash": digest(record), "created_at": "2026-07-14T00:00:00Z",
                "evidence_id": evidence_id, "language": "ko", "original_identifier": f"10-2026-000000{index}",
                "provenance": "fixture", "record_json": canonical_json(record), "run_id": "run",
                "source_locator": f"https://example.test/source/{index}", "source_type": "manual_web",
                "title": f"선행기술 {index}",
            })
        profile = self.store.add_revision("run", "profile_context", {
            "profile": {"facts": {
                "expertise": {"claims": [{"label": "user_statement", "source_id": "interview-expertise"}], "value": "센서 제어 구현"},
                "project_summary": {"claims": [{"label": "user_statement", "source_id": "interview-problem"}], "value": "센서 오차 저감"},
                "technical_domain": {"claims": [{"label": "user_statement", "source_id": "interview-domain"}], "value": "센서 시스템"},
            }}, "profile_revision_hash": "a" * 64, "profile_revision_id": "pr_fixture", "version": "profile-context-v1",
        }, schema_version="profile-context-v1")
        research = self.store.add_revision("run", "research_bundle", {
            "adapter_events": [{"adapter": "manual_web", "adapter_version": "import-v1", "retrieved_at": "2026-07-14T00:00:00Z"}],
            "coverage_limitations": [], "edges": [], "evidence": self.evidence, "observations": [],
            "queries": [{"plan_json": canonical_json({"query": "센서 오차"}), "query_id": "rq_fixture"}],
            "run_id": "run", "version": "research-bundle-v1",
        }, schema_version="research-bundle-v1")
        candidates = []
        for index, evidence in enumerate(self.evidence, start=1):
            reference = {"content_hash": evidence["content_hash"], "evidence_id": evidence["evidence_id"], "limitation": None, "span_hash": json.loads(evidence["record_json"])["excerpt_hashes"][0]}
            candidates.append({
                "candidate_id": f"ca_{index:020x}",
                # technical_problem carries a per-field citation; mechanism
                # deliberately carries none, so the report exercises both the
                # bound and the empty per-field binding.
                "claims": [
                    {"claim": {"label": "user_statement", "source_id": "interview-problem"}, "evidence_references": [dict(reference)], "field": "technical_problem"},
                    {"claim": {"label": "creative_suggestion"}, "evidence_references": [], "field": "mechanism"},
                    {"claim": {"label": "hypothesis"}, "evidence_references": [], "field": "expected_effects"},
                    {"claim": {"label": "creative_suggestion"}, "evidence_references": [], "field": "synthesis_trace"},
                ],
                "components": [f"구성요소 {index}"], "domain": "센서 시스템",
                "evidence_references": [dict(reference)],
                "expected_effects": [f"오차 감소 {index}"], "implementation_example": f"시험 장치 {index}",
                "interactions": [f"상호작용 {index}"], "mechanism": f"보정 메커니즘 {index}",
                "measurable_validation": f"오차율 측정 {index}", "required_inputs": [f"센서 입력 {index}"],
                "technical_problem": f"센서 오차 문제 {index}", "title": f"센서 발명 후보 {index}",
                "transformations": [f"보정 변환 {index}"], "outputs": [f"보정 출력 {index}"],
                "unresolved_questions": [f"환경 조건 {index}"],
            })
        candidate = self.store.add_revision("run", "candidate_set", {
            "candidates": candidates, "run_id": "run", "version": "candidate-set-v1",
        }, schema_version="candidate-set-v1", dependencies=(profile.revision_id, research.revision_id))
        finalists = []
        for index, item in enumerate(candidates, start=1):
            axes = []
            for axis in ("differentiation", "technical_feasibility", "utility_significance"):
                axes.append({
                    "axis": axis, "confidence": "high", "contrary_evidence_references": [],
                    "coverage_assessment": "현재 근거 범위 충분", "coverage_limitations": ["공개자료 한정"],
                    "gaps": ["현장 검증 필요"], "rationale": f"{axis} 근거 설명 {index}",
                    "rubric_version": "rubric-v1", "score": 80 - index,
                    "supporting_evidence_references": [{"evidence_id": self.evidence[index - 1]["evidence_id"]}],
                })
            finalists.append({
                "axes": axes, "candidate_id": item["candidate_id"], "candidate_revision_hash": candidate.content_hash,
                "finalist_id": f"fi_{index:020x}", "rank": index, "selection_priority": index,
                "selection_rationale": f"근거 우선순위 {index}",
            })
        finalist = self.store.add_revision("run", "finalist_set", {
            "finalists": finalists, "run_id": "run", "version": "finalist-set-v1",
        }, schema_version="finalist-set-v1", dependencies=(candidate.revision_id,))
        scorer_config = load_similarity_config().as_dict()
        scorer = self.store.add_revision("run", "scorer_config", {
            "config": scorer_config, "config_hash": digest(scorer_config), "finalist_set_hash": finalist.content_hash,
            "supersedes": None, "version": "scorer-config-v1",
        }, schema_version="scorer-config-v1", dependencies=(finalist.revision_id,))
        corpora = []
        for index, evidence in enumerate(self.evidence, start=1):
            corpora.append({
                "corpus_hash": digest({"corpus": index}), "excluded_count": 0, "excluded_records": [], "failures": [],
                "finalist_id": finalists[index - 1]["finalist_id"], "query_group_id": f"qg_{index}",
                "records": [{
                    "application_identity": f"102026000000{index}", "best_source_rank": 1,
                    "content_hash": evidence["content_hash"], "evidence_id": evidence["evidence_id"],
                    "logical_query_ids": [f"lq_{index}"], "query_hit_count": 1, "query_ids": [f"rq_{index}"],
                    "record": {"application_number": evidence["original_identifier"], "title": evidence["title"]},
                }], "retained_count": 1, "version": "retained-corpus-v1",
            })
        corpus = self.store.add_revision("run", "corpus_set", {
            "config_hash": digest(scorer_config), "corpora": corpora, "finalist_set_hash": finalist.content_hash,
            "run_id": "run", "version": "corpus-set-v1",
        }, schema_version="corpus-set-v1", dependencies=(finalist.revision_id, scorer.revision_id))
        maps = [{"feature_map": {}, "finalist_id": item["finalist_id"], "map_id": f"fm_{index}"} for index, item in enumerate(finalists, start=1)]
        feature = self.store.add_revision("run", "feature_map_set", {
            "corpus_set_hash": corpus.content_hash, "finalist_set_hash": finalist.content_hash,
            "maps": maps, "run_id": "run", "version": "feature-map-set-v1",
        }, schema_version="feature-map-set-v1", dependencies=(finalist.revision_id, corpus.revision_id))
        results = []
        for index, (item, evidence) in enumerate(zip(finalists, self.evidence), start=1):
            results.append({
                "candidate_id": item["candidate_id"], "closest_reference_id": evidence["evidence_id"],
                "corpus_hash": corpora[index - 1]["corpus_hash"], "counterargument": f"차별 특징 검토 {index}",
                "coverage": "90", "finalist_id": item["finalist_id"], "outcome": "audit_approved",
                "pair_scores": [{
                    "C": "20", "D": "10", "F": "30", "Q": "90", "T": "25",
                    "C_exact": {"denominator": 1, "numerator": 20, "value": 20},
                    "D_exact": {"denominator": 1, "numerator": 10, "value": 10},
                    "F_exact": {"denominator": 1, "numerator": 30, "value": 30},
                    "Q_exact": {"denominator": 1, "numerator": 90, "value": 90},
                    "T_exact": {"denominator": 1, "numerator": 25, "value": 25},
                    "differentiated_feature_ids": [f"df_{index}"], "evidence_id": evidence["evidence_id"],
                    "label": "moderate", "matched_feature_ids": [f"mf_{index}"],
                    "r_hi": "45", "r_obs": "40", "version": "simrisk-v1.0.0",
                    "r_hi_exact": {"denominator": 1, "numerator": 45, "value": 45},
                    "r_obs_exact": {"denominator": 1, "numerator": 40, "value": 40},
                }], "r_hi": "45", "r_obs": "40", "upper_bound_reference_id": evidence["evidence_id"],
            })
        self.audit = self.store.add_revision("run", "audit_batch", {
            "corpus_set_hash": corpus.content_hash, "feature_map_set_hash": feature.content_hash,
            "finalist_set_hash": finalist.content_hash, "results": results, "run_id": "run",
            "scorer_config_hash": scorer.content_hash, "version": "audit-batch-v1",
        }, schema_version="audit-batch-v1", dependencies=(finalist.revision_id, corpus.revision_id, feature.revision_id, scorer.revision_id))

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def draft_input(self, sensitive=False):
        return {
            "drafter": {"id": "drafter", "pass_id": "draft-pass", "type": "agent"},
            "handoff_questions": ["권리범위 검토가 필요한가?"],
            "profile_fields": ["expertise", "project_summary", "technical_domain"],
            "recommended_investigations": ["추가 실시예를 확인한다"], "report_date": "2026-07-14",
            "revision": None, "schema_version": "report-input-v1",
            "sensitive_disclosures": [{"field": "candidate.1.mechanism", "reason": "영업비밀", "text": "보정 메커니즘 1"}] if sensitive else [],
        }

    def review_input(self, report_hash, *, findings=None):
        findings = findings or []
        return {
            "checks": [{"details": "독립 검토 통과", "name": name, "status": "pass"} for name in (
                "citation_integrity", "decision_gate_coverage", "factual_grounding", "internal_consistency",
                "legal_language", "schema_completeness", "source_coverage",
            )],
            "decision_gate_verification": {"audit_hash": self.audit.content_hash, "covered_finalist_ids": [], "status": "pass"},
            "disposition": "approved", "evidence_corrections": [], "findings": findings,
            "prohibited_language_findings": [], "report_hash": report_hash,
            "reviewer": {"id": "reviewer", "pass_id": "review-pass", "type": "agent"},
            "schema_version": "review-input-v1",
        }

    def complete(self, *, sensitive=False):
        report = publish_report(self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input(sensitive))
        review = run_review(self.connection, run_root=self.run_root, run_id="run", review_input=self.review_input(report.artifact.content_hash))
        validation = validate_and_complete(self.connection, run_root=self.run_root, run_id="run")
        return report, review, validation


class G007ReportTests(G007Fixture):
    def test_cli_draft_review_validate_contract(self):
        workspace = self.workspace.relative_to(ROOT)
        run_root = self.run_root.relative_to(ROOT)
        draft_input = self.workspace / "draft-input.json"
        draft_input.write_text(json.dumps(self.draft_input(), ensure_ascii=False), encoding="utf-8")
        drafted = run_cli(
            "draft", "--run", run_root, "--run-id", "run", "--input", draft_input.relative_to(ROOT),
            "--workspace-root", workspace,
        )
        self.assertEqual(drafted.returncode, 0, drafted.stdout + drafted.stderr)
        draft_payload = json.loads(drafted.stdout)
        report_hash = self.connection.execute(
            "SELECT ar.content_hash FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='report'",
        ).fetchone()[0]
        self.assertEqual(draft_payload["next_state"], "draft_ready")

        review_input = self.workspace / "review-input.json"
        review_input.write_text(
            json.dumps(self.review_input(report_hash), ensure_ascii=False), encoding="utf-8",
        )
        reviewed = run_cli(
            "review", "--run", run_root, "--run-id", "run", "--input", review_input.relative_to(ROOT),
            "--workspace-root", workspace,
        )
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        self.assertEqual(json.loads(reviewed.stdout)["next_state"], "reviewed")

        validated = run_cli(
            "validate", "--run", run_root, "--run-id", "run", "--workspace-root", workspace,
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertEqual(json.loads(validated.stdout)["next_state"], "complete")

    def test_happy_path_is_exact_deterministic_schema_valid_and_complete(self):
        report, review, validation = self.complete()
        self.assertEqual(validation.next_state, "complete")
        content = report.artifact.content
        self.assertEqual([item["heading"] for item in content["sections"]], SECTION_HEADINGS)
        self.assertIn(REPORT_DISCLAIMER, content["sections"][0]["body"])
        self.assertIn(SIMILARITY_DISCLAIMER, content["sections"][7]["body"])
        self.assertEqual(content["appendix_ids"], [item["evidence_id"] for item in self.evidence])
        self.assertEqual(content["sections"][10]["body"].count("[@ev_"), 3)
        self.assertTrue(Path(report.export_path).name.endswith(".md"))
        if Draft202012Validator is not None:
            root = Path(__file__).resolve().parents[2]
            for name, artifact in (("report", report.artifact), ("review", review.artifact), ("validation", validation.artifact)):
                schema = json.loads((root / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
                Draft202012Validator(schema).validate(artifact.content)

    def test_reviewer_identity_and_pass_must_be_independent(self):
        report = publish_report(self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input())
        value = self.review_input(report.artifact.content_hash)
        value["reviewer"] = {"id": "drafter", "pass_id": "different", "type": "agent"}
        with self.assertRaisesRegex(ValueError, "independent"):
            run_review(self.connection, run_root=self.run_root, run_id="run", review_input=value)

    def test_legal_language_is_sentence_local_and_advisory_findings_are_allowed(self):
        report = publish_report(self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input())
        qualified = json.loads(json.dumps(report.artifact.content))
        qualified["markdown"] += "특허 가능하다는 법적 결론을 제공하지 않습니다.\n"
        _legal_language_check(qualified)
        unqualified = json.loads(json.dumps(report.artifact.content))
        unqualified["markdown"] += "이 발명은 특허를 받을 수 있습니다.\n"
        with self.assertRaisesRegex(ValueError, "unqualified"):
            _legal_language_check(unqualified)
        findings = [{"check": "source_coverage", "code": "advice", "message": "추가 조사가 유용함", "path": "/sections/4", "severity": "advisory"}]
        review = run_review(self.connection, run_root=self.run_root, run_id="run", review_input=self.review_input(report.artifact.content_hash, findings=findings))
        self.assertEqual(review.next_state, "reviewed")
        self.assertEqual(validate_and_complete(self.connection, run_root=self.run_root, run_id="run").next_state, "complete")

    def test_validity_and_non_infringement_claims_fail_review_even_when_checkbox_passes(self):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input(),
        )
        row = self.connection.execute(
            "SELECT revision_id,content_json FROM artifact_revisions WHERE revision_id=?",
            (report.artifact.revision_id,),
        ).fetchone()
        for phrase in (
            "이 특허는 유효성이 있습니다.",
            "이 제품은 타인의 특허를 침해하지 않습니다.",
            "This patent is valid.",
            "This product does not infringe another patent.",
        ):
            value = json.loads(row["content_json"])
            value["sections"][5]["body"] += f"\n- {phrase}"
            value["markdown"] = render_report_markdown(value["sections"])
            validate_report_artifact(value)
            self.connection.execute(
                "UPDATE artifact_revisions SET content_json=? WHERE revision_id=?",
                (canonical_json(value), row["revision_id"]),
            )
            with self.subTest(phrase=phrase), self.assertRaisesRegex(ValueError, "unqualified legal conclusion"):
                run_review(
                    self.connection, run_root=self.run_root, run_id="run",
                    review_input=self.review_input(report.artifact.content_hash),
                )
        self.connection.execute(
            "UPDATE artifact_revisions SET content_json=? WHERE revision_id=?",
            (row["content_json"], row["revision_id"]),
        )

    def test_forged_validation_cannot_use_generic_complete_transition(self):
        report = publish_report(self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input())
        review = run_review(self.connection, run_root=self.run_root, run_id="run", review_input=self.review_input(report.artifact.content_hash))
        self.connection.execute("UPDATE runs SET state='validated' WHERE run_id='run'")
        self.store.add_revision("run", "validation", {
            "artifact_hashes": {}, "checks": [], "policy_hash": "x", "report_hash": report.artifact.content_hash,
            "review_hash": review.artifact.content_hash, "run_id": "run", "schema_versions": {},
            "scoring_version": "x", "status": "passed", "validator_version": "report-validator-v1.0.0",
            "version": "validation-v1", "workflow_version": "x",
        }, schema_version="validation-v1", dependencies=(report.artifact.revision_id, review.artifact.revision_id))
        with self.assertRaises((StateError, ValueError)):
            self.store.transition("run", RunState.COMPLETE, actor="attacker", reason="forge", operation="forge", idempotency_key="forge")

    def test_completion_recomputes_report_review_and_validation_content_hashes(self):
        self.complete()
        self.connection.execute("UPDATE runs SET state='validated' WHERE run_id='run'")
        for kind in ("report", "review", "validation"):
            row = self.connection.execute(
                "SELECT ar.revision_id,ar.content_json FROM artifact_revisions ar "
                "JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
                "WHERE ca.run_id='run' AND ca.kind=?", (kind,),
            ).fetchone()
            forged = json.loads(row["content_json"])
            forged["forged_field"] = kind
            self.connection.execute(
                "UPDATE artifact_revisions SET content_json=? WHERE revision_id=?",
                (canonical_json(forged), row["revision_id"]),
            )
            with self.subTest(kind=kind), self.assertRaisesRegex(StateError, f"completion {kind} artifact content hash mismatch"):
                self.store.transition(
                    "run", RunState.COMPLETE, actor="attacker", reason="forged bytes",
                    operation=f"forge-{kind}", idempotency_key=kind,
                )
            self.connection.execute(
                "UPDATE artifact_revisions SET content_json=? WHERE revision_id=?",
                (row["content_json"], row["revision_id"]),
            )

    def test_authoritative_audit_validation_rejects_empty_omitted_duplicate_and_extra_results(self):
        for name in ("empty", "omitted", "duplicate", "extra"):
            case = G007Fixture(methodName="runTest")
            case.setUp()
            try:
                audit = json.loads(json.dumps(case.audit.content))
                if name == "empty":
                    audit["results"] = []
                elif name == "omitted":
                    audit["results"] = audit["results"][:-1]
                elif name == "duplicate":
                    audit["results"].append(json.loads(json.dumps(audit["results"][0])))
                else:
                    extra = json.loads(json.dumps(audit["results"][0]))
                    extra["finalist_id"] = "fi_ffffffffffffffffffff"
                    extra["candidate_id"] = "ca_ffffffffffffffffffff"
                    audit["results"].append(extra)
                case.store.add_revision(
                    "run", "audit_batch", audit, schema_version="audit-batch-v1",
                )
                with self.subTest(name=name), self.assertRaisesRegex(StateError, "audit"):
                    publish_report(
                        case.connection, run_root=case.run_root, run_id="run",
                        report_input=case.draft_input(),
                    )
            finally:
                case.tearDown()

    def test_completion_revalidates_current_audit_authoritatively(self):
        self.complete()
        audit_row = self.connection.execute(
            "SELECT ar.revision_id,ar.content_json FROM artifact_revisions ar "
            "JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='audit_batch'",
        ).fetchone()
        forged = json.loads(audit_row["content_json"])
        forged["results"] = []
        self.connection.execute(
            "UPDATE artifact_revisions SET content_json=? WHERE revision_id=?",
            (canonical_json(forged), audit_row["revision_id"]),
        )
        self.connection.execute("UPDATE runs SET state='validated' WHERE run_id='run'")
        current_report = json.loads(self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='report'",
        ).fetchone()[0])
        with self.assertRaisesRegex(StateError, "authoritative validation"):
            _semantic_check(self.connection, "run", current_report)
        with self.assertRaisesRegex(StateError, "does not reproduce"):
            self.store.transition(
                "run", RunState.COMPLETE, actor="attacker", reason="empty audit",
                operation="empty-audit-complete", idempotency_key="empty-audit",
            )

    def test_same_version_policy_arrays_reject_any_drift(self):
        original = json.loads((ROOT / "config" / "report-v1.0.0.json").read_text(encoding="utf-8"))
        for field in ("required_review_checks", "prohibited_unqualified_phrases"):
            for mutation in ("reorder", "replace", "append"):
                value = json.loads(json.dumps(original))
                if mutation == "reorder":
                    value[field][0], value[field][1] = value[field][1], value[field][0]
                elif mutation == "replace":
                    value[field][0] = "drifted-value"
                else:
                    value[field].append("drifted-value")
                path = self.workspace / f"policy-{field}-{mutation}.json"
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                with self.subTest(field=field, mutation=mutation), patch("patent_factory.report.POLICY_PATH", path), self.assertRaisesRegex(ValueError, "frozen"):
                    load_report_policy()

    def test_canonical_reconstruction_rejects_extra_or_altered_report_claims(self):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input(),
        ).artifact.content
        mutations = {}

        extra = json.loads(json.dumps(report))
        extra["sections"][5]["body"] += "\n- 확인되지 않은 추가 사실 [@ev_0000000000000001]"
        extra["markdown"] = render_report_markdown(extra["sections"])
        validate_report_artifact(extra)
        mutations["extra_claim"] = extra

        # Axis scores no longer render anywhere (US-019 dropped the numeric axis
        # matrix and removed the score from `axis_line`), so the altered claim
        # targets the axis rationale instead — still a substantive axis assertion
        # the canonical reconstruction must reject. The assertIn keeps the
        # mutation from silently becoming a no-op if the renderer changes again:
        # a vacuous mutation would make this subtest certify nothing.
        altered_axis = json.loads(json.dumps(report))
        original_rationale = "근거 differentiation 근거 설명 1"
        self.assertIn(original_rationale, altered_axis["sections"][5]["body"])
        altered_axis["sections"][5]["body"] = altered_axis["sections"][5]["body"].replace(
            original_rationale, "근거 differentiation 근거 설명 9", 1,
        )
        altered_axis["markdown"] = render_report_markdown(altered_axis["sections"])
        validate_report_artifact(altered_axis)
        mutations["altered_axis_claim"] = altered_axis

        closest = json.loads(json.dumps(report))
        closest["sections"][5]["body"] = closest["sections"][5]["body"].replace(
            "10-2026-0000001", "10-2026-0000002", 1,
        )
        closest["markdown"] = render_report_markdown(closest["sections"])
        validate_report_artifact(closest)
        mutations["changed_closest_reference"] = closest

        for name, value in mutations.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "canonical reconstruction"):
                _semantic_check(self.connection, "run", value)

    def test_canonical_reconstruction_rejects_matrix_and_appendix_mutations(self):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input(),
        ).artifact.content
        matrix_lines = report["sections"][6]["body"].splitlines()
        for name, lines in (
            ("reordered_matrix", matrix_lines[:2] + [matrix_lines[3], matrix_lines[2]] + matrix_lines[4:]),
            ("omitted_matrix", matrix_lines[:3] + matrix_lines[4:]),
        ):
            value = json.loads(json.dumps(report))
            value["sections"][6]["body"] = "\n".join(lines)
            value["markdown"] = render_report_markdown(value["sections"])
            validate_report_artifact(value)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "canonical reconstruction"):
                _semantic_check(self.connection, "run", value)

        for name, appendix_ids in (
            ("reordered_appendix", list(reversed(report["appendix_ids"]))),
            ("omitted_appendix", report["appendix_ids"][:-1]),
        ):
            value = json.loads(json.dumps(report))
            value["appendix_ids"] = appendix_ids
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_report_artifact(value)

    @unittest.skipIf(Draft202012Validator is None, "jsonschema unavailable")
    def test_artifact_schemas_and_runtime_reject_the_same_malicious_nested_records(self):
        report_run, review_run, validation_run = self.complete()
        schemas = {
            name: Draft202012Validator(json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8")))
            for name in ("report", "review", "validation")
        }
        cases = []

        malformed_report = json.loads(json.dumps(report_run.artifact.content))
        malformed_report["draft_spec"]["unexpected"] = ["claim"]
        cases.append(("report.draft_spec", "report", malformed_report, lambda value: validate_report_artifact(value)))

        for field, malformed in (
            ("findings", {"check": "source_coverage", "severity": "advisory", "unexpected": "x"}),
            ("evidence_corrections", {"evidence_id": "bad", "field": "title", "reason": "x", "replacement": "y"}),
            ("prohibited_language_findings", {"phrase": "patentable", "section": "6", "unexpected": "x"}),
        ):
            value = json.loads(json.dumps(review_run.artifact.content))
            value[field].append(malformed)
            cases.append((f"review.{field}", "review", value, lambda item, report=report_run.artifact.content: validate_review_artifact(item, report=report)))

        malformed_validation = json.loads(json.dumps(validation_run.artifact.content))
        malformed_validation["checks"][0]["unexpected"] = True
        cases.append(("validation.check", "validation", malformed_validation, lambda value: validate_validation_artifact(value)))

        for name, schema_name, value, runtime in cases:
            with self.subTest(name=name):
                self.assertFalse(schemas[schema_name].is_valid(value), "schema accepted malicious artifact")
                with self.assertRaises(ValueError):
                    runtime(value)

    def test_report_publish_fault_boundaries_leave_no_authoritative_partial_state(self):
        for boundary in ("after_export_publish", "after_state"):
            case = G007Fixture(methodName="runTest")
            case.setUp()
            try:
                with self.subTest(boundary=boundary), self.assertRaises(InjectedFailure):
                    publish_report(
                        case.connection, run_root=case.run_root, run_id="run",
                        report_input=case.draft_input(), fault_at=boundary,
                    )
                self.assertEqual(case.store.snapshot("run").state, RunState.AUDIT_APPROVED)
                self.assertEqual(case.connection.execute("SELECT count(*) FROM artifact_revisions WHERE kind='report'").fetchone()[0], 0)
                exports = case.run_root / "report-exports"
                self.assertEqual(len(tuple(exports.glob("ar_*.md"))), 1)
                StateStore(case.connection, export_directories=(exports,))
                self.assertEqual(tuple(exports.glob("ar_*.md")), ())
            finally:
                case.tearDown()

    def test_markdown_temp_orphan_and_registered_tamper_are_detected(self):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input(),
        )
        exports = self.run_root / "report-exports"
        temporary = exports / ".artifact-interrupted.tmp"
        temporary.write_bytes(b"partial")
        StateStore(self.connection, export_directories=(exports,))
        self.assertFalse(temporary.exists())
        Path(report.export_path).write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ArtifactError, "registered export mismatch"):
            StateStore(self.connection, export_directories=(exports,))

    def test_identical_and_different_concurrent_drafts_have_deterministic_winners(self):
        def race(case, inputs):
            database = case.run_root / "factory.sqlite3"

            def execute(value):
                connection = connect_database(database)
                try:
                    return publish_report(
                        connection, run_root=case.run_root, run_id="run", report_input=value,
                    )
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(execute, value) for value in inputs]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(("ok", future.result()))
                    except Exception as exc:  # concurrent loser is the evidence under test
                        outcomes.append(("error", exc))
                return outcomes

        identical_case = G007Fixture(methodName="runTest")
        identical_case.setUp()
        try:
            outcomes = race(identical_case, [identical_case.draft_input(), identical_case.draft_input()])
            self.assertEqual([status for status, _item in outcomes].count("ok"), 2)
            self.assertEqual({item.artifact.revision_id for status, item in outcomes if status == "ok"}.__len__(), 1)
            self.assertEqual(sum(item.replayed for status, item in outcomes if status == "ok"), 1)
        finally:
            identical_case.tearDown()

        different_case = G007Fixture(methodName="runTest")
        different_case.setUp()
        try:
            other = different_case.draft_input()
            other["handoff_questions"] = ["다른 인계 질문인가?"]
            outcomes = race(different_case, [different_case.draft_input(), other])
            self.assertEqual([status for status, _item in outcomes].count("ok"), 1)
            self.assertEqual([status for status, _item in outcomes].count("error"), 1)
            self.assertEqual(different_case.connection.execute("SELECT count(*) FROM artifact_revisions WHERE kind='report'").fetchone()[0], 1)
        finally:
            different_case.tearDown()

    def test_g008_surfaces_are_absent_from_g007_cli_and_report_artifact(self):
        commands = set(build_parser()._subparsers._group_actions[0].choices)
        self.assertTrue({"draft", "review", "validate", "share"}.issubset(commands))
        self.assertTrue(commands.isdisjoint({"serve", "web", "deploy", "notify", "email"}))
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input(),
        ).artifact.content
        self.assertTrue(set(report).isdisjoint({"ui", "deployment", "email_delivery", "notification"}))


class G007SensitiveTests(G007Fixture):
    def _share_request(self, report_hash, **changes):
        value = {
            "destination": "shares", "purpose": "변리사 검토", "recipient": "attorney@example.test",
            "report_hash": report_hash, "schema_version": "external-report-share-v1",
            "sensitive_fields": ["candidate.1.mechanism"],
        }
        value.update(changes)
        return value

    def _open_sensitive_gate(self, report_hash):
        (self.workspace / "shares").mkdir(exist_ok=True)
        request = self._share_request(report_hash)
        with self.assertRaises(SensitiveDisclosureRequiredError) as caught:
            share_report(self.connection, run_root=self.run_root, run_id="run", share_input=request)
        return request, caught.exception.gate

    def _decide(self, gate, action, reason="reviewed"):
        return resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input={
            "action": action, "actor": "user", "approval_scope": gate.approval_scope,
            "decisions": [], "gate_id": gate.gate_id, "plan": {}, "reason": reason,
            "schema_version": "gate-decision-input-v1", "subject_revision_hash": gate.subject_revision_hash,
        })

    def test_external_share_requires_exact_one_use_approval(self):
        report, _review, _validation = self.complete(sensitive=True)
        destination = self.workspace / "shares"
        destination.mkdir()
        request = {
            "destination": "shares", "purpose": "변리사 검토", "recipient": "attorney@example.test",
            "report_hash": report.artifact.content_hash, "schema_version": "external-report-share-v1",
            "sensitive_fields": ["candidate.1.mechanism"],
        }
        with self.assertRaises(SensitiveDisclosureRequiredError) as caught:
            share_report(self.connection, run_root=self.run_root, run_id="run", share_input=request)
        gate = caught.exception.gate
        decision_input = {
            "action": "approve", "actor": "user", "approval_scope": gate.approval_scope,
            "decisions": [], "gate_id": gate.gate_id, "plan": {}, "reason": "exact external share approved",
            "schema_version": "gate-decision-input-v1", "subject_revision_hash": gate.subject_revision_hash,
        }
        decision = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input=decision_input)
        shared = share_report(self.connection, run_root=self.run_root, run_id="run", share_input=request, decision_id=decision.decision_id)
        self.assertTrue(Path(shared.export_path).is_file())
        row = self.connection.execute("SELECT used_at FROM gate_decisions WHERE decision_id=?", (decision.decision_id,)).fetchone()
        self.assertTrue(row["used_at"])

    def test_redact_creates_new_report_and_invalidates_review_validation(self):
        report, review, validation = self.complete(sensitive=True)
        (self.workspace / "shares").mkdir()
        request = {
            "destination": "shares", "purpose": "검토", "recipient": "attorney",
            "report_hash": report.artifact.content_hash, "schema_version": "external-report-share-v1",
            "sensitive_fields": ["candidate.1.mechanism"],
        }
        with self.assertRaises(SensitiveDisclosureRequiredError) as caught:
            share_report(self.connection, run_root=self.run_root, run_id="run", share_input=request)
        gate = caught.exception.gate
        result = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input={
            "action": "redact", "actor": "user", "approval_scope": gate.approval_scope,
            "decisions": [], "gate_id": gate.gate_id, "plan": {}, "reason": "민감 필드 삭제",
            "schema_version": "gate-decision-input-v1", "subject_revision_hash": gate.subject_revision_hash,
        })
        self.assertEqual(result.next_state, "draft_ready")
        current = self.connection.execute(
            "SELECT ar.content_hash,ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='report'"
        ).fetchone()
        redacted = json.loads(current["content_json"])
        self.assertNotIn("보정 메커니즘 1", redacted["markdown"])
        self.assertTrue(redacted["redactions"])
        self.assertTrue(all("text" not in item for item in redacted["redactions"]))
        for revision in (report.artifact.revision_id, review.artifact.revision_id, validation.artifact.revision_id):
            self.assertEqual(self.connection.execute("SELECT stale FROM artifact_revisions WHERE revision_id=?", (revision,)).fetchone()[0], 1)
        new_review = run_review(
            self.connection, run_root=self.run_root, run_id="run",
            review_input=self.review_input(current["content_hash"]),
        )
        self.assertEqual(new_review.next_state, "reviewed")
        completed = validate_and_complete(self.connection, run_root=self.run_root, run_id="run")
        self.assertEqual(completed.next_state, "complete")

    def test_symlinked_destination_ancestor_rejects_without_gate_or_state_change(self):
        report, _review, _validation = self.complete(sensitive=True)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "subdir").mkdir()
        os.symlink(outside, self.workspace / "link")
        before = self.store.snapshot("run")
        request = {
            "destination": "link/subdir", "purpose": "검토", "recipient": "attorney",
            "report_hash": report.artifact.content_hash, "schema_version": "external-report-share-v1",
            "sensitive_fields": ["candidate.1.mechanism"],
        }
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            share_report(self.connection, run_root=self.run_root, run_id="run", share_input=request)
        after = self.store.snapshot("run")
        self.assertEqual((after.state, after.state_version), (before.state, before.state_version))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_decisions").fetchone()[0], 0)

    def test_sensitive_stop_is_terminal_and_publishes_nothing(self):
        report, _review, _validation = self.complete(sensitive=True)
        _request, gate = self._open_sensitive_gate(report.artifact.content_hash)
        decision = self._decide(gate, "stop", "do not disclose")
        self.assertEqual(decision.next_state, "stopped")
        self.assertEqual(self.store.snapshot("run").state, RunState.STOPPED)
        self.assertEqual(tuple((self.workspace / "shares").iterdir()), ())
        self.assertEqual(self.connection.execute("SELECT count(*) FROM artifact_revisions WHERE kind='share_receipt'").fetchone()[0], 0)

    def test_share_approval_rejects_changed_scope_content_recipient_and_purpose(self):
        report, _review, _validation = self.complete(sensitive=True)
        request, gate = self._open_sensitive_gate(report.artifact.content_hash)
        decision = self._decide(gate, "approve")
        mutations = {
            "scope": {**request, "sensitive_fields": []},
            "content": {**request, "report_hash": "f" * 64},
            "recipient": {**request, "recipient": "other@example.test"},
            "purpose": {**request, "purpose": "publication"},
        }
        for name, changed in mutations.items():
            with self.subTest(name=name), self.assertRaises((GateMismatchError, StateError, ValueError)):
                share_report(
                    self.connection, run_root=self.run_root, run_id="run",
                    share_input=changed, decision_id=decision.decision_id,
                )
        self.assertIsNone(self.connection.execute("SELECT used_at FROM gate_decisions WHERE decision_id=?", (decision.decision_id,)).fetchone()[0])
        shared = share_report(
            self.connection, run_root=self.run_root, run_id="run",
            share_input=request, decision_id=decision.decision_id,
        )
        self.assertTrue(Path(shared.export_path).is_file())

    def test_share_exact_retry_replays_without_reusing_approval(self):
        report, _review, _validation = self.complete(sensitive=True)
        request, gate = self._open_sensitive_gate(report.artifact.content_hash)
        decision = self._decide(gate, "approve")
        first = share_report(
            self.connection, run_root=self.run_root, run_id="run",
            share_input=request, decision_id=decision.decision_id,
        )
        before = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("artifact_revisions", "gate_decisions", "idempotency_records", "transition_events")
        }
        before_version = self.store.snapshot("run").state_version
        with_decision = share_report(
            self.connection, run_root=self.run_root, run_id="run",
            share_input=request, decision_id=decision.decision_id,
        )
        without_decision = share_report(
            self.connection, run_root=self.run_root, run_id="run", share_input=request,
        )
        self.assertTrue(with_decision.replayed and without_decision.replayed)
        self.assertEqual(
            {first.receipt_revision_id, with_decision.receipt_revision_id, without_decision.receipt_revision_id},
            {first.receipt_revision_id},
        )
        self.assertEqual(
            {
                table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in before
            },
            before,
        )
        self.assertEqual(self.store.snapshot("run").state_version, before_version)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM artifact_revisions WHERE kind='share_receipt'").fetchone()[0], 1)

    def test_share_manages_only_owner_child_and_preserves_caller_artifacts(self):
        report, _review, _validation = self.complete(sensitive=True)
        request, gate = self._open_sensitive_gate(report.artifact.content_hash)
        destination = self.workspace / "shares"
        keep = {
            destination / "ar_keep.md": b"caller markdown\n",
            destination / "ar_keep.json": b'{"caller":true}\n',
        }
        for path, payload in keep.items():
            path.write_bytes(payload)
        decision = self._decide(gate, "approve")
        shared = share_report(
            self.connection, run_root=self.run_root, run_id="run",
            share_input=request, decision_id=decision.decision_id,
        )
        export = Path(shared.export_path)
        self.assertEqual(export.parent.parent, destination)
        self.assertEqual(stat.S_IMODE(export.parent.lstat().st_mode), 0o700)
        StateStore(
            self.connection,
            export_directories=workspace_export_directories(self.connection, self.run_root),
        )
        changed = {**request, "purpose": "redact before another disclosure"}
        with self.assertRaises(SensitiveDisclosureRequiredError) as caught:
            share_report(self.connection, run_root=self.run_root, run_id="run", share_input=changed)
        self._decide(caught.exception.gate, "redact", "remove sensitive text")
        for path, payload in keep.items():
            self.assertEqual(path.read_bytes(), payload)

    def test_unsafe_managed_share_child_does_not_consume_approval(self):
        report, _review, _validation = self.complete(sensitive=True)
        request, gate = self._open_sensitive_gate(report.artifact.content_hash)
        decision = self._decide(gate, "approve")
        outside = Path(self.temporary.name) / "outside-managed"
        outside.mkdir()
        os.symlink(outside, self.workspace / "shares" / ".patent-factory-shares")
        with self.assertRaisesRegex(ValueError, "non-symbolic-link"):
            share_report(
                self.connection, run_root=self.run_root, run_id="run",
                share_input=request, decision_id=decision.decision_id,
            )
        row = self.connection.execute(
            "SELECT consumed_at,used_at FROM gate_decisions WHERE decision_id=?",
            (decision.decision_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (None, None))

    def test_changed_share_scope_requires_fresh_gate_and_second_distinct_share_succeeds(self):
        report, _review, _validation = self.complete(sensitive=True)
        request, gate = self._open_sensitive_gate(report.artifact.content_hash)
        first_decision = self._decide(gate, "approve")
        share_report(
            self.connection, run_root=self.run_root, run_id="run",
            share_input=request, decision_id=first_decision.decision_id,
        )
        (self.workspace / "shares-second").mkdir()
        changed = {**request, "destination": "shares-second", "purpose": "second attorney review"}
        with self.assertRaises(SensitiveDisclosureRequiredError) as caught:
            share_report(self.connection, run_root=self.run_root, run_id="run", share_input=changed)
        second_decision = self._decide(caught.exception.gate, "approve")
        second = share_report(
            self.connection, run_root=self.run_root, run_id="run",
            share_input=changed, decision_id=second_decision.decision_id,
        )
        self.assertTrue(Path(second.export_path).is_file())
        self.assertEqual(self.connection.execute("SELECT count(*) FROM artifact_revisions WHERE kind='share_receipt'").fetchone()[0], 2)

    def test_redaction_rejects_secret_canary_and_exact_repeat_replays_without_mutation(self):
        report, _review, _validation = self.complete(sensitive=True)
        _request, gate = self._open_sensitive_gate(report.artifact.content_hash)
        canary = "G007-REDACTION-SECRET-CANARY"
        with patch.dict(os.environ, {"KIPRIS_PLUS_API_KEY": canary}), self.assertRaises(ValueError):
            self._decide(gate, "redact", canary)
        self.assertEqual(self.store.snapshot("run").state, RunState.SENSITIVE_DISCLOSURE_REQUIRED)
        decision = self._decide(gate, "redact", "remove sensitive text")
        self.assertEqual(decision.next_state, "draft_ready")
        self.assertNotIn(canary.encode(), (self.run_root / "factory.sqlite3").read_bytes())
        before = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("artifact_revisions", "gate_decisions", "idempotency_records", "transition_events")
        }
        before_version = self.store.snapshot("run").state_version
        repeated = self._decide(gate, "redact", "remove sensitive text")
        self.assertTrue(repeated.replayed)
        self.assertEqual(repeated, DecisionRun(
            decision.run_id, decision.gate_id, decision.decision_id,
            decision.artifact_revision_id, decision.action, decision.next_state,
            True, decision.report_revision_id,
        ))
        self.assertEqual(
            {
                table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in before
            },
            before,
        )
        self.assertEqual(self.store.snapshot("run").state_version, before_version)
        with self.assertRaises((GateMismatchError, StateError)):
            self._decide(gate, "redact", "changed reason requires live validation")


if __name__ == "__main__":
    unittest.main()
