"""Read-only draft builders for the versioned request inputs.

Each builder reads authoritative state (run database and/or profile database)
and emits a *draft* request object with every hash/ID binding pre-filled from
real revisions and every human-judgment field stubbed with a ``TODO(agent):``
marker. The core verbs still validate authoritatively on submission — a
scaffold only removes clerical hash-copying, never judgment.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from .config import EvaluationConfig
from .database import profile_payload
from .ideation import _profile_claim_categories, _profile_domain
from .provenance import Claim, EpistemicLabel, normalize
from .report import (
    DEFAULT_REPORT_LANGUAGE,
    REPORT_INPUT_VERSION_V2,
    REPORT_LANGUAGES,
    _current_artifact,
)

TODO = "TODO(agent): "
_FORBIDDEN_PROFILE_FIELDS = {"api_key", "credential", "email", "name", "phone", "raw_document", "secret"}


class ScaffoldError(ValueError):
    """A scaffold precondition is unmet (missing upstream state or facts)."""


def _evidence_records(connection: sqlite3.Connection, run_id: str) -> list[Mapping[str, Any]]:
    _row, research = _current_artifact(connection, run_id, "research_bundle")
    records = research.get("evidence")
    if not isinstance(records, list) or not records:
        raise ScaffoldError("scaffold requires a current research bundle with evidence — run /research first")
    return records


def _evidence_reference(record: Mapping[str, Any]) -> dict[str, Any]:
    record_json = record.get("record_json")
    normalized = json.loads(record_json) if isinstance(record_json, str) else (record_json or {})
    spans = normalized.get("excerpt_hashes") if isinstance(normalized, Mapping) else None
    span = spans[0] if isinstance(spans, list) and spans else None
    return {
        "content_hash": record["content_hash"],
        "evidence_id": record["evidence_id"],
        "limitation": None if span else "no recorded span; whole-record reference",
        "span_hash": span,
    }


def scaffold_candidate_input(
    connection: sqlite3.Connection,
    profile_connection: sqlite3.Connection,
    *,
    run_id: str,
    count: int = 3,
) -> dict[str, Any]:
    if not 1 <= count <= 12:
        raise ScaffoldError("scaffold candidate count must be between 1 and 12")
    profile = profile_payload(profile_connection)
    categories = _profile_claim_categories(profile)
    problem_ref = next(
        ({"claim_id": claim_id, "field": field, "kind": "problem"}
         for (field, claim_id), kinds in sorted(categories.items()) if "problem" in kinds),
        None,
    )
    capability_ref = next(
        ({"claim_id": claim_id, "field": field, "kind": "capability"}
         for (field, claim_id), kinds in sorted(categories.items()) if "capability" in kinds),
        None,
    )
    if problem_ref is None or capability_ref is None:
        raise ScaffoldError(
            "scaffold requires both a problem-like and a capability-like profile fact "
            "(e.g. project_summary and expertise) — enrich the profile with /setup first"
        )
    domain = _profile_domain(profile) or TODO + "technical domain"
    evidence = _evidence_records(connection, run_id)
    hypothesis = Claim(EpistemicLabel.HYPOTHESIS).as_dict()
    creative = Claim(EpistemicLabel.CREATIVE_SUGGESTION).as_dict()
    candidates = []
    for index in range(count):
        reference = _evidence_reference(evidence[index % len(evidence)])
        candidates.append({
            "claims": [
                {"claim": dict(hypothesis), "field": "technical_problem"},
                {"claim": dict(creative), "field": "mechanism"},
                {"claim": dict(hypothesis), "field": "expected_effects"},
                {"claim": dict(creative), "field": "synthesis_trace"},
            ],
            "components": [TODO + "key component"],
            "domain": domain,
            "evidence_references": [reference],
            "expected_effects": [TODO + "expected measurable technical effect"],
            "implementation_example": TODO + "concrete implementation example",
            "interactions": [TODO + "how the components interact"],
            "mechanism": TODO + "proposed mechanism (modify/combine/adapt a researched one)",
            "measurable_validation": TODO + "how the effect would be measured",
            "outputs": [TODO + "output"],
            "profile_references": [dict(problem_ref), dict(capability_ref)],
            "required_inputs": [TODO + "required input"],
            "synthesis_trace": {
                "evidence_ids": [reference["evidence_id"]],
                "method": "combine",
                "narrative": TODO + "which researched mechanisms were combined/adapted, and what the ~10-30% creative delta is",
            },
            "technical_problem": TODO + "the concrete technical problem",
            "title": TODO + f"candidate title {index + 1}",
            "transformations": [TODO + "core transformation"],
            "unresolved_dependencies": [],
            "unresolved_questions": [TODO + "open question for the user"],
        })
    return {"candidates": candidates, "schema_version": "candidate-input-v1"}


def scaffold_shortlist_input(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    config: EvaluationConfig,
) -> dict[str, Any]:
    _row, candidate_set = _current_artifact(connection, run_id, "candidate_set")
    candidates = candidate_set.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ScaffoldError("scaffold requires a current candidate set — run /ideate first")
    finalists = []
    for index, candidate in enumerate(candidates[:3], start=1):
        supporting = [
            dict(reference)
            for reference in candidate.get("evidence_references", [])
            if isinstance(reference, Mapping) and reference.get("evidence_id")
        ]
        finalists.append({
            "axes": [
                {
                    "axis": axis,
                    "confidence": "medium",
                    "contrary_evidence_references": [],
                    "coverage_assessment": TODO + "what the current evidence does and does not cover",
                    "coverage_limitations": [TODO + "known coverage limitation"],
                    "gaps": [],
                    "rationale": TODO + f"why this candidate scores as it does on {axis}",
                    "rubric_version": config.rubrics[axis],
                    "score": 0,
                    "supporting_evidence_references": supporting,
                }
                for axis in ("differentiation", "technical_feasibility", "utility_significance")
            ],
            "candidate_id": candidate.get("candidate_id"),
            "priority": index,
            "selection_rationale": TODO + "why this candidate is a finalist",
        })
    return {
        "exclusions": [],
        "finalists": finalists,
        "insufficiency": None,
        "schema_version": "shortlist-input-v1",
    }


def scaffold_audit_query_input(
    connection: sqlite3.Connection,
    *,
    run_id: str,
) -> dict[str, Any]:
    row, finalist_set = _current_artifact(connection, run_id, "finalist_set")
    finalists = finalist_set.get("finalists")
    if not isinstance(finalists, list) or not finalists:
        raise ScaffoldError("scaffold requires a current finalist set — run /shortlist first")
    return {
        "finalist_set_hash": row["content_hash"],
        "groups": [
            {
                "finalist_id": finalist.get("finalist_id"),
                "queries": [
                    {"language": "ko", "term": TODO + "Korean search term for this finalist"},
                    {"language": "en", "term": TODO + "English search term for this finalist"},
                ],
            }
            for finalist in finalists
        ],
        "schema_version": "audit-query-input-v1",
    }


def scaffold_report_input(
    profile_connection: sqlite3.Connection,
    *,
    language: str = DEFAULT_REPORT_LANGUAGE,
) -> dict[str, Any]:
    if language not in REPORT_LANGUAGES:
        raise ScaffoldError("scaffold report language must be en or ko")
    profile = profile_payload(profile_connection)
    facts = profile.get("facts")
    fields = sorted(
        field for field in (facts or {})
        if normalize(str(field)).casefold() not in _FORBIDDEN_PROFILE_FIELDS
    )
    if not fields:
        raise ScaffoldError("scaffold requires renderable technical profile fields — run /setup first")
    return {
        "drafter": {"id": TODO + "drafter id", "pass_id": TODO + "draft pass id", "type": "agent"},
        "handoff_questions": [TODO + "question for the patent attorney"],
        "language": language,
        "profile_fields": fields,
        "recommended_investigations": [TODO + "recommended follow-up investigation"],
        "report_date": TODO + "YYYY-MM-DD",
        "revision": None,
        "schema_version": REPORT_INPUT_VERSION_V2,
        "sensitive_disclosures": [],
    }


def evidence_binding_table(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Per-evidence hash table echoed to the agent for later label upgrades."""

    table = []
    for record in _evidence_records(connection, run_id):
        record_json = record.get("record_json")
        normalized = json.loads(record_json) if isinstance(record_json, str) else (record_json or {})
        spans = normalized.get("excerpt_hashes") if isinstance(normalized, Mapping) else None
        table.append({
            "content_hash": record.get("content_hash"),
            "evidence_id": record.get("evidence_id"),
            "excerpt_hashes": list(spans) if isinstance(spans, list) else [],
            "title": record.get("title"),
        })
    return table


def count_todos(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.startswith(TODO) else 0
    if isinstance(value, Mapping):
        return sum(count_todos(item) for item in value.values())
    if isinstance(value, list):
        return sum(count_todos(item) for item in value)
    return 0


__all__ = [
    "ScaffoldError", "TODO", "count_todos", "evidence_binding_table",
    "scaffold_audit_query_input", "scaffold_candidate_input", "scaffold_report_input",
    "scaffold_shortlist_input",
]
