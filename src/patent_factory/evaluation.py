from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from .config import EvaluationConfig
from .database import FaultInjector
from .ideation import (
    EvidenceReference,
    _current_artifact,
    _exact_fields,
    _object,
    _research_evidence,
    _state_with_exports,
    _text,
    _texts,
    candidate_map,
)
from .models import ArtifactRevision, RunState
from .provenance import digest, normalize
from .privacy import assert_canaries_absent, credential_canaries
from .state import StateError, StateStore


REQUIRED_AXES = ("differentiation", "technical_feasibility", "utility_significance")
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})


@dataclass(frozen=True)
class EvaluationAxis:
    axis: str
    score: int
    rubric_version: str
    rationale: str
    confidence: str
    supporting_evidence_references: tuple[EvidenceReference, ...]
    contrary_evidence_references: tuple[EvidenceReference, ...]
    gaps: tuple[str, ...]
    coverage_assessment: str
    coverage_limitations: tuple[str, ...]

    @classmethod
    def from_dict(
        cls,
        value: Any,
        path: str,
        *,
        expected_axis: str,
        expected_rubric: str,
        evidence: Mapping[str, Mapping[str, Any]],
        candidate_evidence_ids: set[str],
    ) -> "EvaluationAxis":
        item = _object(value, path)
        fields = {
            "axis", "confidence", "contrary_evidence_references", "coverage_assessment",
            "coverage_limitations", "gaps", "rationale", "rubric_version", "score",
            "supporting_evidence_references",
        }
        _exact_fields(item, fields, path)
        axis = _text(item["axis"], f"{path}.axis")
        if axis != expected_axis:
            raise ValueError(f"{path}.axis: expected {expected_axis}")
        score = item["score"]
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise ValueError(f"{path}.score: integer between 0 and 100 required")
        rubric = _text(item["rubric_version"], f"{path}.rubric_version")
        if rubric != expected_rubric or rubric.startswith("simrisk-"):
            raise ValueError(f"{path}.rubric_version: expected preliminary G004 rubric")
        confidence = _text(item["confidence"], f"{path}.confidence")
        if confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"{path}.confidence: low, medium, or high required")
        support_input = item["supporting_evidence_references"]
        contrary_input = item["contrary_evidence_references"]
        if not isinstance(support_input, list) or not isinstance(contrary_input, list):
            raise ValueError(f"{path}: supporting and contrary evidence arrays required")
        supporting = tuple(
            EvidenceReference.from_dict(ref, f"{path}.supporting_evidence_references[{index}]", evidence)
            for index, ref in enumerate(support_input)
        )
        contrary = tuple(
            EvidenceReference.from_dict(ref, f"{path}.contrary_evidence_references[{index}]", evidence)
            for index, ref in enumerate(contrary_input)
        )
        if not supporting:
            raise ValueError(f"{path}.supporting_evidence_references: at least one reference required")
        all_ids = {ref.evidence_id for ref in supporting + contrary}
        if not all_ids.issubset(candidate_evidence_ids):
            raise ValueError(f"{path}: evidence must be traced by the candidate")
        gaps = _texts(item["gaps"], f"{path}.gaps", allow_empty=True)
        coverage_assessment = _text(item["coverage_assessment"], f"{path}.coverage_assessment")
        coverage_limitations = _texts(
            item["coverage_limitations"], f"{path}.coverage_limitations", allow_empty=True,
        )
        if (not contrary or not gaps) and not (coverage_assessment or coverage_limitations):
            raise ValueError(f"{path}: empty contrary evidence or gaps requires coverage assessment")
        return cls(
            axis, score, rubric, _text(item["rationale"], f"{path}.rationale"), confidence,
            supporting, contrary, gaps, coverage_assessment, coverage_limitations,
        )

    def as_dict(self) -> dict[str, Any]:
        return normalize({
            "axis": self.axis, "confidence": self.confidence,
            "contrary_evidence_references": [ref.as_dict() for ref in self.contrary_evidence_references],
            "coverage_assessment": self.coverage_assessment,
            "coverage_limitations": list(self.coverage_limitations), "gaps": list(self.gaps),
            "rationale": self.rationale, "rubric_version": self.rubric_version, "score": self.score,
            "supporting_evidence_references": [ref.as_dict() for ref in self.supporting_evidence_references],
        })


