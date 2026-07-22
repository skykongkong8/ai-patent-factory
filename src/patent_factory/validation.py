from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters.base import normalized_patent_number
from .database import FaultInjector
from .models import ArtifactRevision, RunState
from .provenance import digest, normalize
from .report import (
    CITATION_RE,
    HEDGED_LABELS,
    _bound_decision,
    _current_artifact,
    _evidence_map,
    _report_payload,
    _report_state,
    load_report_policy,
    validate_report_artifact,
)
from .review import validate_review_artifact
from .state import StateError


VALIDATION_VERSION = "validation-v1"
VALIDATOR_VERSION = "report-validator-v1.0.0"
VALIDATION_CHECK_NAMES = [
    "artifact_bindings", "citation_integrity", "decision_coverage", "identifier_shape",
    "legal_language", "narrative_language", "report_structure", "review_binding",
    "semantic_reconstruction",
]


@dataclass(frozen=True)
class ValidationRun:
    run_id: str
    prior_state: str
    next_state: str
    artifact: ArtifactRevision
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ids": [self.artifact.revision_id], "command": "validate",
            "next_state": self.next_state, "prior_state": self.prior_state,
            "replayed": self.replayed, "run_id": self.run_id, "status": self.next_state,
        }


def _binding_check(
    connection: sqlite3.Connection, run_id: str, report: Mapping[str, Any],
) -> None:
    bindings = report["bindings"]
    for kind in (
        "profile_context", "research_bundle", "candidate_set", "finalist_set", "corpus_set",
        "feature_map_set", "scorer_config", "audit_batch",
    ):
        row, _content = _current_artifact(connection, run_id, kind)
        if bindings.get(kind) != row["content_hash"]:
            raise ValueError(f"validation.artifact_bindings: stale {kind}")
    for binding_key, label in (
        ("excessive_gate_resolution", "excessive"), ("checkpoint_gate_resolution", "checkpoint"),
    ):
        if binding_key in bindings:
            row = connection.execute(
                "SELECT * FROM artifact_revisions WHERE run_id=? AND kind='gate_resolution' AND content_hash=? AND stale=0",
                (run_id, bindings[binding_key]),
            ).fetchone()
            if row is None:
                raise ValueError(f"validation.artifact_bindings: stale {label} decision")


def _citation_check(
    connection: sqlite3.Connection, run_id: str, report: Mapping[str, Any],
) -> None:
    _research_row, research = _current_artifact(connection, run_id, "research_bundle")
    _corpus_row, corpus = _current_artifact(connection, run_id, "corpus_set")
    evidence = _evidence_map(research, corpus, connection=connection, run_id=run_id)
    citations = report["citations"]
    ids = [item["evidence_id"] for item in citations]
    if ids != report["appendix_ids"] or set(CITATION_RE.findall(report["markdown"])) != set(ids):
        raise ValueError("validation.citations: token and appendix sets differ")
    # Set equality above is blind to a token sitting on the wrong bullet.
    _hedged_citation_check(report)
    unknown = "미상" if report.get("language", "ko") == "ko" else "unknown"
    for item in citations:
        current = evidence.get(item["evidence_id"])
        if current is None or any(
            item.get(name) != (
                current.get(source) or unknown if name == "observation_date" else current.get(source)
            )
            for name, source in (
                ("content_hash", "content_hash"), ("identifier", "identifier"),
                ("limitations", "limitations"), ("observation_date", "observation_date"),
                ("source_type", "source_type"),
                ("title", "title"), ("url", "canonical_url"),
            )
        ):
            raise ValueError("validation.citations: evidence hash is missing or stale")


def _hedged_citation_check(report: Mapping[str, Any]) -> None:
    """Reject any rendered line that hedges and cites at the same time.

    The token-set equality in _citation_check compares the whole document
    against the appendix, so it cannot see a citation sitting on the WRONG
    bullet. A line labelled [creative suggestion] / [hypothesis] / [candidate
    hypothesis] / [profile-based inference] (and the Korean equivalents)
    asserts no evidentiary support, so a prior-art token on it is misleading.
    """

    for line in str(report.get("markdown", "")).splitlines():
        if any(label in line for label in HEDGED_LABELS) and CITATION_RE.search(line):
            raise ValueError(f"validation.citations: hedged line carries a prior-art citation: {line[:120]}")


