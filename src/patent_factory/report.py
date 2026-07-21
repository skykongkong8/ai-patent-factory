from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .audit import validate_audit_artifact
from .config import SimilarityConfig
from .database import FaultInjector
from .models import ArtifactRevision, RunState
from .privacy import assert_canaries_absent, credential_canaries
from .provenance import canonical_json, digest, normalize
from .state import StateError, StateStore, workspace_export_directories


REPORT_VERSION = "report-v1"
REPORT_INPUT_VERSION = "report-input-v1"
REPORT_INPUT_VERSION_V2 = "report-input-v2"
REPORT_LANGUAGES = ("en", "ko")
DEFAULT_REPORT_LANGUAGE = "en"
CITATION_RE = re.compile(r"\[@(ev_[0-9a-f]{16})\]")
# The literal hedging markers the renderer stamps onto bullets that assert no
# evidentiary support. A hedged bullet must never carry a prior-art citation
# token: doing so implies the hedged statement is backed by the cited document.
HEDGED_LABELS = (
    "[후보 가설]", "[창의적 제안]", "[프로필 기반 추론]", "[가설]",
    "[candidate hypothesis]", "[creative suggestion]", "[profile-based inference]", "[hypothesis]",
)
# The LEXICON line keys whose ko AND en templates carry one of HEDGED_LABELS.
# Hedging is a property of the RENDER SITE, not of the underlying claim label:
# the same field (e.g. technical_problem) is rendered hedged in section 3 and
# unhedged in section 6, so the decision cannot be delegated to the claim.
# test_us016_hedged_labels keeps this set from drifting away from the templates.
HEDGED_LINE_KEYS = frozenset({
    "components_line", "effects_line", "fit_line", "implementation_line",
    "problem_hypothesis_line", "unresolved_line",
})
POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "report-v1.0.0.json"
POLICY_PATHS = {
    "en": Path(__file__).resolve().parents[2] / "config" / "report-en-v1.0.0.json",
    "ko": POLICY_PATH,
}
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "report-ko.md"
TEMPLATE_PATHS = {
    "en": Path(__file__).resolve().parents[2] / "templates" / "report-en.md",
    "ko": TEMPLATE_PATH,
}
SECTION_HEADINGS = [
    "문서 목적·범위 및 면책", "사용자·발명자 기술 배경 및 도메인 맥락", "문제 및 기회 영역",
    "조사 범위 및 방법", "핵심 선행기술 환경", "최종 후보", "후보 비교 매트릭스",
    "최종 KIPRIS 유사도 위험 감사", "유사도 체크포인트 사용자 결정",
    "변리사 인계 질문 및 후속 조사", "출처·근거 부록",
]
SECTION_HEADINGS_EN = [
    "Document Purpose, Scope, and Disclaimer", "Inventor Technical Background and Domain Context",
    "Problem and Opportunity Areas", "Research Scope and Method", "Key Prior-Art Landscape",
    "Final Candidates", "Candidate Comparison Matrix", "Final KIPRIS Similarity-Risk Audit",
    "User Decisions at Similarity Checkpoints",
    "Patent-Attorney Handoff Questions and Follow-Up Investigations", "Source and Evidence Appendix",
]
SECTION_HEADINGS_BY_LANGUAGE = {"en": SECTION_HEADINGS_EN, "ko": SECTION_HEADINGS}
REPORT_DISCLAIMER = (
    "이 도구의 결과는 발명 정리 보조 자료이며 법률 자문이 아닙니다. 특허성, 신규성, 유효성 또는 "
    "비침해/FTO에 관한 법적 결론을 제공하지 않으며, 필요한 판단은 자격 있는 변리사·변호사와 확인해야 합니다."
)
REPORT_DISCLAIMER_EN = (
    "The output of this tool is invention-organizing support material and is not legal advice. "
    "It provides no legal conclusion on patentability, novelty, validity, or non-infringement/FTO; "
    "confirm any decision that matters with a qualified patent attorney."
)
SIMILARITY_DISCLAIMER = (
    "모든 유사도 수치는 검색된 코퍼스 범위 내의 잠정적 연구 보조 지표이며, "
    "법적 신규성·진보성·특허성·비침해/FTO 판단이 아닙니다."
)
SIMILARITY_DISCLAIMER_EN = (
    "All similarity figures are provisional research-aid indicators within the retrieved corpus only, "
    "and are not a legal determination of novelty, inventive step, patentability, or non-infringement/FTO."
)
REPORT_DISCLAIMERS = {"en": REPORT_DISCLAIMER_EN, "ko": REPORT_DISCLAIMER}
SIMILARITY_DISCLAIMERS = {"en": SIMILARITY_DISCLAIMER_EN, "ko": SIMILARITY_DISCLAIMER}
REQUIRED_REVIEW_CHECKS = [
    "citation_integrity", "decision_gate_coverage", "factual_grounding", "internal_consistency",
    "legal_language", "schema_completeness", "source_coverage",
]
PROHIBITED_UNQUALIFIED_PHRASES = [
    "특허 가능하다", "신규성이 있다", "진보성이 있다", "비침해이다", "FTO가 확보되었다",
    "freedom to operate", "patentable",
]
REDACTION_REPLACEMENTS = {"en": "[REDACTED: sensitive information]", "ko": "[삭제됨: 민감 정보]"}
# Renderer lexicon: every human-facing literal in _section_bodies, per language.
# The ko values are byte-exact transcriptions of the original renderer strings —
# the existing Korean golden/reconstruction tests pin them.
LEXICON: dict[str, dict[str, str]] = {
    "ko": {
        "adapters_line": "- 데이터베이스·사이트/어댑터: {adapters}",
        "aggregate_weights_line": "- 종합 가중치: {weights}",
        "appendix_line": (
            "- [@{evidence_id}] {title} — {identifier}{url} — 출처유형 {source_type}"
            " — 관찰일 {observed} — 콘텐츠해시 {content_hash}{legal_status} — 한계 {limitations}"
        ),
        "audit_failures_line": "- 최종 감사 실패 기록: {count}건",
        # Retrieved metadata only. The token is reproduced exactly as the source
        # emitted it and paired with an observation date; interpreting it (e.g.
        # rendering 소멸 as "만료" or "무효") would be a legal conclusion, which
        # CLAUDE.md section 6 forbids.
        "legal_status_clause": " — 출처 보고 법적상태 {status} (관찰일 {observed}, 원문 표기 그대로)",
        "audit_result_line": (
            "- {finalist_id}: 관측 위험 {r_obs} / 상한 {r_hi} / 커버리지 {coverage} / 분류 {outcome} / "
            "근접 문헌 {closest_identifier} {closest_title} / 상한 문헌 {upper_identifier} {upper_title}"
        ),
        "axes_heading": "- 평가 축:",
        "axis_line": (
            "  - {axis}: 신뢰도 {confidence} / 근거 {rationale} / "
            "커버리지 {coverage} / 한계 {limitations} / 공백 {gaps} {tokens}"
        ),
        "axis_figures_note": (
            "- 축 수치는 표시하지 않습니다. 작성자가 입력한 값이며 도구가 계산하지 않고, "
            "후보 순서를 정하는 데에도 사용되지 않습니다."
        ),
        "checkpoint_action_line": "- 체크포인트 결정: {action} — {reason}",
        "checkpoint_feedback_line": "  - {finalist_id}: 흥미로운 점 {interesting} / 지루한 점 {boring}",
        "closest_identifier_fallback": "식별자 없음",
        "comparison_axis_line": (
            "- {axis}: {rationale} 커버리지: {coverage}. 커버리지 한계: {limitations}. 공백: {gaps}. {tokens}"
        ),
        "delta_heading": "- 근접 검색 문헌과 다른 것으로 기술된 특징:",
        "delta_line": "  - {feature} — 합성 조작 {method}; {axis} 축에서 다른 것으로 기술됨 [@{closest}]",
        "delta_none": "- 근접 검색 문헌 대비 기록된 상이 특징이 없습니다.",
        "ranking_basis_line": (
            "- 아래 순서는 후보별로 기록된 선정 우선순위를 따릅니다. 축 점수에 따른 순위가 아닙니다."
        ),
        "closest_line": "- 가장 가까운 선행기술과의 관계: {title} ({identifier})",
        "closest_title_fallback": "확인된 근접 문헌 없음",
        "components_line": "- 핵심 구성요소와 상호작용 [후보 가설]: {components}; {interactions} {tokens}",
        "corpus_cap_line": "- 코퍼스 상한: 후보별 {limit}건",
        "counter_line": "  - 반론·한계: {counterargument}",
        "diff_fallback": "근거 범위에서 확인 필요",
        "differentiated_line": "- 차별화 특징: {differentiated}",
        "effects_line": "- 기대 기술 효과 [창의적 제안]: {effects} {tokens}",
        "evidence_count_line": "- 증거 기록 수 (조사 단계 기준): {count}",
        "feature_weights_line": "- 특징 가중치: {weights}",
        "fit_line": "- 사용자·도메인 적합성 [프로필 기반 추론]: {domain} {tokens}",
        "followup_heading": "### 권고 후속 조사",
        "handoff_heading": "### 변리사 인계 질문",
        "identifier_fallback": "식별자 미상",
        "implementation_line": "- 구현·검증 예 [창의적 제안]: {example}; {validation} {tokens}",
        "landscape_line": "- {title} ({identifier}) [@{evidence_id}]",
        "limitations_line": "- 알려진 한계: {limitations}",
        "mechanism_line": "- 제안 메커니즘: {mechanism} {tokens}",
        "no_decision_body": "해당 없음: 현재 감사에서 과도 유사도 사용자 체크포인트가 발생하지 않았습니다.",
        "no_separate_record": "별도 기록 없음",
        "none": "없음",
        "none_recorded": "기록 없음",
        "pair_line": (
            "  - {evidence_id} [@{evidence_id}]: 버전={version}, T={T}, F={F}, C={C}, "
            "D={D}, Q={Q}, R_obs={r_obs}, R_hi={r_hi}, 일치={matched}, 차이={differentiated}"
        ),
        "problem_hypothesis_line": "- [후보 가설] {problem} {tokens}",
        "problem_line": "- 문제: {problem} {tokens}",
        "profile_line": "- [프로필 근거: {labels}; 출처: {sources}] {field}: {value}",
        "profile_source_fallback": "프로필 기록",
        "privacy_note": "[개인정보 최소화] 사용자가 명시적으로 선택한 현재 기술 프로필 필드만 출처 유형과 함께 표시합니다.",
        "purpose_line": "- 목적과 범위: 변리사 검토 전 발명 아이디어와 근거를 구조화하는 내부 보고서",
        "query_count_line": "- 조사 기록 수: {count}",
        "query_strategy_fallback": "저장된 질의 지문과 이중언어 확장 사용",
        "query_strategy_line": "- 질의 전략: {strategy}",
        "report_date_line": "- 작성일: {date}",
        "scoring_policy_line": "- 점수 정책: {version}",
        "search_dates_line": "- 검색일: {dates}",
        "title_fallback": "제목 미상",
        "unknown": "미상",
        "unresolved_fallback": "추가 확인 필요",
        "unresolved_line": "- 불확실성·후속 질문 [가설]: {questions} {tokens}",
        "version_line": "- 워크플로/도구 버전: {report_version} / {policy_version}",
    },
    "en": {
        "adapters_line": "- Databases/sites/adapters: {adapters}",
        "aggregate_weights_line": "- Aggregate weights: {weights}",
        "appendix_line": (
            "- [@{evidence_id}] {title} — {identifier}{url} — source type {source_type}"
            " — observed {observed} — content hash {content_hash}{legal_status} — limitations {limitations}"
        ),
        "audit_failures_line": "- Final audit failure records: {count}",
        # Retrieved metadata only. The token is reproduced exactly as the source
        # emitted it and paired with an observation date; interpreting it (e.g.
        # rendering 소멸 as "expired" or "invalid") would be a legal conclusion,
        # which CLAUDE.md section 6 forbids.
        "legal_status_clause": " — source-reported legal status {status} (observed {observed}, verbatim source token)",
        "audit_result_line": (
            "- {finalist_id}: observed risk {r_obs} / upper bound {r_hi} / coverage {coverage} / classification {outcome} / "
            "closest reference {closest_identifier} {closest_title} / upper-bound reference {upper_identifier} {upper_title}"
        ),
        "axes_heading": "- Evaluation axes:",
        "axis_line": (
            "  - {axis}: confidence {confidence} / rationale {rationale} / "
            "coverage {coverage} / limitations {limitations} / gaps {gaps} {tokens}"
        ),
        "axis_figures_note": (
            "- Axis figures are not shown: they are author-supplied inputs, are never computed "
            "by this tool, and never determine candidate order."
        ),
        "checkpoint_action_line": "- Checkpoint decision: {action} — {reason}",
        "checkpoint_feedback_line": "  - {finalist_id}: interesting {interesting} / boring {boring}",
        "closest_identifier_fallback": "no identifier",
        "comparison_axis_line": (
            "- {axis}: {rationale} Coverage: {coverage}. Coverage limitations: {limitations}. Gaps: {gaps}. {tokens}"
        ),
        "delta_heading": "- Described differences from the closest retrieved reference:",
        "delta_line": (
            "  - {feature} — synthesis operation {method}; described as differing on the {axis} axis [@{closest}]"
        ),
        "delta_none": "- No differing features are recorded against the closest retrieved reference.",
        "ranking_basis_line": (
            "- Ordering below follows the shortlist priority recorded for each candidate. "
            "It is not an ordering by axis score."
        ),
        "closest_line": "- Relationship to the closest prior art: {title} ({identifier})",
        "closest_title_fallback": "no close reference identified",
        "components_line": "- Key components and interactions [candidate hypothesis]: {components}; {interactions} {tokens}",
        "corpus_cap_line": "- Corpus cap: {limit} records per finalist",
        "counter_line": "  - Counterarguments and limits: {counterargument}",
        "diff_fallback": "requires confirmation within the evidence scope",
        "differentiated_line": "- Differentiating features: {differentiated}",
        "effects_line": "- Expected technical effects [creative suggestion]: {effects} {tokens}",
        "evidence_count_line": "- Evidence record count (research-stage scope): {count}",
        "feature_weights_line": "- Feature weights: {weights}",
        "fit_line": "- User/domain fit [profile-based inference]: {domain} {tokens}",
        "followup_heading": "### Recommended follow-up investigations",
        "handoff_heading": "### Patent-attorney handoff questions",
        "identifier_fallback": "identifier unknown",
        "implementation_line": "- Implementation and validation example [creative suggestion]: {example}; {validation} {tokens}",
        "landscape_line": "- {title} ({identifier}) [@{evidence_id}]",
        "limitations_line": "- Known limitations: {limitations}",
        "mechanism_line": "- Proposed mechanism: {mechanism} {tokens}",
        "no_decision_body": "Not applicable: no excessive-similarity user checkpoint arose in the current audit.",
        "no_separate_record": "none recorded",
        "none": "none",
        "none_recorded": "none recorded",
        "pair_line": (
            "  - {evidence_id} [@{evidence_id}]: version={version}, T={T}, F={F}, C={C}, "
            "D={D}, Q={Q}, R_obs={r_obs}, R_hi={r_hi}, matched={matched}, differentiated={differentiated}"
        ),
        "problem_hypothesis_line": "- [candidate hypothesis] {problem} {tokens}",
        "problem_line": "- Problem: {problem} {tokens}",
        "profile_line": "- [profile basis: {labels}; source: {sources}] {field}: {value}",
        "profile_source_fallback": "profile record",
        "privacy_note": "[Privacy minimization] Only the current technical profile fields the user explicitly selected are shown, with their source types.",
        "purpose_line": "- Purpose and scope: an internal report structuring invention ideas and evidence before patent-attorney review",
        "query_count_line": "- Query record count: {count}",
        "query_strategy_fallback": "stored query fingerprints with bilingual expansion",
        "query_strategy_line": "- Query strategy: {strategy}",
        "report_date_line": "- Date: {date}",
        "scoring_policy_line": "- Scoring policy: {version}",
        "search_dates_line": "- Search dates: {dates}",
        "title_fallback": "untitled",
        "unknown": "unknown",
        "unresolved_fallback": "further confirmation required",
        "unresolved_line": "- Uncertainties and follow-up questions [hypothesis]: {questions} {tokens}",
        "version_line": "- Workflow/tool version: {report_version} / {policy_version}",
    },
}