@dataclass(frozen=True)
class Finalist:
    finalist_id: str
    candidate_id: str
    candidate_revision_hash: str
    rank: int
    selection_priority: int
    selection_rationale: str
    axes: tuple[EvaluationAxis, ...]

    def as_dict(self) -> dict[str, Any]:
        return normalize({
            "axes": [axis.as_dict() for axis in self.axes], "candidate_id": self.candidate_id,
            "candidate_revision_hash": self.candidate_revision_hash, "finalist_id": self.finalist_id,
            "rank": self.rank, "selection_priority": self.selection_priority,
            "selection_rationale": self.selection_rationale,
        })


@dataclass(frozen=True)
class Exclusion:
    candidate_id: str
    reason_codes: tuple[str, ...]
    rationale: str

    @classmethod
    def from_dict(cls, value: Any, path: str, candidates: set[str]) -> "Exclusion":
        item = _object(value, path)
        _exact_fields(item, {"candidate_id", "rationale", "reason_codes"}, path)
        candidate_id = _text(item["candidate_id"], f"{path}.candidate_id")
        if candidate_id not in candidates:
            raise ValueError(f"{path}.candidate_id: unknown candidate")
        return cls(
            candidate_id, _texts(item["reason_codes"], f"{path}.reason_codes"),
            _text(item["rationale"], f"{path}.rationale"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "rationale": self.rationale, "reason_codes": list(self.reason_codes)}


@dataclass(frozen=True)
class InsufficiencyReport:
    eligible_candidate_ids: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    recommended_research: tuple[str, ...]

    @classmethod
    def from_dict(
        cls, value: Any, path: str, *, candidates: set[str], selected: set[str], excluded: set[str],
    ) -> "InsufficiencyReport":
        item = _object(value, path)
        fields = {
            "eligible_candidate_ids", "limitations", "missing_evidence", "reason_codes",
            "recommended_research", "rejected_candidate_ids", "unresolved_questions",
        }
        _exact_fields(item, fields, path)
        eligible = _texts(item["eligible_candidate_ids"], f"{path}.eligible_candidate_ids", allow_empty=True)
        rejected = _texts(item["rejected_candidate_ids"], f"{path}.rejected_candidate_ids", allow_empty=True)
        if set(eligible) != selected:
            raise ValueError(f"{path}.eligible_candidate_ids: must equal structurally complete selections")
        if not set(rejected).issubset(candidates) or selected & set(rejected):
            raise ValueError(f"{path}.rejected_candidate_ids: invalid candidate set")
        if not set(rejected).issubset(excluded):
            raise ValueError(f"{path}.rejected_candidate_ids: rejected candidates need exclusion reasons")
        return cls(
            eligible, rejected, _texts(item["reason_codes"], f"{path}.reason_codes"),
            _texts(item["missing_evidence"], f"{path}.missing_evidence"),
            _texts(item["limitations"], f"{path}.limitations"),
            _texts(item["unresolved_questions"], f"{path}.unresolved_questions"),
            _texts(item["recommended_research"], f"{path}.recommended_research"),
        )

    def as_dict(self) -> dict[str, Any]:
        return normalize({
            "eligible_candidate_ids": list(self.eligible_candidate_ids), "limitations": list(self.limitations),
            "missing_evidence": list(self.missing_evidence), "reason_codes": list(self.reason_codes),
            "recommended_research": list(self.recommended_research),
            "rejected_candidate_ids": list(self.rejected_candidate_ids),
            "unresolved_questions": list(self.unresolved_questions),
        })


@dataclass(frozen=True)
class ShortlistRun:
    run_id: str
    prior_state: str
    next_state: str
    artifact: ArtifactRevision
    finalist_ids: tuple[str, ...]
    event_id: str
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        insufficient = self.next_state == RunState.INSUFFICIENT_EVIDENCE.value
        return {
            "artifact_ids": [self.artifact.revision_id], "command": "shortlist",
            "finalist_ids": list(self.finalist_ids), "next_state": self.next_state,
            "prior_state": self.prior_state, "replayed": self.replayed, "run_id": self.run_id,
            "status": "insufficient_evidence" if insufficient else "finalists_ready",
            "transition_event_ids": [self.event_id],
        }


def _parse_finalists(
    values: Any,
    *,
    candidates: Mapping[str, Mapping[str, Any]],
    candidate_revision_hash: str,
    evidence: Mapping[str, Mapping[str, Any]],
    config: EvaluationConfig,
) -> tuple[Finalist, ...]:
    if not isinstance(values, list):
        raise ValueError("shortlist_input.finalists: array required")
    prepared = []
    for index, raw in enumerate(values):
        path = f"shortlist_input.finalists[{index}]"
        item = _object(raw, path)
        _exact_fields(item, {"axes", "candidate_id", "priority", "selection_rationale"}, path)
        candidate_id = _text(item["candidate_id"], f"{path}.candidate_id")
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise ValueError(f"{path}.candidate_id: unknown candidate")
        priority = item["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
            raise ValueError(f"{path}.priority: positive integer required")
        axes_input = item["axes"]
        if not isinstance(axes_input, list) or len(axes_input) != len(REQUIRED_AXES):
            raise ValueError(f"{path}.axes: exactly three independent axes required")
        by_name = {}
        for axis_raw in axes_input:
            axis_item = _object(axis_raw, f"{path}.axes")
            axis_name = _text(axis_item.get("axis"), f"{path}.axes.axis")
            if axis_name in by_name:
                raise ValueError(f"{path}.axes: duplicate axis")
            by_name[axis_name] = axis_raw
        if set(by_name) != set(REQUIRED_AXES):
            raise ValueError(f"{path}.axes: exact required axes are mandatory")
        candidate_evidence_ids = {
            ref["evidence_id"] for ref in candidate.get("evidence_references", ()) if isinstance(ref, Mapping)
        }
        axes = tuple(
            EvaluationAxis.from_dict(
                by_name[name], f"{path}.axes.{name}", expected_axis=name,
                expected_rubric=config.rubrics[name], evidence=evidence,
                candidate_evidence_ids=candidate_evidence_ids,
            )
            for name in REQUIRED_AXES
        )
        prepared.append((priority, candidate_id, _text(item["selection_rationale"], f"{path}.selection_rationale"), axes))
    if len({item[1] for item in prepared}) != len(prepared):
        raise ValueError("shortlist_input.finalists: duplicate candidates are not allowed")
    ordered = sorted(prepared, key=lambda item: (item[0], item[1]))
    finalists = []
    for rank, (priority, candidate_id, rationale, axes) in enumerate(ordered, start=1):
        body = {
            "axes": [axis.as_dict() for axis in axes], "candidate_id": candidate_id,
            "candidate_revision_hash": candidate_revision_hash, "rank": rank,
            "selection_priority": priority, "selection_rationale": rationale,
        }
        finalists.append(Finalist(
            "fi_" + digest(body)[:20], candidate_id, candidate_revision_hash,
            rank, priority, rationale, axes,
        ))
    return tuple(finalists)


def run_shortlist(
    connection: sqlite3.Connection,
    *,
    run_root,
    run_id: str,
    shortlist_input: Mapping[str, Any],
    config: EvaluationConfig,
    fault_at: FaultInjector = None,
) -> ShortlistRun:
    """Publish finalists, or an immutable explicit insufficiency result, without scoring/network side effects."""

    canaries = credential_canaries()
    assert_canaries_absent(
        shortlist_input, canaries,
        boundary="shortlist_input",
    )
    state, _exports = _state_with_exports(connection, run_root, create_ideation=False)
    prior = state.snapshot(run_id)
    if prior.state not in {RunState.CANDIDATES_READY, RunState.FINALISTS_READY, RunState.INSUFFICIENT_EVIDENCE}:
        raise StateError("shortlist requires candidates_ready")
    candidate_revision = _current_artifact(connection, run_id, "candidate_set")
    research_revision = _current_artifact(connection, run_id, "research_bundle")
    candidates = candidate_map(candidate_revision)
    evidence = _research_evidence(research_revision)
    request = _object(shortlist_input, "shortlist_input")
    _exact_fields(request, {"exclusions", "finalists", "insufficiency", "schema_version"}, "shortlist_input")
    if request["schema_version"] != "shortlist-input-v1":
        raise ValueError("shortlist_input.schema_version: shortlist-input-v1 required")
    finalists = _parse_finalists(
        request["finalists"], candidates=candidates,
        candidate_revision_hash=candidate_revision.content_hash, evidence=evidence, config=config,
    )
    finalist_candidate_ids = {finalist.candidate_id for finalist in finalists}
    exclusions_input = request["exclusions"]
    if not isinstance(exclusions_input, list):
        raise ValueError("shortlist_input.exclusions: array required")
    exclusions = tuple(
        Exclusion.from_dict(value, f"shortlist_input.exclusions[{index}]", set(candidates))
        for index, value in enumerate(exclusions_input)
    )
    excluded_ids = {exclusion.candidate_id for exclusion in exclusions}
    if len(excluded_ids) != len(exclusions) or excluded_ids & finalist_candidate_ids:
        raise ValueError("shortlist_input.exclusions: duplicate or selected candidate")
    if finalist_candidate_ids | excluded_ids != set(candidates):
        raise ValueError("shortlist_input: every candidate must be selected or explicitly excluded")

    base = {
        "candidate_revision_hash": candidate_revision.content_hash,
        "evaluation_config_hash": config.content_hash,
        "exclusions": [exclusion.as_dict() for exclusion in sorted(exclusions, key=lambda item: item.candidate_id)],
        "research_revision_hash": research_revision.content_hash, "run_id": run_id,
    }
    if len(finalists) >= config.minimum_finalists:
        if request["insufficiency"] is not None:
            raise ValueError("shortlist_input.insufficiency: must be null when finalists are sufficient")
        payload = {
            **base, "finalists": [finalist.as_dict() for finalist in finalists],
            "minimum_finalists": config.minimum_finalists, "version": "finalist-set-v1",
        }
        target = RunState.FINALISTS_READY
        kind = "finalist_set"
        schema = "finalist-set-v1"
    else:
        report = InsufficiencyReport.from_dict(
            request["insufficiency"], "shortlist_input.insufficiency", candidates=set(candidates),
            selected=finalist_candidate_ids, excluded=excluded_ids,
        )
        payload = {
            **base, "candidate_count": len(candidates), "finalist_count": 0,
            "insufficiency": report.as_dict(), "minimum_finalists": config.minimum_finalists,
            "version": "insufficiency-v1",
        }
        target = RunState.INSUFFICIENT_EVIDENCE
        kind = "insufficiency"
        schema = "insufficiency-v1"

    operation_hash = digest({
        "candidate_revision_id": candidate_revision.revision_id,
        "config": config.as_dict(), "payload": payload,
    })
    state, exports = _state_with_exports(connection, run_root, create_ideation=True)
    finished, _export = state.publish_transition(
        run_id, target, actor="shortlist-cli", reason="finalist selection persisted",
        operation="shortlist.publish", idempotency_key=operation_hash, artifact_kind=kind,
        artifact_content=payload, artifact_schema_version=schema,
        dependencies=(candidate_revision.revision_id,), export_directory=exports,
        evidence_hashes=tuple(
            ref.evidence_id for finalist in finalists for axis in finalist.axes
            for ref in axis.supporting_evidence_references + axis.contrary_evidence_references
        ),
        fault_at=fault_at,
    )
    if finished.artifact is None:
        raise RuntimeError("shortlist did not publish its result artifact")
    return ShortlistRun(
        run_id, prior.state.value, finished.snapshot.state.value, finished.artifact,
        tuple(finalist.finalist_id for finalist in finalists) if target is RunState.FINALISTS_READY else (),
        finished.event_id, finished.replayed,
    )


__all__ = [
    "CONFIDENCE_LEVELS", "REQUIRED_AXES", "EvaluationAxis", "Finalist", "InsufficiencyReport",
    "ShortlistRun", "run_shortlist",
]