def _identifier_check(report: Mapping[str, Any]) -> None:
    for item in report["citations"]:
        identifier = item.get("identifier")
        if not isinstance(identifier, str) or not normalized_patent_number(identifier):
            raise ValueError("validation.identifiers: source identifier is malformed")
        url = item.get("url")
        if url is not None:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
                raise ValueError("validation.identifiers: source URL must be safe HTTPS")


def _scan_prohibited_language(text: str, language: str) -> None:
    """Reject any sentence in ``text`` that reads as an unqualified legal conclusion.

    Extracted from ``_legal_language_check`` (which now just calls this on
    ``report["markdown"]``) so the identical deterministic, sentence-local
    policy scan can also run on hand-authored checkpoint decision prose
    (``gate decide``'s top-level ``reason``, every ``feedback[].interesting``/
    ``.boring``, and approve-path ``decisions[].reason``) — text that becomes
    durable Section 9 content but, unlike ``report-input`` text, can never be
    re-authored once persisted. ``prohibited_unqualified_phrases`` is frozen
    identical across both language policies, so any supported ``language``
    scans the same phrase list.
    """
    policy = load_report_policy(language)
    dangerous = (
        re.compile(r"특허(?:를\s*받을\s*수\s*있|\s*가능(?:하|성))", re.IGNORECASE),
        re.compile(r"(?:신규성|진보성).{0,16}(?:있|충족|인정)", re.IGNORECASE),
        re.compile(r"(?:특허.{0,12})?유효성.{0,16}(?:있|인정|확보|유효)", re.IGNORECASE),
        re.compile(r"(?:patent.{0,16})?(?:is\s+valid|validity.{0,12}(?:is|has|established))", re.IGNORECASE),
        re.compile(r"(?:타인의\s*)?특허(?:를|가)?\s*침해하지\s*않", re.IGNORECASE),
        re.compile(r"(?:does\s+not\s+infringe|non[- ]?infringement(?:\s+(?:is|has|confirmed))?)", re.IGNORECASE),
        re.compile(r"(?:비침해|non[- ]?infring|freedom\s+to\s+operate|\bFTO\b).{0,16}(?:확보|가능|보장|is|has)", re.IGNORECASE),
        re.compile(r"\bpatentable\b", re.IGNORECASE),
        re.compile(r"\bis\s+novel\b", re.IGNORECASE),
        re.compile(r"\bnovelty\s+(?:is|has\s+been)\s+(?:established|confirmed|present)\b", re.IGNORECASE),
        re.compile(r"\binventive\s+step\s+(?:is|has)\b", re.IGNORECASE),
    )
    qualifiers = (
        "아닙", "아니다", "단정할 수 없", "판단이 아니", "결론이 아니", "확인해야",
        "확인이 필요", "검토가 필요", "가능성을 검토", "여부", "질문", "research aid",
        "법적 결론을 제공하지", "법적 판단이 아니", "not a legal", "cannot conclude", "does not conclude",
        "not legal advice", "no legal conclusion", "not legal determination", "question",
    )
    sentences = [item.strip() for item in re.split(r"(?<=[.!?。])\s+|\n+", text) if item.strip()]
    frozen_phrases = tuple(item.casefold() for item in policy["prohibited_unqualified_phrases"])
    for sentence in sentences:
        normalized = re.sub(r"\s+", " ", sentence).casefold()
        risky = any(pattern.search(sentence) for pattern in dangerous) or any(item in normalized for item in frozen_phrases)
        if risky and not any(item.casefold() in normalized for item in qualifiers):
            raise ValueError(f"validation.legal_language: unqualified legal conclusion: {sentence[:120]}")


def _legal_language_check(report: Mapping[str, Any]) -> None:
    _scan_prohibited_language(report["markdown"], report.get("language", "ko"))


