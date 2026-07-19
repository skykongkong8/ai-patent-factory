import json
import unittest
from pathlib import Path
from unittest.mock import patch

from patent_factory.decisions import resolve_gate
from patent_factory.report import (
    POLICY_PATHS,
    REDACTION_REPLACEMENTS,
    REPORT_DISCLAIMERS,
    SECTION_HEADINGS_EN,
    SIMILARITY_DISCLAIMERS,
    load_report_policy,
    publish_report,
)
from patent_factory.review import run_review
from patent_factory.sharing import SensitiveDisclosureRequiredError, share_report
from patent_factory.validation import _legal_language_check, validate_and_complete
from tests.integration.test_g007_report_review_validation import G007Fixture

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parents[2]


class EnglishReportTests(G007Fixture):
    def english_draft_input(self, *, sensitive=False):
        return {
            "drafter": {"id": "drafter", "pass_id": "draft-pass", "type": "agent"},
            "handoff_questions": ["Can the runtime-signal features be claimed independently?"],
            "language": "en",
            "profile_fields": ["expertise", "project_summary", "technical_domain"],
            "recommended_investigations": ["Verify the public NPU power-state API specification"],
            "report_date": "2026-07-19",
            "revision": None,
            "schema_version": "report-input-v2",
            "sensitive_disclosures": [
                {"field": "candidate.1.mechanism", "reason": "trade secret", "text": "보정 메커니즘 1"}
            ] if sensitive else [],
        }

    def test_english_report_completes_review_and_validation(self):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run",
            report_input=self.english_draft_input(),
        )
        content = report.artifact.content
        self.assertEqual(content["language"], "en")
        self.assertEqual([item["heading"] for item in content["sections"]], SECTION_HEADINGS_EN)
        self.assertIn(REPORT_DISCLAIMERS["en"], content["sections"][0]["body"])
        self.assertIn(SIMILARITY_DISCLAIMERS["en"], content["sections"][7]["body"])
        self.assertTrue(content["markdown"].startswith("# Korean Patent Proposal Review Report"))
        self.assertIn("## 8 Final KIPRIS Similarity-Risk Audit", content["markdown"])
        self.assertIn("observed risk", content["markdown"])
        self.assertNotIn("관측 위험", content["markdown"])
        if Draft202012Validator is not None:
            schema = json.loads((ROOT / "schemas" / "report.schema.json").read_text(encoding="utf-8"))
            Draft202012Validator(schema).validate(content)
        review = run_review(
            self.connection, run_root=self.run_root, run_id="run",
            review_input=self.review_input(report.artifact.content_hash),
        )
        self.assertEqual(review.next_state, "reviewed")
        validation = validate_and_complete(self.connection, run_root=self.run_root, run_id="run")
        self.assertEqual(validation.next_state, "complete")
        check_names = [item["name"] for item in validation.artifact.content["checks"]]
        self.assertIn("narrative_language", check_names)
        self.assertNotIn("korean_narrative", check_names)

    def test_v2_korean_input_replays_identically_to_v1(self):
        first = publish_report(
            self.connection, run_root=self.run_root, run_id="run", report_input=self.draft_input(),
        )
        v2 = dict(self.draft_input())
        v2["schema_version"] = "report-input-v2"
        v2["language"] = "ko"
        second = publish_report(
            self.connection, run_root=self.run_root, run_id="run", report_input=v2,
        )
        self.assertTrue(second.replayed)
        self.assertEqual(second.artifact.revision_id, first.artifact.revision_id)
        self.assertEqual(first.artifact.content["language"], "ko")

    def test_english_unqualified_legal_conclusions_are_blocked(self):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run",
            report_input=self.english_draft_input(),
        ).artifact.content
        for phrase in (
            "This invention is patentable.",
            "The proposed device does not infringe existing patents.",
            "The mechanism is novel.",
        ):
            candidate = json.loads(json.dumps(report))
            candidate["markdown"] += f"\n{phrase}"
            with self.subTest(phrase=phrase), self.assertRaisesRegex(ValueError, "unqualified"):
                _legal_language_check(candidate)
        qualified = json.loads(json.dumps(report))
        qualified["markdown"] += "\nWhether the invention is patentable is a question for counsel and not a legal conclusion of this report."
        _legal_language_check(qualified)

    def test_english_policy_file_rejects_any_drift(self):
        original = json.loads(POLICY_PATHS["en"].read_text(encoding="utf-8"))
        for field, mutation in (
            ("section_headings", "reorder"), ("report_disclaimer", "replace"),
            ("prohibited_unqualified_phrases", "append"),
        ):
            value = json.loads(json.dumps(original))
            if mutation == "reorder":
                value[field][0], value[field][1] = value[field][1], value[field][0]
            elif mutation == "replace":
                value[field] = "drifted"
            else:
                value[field].append("drifted-value")
            path = self.workspace / f"en-policy-{field}.json"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            with self.subTest(field=field), patch.dict(POLICY_PATHS, {"en": path}), self.assertRaisesRegex(ValueError, "frozen"):
                load_report_policy("en")
        with self.assertRaisesRegex(ValueError, "supported language"):
            load_report_policy("de")

    def test_english_redaction_produces_english_replacement(self):
        report = publish_report(
            self.connection, run_root=self.run_root, run_id="run",
            report_input=self.english_draft_input(sensitive=True),
        )
        review = run_review(
            self.connection, run_root=self.run_root, run_id="run",
            review_input=self.review_input(report.artifact.content_hash),
        )
        self.assertEqual(review.next_state, "reviewed")
        self.assertEqual(
            validate_and_complete(self.connection, run_root=self.run_root, run_id="run").next_state,
            "complete",
        )
        (self.workspace / "shares").mkdir()
        request = {
            "destination": "shares", "purpose": "attorney review", "recipient": "attorney@example.test",
            "report_hash": report.artifact.content_hash, "schema_version": "external-report-share-v1",
            "sensitive_fields": ["candidate.1.mechanism"],
        }
        with self.assertRaises(SensitiveDisclosureRequiredError) as caught:
            share_report(self.connection, run_root=self.run_root, run_id="run", share_input=request)
        gate = caught.exception.gate
        result = resolve_gate(self.connection, run_root=self.run_root, run_id="run", decision_input={
            "action": "redact", "actor": "user", "approval_scope": gate.approval_scope,
            "decisions": [], "gate_id": gate.gate_id, "plan": {}, "reason": "remove sensitive text",
            "schema_version": "gate-decision-input-v1", "subject_revision_hash": gate.subject_revision_hash,
        })
        self.assertEqual(result.next_state, "draft_ready")
        current = json.loads(self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='report'",
        ).fetchone()[0])
        self.assertEqual(current["language"], "en")
        self.assertNotIn("보정 메커니즘 1", current["markdown"])
        self.assertIn(REDACTION_REPLACEMENTS["en"], current["markdown"])
        self.assertTrue(all(
            item["replacement"] == REDACTION_REPLACEMENTS["en"] for item in current["redactions"]
        ))


if __name__ == "__main__":
    unittest.main()