@dataclass(frozen=True)
class ReportRun:
    run_id: str
    prior_state: str
    next_state: str
    artifact: ArtifactRevision
    export_path: str
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ids": [self.artifact.revision_id],
            "command": "draft",
            "export_path": self.export_path,
            "language": self.artifact.content.get("language"),
            "next_state": self.next_state,
            "prior_state": self.prior_state,
            "replayed": self.replayed,
            "report_hash": self.artifact.content_hash,
            "run_id": self.run_id,
            "status": self.next_state,
        }


def load_report_policy(language: str = "ko") -> dict[str, Any]:
    if language not in REPORT_LANGUAGES:
        raise ValueError("report_policy: supported language required")
    # Keep the module-level POLICY_PATH the ko patch point so frozen-contract
    # tests (and any caller overriding it) keep working unchanged.
    path = POLICY_PATH if language == "ko" else POLICY_PATHS[language]
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "language", "prohibited_unqualified_phrases", "report_disclaimer",
        "required_review_checks", "section_headings", "similarity_disclaimer", "version",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("version") != "report-policy-v1.0.0":
        raise ValueError("report_policy: exact report-policy-v1.0.0 fields required")
    if (
        value.get("language") != language
        or value.get("section_headings") != SECTION_HEADINGS_BY_LANGUAGE[language]
        or value.get("report_disclaimer") != REPORT_DISCLAIMERS[language]
        or value.get("similarity_disclaimer") != SIMILARITY_DISCLAIMERS[language]
        or value.get("required_review_checks") != REQUIRED_REVIEW_CHECKS
        or value.get("prohibited_unqualified_phrases") != PROHIBITED_UNQUALIFIED_PHRASES
    ):
        raise ValueError("report_policy: frozen report-policy-v1.0.0 contract required")
    return normalize(value)


def _text(value: Any, path: str) -> str:
    item = normalize(value)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{path}: non-empty string required")
    return item