def _narrative_language_check(report: Mapping[str, Any]) -> None:
    language = report.get("language", "ko")
    # Source titles/identifiers keep their original language (often Korean),
    # so the check asserts the presence of the declared narrative language,
    # never the absence of the other.
    pattern, label = (r"[가-힣]", "Korean") if language == "ko" else (r"[A-Za-z]", "English")
    for index, section in enumerate(report["sections"]):
        if re.search(pattern, section["body"]) is None:
            raise ValueError(f"validation.narrative_language: section {index + 1} has no {label} narrative")


def _decision_check(
    connection: sqlite3.Connection, run_id: str, report: Mapping[str, Any],
) -> None:
    audit_row, audit = _current_artifact(connection, run_id, "audit_batch")
    decision_row, decision = _bound_decision(connection, run_id, audit_row["content_hash"], audit)
    if decision_row is None:
        if "excessive_gate_resolution" in report["bindings"] or "checkpoint_gate_resolution" in report["bindings"]:
            raise ValueError("validation.decision_coverage: unexpected decision binding")
        return
    # Mirror report.py's own binding dispatch: a checkpoint resolution binds
    # under "checkpoint_gate_resolution", a legacy excessive one under
    # "excessive_gate_resolution" — never both for the same current audit.
    is_checkpoint = decision.get("gate_kind") == "post_audit_checkpoint"
    binding_key = "checkpoint_gate_resolution" if is_checkpoint else "excessive_gate_resolution"
    if report["bindings"].get(binding_key) != decision_row["content_hash"]:
        raise ValueError("validation.decision_coverage: report omits current decision")
    section = report["sections"][8]["body"]
    for item in decision["decisions"]:
        if item["finalist_id"] not in section or item["action"] not in section:
            raise ValueError("validation.decision_coverage: report omits finalist decision")
        if item.get("warning") and item["warning"] not in section:
            raise ValueError("validation.decision_coverage: report omits retain warning")
    if is_checkpoint:
        # Finding #13: `decisions[]` is EMPTY on a clean checkpoint approve —
        # the normal path — so the loop above asserts nothing there. The
        # top-level `action`/`reason` and every per-finalist `feedback` pair
        # are still durable, hash-bound human judgement a renderer
        # regression could silently drop from Section 9 while every other
        # check (including `_narrative_language_check`) still passes.
        if decision["action"] not in section or decision["reason"] not in section:
            raise ValueError("validation.decision_coverage: report omits the checkpoint action or reason")
        for item in decision.get("feedback", []):
            if item["interesting"] not in section or item["boring"] not in section:
                raise ValueError("validation.decision_coverage: report omits per-finalist feedback")


def _semantic_check(
    connection: sqlite3.Connection, run_id: str, report: Mapping[str, Any],
) -> None:
    draft_spec = report.get("draft_spec")
    if not isinstance(draft_spec, Mapping):
        raise ValueError("validation.semantic: structured draft specification is missing")
    request = {
        "drafter": report.get("drafter"),
        "handoff_questions": draft_spec.get("handoff_questions"),
        "language": report.get("language", "ko"),
        "profile_fields": draft_spec.get("profile_fields"),
        "recommended_investigations": draft_spec.get("recommended_investigations"),
        "report_date": report.get("report_date"),
        "revision": report.get("revision"),
        "schema_version": "report-input-v2",
        "sensitive_disclosures": [
            {"field": item.get("field"), "reason": item.get("reason"), "text": item.get("text")}
            for item in report.get("sensitive_disclosures", []) if isinstance(item, Mapping)
        ],
    }
    expected, _dependencies = _report_payload(
        connection, run_id=run_id, report_input=request, redactions=report.get("redactions", []),
    )
    if expected != normalize(dict(report)):
        raise ValueError("validation.semantic: report differs from the exact canonical reconstruction")


def _review_check(
    report_row: sqlite3.Row, report: Mapping[str, Any], review_row: sqlite3.Row,
    review: Mapping[str, Any],
) -> None:
    validate_review_artifact(review, report=report)
    if review.get("report_hash") != report_row["content_hash"] or review.get("disposition") != "approved":
        raise ValueError("validation.review: current approved hash-bound review required")
    if any(item.get("status") != "pass" for item in review.get("checks", [])):
        raise ValueError("validation.review: every independent review check must pass")
    if (
        any(item.get("severity") == "blocking" for item in review.get("findings", []))
        or review.get("evidence_corrections") or review.get("prohibited_language_findings")
    ):
        raise ValueError("validation.review: unresolved review findings remain")


