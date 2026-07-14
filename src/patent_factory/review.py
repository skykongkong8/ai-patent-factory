from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .database import FaultInjector
from .models import ArtifactRevision, RunState
from .privacy import assert_canaries_absent, environment_secret
from .provenance import digest, normalize
from .report import _current_artifact, _report_state, load_report_policy, validate_report_artifact
from .state import StateError


REVIEW_INPUT_VERSION = "review-input-v1"
REVIEW_VERSION = "review-v1"


@dataclass(frozen=True)
class ReviewRun:
    run_id: str
    prior_state: str
    next_state: str
    artifact: ArtifactRevision
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ids": [self.artifact.revision_id], "command": "review",
            "next_state": self.next_state, "prior_state": self.prior_state,
            "replayed": self.replayed, "run_id": self.run_id, "status": self.next_state,
        }


def _text(value: Any, path: str) -> str:
    item = normalize(value)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{path}: non-empty string required")
    return item


def validate_review_input(value: Mapping[str, Any], *, report: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "checks", "decision_gate_verification", "disposition", "evidence_corrections",
        "findings", "prohibited_language_findings", "report_hash", "reviewer", "schema_version",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != REVIEW_INPUT_VERSION:
        raise ValueError("review_input: exact review-input-v1 fields required")
    reviewer = value["reviewer"]
    if not isinstance(reviewer, Mapping) or set(reviewer) != {"id", "pass_id", "type"}:
        raise ValueError("review_input.reviewer: exact identity fields required")
    resolved_reviewer = {name: _text(reviewer[name], f"review_input.reviewer.{name}") for name in ("id", "pass_id", "type")}
    if resolved_reviewer["type"] not in {"agent", "human"}:
        raise ValueError("review_input.reviewer.type: agent or human required")
    drafter = report.get("drafter", {})
    if resolved_reviewer["id"] == drafter.get("id") or resolved_reviewer["pass_id"] == drafter.get("pass_id"):
        raise ValueError("review_input.reviewer: reviewer identity and pass must be independent from drafter")
    policy = load_report_policy()
    checks = value["checks"]
    if not isinstance(checks, list):
        raise ValueError("review_input.checks: array required")
    resolved_checks = []
    for index, raw in enumerate(checks):
        path = f"review_input.checks[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"details", "name", "status"}:
            raise ValueError(f"{path}: exact check fields required")
        item = {"details": _text(raw["details"], f"{path}.details"), "name": _text(raw["name"], f"{path}.name"), "status": _text(raw["status"], f"{path}.status")}
        if item["status"] not in {"pass", "fail"}:
            raise ValueError(f"{path}.status: pass or fail required")
        resolved_checks.append(item)
    if [item["name"] for item in resolved_checks] != policy["required_review_checks"]:
        raise ValueError("review_input.checks: exact policy-owned sorted checks required")
    findings = value["findings"]
    if not isinstance(findings, list):
        raise ValueError("review_input.findings: array required")
    resolved_findings = []
    for index, raw in enumerate(findings):
        path = f"review_input.findings[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"check", "code", "message", "path", "severity"}:
            raise ValueError(f"{path}: exact finding fields required")
        item = {name: _text(raw[name], f"{path}.{name}") for name in ("check", "code", "message", "path", "severity")}
        if item["check"] not in policy["required_review_checks"] or item["severity"] not in {"advisory", "blocking"}:
            raise ValueError(f"{path}: supported check and severity required")
        resolved_findings.append(item)
    corrections = value["evidence_corrections"]
    if not isinstance(corrections, list):
        raise ValueError("review_input.evidence_corrections: array required")
    resolved_corrections = []
    for index, raw in enumerate(corrections):
        path = f"review_input.evidence_corrections[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"evidence_id", "field", "reason", "replacement"}:
            raise ValueError(f"{path}: exact correction fields required")
        correction = {name: _text(raw[name], f"{path}.{name}") for name in ("evidence_id", "field", "reason", "replacement")}
        if re.fullmatch(r"ev_[0-9a-f]{16}", correction["evidence_id"]) is None:
            raise ValueError(f"{path}.evidence_id: canonical evidence identifier required")
        resolved_corrections.append(correction)
    prohibited = value["prohibited_language_findings"]
    if not isinstance(prohibited, list):
        raise ValueError("review_input.prohibited_language_findings: array required")
    resolved_prohibited = []
    for index, raw in enumerate(prohibited):
        path = f"review_input.prohibited_language_findings[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"phrase", "section"}:
            raise ValueError(f"{path}: exact prohibited-language fields required")
        resolved_prohibited.append({"phrase": _text(raw["phrase"], f"{path}.phrase"), "section": _text(raw["section"], f"{path}.section")})
    gate = value["decision_gate_verification"]
    if not isinstance(gate, Mapping) or set(gate) != {"audit_hash", "covered_finalist_ids", "status"}:
        raise ValueError("review_input.decision_gate_verification: exact fields required")
    covered = gate["covered_finalist_ids"]
    if not isinstance(covered, list) or any(not isinstance(item, str) or not item for item in covered) or covered != sorted(set(covered)):
        raise ValueError("review_input.decision_gate_verification.covered_finalist_ids: sorted unique strings required")
    resolved_gate = {
        "audit_hash": _text(gate["audit_hash"], "review_input.decision_gate_verification.audit_hash"),
        "covered_finalist_ids": covered,
        "status": _text(gate["status"], "review_input.decision_gate_verification.status"),
    }
    if resolved_gate["status"] not in {"pass", "fail"}:
        raise ValueError("review_input.decision_gate_verification.status: pass or fail required")
    blocking = (
        any(item["status"] == "fail" for item in resolved_checks)
        or any(item["severity"] == "blocking" for item in resolved_findings)
        or bool(resolved_corrections) or bool(resolved_prohibited) or resolved_gate["status"] == "fail"
    )
    expected = "revise" if blocking else "approved"
    if value["disposition"] != expected:
        raise ValueError("review_input.disposition: must match the derived review result")
    return normalize({
        "checks": resolved_checks, "decision_gate_verification": resolved_gate,
        "disposition": expected, "evidence_corrections": resolved_corrections,
        "findings": resolved_findings, "prohibited_language_findings": resolved_prohibited,
        "report_hash": _text(value["report_hash"], "review_input.report_hash"),
        "reviewer": resolved_reviewer, "schema_version": REVIEW_INPUT_VERSION,
    })


def validate_review_artifact(value: Mapping[str, Any], *, report: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "checks", "decision_gate_verification", "disposition", "evidence_corrections", "findings",
        "policy_hash", "prohibited_language_findings", "report_bindings", "report_hash", "reviewer",
        "run_id", "version",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("version") != REVIEW_VERSION:
        raise ValueError("review_artifact: exact review-v1 fields required")
    if not isinstance(value.get("run_id"), str) or not value["run_id"]:
        raise ValueError("review_artifact.run_id: non-empty string required")
    if re.fullmatch(r"[0-9a-f]{64}", str(value.get("report_hash", ""))) is None:
        raise ValueError("review_artifact.report_hash: sha256 required")
    if value.get("policy_hash") != report.get("policy_hash") or value.get("report_bindings") != report.get("bindings"):
        raise ValueError("review_artifact: report policy and artifact bindings must match")
    # Reuse the input contract to keep exact-field and derived-disposition parity.
    validate_review_input({
        "checks": value["checks"], "decision_gate_verification": value["decision_gate_verification"],
        "disposition": value["disposition"], "evidence_corrections": value["evidence_corrections"],
        "findings": value["findings"], "prohibited_language_findings": value["prohibited_language_findings"],
        "report_hash": value["report_hash"], "reviewer": value["reviewer"],
        "schema_version": REVIEW_INPUT_VERSION,
    }, report=report)
    return normalize(dict(value))


def run_review(
    connection: sqlite3.Connection, *, run_root: Path, run_id: str,
    review_input: Mapping[str, Any], fault_at: FaultInjector = None,
) -> ReviewRun:
    secret = environment_secret("KIPRIS_PLUS_API_KEY")
    assert_canaries_absent(review_input, (secret,) if secret else (), boundary="review_input")
    state, exports = _report_state(connection, run_root)
    prior = state.snapshot(run_id)
    if prior.state not in {RunState.DRAFT_READY, RunState.REVIEW_REQUIRED, RunState.REVIEWED, RunState.REVISION_REQUIRED}:
        raise StateError("review requires draft_ready")
    report_row, report = _current_artifact(connection, run_id, "report")
    validate_report_artifact(report)
    # Review never trusts the caller-authored legal-language checkbox: the
    # deterministic sentence-local policy scan is an independent prerequisite.
    from .validation import _legal_language_check

    _legal_language_check(report)
    request = validate_review_input(review_input, report=report)
    if request["report_hash"] != report_row["content_hash"]:
        raise StateError("review must bind the current report artifact hash")
    audit_hash = report["bindings"]["audit_batch"]
    if request["decision_gate_verification"]["audit_hash"] != audit_hash:
        raise ValueError("review decision-gate verification must bind the report audit")
    audit_row, audit = _current_artifact(connection, run_id, "audit_batch")
    if audit_row["content_hash"] != audit_hash:
        raise StateError("review report audit binding is stale")
    affected = sorted(item["finalist_id"] for item in audit.get("results", []) if item.get("outcome") == "decision_required")
    if request["decision_gate_verification"]["covered_finalist_ids"] != affected:
        raise ValueError("review decision-gate coverage does not match the current audit")
    input_hash = digest({"request": request, "report_revision_id": report_row["revision_id"]})
    if prior.state is RunState.DRAFT_READY:
        state.transition(
            run_id, RunState.REVIEW_REQUIRED, actor="review-cli", reason="independent review started",
            operation="review.start", idempotency_key=input_hash,
        )
    payload = normalize({
        "checks": request["checks"], "decision_gate_verification": request["decision_gate_verification"],
        "disposition": request["disposition"], "evidence_corrections": request["evidence_corrections"],
        "findings": request["findings"], "policy_hash": report["policy_hash"],
        "prohibited_language_findings": request["prohibited_language_findings"],
        "report_bindings": report["bindings"], "report_hash": report_row["content_hash"],
        "reviewer": request["reviewer"], "run_id": run_id, "version": REVIEW_VERSION,
    })
    validate_review_artifact(payload, report=report)
    target = RunState.REVIEWED if payload["disposition"] == "approved" else RunState.REVISION_REQUIRED
    result, _export = state.publish_transition(
        run_id, target, actor="review-cli", reason="independent report review persisted",
        operation="review.publish", idempotency_key=input_hash, artifact_kind="review",
        artifact_content=payload, artifact_schema_version=REVIEW_VERSION,
        dependencies=(report_row["revision_id"],), export_directory=exports, fault_at=fault_at,
    )
    if result.artifact is None:
        raise RuntimeError("review publication produced no artifact")
    return ReviewRun(run_id, prior.state.value, result.snapshot.state.value, result.artifact, result.replayed)


__all__ = [
    "REVIEW_INPUT_VERSION", "REVIEW_VERSION", "ReviewRun", "run_review",
    "validate_review_artifact", "validate_review_input",
]