def _texts(value: Any, path: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: array required")
    items = [_text(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if not allow_empty and not items:
        raise ValueError(f"{path}: at least one item required")
    if len(set(items)) != len(items):
        raise ValueError(f"{path}: duplicate items are not allowed")
    return items


def _current_artifact(connection: sqlite3.Connection, run_id: str, kind: str) -> tuple[sqlite3.Row, dict[str, Any]]:
    row = connection.execute(
        "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
        "WHERE ar.run_id=? AND ca.kind=? AND ar.stale=0", (run_id, kind),
    ).fetchone()
    if row is None:
        raise StateError(f"report requires current {kind}")
    value = json.loads(row["content_json"])
    if not isinstance(value, dict):
        raise StateError(f"report requires object {kind}")
    return row, value


def _report_state(connection: sqlite3.Connection, run_root: Path) -> tuple[StateStore, Path]:
    root = Path(run_root).absolute()
    if not root.is_dir() or stat.S_ISLNK(root.lstat().st_mode):
        raise ValueError("report_export: safe run directory required")
    directory = root / "report-exports"
    if directory.exists() and (not directory.is_dir() or stat.S_ISLNK(directory.lstat().st_mode)):
        raise ValueError("report_export: unsafe export directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700, follow_symlinks=False)
    except OSError:
        pass
    directories = workspace_export_directories(connection, root, (directory,))
    return StateStore(connection, export_directories=directories), directory


def validate_report_input(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "drafter", "handoff_questions", "recommended_investigations", "report_date",
        "profile_fields", "revision", "schema_version", "sensitive_disclosures",
    }
    if not isinstance(value, Mapping):
        raise ValueError("report_input: exact report-input-v1 fields required")
    version = value.get("schema_version")
    if version == REPORT_INPUT_VERSION:
        if set(value) != required:
            raise ValueError("report_input: exact report-input-v1 fields required")
        language = "ko"
    elif version == REPORT_INPUT_VERSION_V2:
        if set(value) != required | {"language"}:
            raise ValueError("report_input: exact report-input-v2 fields required")
        language = value.get("language")
        if language not in REPORT_LANGUAGES:
            raise ValueError("report_input.language: en or ko required")
    else:
        raise ValueError("report_input: exact report-input-v1 fields required")
    drafter = value["drafter"]
    if not isinstance(drafter, Mapping) or set(drafter) != {"id", "pass_id", "type"}:
        raise ValueError("report_input.drafter: exact identity fields required")
    resolved_drafter = {
        "id": _text(drafter["id"], "report_input.drafter.id"),
        "pass_id": _text(drafter["pass_id"], "report_input.drafter.pass_id"),
        "type": _text(drafter["type"], "report_input.drafter.type"),
    }
    if resolved_drafter["type"] not in {"agent", "human"}:
        raise ValueError("report_input.drafter.type: agent or human required")
    report_date = _text(value["report_date"], "report_input.report_date")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date) is None:
        raise ValueError("report_input.report_date: YYYY-MM-DD required")
    disclosures = value["sensitive_disclosures"]
    if not isinstance(disclosures, list):
        raise ValueError("report_input.sensitive_disclosures: array required")
    resolved_disclosures = []
    for index, raw in enumerate(disclosures):
        path = f"report_input.sensitive_disclosures[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"field", "reason", "text"}:
            raise ValueError(f"{path}: exact field, reason, and text required")
        resolved_disclosures.append({
            "field": _text(raw["field"], f"{path}.field"),
            "reason": _text(raw["reason"], f"{path}.reason"),
            "text": _text(raw["text"], f"{path}.text"),
        })
    if len({item["field"] for item in resolved_disclosures}) != len(resolved_disclosures):
        raise ValueError("report_input.sensitive_disclosures: duplicate fields")
    resolved_disclosures.sort(key=lambda item: item["field"])
    revision = value["revision"]
    if revision is not None:
        if not isinstance(revision, Mapping) or set(revision) != {"reason", "report_hash", "review_hash"}:
            raise ValueError("report_input.revision: exact revision binding required")
        revision = {
            "reason": _text(revision["reason"], "report_input.revision.reason"),
            "report_hash": _text(revision["report_hash"], "report_input.revision.report_hash"),
            "review_hash": _text(revision["review_hash"], "report_input.revision.review_hash"),
        }
    return normalize({
        "drafter": resolved_drafter,
        "handoff_questions": _texts(value["handoff_questions"], "report_input.handoff_questions"),
        "language": language,
        "profile_fields": _texts(value["profile_fields"], "report_input.profile_fields"),
        "recommended_investigations": _texts(value["recommended_investigations"], "report_input.recommended_investigations"),
        "report_date": report_date,
        "revision": revision,
        "schema_version": REPORT_INPUT_VERSION_V2,
        "sensitive_disclosures": resolved_disclosures,
    })


def _evidence_map(
    research: Mapping[str, Any], corpus: Mapping[str, Any], *,
    connection: sqlite3.Connection | None = None, run_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in research.get("evidence", []):
        if isinstance(raw, Mapping) and isinstance(raw.get("evidence_id"), str):
            record = json.loads(raw["record_json"]) if isinstance(raw.get("record_json"), str) else raw.get("record_json", {})
            result[raw["evidence_id"]] = {
                "canonical_url": raw.get("canonical_url"), "content_hash": raw.get("content_hash"),
                "limitations": record.get("limitations", []) if isinstance(record, Mapping) else [],
                "observation_date": str(raw.get("created_at", ""))[:10],
                "evidence_id": raw["evidence_id"], "identifier": raw.get("original_identifier"),
                "record": record, "source_type": raw.get("source_type"), "title": raw.get("title"),
            }
    for group in corpus.get("corpora", []):
        if not isinstance(group, Mapping):
            continue
        for raw in group.get("records", []):
            if not isinstance(raw, Mapping) or not isinstance(raw.get("evidence_id"), str):
                continue
            if raw["evidence_id"] in result:
                # The research bundle (above) and the authoritative evidence_records
                # loop (below) carry richer provenance; the audit-corpus projection
                # only fills identifiers that are otherwise unknown.
                continue
            record = raw.get("record", {})
            result[raw["evidence_id"]] = {
                "canonical_url": record.get("canonical_url") if isinstance(record, Mapping) else None,
                "content_hash": raw.get("content_hash"), "evidence_id": raw["evidence_id"],
                "identifier": (record.get("application_number") or record.get("publication_number") or raw.get("application_identity")) if isinstance(record, Mapping) else raw.get("application_identity"),
                "limitations": record.get("limitations", []) if isinstance(record, Mapping) else [],
                "observation_date": "",
                "record": record, "source_type": "kipris_audit",
                "title": record.get("title") if isinstance(record, Mapping) else "",
            }
    if connection is not None and run_id is not None:
        for raw in connection.execute(
            "SELECT * FROM evidence_records WHERE run_id=? ORDER BY evidence_id", (run_id,),
        ):
            record = json.loads(raw["record_json"])
            result[raw["evidence_id"]] = {
                "canonical_url": raw["canonical_url"], "content_hash": raw["content_hash"],
                "evidence_id": raw["evidence_id"], "identifier": raw["original_identifier"],
                "limitations": record.get("limitations", []) if isinstance(record, Mapping) else [],
                "observation_date": str(raw["created_at"])[:10], "record": record,
                "source_type": raw["source_type"], "title": raw["title"],
            }
    return result


def _field_reference_tokens(candidate: Mapping[str, Any], field: str) -> str:
    """Render the citation tokens bound to ONE candidate field.

    Per-field references live on ``Candidate.claims[*].evidence_references``
    (ideation.CandidateClaim) rather than in a parallel field -> references map.
    ``claims`` is already the field-keyed structure — it carries ``field`` plus
    the epistemic ``Claim`` for that field — so a sibling map would duplicate the
    same key space and could silently disagree with it about which fields exist.
    Keeping the references on the claim makes the label and its support one
    atomic unit, and reuses the existing claim/evidence cross-check in
    ideation.Candidate.from_dict instead of adding a second one.

    A field with no claim, or a claim with an empty reference list, renders no
    token at all — an empty per-field binding is legitimate, not an error.
    """

    ids = sorted({
        reference["evidence_id"]
        for entry in candidate.get("claims", []) or ()
        if isinstance(entry, Mapping) and entry.get("field") == field
        for reference in entry.get("evidence_references", []) or ()
        if isinstance(reference, Mapping) and isinstance(reference.get("evidence_id"), str)
    })
    return " ".join(f"[@{item}]" for item in ids)


def _cited_ids(candidates: Iterable[Mapping[str, Any]], finalists: Iterable[Mapping[str, Any]], audit: Mapping[str, Any]) -> list[str]:
    cited: set[str] = set()
    for candidate in candidates:
        for reference in candidate.get("evidence_references", []):
            if isinstance(reference, Mapping) and isinstance(reference.get("evidence_id"), str):
                cited.add(reference["evidence_id"])
    for finalist in finalists:
        for axis in finalist.get("axes", []):
            if not isinstance(axis, Mapping):
                continue
            for name in ("supporting_evidence_references", "contrary_evidence_references"):
                for reference in axis.get(name, []):
                    if isinstance(reference, Mapping) and isinstance(reference.get("evidence_id"), str):
                        cited.add(reference["evidence_id"])
    for result in audit.get("results", []):
        if not isinstance(result, Mapping):
            continue
        for score in result.get("pair_scores", []):
            if isinstance(score, Mapping) and isinstance(score.get("evidence_id"), str):
                cited.add(score["evidence_id"])
    return sorted(cited)


def _excessive_decision(
    connection: sqlite3.Connection, run_id: str, audit_hash: str, audit: Mapping[str, Any],
) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
    affected = sorted(
        item["finalist_id"] for item in audit.get("results", [])
        if isinstance(item, Mapping) and item.get("outcome") == "decision_required"
    )
    if not affected:
        return None, None
    matches = []
    for row in connection.execute(
        "SELECT * FROM artifact_revisions WHERE run_id=? AND kind='gate_resolution' AND stale=0",
        (run_id,),
    ):
        content = json.loads(row["content_json"])
        if content.get("gate_kind") == "excessive_similarity" and content.get("audit_hash") == audit_hash:
            matches.append((row, content))
    if len(matches) != 1:
        raise StateError("report requires exactly one current excessive-similarity decision")
    row, content = matches[0]
    decisions = content.get("decisions")
    if (
        content.get("action") != "retain_with_warning"
        or content.get("subject_revision_hash") != audit_hash
        or not isinstance(decisions, list)
        or sorted(item.get("finalist_id") for item in decisions if isinstance(item, Mapping)) != affected
        or any(item.get("action") != "retain_with_warning" or not normalize(item.get("warning", "")) for item in decisions)
    ):
        raise StateError("report requires a complete current excessive-similarity decision")
    decision_row = connection.execute(
        "SELECT gd.*,ge.approval_scope_json,ge.subject_revision_hash AS envelope_subject,"
        "ge.suspended_operation AS envelope_operation,ge.status AS envelope_status "
        "FROM gate_decisions gd JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
        "WHERE gd.decision_id=? AND gd.run_id=? AND ge.kind='excessive_similarity'",
        (content.get("decision_id"), run_id),
    ).fetchone()
    if (
        decision_row is None or decision_row["stale"] or not decision_row["used_at"]
        or not decision_row["consumed_by_event_id"] or decision_row["action"] != "retain_with_warning"
        or decision_row["subject_revision_hash"] != audit_hash
        or decision_row["envelope_subject"] != audit_hash
        or decision_row["approval_scope_hash"] != content.get("approval_scope_hash")
        or decision_row["envelope_status"] != "decided"
    ):
        raise StateError("report excessive decision row is stale or incompletely consumed")
    return row, content


def _checkpoint_decision(
    connection: sqlite3.Connection, run_id: str, audit_hash: str, audit: Mapping[str, Any],
    matches: list[tuple[sqlite3.Row, dict[str, Any]]],
) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
    """Mirror ``_excessive_decision`` for a ``post_audit_checkpoint`` resolution.

    Unlike the excessive path, this is entered on BOTH clean and breaching
    audits (the checkpoint is always raised), so it never short-circuits to
    ``None, None`` on an empty ``affected`` set — the caller already confirmed
    a checkpoint resolution artifact bound to this audit_hash exists before
    calling this.
    """
    affected = sorted(
        item["finalist_id"] for item in audit.get("results", [])
        if isinstance(item, Mapping) and item.get("outcome") == "decision_required"
    )
    if len(matches) != 1:
        raise StateError("report requires exactly one current checkpoint decision")
    row, content = matches[0]
    decisions = content.get("decisions")
    if (
        content.get("action") != "approve"
        or content.get("subject_revision_hash") != audit_hash
        or not isinstance(decisions, list)
        or sorted(item.get("finalist_id") for item in decisions if isinstance(item, Mapping)) != affected
        or any(item.get("action") != "retain_with_warning" or not normalize(item.get("warning", "")) for item in decisions)
    ):
        raise StateError("report requires a complete current checkpoint decision")
    decision_row = connection.execute(
        "SELECT gd.*,ge.approval_scope_json,ge.subject_revision_hash AS envelope_subject,"
        "ge.suspended_operation AS envelope_operation,ge.status AS envelope_status "
        "FROM gate_decisions gd JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
        "WHERE gd.decision_id=? AND gd.run_id=? AND ge.kind='post_audit_checkpoint'",
        (content.get("decision_id"), run_id),
    ).fetchone()
    if (
        decision_row is None or decision_row["stale"] or not decision_row["used_at"]
        or not decision_row["consumed_by_event_id"] or decision_row["action"] != "approve"
        or decision_row["subject_revision_hash"] != audit_hash
        or decision_row["envelope_subject"] != audit_hash
        or decision_row["approval_scope_hash"] != content.get("approval_scope_hash")
        or decision_row["envelope_status"] != "decided"
    ):
        raise StateError("report checkpoint decision row is stale or incompletely consumed")
    return row, content


def _bound_decision(
    connection: sqlite3.Connection, run_id: str, audit_hash: str, audit: Mapping[str, Any],
) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
    """Resolve the decision bound to the CURRENT audit, by the gate kind that raised it.

    A breaching batch approved through the checkpoint has no
    ``excessive_similarity`` resolution to match (RF#1) — ``affected`` alone
    cannot tell a legacy excessive run from a checkpoint-breaching run, so the
    discriminator is which gate kind actually produced a resolution bound to
    this audit_hash. Checkpoint resolutions match on
    ``content.subject_revision_hash`` (every resolution carries it); legacy
    excessive resolutions match on ``content.audit_hash`` (only they carry it).
    """
    checkpoint_matches = []
    for row in connection.execute(
        "SELECT * FROM artifact_revisions WHERE run_id=? AND kind='gate_resolution' AND stale=0",
        (run_id,),
    ):
        content = json.loads(row["content_json"])
        if content.get("gate_kind") == "post_audit_checkpoint" and content.get("subject_revision_hash") == audit_hash:
            checkpoint_matches.append((row, content))
    if checkpoint_matches:
        return _checkpoint_decision(connection, run_id, audit_hash, audit, checkpoint_matches)
    return _excessive_decision(connection, run_id, audit_hash, audit)


def _section_bodies(
    *, policy: Mapping[str, Any], report_input: Mapping[str, Any], profile: Mapping[str, Any],
    research: Mapping[str, Any], candidates: list[Mapping[str, Any]], finalists: list[Mapping[str, Any]],
    corpus: Mapping[str, Any], audit: Mapping[str, Any], decision: Mapping[str, Any] | None,
    evidence: Mapping[str, Mapping[str, Any]], cited_ids: list[str], scorer: Mapping[str, Any],
    language: str = "ko",
    feature_descriptions: Mapping[str, Mapping[str, str]] | None = None,
) -> list[str]:
    lex = LEXICON[language]
    descriptions = feature_descriptions or {}

    def bullet(key: str, candidate: Mapping[str, Any], field: str, **values: Any) -> str:
        """Render one candidate bullet with the citations bound to ITS own field.

        A bullet whose template carries a hedged label renders no prior-art
        token regardless of what the field binds, because the label states the
        line is not evidence-backed.
        """

        tokens = "" if key in HEDGED_LINE_KEYS else _field_reference_tokens(candidate, field)
        return lex[key].format(tokens=tokens, **values).rstrip()

    def described(finalist_id: Any, feature_ids: Any) -> list[str]:
        table = descriptions.get(finalist_id, {})
        return [table.get(item, item) for item in (feature_ids or [])]
    profile_value = profile.get("profile", {})
    profile_facts = profile_value.get("facts", {}) if isinstance(profile_value, Mapping) else {}
    profile_lines = []
    forbidden_profile_fields = {"api_key", "credential", "email", "name", "phone", "raw_document", "secret"}
    for field in report_input["profile_fields"]:
        if field.casefold() in forbidden_profile_fields or field not in profile_facts:
            raise ValueError("report_input.profile_fields: only explicit current privacy-safe technical fields may be included")
        entry = profile_facts[field]
        if not isinstance(entry, Mapping) or "value" not in entry or not isinstance(entry.get("claims"), list) or not entry["claims"]:
            raise ValueError("report profile field requires authoritative provenance claims")
        labels = sorted({str(item.get("label", "")) for item in entry["claims"] if isinstance(item, Mapping)})
        sources = sorted({str(item.get("source_id", "")) for item in entry["claims"] if isinstance(item, Mapping) and item.get("source_id")})
        value = entry["value"] if isinstance(entry["value"], str) else canonical_json(entry["value"])
        profile_lines.append(lex["profile_line"].format(
            labels=",".join(labels), sources=",".join(sources) or lex["profile_source_fallback"],
            field=field, value=value,
        ))
    candidate_by_id = {item.get("candidate_id"): item for item in candidates}
    audit_by_finalist = {
        item.get("finalist_id"): item for item in audit.get("results", []) if isinstance(item, Mapping)
    }
    finalist_lines = []
    # Section 7 is prose, not a score table. The axis scores it used to tabulate
    # are validated pass-throughs (evaluation.EvaluationAxis): never computed
    # here and never used to order finalists, which evaluation.py ranks by
    # (priority, candidate_id). A bare number under a column headed
    # "Differentiation" reads as a novelty assessment whoever authored it, so the
    # section states its ranking basis and renders the axis fields as prose.
    comparison_lines = [lex["ranking_basis_line"], lex["axis_figures_note"]]
    for finalist in sorted(finalists, key=lambda item: (item.get("rank", 0), item.get("finalist_id", ""))):
        candidate = candidate_by_id.get(finalist.get("candidate_id"), {})
        # Candidate-level aggregate. Used ONLY by the section 7 candidate
        # heading, which summarises the whole candidate. Individual bullets bind
        # their own field references via _field_reference_tokens.
        refs = sorted({ref.get("evidence_id") for ref in candidate.get("evidence_references", []) if isinstance(ref, Mapping) and ref.get("evidence_id")})
        tokens = " ".join(f"[@{item}]" for item in refs)
        audit_result = audit_by_finalist.get(finalist.get("finalist_id"), {})
        closest_id = audit_result.get("closest_reference_id")
        closest = evidence.get(closest_id, {}) if closest_id else {}
        pair = next((item for item in audit_result.get("pair_scores", []) if item.get("evidence_id") == closest_id), None)
        differentiated = (
            ", ".join(described(finalist.get("finalist_id"), pair.get("differentiated_feature_ids", [])))
            if pair else lex["diff_fallback"]
        )
        finalist_lines.extend([
            f"### {finalist.get('rank')}. {candidate.get('title', '')}",
            bullet("problem_line", candidate, "technical_problem", problem=candidate.get("technical_problem", "")),
            bullet("mechanism_line", candidate, "mechanism", mechanism=candidate.get("mechanism", "")),
            bullet(
                "components_line", candidate, "components",
                components=", ".join(candidate.get("components", [])),
                interactions=", ".join(candidate.get("interactions", [])),
            ),
            bullet(
                "effects_line", candidate, "expected_effects",
                effects=", ".join(candidate.get("expected_effects", [])),
            ),
            bullet("fit_line", candidate, "domain", domain=candidate.get("domain", "")),
            lex["closest_line"].format(
                title=closest.get("title") or lex["closest_title_fallback"],
                identifier=closest.get("identifier") or lex["closest_identifier_fallback"],
            )
            + (f" [@{closest_id}]" if closest_id else ""),
            lex["differentiated_line"].format(differentiated=differentiated)
            + (f" [@{closest_id}]" if closest_id else ""),
            bullet(
                "implementation_line", candidate, "implementation_example",
                example=candidate.get("implementation_example", ""),
                validation=candidate.get("measurable_validation", ""),
            ),
            bullet(
                "unresolved_line", candidate, "unresolved_questions",
                questions=", ".join(candidate.get("unresolved_questions", [])) or lex["unresolved_fallback"],
            ),
        ])
        axes = {axis.get("axis"): axis for axis in finalist.get("axes", []) if isinstance(axis, Mapping)}
        finalist_lines.append(lex["axes_heading"])
        comparison_lines.append(f"### {finalist.get('rank')}. {candidate.get('title', '')} {tokens}".rstrip())
        for axis_name in ("differentiation", "technical_feasibility", "utility_significance"):
            axis = axes.get(axis_name, {})
            axis_refs = sorted({
                ref.get("evidence_id")
                for field in ("supporting_evidence_references", "contrary_evidence_references")
                for ref in axis.get(field, []) if isinstance(ref, Mapping) and ref.get("evidence_id")
            })
            axis_tokens = " ".join(f"[@{item}]" for item in axis_refs)
            axis_limitations = ", ".join(axis.get("coverage_limitations", [])) or lex["none"]
            axis_gaps = ", ".join(axis.get("gaps", [])) or lex["none"]
            finalist_lines.append(
                lex["axis_line"].format(
                    axis=axis_name, confidence=axis.get("confidence", ""),
                    rationale=axis.get("rationale", ""), coverage=axis.get("coverage_assessment", ""),
                    limitations=axis_limitations, gaps=axis_gaps, tokens=axis_tokens,
                ).rstrip()
            )
            comparison_lines.append(
                lex["comparison_axis_line"].format(
                    axis=axis_name, rationale=axis.get("rationale", ""),
                    coverage=axis.get("coverage_assessment", ""),
                    limitations=axis_limitations, gaps=axis_gaps, tokens=axis_tokens,
                ).rstrip()
            )
        # Per-feature delta narrative: strictly descriptive. It reports which
        # features the similarity feature map recorded as differing from the
        # closest retrieved reference, and under which synthesis operation the
        # candidate was authored. Features are not axis-tagged in the frozen
        # candidate input schema, so the line names `differentiation` — the only
        # axis these deltas bear on — rather than inventing a per-feature axis.
        trace = candidate.get("synthesis_trace")
        method = trace.get("method", "") if isinstance(trace, Mapping) else ""
        delta_features = described(
            finalist.get("finalist_id"), pair.get("differentiated_feature_ids", []),
        ) if pair else []
        if delta_features and closest_id:
            comparison_lines.append(lex["delta_heading"])
            comparison_lines.extend(
                lex["delta_line"].format(
                    feature=item, method=method, axis="differentiation", closest=closest_id,
                )
                for item in delta_features
            )
        else:
            comparison_lines.append(lex["delta_none"])
    landscape = []
    for evidence_id in cited_ids:
        item = evidence[evidence_id]
        landscape.append(lex["landscape_line"].format(
            title=item.get("title") or lex["title_fallback"],
            identifier=item.get("identifier") or lex["identifier_fallback"], evidence_id=evidence_id,
        ))
    resolved_scorer = scorer.get("config", scorer) if isinstance(scorer, Mapping) else {}
    audit_lines = [
        policy["similarity_disclaimer"],
        lex["scoring_policy_line"].format(version=resolved_scorer.get("version", "simrisk-v1.0.0")),
        lex["aggregate_weights_line"].format(weights=canonical_json(resolved_scorer.get("aggregate_weights", {}))),
        lex["feature_weights_line"].format(weights=canonical_json(resolved_scorer.get("feature_weights", {}))),
        lex["corpus_cap_line"].format(limit=resolved_scorer.get("corpus_limit", 100)),
    ]
    for result in sorted(audit.get("results", []), key=lambda item: item.get("finalist_id", "")):
        closest = evidence.get(result.get("closest_reference_id"), {})
        upper = evidence.get(result.get("upper_bound_reference_id"), {})
        audit_lines.append(lex["audit_result_line"].format(
            finalist_id=result.get("finalist_id"), r_obs=result.get("r_obs"), r_hi=result.get("r_hi"),
            coverage=result.get("coverage"), outcome=result.get("outcome"),
            closest_identifier=closest.get("identifier") or lex["none"], closest_title=closest.get("title") or "",
            upper_identifier=upper.get("identifier") or lex["none"], upper_title=upper.get("title") or "",
        ))
        for pair in result.get("pair_scores", []):
            audit_lines.append(lex["pair_line"].format(
                evidence_id=pair.get("evidence_id"), version=pair.get("version"),
                T=pair.get("T"), F=pair.get("F"), C=pair.get("C"), D=pair.get("D"), Q=pair.get("Q"),
                r_obs=pair.get("r_obs"), r_hi=pair.get("r_hi"),
                matched=",".join(described(result.get("finalist_id"), pair.get("matched_feature_ids", []))) or lex["none"],
                differentiated=",".join(described(result.get("finalist_id"), pair.get("differentiated_feature_ids", []))) or lex["none"],
            ))
        audit_lines.append(lex["counter_line"].format(counterargument=result.get("counterargument", "")))
    if decision is None:
        decision_body = lex["no_decision_body"]
    elif decision.get("gate_kind") == "post_audit_checkpoint":
        # Render honestly: the inventor's action/reason plus per-finalist
        # feedback, and — only when breaches exist — the same retain-with-
        # warning lines the legacy excessive branch renders below. No legal
        # conclusion: action/feedback are framed as the inventor's research-aid
        # decision, never as a novelty/validity finding.
        checkpoint_lines = [lex["checkpoint_action_line"].format(action=decision["action"], reason=decision["reason"])]
        checkpoint_lines.extend(
            f"- {item['finalist_id']}: {item['action']} — {item['reason']}"
            + (f" — {item['warning']}" if item.get("warning") else "")
            for item in decision.get("decisions", [])
        )
        checkpoint_lines.extend(
            lex["checkpoint_feedback_line"].format(
                finalist_id=item["finalist_id"], interesting=item["interesting"], boring=item["boring"],
            )
            for item in sorted(decision.get("feedback", []), key=lambda item: item["finalist_id"])
        )
        decision_body = "\n".join(checkpoint_lines)
    else:
        decision_body = "\n".join(
            f"- {item['finalist_id']}: {item['action']} — {item['reason']}"
            + (f" — {item['warning']}" if item.get("warning") else "")
            for item in decision["decisions"]
        )
    limitations = [item.get("limitation", "") for item in research.get("coverage_limitations", []) if isinstance(item, Mapping)]
    corpus_failures = [failure for group in corpus.get("corpora", []) if isinstance(group, Mapping) for failure in group.get("failures", [])]
    adapters = sorted({
        f"{item.get('adapter', '')}/{item.get('adapter_version', '')}"
        for item in research.get("adapter_events", []) if isinstance(item, Mapping)
    })
    search_dates = sorted({
        str(item.get("retrieved_at", ""))[:10]
        for item in research.get("adapter_events", []) if isinstance(item, Mapping) and item.get("retrieved_at")
    })
    query_strategy = []
    _query_strategy_seen: set[tuple[str, str]] = set()
    for item in research.get("queries", []):
        if not isinstance(item, Mapping):
            continue
        plan = json.loads(item["plan_json"]) if isinstance(item.get("plan_json"), str) else item.get("plan_json", {})
        # `plan_json` is `PlannedQuery.as_dict()` (research.py), which emits
        # `{depth, origin_query, term, term_kind}` — never `query`,
        # `original_query`, or `normalized_query`. Reading those three keys meant
        # this line always fell through to `query_id`, so "Research Scope and
        # Method" reported the search method as a list of opaque `qu_…` digests
        # and no reader could tell what was actually searched.
        #
        # `term_kind` is rendered alongside the term because it is the only place
        # the expansion strategy becomes visible: the planner flattens every kind
        # (origin, synonym_ko, synonym_en, classification, applicant, inventor)
        # into the same free-text `word=` projection, so the wire request cannot
        # distinguish them and the persisted plan is the sole surviving record of
        # which kind a term came from.
        term = normalize(str(plan.get("term") or plan.get("origin_query") or ""))
        kind = normalize(str(plan.get("term_kind") or ""))
        # Deduped by (term, term_kind): a term that produced more than one
        # `research_queries` row (e.g. one row per adapter retry or, once paging
        # ships, one row per page) is still one searched term, and printing it
        # once per row was unreadable at scale (review: a 12-term batch would
        # render as a 60-item list). Rows lacking a parseable term keep falling
        # back to their query_id, deduped the same way so repeats of that
        # fallback also collapse.
        key = (term, kind) if term else (str(item.get("query_id", "")), "")
        if key in _query_strategy_seen:
            continue
        _query_strategy_seen.add(key)
        if term:
            query_strategy.append(f"{term} ({kind})" if kind else term)
        else:
            query_strategy.append(str(item.get("query_id", "")))
    strategy_terms = [item for item in query_strategy if item]
    appendix = []
    for evidence_id in cited_ids:
        item = evidence[evidence_id]
        url = f" — {item['canonical_url']}" if item.get("canonical_url") else ""
        record = item.get("record") if isinstance(item.get("record"), Mapping) else {}
        status_token = record.get("register_status")
        legal_status = lex["legal_status_clause"].format(
            status=status_token,
            observed=record.get("register_date") or item.get("observation_date") or lex["unknown"],
        ) if status_token else ""
        appendix.append(lex["appendix_line"].format(
            legal_status=legal_status,
            evidence_id=evidence_id, title=item.get("title") or lex["title_fallback"],
            identifier=item.get("identifier") or lex["identifier_fallback"], url=url,
            source_type=item.get("source_type") or lex["unknown"],
            observed=item.get("observation_date") or lex["unknown"],
            content_hash=item.get("content_hash"),
            limitations=", ".join(item.get("limitations", [])) or lex["no_separate_record"],
        ))
    return [
        "\n".join([
            lex["report_date_line"].format(date=report_input["report_date"]),
            lex["version_line"].format(report_version=REPORT_VERSION, policy_version=policy["version"]),
            lex["purpose_line"],
            policy["report_disclaimer"],
        ]),
        lex["privacy_note"] + "\n"
        + "\n".join(profile_lines),
        "\n".join(
            bullet("problem_hypothesis_line", candidate, "technical_problem", problem=candidate.get("technical_problem", ""))
            for candidate in candidates
        ),
        "\n".join([
            lex["adapters_line"].format(adapters=", ".join(adapters) or lex["none_recorded"]),
            lex["search_dates_line"].format(dates=", ".join(search_dates) or lex["none_recorded"]),
            lex["query_strategy_line"].format(strategy=", ".join(strategy_terms) or lex["query_strategy_fallback"]),
            # Denominator matches what is actually printed above: the deduped,
            # non-empty strategy entries, not the raw `research_queries` row
            # count (which over-counts once the same term produces more than
            # one row).
            lex["query_count_line"].format(count=len(strategy_terms)),
            # The research-stage count, not the run-wide one. `evidence` is
            # `_evidence_map`, which unions the audit-corpus projection with a
            # full re-read of `evidence_records` (every row the run ever
            # retrieved, audit included). Rendering its length under "Research
            # Scope and Method" overstated the research stage by every record the
            # audit later pulled — 563 against 154 in the shipped sample.
            # `research["evidence"]` is the research bundle's own frozen
            # evidence list, published before the audit runs — and, since
            # `ResearchStore.manifest` stage-scopes its reads (excludes
            # `term_kind` values starting `audit_`, research.py `manifest()`),
            # it stays the honest research-stage denominator even after a
            # COVERAGE-expand re-entry re-reads the run. The label says so
            # explicitly so it cannot be misread as contradicting §5/§11, which
            # count the whole run.
            lex["evidence_count_line"].format(count=len(research.get("evidence", []))),
            lex["limitations_line"].format(limitations=", ".join(item for item in limitations if item) or lex["no_separate_record"]),
            lex["audit_failures_line"].format(count=len(corpus_failures)),
        ]),
        "\n".join(landscape),
        "\n".join(finalist_lines),
        "\n".join(comparison_lines),
        "\n".join(audit_lines),
        decision_body,
        "\n".join([
            lex["handoff_heading"],
            *(f"- {item}" for item in report_input["handoff_questions"]),
            lex["followup_heading"],
            *(f"- {item}" for item in report_input["recommended_investigations"]),
        ]),
        "\n".join(appendix),
    ]


def render_report_markdown(sections: Iterable[Mapping[str, Any]], language: str = "ko") -> str:
    if language not in REPORT_LANGUAGES:
        raise ValueError("report_template: supported language required")
    template = TEMPLATE_PATHS[language].read_text(encoding="utf-8")
    items = list(sections)
    if len(items) != 11:
        raise ValueError("report.sections: exactly eleven sections required")
    rendered = template
    for index, section in enumerate(items, start=1):
        token = "{{section_%02d}}" % index
        if rendered.count(token) != 1:
            raise ValueError("report_template: each section placeholder required exactly once")
        rendered = rendered.replace(token, _text(section.get("body"), f"report.sections[{index - 1}].body"))
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("report_template: unresolved placeholder")
    return normalize(rendered)


def validate_report_artifact(value: Mapping[str, Any], *, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    required = {
        "appendix_ids", "bindings", "citations", "draft_spec", "draft_spec_hash", "drafter", "language", "markdown",
        "policy_hash", "redactions", "report_date", "revision", "run_id", "sections",
        "sensitive_disclosures", "template_hash", "version",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("version") != REPORT_VERSION:
        raise ValueError("report_artifact: exact report-v1 fields required")
    language = value.get("language")
    if language not in REPORT_LANGUAGES:
        raise ValueError("report_artifact: policy binding mismatch")
    resolved_policy = dict(policy or load_report_policy(language))
    if resolved_policy.get("language") != language or value.get("policy_hash") != digest(resolved_policy):
        raise ValueError("report_artifact: policy binding mismatch")
    drafter = value.get("drafter")
    if (
        not isinstance(drafter, Mapping) or set(drafter) != {"id", "pass_id", "type"}
        or any(not isinstance(drafter.get(name), str) or not drafter[name] for name in ("id", "pass_id", "type"))
        or drafter["type"] not in {"agent", "human"}
    ):
        raise ValueError("report_artifact.drafter: exact identity required")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value.get("report_date", ""))) is None:
        raise ValueError("report_artifact.report_date: YYYY-MM-DD required")
    if not isinstance(value.get("run_id"), str) or not value["run_id"]:
        raise ValueError("report_artifact.run_id: non-empty string required")
    revision = value.get("revision")
    if revision is not None and (
        not isinstance(revision, Mapping) or set(revision) != {"reason", "report_hash", "review_hash"}
        or not isinstance(revision.get("reason"), str) or not revision["reason"]
        or any(re.fullmatch(r"[0-9a-f]{64}", str(revision.get(name, ""))) is None for name in ("report_hash", "review_hash"))
    ):
        raise ValueError("report_artifact.revision: exact hash-bound revision required")
    redactions = value.get("redactions")
    redaction_fields = {"decision_id", "field", "prior_report_hash", "reason", "replacement", "text_hash"}
    if (
        not isinstance(redactions, list)
        or any(
            not isinstance(item, Mapping) or set(item) != redaction_fields
            or any(not isinstance(item.get(name), str) or not item[name] for name in redaction_fields)
            or item.get("replacement") != REDACTION_REPLACEMENTS[language]
            or any(re.fullmatch(r"[0-9a-f]{64}", item[name]) is None for name in ("prior_report_hash", "text_hash"))
            for item in redactions
        )
        or [item["field"] for item in redactions] != sorted({item["field"] for item in redactions})
    ):
        raise ValueError("report_artifact.redactions: exact non-plaintext redaction history required")
    draft_spec = value.get("draft_spec")
    if (
        not isinstance(draft_spec, Mapping)
        or set(draft_spec) != {"handoff_questions", "profile_fields", "recommended_investigations"}
        or any(
            not isinstance(draft_spec.get(name), list)
            or not draft_spec[name]
            or any(not isinstance(item, str) or not item for item in draft_spec[name])
            or len(set(draft_spec[name])) != len(draft_spec[name])
            for name in ("handoff_questions", "profile_fields", "recommended_investigations")
        )
        or value.get("draft_spec_hash") != digest(draft_spec)
    ):
        raise ValueError("report_artifact.draft_spec: exact hash-bound structured draft inputs required")
    template = TEMPLATE_PATHS[language].read_text(encoding="utf-8")
    if value.get("template_hash") != digest({"text": normalize(template)}):
        raise ValueError("report_artifact: template binding mismatch")
    sections = value.get("sections")
    if not isinstance(sections, list) or len(sections) != 11:
        raise ValueError("report_artifact.sections: exactly eleven required")
    expected_headings = resolved_policy["section_headings"]
    for index, section in enumerate(sections, start=1):
        if not isinstance(section, Mapping) or set(section) != {"body", "heading", "number"}:
            raise ValueError("report_artifact.section: exact fields required")
        if section.get("number") != index or section.get("heading") != expected_headings[index - 1]:
            raise ValueError("report_artifact.sections: exact heading order required")
        _text(section.get("body"), f"report_artifact.sections[{index - 1}].body")
    expected_markdown = render_report_markdown(sections, language)
    if value.get("markdown") != expected_markdown:
        raise ValueError("report_artifact.markdown: renderer mismatch")
    headings = re.findall(r"^## (?!#)(.+)$", expected_markdown, flags=re.MULTILINE)
    expected_h2 = [f"{index} {heading}" for index, heading in enumerate(expected_headings, start=1)]
    if expected_markdown.count("\n# ") or len(re.findall(r"^# (?!#)", expected_markdown, flags=re.MULTILINE)) != 1 or headings != expected_h2:
        raise ValueError("report_artifact.markdown: exact H1/H2 structure required")
    if resolved_policy["report_disclaimer"] not in sections[0]["body"] or resolved_policy["similarity_disclaimer"] not in sections[7]["body"]:
        raise ValueError("report_artifact: exact required disclaimers missing")
    citations = value.get("citations")
    appendix_ids = value.get("appendix_ids")
    if not isinstance(citations, list) or not isinstance(appendix_ids, list) or appendix_ids != sorted(set(appendix_ids)):
        raise ValueError("report_artifact: sorted unique citation appendix required")
    if any(not isinstance(item, Mapping) or set(item) != {"content_hash", "evidence_id", "identifier", "limitations", "observation_date", "source_type", "title", "url"} for item in citations):
        raise ValueError("report_artifact.citations: exact fields required")
    if any(
        re.fullmatch(r"[0-9a-f]{64}", str(item.get("content_hash", ""))) is None
        or CITATION_RE.fullmatch(f"[@{item.get('evidence_id', '')}]") is None
        or any(not isinstance(item.get(name), str) or not item[name] for name in ("identifier", "observation_date", "source_type", "title"))
        or not isinstance(item.get("limitations"), list)
        or any(not isinstance(limit, str) for limit in item["limitations"])
        or item.get("url") is not None and not isinstance(item.get("url"), str)
        for item in citations
    ):
        raise ValueError("report_artifact.citations: schema-conformant metadata required")
    citation_ids = [item["evidence_id"] for item in citations]
    if citation_ids != appendix_ids or set(CITATION_RE.findall(expected_markdown)) != set(appendix_ids):
        raise ValueError("report_artifact: citation and appendix identifiers must match exactly")
    appendix_tokens = CITATION_RE.findall(sections[10]["body"])
    if appendix_tokens != appendix_ids:
        raise ValueError("report_artifact.appendix: exactly every cited ID in sorted order required")
    bindings = value.get("bindings")
    required_bindings = {
        "audit_batch", "candidate_set", "corpus_set", "feature_map_set", "finalist_set",
        "profile_context", "research_bundle", "scorer_config",
    }
    if not isinstance(bindings, Mapping) or not required_bindings.issubset(bindings) or any(
        re.fullmatch(r"[0-9a-f]{64}", str(item)) is None for item in bindings.values()
    ):
        raise ValueError("report_artifact.bindings: current artifact hashes required")
    disclosures = value.get("sensitive_disclosures")
    if not isinstance(disclosures, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"field", "reason", "text", "text_hash"}
        or any(not isinstance(item.get(name), str) or not item[name] for name in ("field", "reason", "text"))
        or digest(item.get("text")) != item.get("text_hash")
        for item in disclosures
    ):
        raise ValueError("report_artifact.sensitive_disclosures: exact hash-bound private fields required")
    if [item["field"] for item in disclosures] != sorted({item["field"] for item in disclosures}):
        raise ValueError("report_artifact.sensitive_disclosures: sorted unique fields required")
    return normalize(dict(value))


def _report_payload(
    connection: sqlite3.Connection, *, run_id: str, report_input: Mapping[str, Any],
    redactions: Iterable[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], tuple[str, ...]]:
    language = report_input.get("language", "ko")
    if language not in REPORT_LANGUAGES:
        raise ValueError("report_input.language: en or ko required")
    policy = load_report_policy(language)
    kinds = (
        "profile_context", "research_bundle", "candidate_set", "finalist_set", "corpus_set",
        "feature_map_set", "scorer_config", "audit_batch",
    )
    artifacts = {kind: _current_artifact(connection, run_id, kind) for kind in kinds}
    rows = {kind: item[0] for kind, item in artifacts.items()}
    content = {kind: item[1] for kind, item in artifacts.items()}
    candidates = content["candidate_set"].get("candidates", [])
    finalists = content["finalist_set"].get("finalists", [])
    if not isinstance(candidates, list) or not isinstance(finalists, list) or len(finalists) < 3:
        raise StateError("report requires at least three current finalists")
    audit = content["audit_batch"]
    if (
        audit.get("finalist_set_hash") != rows["finalist_set"]["content_hash"]
        or audit.get("corpus_set_hash") != rows["corpus_set"]["content_hash"]
        or audit.get("feature_map_set_hash") != rows["feature_map_set"]["content_hash"]
        or audit.get("scorer_config_hash") != rows["scorer_config"]["content_hash"]
        or any(item.get("outcome") == "coverage_insufficient" for item in audit.get("results", []))
    ):
        raise StateError("report requires an exact current approved audit")
    scorer_content = content["scorer_config"]
    resolved_config = scorer_content.get("config", scorer_content) if isinstance(scorer_content, Mapping) else scorer_content
    if not isinstance(resolved_config, Mapping):
        raise StateError("report requires a structured current scorer configuration")
    try:
        similarity_config = SimilarityConfig(**dict(resolved_config))
        similarity_config.validate()
        validate_audit_artifact(audit, similarity_config)
    except (TypeError, ValueError) as exc:
        raise StateError(f"report current audit failed authoritative validation: {exc}") from exc
    candidate_ids = {
        item.get("candidate_id") for item in candidates
        if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
    }
    finalist_pairs = [
        (item.get("finalist_id"), item.get("candidate_id"))
        for item in finalists if isinstance(item, Mapping)
    ]
    result_pairs = [
        (item.get("finalist_id"), item.get("candidate_id"))
        for item in audit.get("results", []) if isinstance(item, Mapping)
    ]
    if (
        len(candidate_ids) != len(candidates)
        or len(finalist_pairs) != len(finalists)
        or len({item[0] for item in finalist_pairs}) != len(finalist_pairs)
        or len({item[1] for item in finalist_pairs}) != len(finalist_pairs)
        or any(candidate_id not in candidate_ids for _finalist_id, candidate_id in finalist_pairs)
        or len(result_pairs) != len(finalist_pairs)
        or sorted(result_pairs) != sorted(finalist_pairs)
    ):
        raise StateError("report audit must contain exactly one matching result per current finalist and candidate")
    decision_row, decision = _bound_decision(connection, run_id, rows["audit_batch"]["content_hash"], audit)
    evidence = _evidence_map(
        content["research_bundle"], content["corpus_set"], connection=connection, run_id=run_id,
    )
    cited_ids = _cited_ids(candidates, finalists, audit)
    missing = [item for item in cited_ids if item not in evidence]
    if missing or any(CITATION_RE.fullmatch(f"[@{item}]") is None for item in cited_ids):
        raise StateError("report citation does not resolve to current evidence")
    feature_descriptions: dict[str, dict[str, str]] = {}
    for entry in content["feature_map_set"].get("maps", []):
        if not isinstance(entry, Mapping):
            continue
        feature_map = entry.get("feature_map")
        features = feature_map.get("features") if isinstance(feature_map, Mapping) else None
        if not isinstance(features, Mapping):
            continue
        table = {
            feature_id: feature["description"]
            for feature_id, feature in features.items()
            if isinstance(feature, Mapping)
            and isinstance(feature.get("description"), str) and feature["description"]
        }
        if table:
            feature_descriptions[entry.get("finalist_id")] = table
    bodies = _section_bodies(
        policy=policy, report_input=report_input, profile=content["profile_context"],
        research=content["research_bundle"], candidates=candidates, finalists=finalists,
        corpus=content["corpus_set"], audit=audit, decision=decision, evidence=evidence,
        cited_ids=cited_ids, scorer=content["scorer_config"], language=language,
        feature_descriptions=feature_descriptions,
    )
    sections = [
        {"body": body, "heading": policy["section_headings"][index - 1], "number": index}
        for index, body in enumerate(bodies, start=1)
    ]
    resolved_redactions = normalize(list(redactions))
    for item in resolved_redactions:
        if not isinstance(item, Mapping):
            raise StateError("report redaction history is malformed")
        revision = report_input.get("revision")
        if not isinstance(revision, Mapping) or revision.get("report_hash") != item.get("prior_report_hash"):
            raise StateError("report redaction history does not bind the prior report revision")
        prior_row = connection.execute(
            "SELECT content_json FROM artifact_revisions WHERE run_id=? AND kind='report' AND content_hash=?",
            (run_id, item.get("prior_report_hash")),
        ).fetchone()
        redaction_decision = connection.execute(
            "SELECT gd.action,gd.reason,gd.subject_revision_hash,gd.used_at,gd.consumed_by_event_id,ge.kind "
            "FROM gate_decisions gd JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
            "WHERE gd.decision_id=? AND gd.run_id=?",
            (item.get("decision_id"), run_id),
        ).fetchone()
        if prior_row is None or redaction_decision is None:
            raise StateError("report redaction history is missing its immutable source or decision")
        prior_report = json.loads(prior_row["content_json"])
        disclosure = next((
            disclosure for disclosure in prior_report.get("sensitive_disclosures", [])
            if disclosure.get("field") == item.get("field") and disclosure.get("text_hash") == item.get("text_hash")
        ), None)
        if (
            disclosure is None or digest(disclosure.get("text")) != item.get("text_hash")
            or redaction_decision["kind"] != "sensitive_disclosure" or redaction_decision["action"] != "redact"
            or redaction_decision["subject_revision_hash"] != item.get("prior_report_hash")
            or redaction_decision["reason"] != item.get("reason")
            or not redaction_decision["used_at"] or not redaction_decision["consumed_by_event_id"]
            or item.get("replacement") != REDACTION_REPLACEMENTS[language]
        ):
            raise StateError("report redaction history failed exact decision/source validation")
        replaced = False
        for section in sections:
            if disclosure["text"] in section["body"]:
                section["body"] = section["body"].replace(disclosure["text"], item["replacement"])
                replaced = True
        if not replaced:
            raise StateError("report redaction source text is absent from canonical sections")
    bindings = {kind: rows[kind]["content_hash"] for kind in kinds}
    dependencies = [rows[kind]["revision_id"] for kind in kinds]
    if decision_row is not None:
        binding_key = "checkpoint_gate_resolution" if decision.get("gate_kind") == "post_audit_checkpoint" else "excessive_gate_resolution"
        bindings[binding_key] = decision_row["content_hash"]
        dependencies.append(decision_row["revision_id"])
    citations = [{
        "content_hash": evidence[item].get("content_hash"), "evidence_id": item,
        "identifier": evidence[item].get("identifier"), "title": evidence[item].get("title"),
        "limitations": evidence[item].get("limitations", []),
        "observation_date": evidence[item].get("observation_date") or LEXICON[language]["unknown"],
        "source_type": evidence[item].get("source_type") or "unknown",
        "url": evidence[item].get("canonical_url"),
    } for item in cited_ids]
    draft_spec = {
        "handoff_questions": report_input["handoff_questions"],
        "profile_fields": report_input["profile_fields"],
        "recommended_investigations": report_input["recommended_investigations"],
    }
    payload = {
        "appendix_ids": cited_ids, "bindings": bindings, "citations": citations,
        "draft_spec": draft_spec, "draft_spec_hash": digest(draft_spec),
        "drafter": report_input["drafter"], "language": language,
        "policy_hash": digest(policy), "redactions": resolved_redactions,
        "report_date": report_input["report_date"],
        "revision": report_input["revision"], "run_id": run_id, "sections": sections,
        "sensitive_disclosures": [{
            "field": item["field"], "reason": item["reason"], "text": item["text"],
            "text_hash": digest(item["text"]),
        } for item in report_input["sensitive_disclosures"]],
        "template_hash": digest({"text": normalize(TEMPLATE_PATHS[language].read_text(encoding="utf-8"))}),
        "version": REPORT_VERSION,
    }
    payload["markdown"] = render_report_markdown(sections, language)
    for item in report_input["sensitive_disclosures"]:
        if item["text"] not in payload["markdown"]:
            raise ValueError(f"report_input.sensitive_disclosures: text for {item['field']} is absent")
    return validate_report_artifact(payload, policy=policy), tuple(sorted(dependencies))


def publish_report(
    connection: sqlite3.Connection, *, run_root: Path, run_id: str,
    report_input: Mapping[str, Any], fault_at: FaultInjector = None,
) -> ReportRun:
    canaries = credential_canaries()
    assert_canaries_absent(report_input, canaries, boundary="report_input")
    request = validate_report_input(report_input)
    state, exports = _report_state(connection, run_root)
    prior = state.snapshot(run_id)
    if prior.state not in {RunState.AUDIT_APPROVED, RunState.REVISION_REQUIRED, RunState.DRAFT_READY}:
        raise StateError("draft requires audit_approved or revision_required")
    if prior.state is RunState.AUDIT_APPROVED and request["revision"] is not None:
        raise ValueError("report_input.revision: initial report must not claim a revision")
    if prior.state is RunState.REVISION_REQUIRED:
        revision = request["revision"]
        if revision is None:
            raise ValueError("report_input.revision: current report and review bindings required")
        report_row, _report = _current_artifact(connection, run_id, "report")
        review_row, review = _current_artifact(connection, run_id, "review")
        validation_failed = connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ar.run_id=? AND ca.kind='validation' AND ar.stale=0", (run_id,),
        ).fetchone()
        failed = False
        if validation_failed is not None:
            validation_content = json.loads(validation_failed["content_json"])
            failed = (
                validation_content.get("status") == "failed"
                and validation_content.get("report_hash") == report_row["content_hash"]
                and validation_content.get("review_hash") == review_row["content_hash"]
            )
        if revision["report_hash"] != report_row["content_hash"] or revision["review_hash"] != review_row["content_hash"] or (review.get("disposition") != "revise" and not failed):
            raise StateError("report revision does not bind the current blocking review")
    payload, dependencies = _report_payload(connection, run_id=run_id, report_input=request)
    operation = "report.revise" if request["revision"] is not None else "report.publish"
    target = RunState.DRAFT_READY
    result, exported = state.publish_transition(
        run_id, target, actor="draft-cli", reason="report rendered from approved artifacts",
        operation=operation, idempotency_key=digest({"request": request, "bindings": payload["bindings"]}),
        artifact_kind="report", artifact_content=payload, artifact_schema_version=REPORT_VERSION,
        dependencies=dependencies, export_directory=exports,
        export_payload=(payload["markdown"] + "\n").encode("utf-8"), export_suffix=".md", fault_at=fault_at,
    )
    if result.artifact is None:
        raise RuntimeError("report publication produced no artifact")
    return ReportRun(run_id, prior.state.value, result.snapshot.state.value, result.artifact, exported.path, result.replayed)


def apply_sensitive_redaction(
    connection: sqlite3.Connection, *, run_root: Path, run_id: str,
    decision_id: str, reason: str, fault_at: FaultInjector = None,
) -> ReportRun:
    state, exports = _report_state(connection, run_root)
    prior = state.snapshot(run_id)
    if prior.state is not RunState.REVISION_REQUIRED:
        raise StateError("sensitive redaction requires revision_required")
    report_row, report = _current_artifact(connection, run_id, "report")
    review_row, _review = _current_artifact(connection, run_id, "review")
    decision = connection.execute(
        "SELECT gd.*,ge.approval_scope_json,ge.kind FROM gate_decisions gd "
        "JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id WHERE gd.decision_id=? AND gd.run_id=?",
        (decision_id, run_id),
    ).fetchone()
    if (
        decision is None or decision["kind"] != "sensitive_disclosure" or decision["action"] != "redact"
        or decision["subject_revision_hash"] != report_row["content_hash"] or decision["stale"]
        or not decision["used_at"] or not decision["consumed_by_event_id"]
    ):
        raise StateError("redaction requires the exact current sensitive-disclosure decision")
    scope = json.loads(decision["approval_scope_json"])
    expected_fields = [{
        "field": item["field"], "reason": item["reason"], "text_hash": item["text_hash"],
    } for item in sorted(report.get("sensitive_disclosures", []), key=lambda item: item["field"])]
    if scope.get("report_hash") != report_row["content_hash"] or scope.get("fields") != expected_fields:
        raise StateError("redaction decision scope does not bind the current report fields")
    revision = {
        "reason": _text(reason, "redaction.reason"), "report_hash": report_row["content_hash"],
        "review_hash": review_row["content_hash"],
    }
    language = report.get("language", "ko")
    if language not in REPORT_LANGUAGES:
        raise StateError("redaction requires a supported report language")
    redactions = [{
        "decision_id": decision_id, "field": disclosure["field"],
        "prior_report_hash": report_row["content_hash"], "reason": revision["reason"],
        "replacement": REDACTION_REPLACEMENTS[language], "text_hash": disclosure["text_hash"],
    } for disclosure in report.get("sensitive_disclosures", [])]
    draft_spec = report["draft_spec"]
    request = validate_report_input({
        "drafter": report["drafter"], "handoff_questions": draft_spec["handoff_questions"],
        "language": language,
        "profile_fields": draft_spec["profile_fields"],
        "recommended_investigations": draft_spec["recommended_investigations"],
        "report_date": report["report_date"], "revision": revision,
        "schema_version": REPORT_INPUT_VERSION_V2, "sensitive_disclosures": [],
    })
    revised, dependencies = _report_payload(
        connection, run_id=run_id, report_input=request, redactions=redactions,
    )
    result, exported = state.publish_transition(
        run_id, RunState.DRAFT_READY, actor="gate-cli", reason="sensitive fields redacted into a new report revision",
        operation=f"report.redact:{decision_id}", idempotency_key=digest({"decision_id": decision_id, "report": revised}),
        artifact_kind="report", artifact_content=revised, artifact_schema_version=REPORT_VERSION,
        dependencies=dependencies, export_directory=exports,
        export_payload=(revised["markdown"] + "\n").encode("utf-8"), export_suffix=".md", fault_at=fault_at,
        supersede_prior=True,
    )
    if result.artifact is None:
        raise RuntimeError("redaction produced no report artifact")
    return ReportRun(run_id, prior.state.value, result.snapshot.state.value, result.artifact, exported.path, result.replayed)


__all__ = [
    "CITATION_RE", "DEFAULT_REPORT_LANGUAGE", "LEXICON", "POLICY_PATH", "POLICY_PATHS",
    "REDACTION_REPLACEMENTS", "REPORT_INPUT_VERSION", "REPORT_INPUT_VERSION_V2",
    "REPORT_LANGUAGES", "REPORT_VERSION", "ReportRun",
    "apply_sensitive_redaction", "load_report_policy", "publish_report", "render_report_markdown", "validate_report_artifact",
    "validate_report_input",
]