def build_validation_manifest(
    connection: sqlite3.Connection, *, run_id: str, report_row: sqlite3.Row,
    report: Mapping[str, Any], review_row: sqlite3.Row, review: Mapping[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    validators: tuple[tuple[str, Callable[[], None]], ...] = (
        ("artifact_bindings", lambda: _binding_check(connection, run_id, report)),
        ("citation_integrity", lambda: _citation_check(connection, run_id, report)),
        ("decision_coverage", lambda: _decision_check(connection, run_id, report)),
        ("identifier_shape", lambda: _identifier_check(report)),
        ("legal_language", lambda: _legal_language_check(report)),
        ("narrative_language", lambda: _narrative_language_check(report)),
        ("report_structure", lambda: validate_report_artifact(report)),
        ("review_binding", lambda: _review_check(report_row, report, review_row, review)),
        ("semantic_reconstruction", lambda: _semantic_check(connection, run_id, report)),
    )
    for name, validator in validators:
        try:
            validator()
        except (StateError, ValueError) as exc:
            checks.append({"details": str(exc), "name": name, "status": "fail"})
        else:
            checks.append({"details": "deterministic check passed", "name": name, "status": "pass"})
    status = "passed" if all(item["status"] == "pass" for item in checks) else "failed"
    scorer_row, scorer = _current_artifact(connection, run_id, "scorer_config")
    resolved_scorer = scorer.get("config", scorer) if isinstance(scorer, Mapping) else {}
    return normalize({
        "artifact_hashes": {**report["bindings"], "report": report_row["content_hash"], "review": review_row["content_hash"]},
        "checks": checks, "policy_hash": report["policy_hash"],
        "report_hash": report_row["content_hash"], "review_hash": review_row["content_hash"],
        "run_id": run_id, "schema_versions": {"report": "report-v1", "review": "review-v1", "validation": VALIDATION_VERSION},
        "scoring_version": resolved_scorer.get("version", scorer_row["schema_version"]), "status": status,
        "validator_version": VALIDATOR_VERSION, "version": VALIDATION_VERSION,
        "workflow_version": "patent-factory-v1",
    })


def validate_validation_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "artifact_hashes", "checks", "policy_hash", "report_hash", "review_hash", "run_id",
        "schema_versions", "scoring_version", "status", "validator_version", "version", "workflow_version",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("version") != VALIDATION_VERSION:
        raise ValueError("validation_artifact: exact validation-v1 fields required")
    if value.get("status") not in {"passed", "failed"} or value.get("validator_version") != VALIDATOR_VERSION:
        raise ValueError("validation_artifact: supported status and validator required")
    for name in ("run_id", "scoring_version", "workflow_version"):
        if not isinstance(value.get(name), str) or not value[name]:
            raise ValueError(f"validation_artifact.{name}: non-empty string required")
    for name in ("policy_hash", "report_hash", "review_hash"):
        if re.fullmatch(r"[0-9a-f]{64}", str(value.get(name, ""))) is None:
            raise ValueError(f"validation_artifact.{name}: sha256 required")
    artifact_hashes = value.get("artifact_hashes")
    required_artifacts = {
        "audit_batch", "candidate_set", "corpus_set", "feature_map_set", "finalist_set",
        "profile_context", "report", "research_bundle", "review", "scorer_config",
    }
    if (
        not isinstance(artifact_hashes, Mapping) or not required_artifacts.issubset(artifact_hashes)
        or any(re.fullmatch(r"[0-9a-f]{64}", str(item)) is None for item in artifact_hashes.values())
    ):
        raise ValueError("validation_artifact.artifact_hashes: exact reproducibility hashes required")
    if value.get("schema_versions") != {"report": "report-v1", "review": "review-v1", "validation": "validation-v1"}:
        raise ValueError("validation_artifact.schema_versions: exact current schema versions required")
    checks = value.get("checks")
    if not isinstance(checks, list) or [item.get("name") for item in checks if isinstance(item, Mapping)] != VALIDATION_CHECK_NAMES:
        raise ValueError("validation_artifact.checks: exact deterministic check set required")
    if any(
        not isinstance(item, Mapping) or set(item) != {"details", "name", "status"}
        or not isinstance(item.get("details"), str) or not item["details"]
        or item["status"] not in {"pass", "fail"}
        for item in checks
    ):
        raise ValueError("validation_artifact.checks: exact check fields required")
    expected = "passed" if all(item["status"] == "pass" for item in checks) else "failed"
    if value["status"] != expected:
        raise ValueError("validation_artifact.status: does not match checks")
    return normalize(dict(value))


def complete_validated_run(
    connection: sqlite3.Connection, *, run_id: str, validation: ArtifactRevision,
    fault_at: FaultInjector = None,
):
    state, _exports = _report_state(connection, Path(connection.execute(
        "SELECT path FROM artifact_exports WHERE run_id=? ORDER BY created_at LIMIT 1", (run_id,),
    ).fetchone()["path"]).parent.parent)
    # StateStore performs a second structural current report/review/validation guard.
    return state.transition(
        run_id, RunState.COMPLETE, actor="validate-cli", reason="current report passed independent review and deterministic validation",
        operation="validation.complete", idempotency_key=validation.content_hash,
        evidence_hashes=(validation.content_hash,), fault_at=fault_at,
    )


def validate_and_complete(
    connection: sqlite3.Connection, *, run_root: Path, run_id: str,
    fault_at: FaultInjector = None,
) -> ValidationRun:
    state, exports = _report_state(connection, run_root)
    prior = state.snapshot(run_id)
    if prior.state is RunState.COMPLETE:
        row, content = _current_artifact(connection, run_id, "validation")
        validate_validation_artifact(content)
        artifact = ArtifactRevision(row["revision_id"], run_id, "validation", row["content_hash"], content, row["schema_version"], row["created_at"], False)
        return ValidationRun(run_id, prior.state.value, prior.state.value, artifact, True)
    if prior.state not in {RunState.REVIEWED, RunState.VALIDATED}:
        raise StateError("validate requires reviewed")
    report_row, report = _current_artifact(connection, run_id, "report")
    review_row, review = _current_artifact(connection, run_id, "review")
    if prior.state is RunState.REVIEWED:
        payload = validate_validation_artifact(build_validation_manifest(
            connection, run_id=run_id, report_row=report_row, report=report,
            review_row=review_row, review=review,
        ))
        target = RunState.VALIDATED if payload["status"] == "passed" else RunState.REVISION_REQUIRED
        result, _export = state.publish_transition(
            run_id, target, actor="validate-cli", reason="deterministic report validation persisted",
            operation="validation.publish", idempotency_key=digest(payload), artifact_kind="validation",
            artifact_content=payload, artifact_schema_version=VALIDATION_VERSION,
            dependencies=(report_row["revision_id"], review_row["revision_id"]),
            export_directory=exports, fault_at=fault_at,
        )
        if result.artifact is None:
            raise RuntimeError("validation publication produced no artifact")
        validation = result.artifact
        replayed = result.replayed
        if target is RunState.REVISION_REQUIRED:
            return ValidationRun(run_id, prior.state.value, result.snapshot.state.value, validation, replayed)
    else:
        row, payload = _current_artifact(connection, run_id, "validation")
        validate_validation_artifact(payload)
        if payload["status"] != "passed" or payload["report_hash"] != report_row["content_hash"] or payload["review_hash"] != review_row["content_hash"]:
            raise StateError("current validation no longer binds the report and review")
        validation = ArtifactRevision(row["revision_id"], run_id, "validation", row["content_hash"], payload, row["schema_version"], row["created_at"], False)
        replayed = False
    completed = state.transition(
        run_id, RunState.COMPLETE, actor="validate-cli", reason="current report passed independent review and deterministic validation",
        operation="validation.complete", idempotency_key=validation.content_hash,
        evidence_hashes=(validation.content_hash,), fault_at=fault_at,
    )
    return ValidationRun(run_id, prior.state.value, completed.snapshot.state.value, validation, replayed or completed.replayed)


__all__ = [
    "VALIDATION_VERSION", "VALIDATOR_VERSION", "ValidationRun", "build_validation_manifest",
    "validate_and_complete", "validate_validation_artifact",
]
