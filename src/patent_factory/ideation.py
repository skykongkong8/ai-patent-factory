from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import EvaluationConfig
from .database import FaultInjector, profile_payload
from .models import ArtifactRevision, GateEnvelope, GateKind, RunState
from .provenance import Claim, EpistemicLabel, canonical_json, claim_from_dict, digest, normalize
from .privacy import assert_canaries_absent, credential_canaries
from .state import StateError, StateStore, workspace_export_directories


CANDIDATE_SCHEMA_VERSION = "candidate-v1"
SYNTHESIS_METHODS = frozenset({"modify", "combine", "adapt", "constrain", "transfer"})
PROFILE_REF_KINDS = frozenset({"problem", "capability"})


class DomainPivotRequiredError(RuntimeError):
    def __init__(self, gate: GateEnvelope) -> None:
        super().__init__("domain_pivot_required: approve or reject the exact proposed domain change")
        self.gate = gate


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}: object required")
    return value


def _exact_fields(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value))
    if unknown:
        raise ValueError(f"{path}: unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{path}: missing fields: {', '.join(missing)}")


def _text(value: Any, path: str) -> str:
    item = normalize(value)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{path}: non-empty string required")
    return item


def _texts(value: Any, path: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: array required")
    items = tuple(_text(item, f"{path}[{index}]") for index, item in enumerate(value))
    if not allow_empty and not items:
        raise ValueError(f"{path}: at least one item required")
    if len(set(items)) != len(items):
        raise ValueError(f"{path}: duplicate items are not allowed")
    return items


@dataclass(frozen=True)
class ProfileReference:
    field: str
    kind: str
    claim_id: str

    @classmethod
    def from_dict(
        cls, value: Any, path: str,
        claim_categories: Mapping[tuple[str, str], frozenset[str]],
    ) -> "ProfileReference":
        item = _object(value, path)
        _exact_fields(item, {"claim_id", "field", "kind"}, path)
        field = _text(item["field"], f"{path}.field")
        kind = _text(item["kind"], f"{path}.kind")
        claim_id = _text(item["claim_id"], f"{path}.claim_id")
        if kind not in PROFILE_REF_KINDS:
            raise ValueError(f"{path}.kind: problem or capability required")
        identity = (field, claim_id)
        if identity not in claim_categories:
            raise ValueError(f"{path}.claim_id: unknown profile claim")
        if kind not in claim_categories[identity]:
            raise ValueError(f"{path}: reference kind does not match authoritative profile category")
        return cls(field, kind, claim_id)

    def as_dict(self) -> dict[str, str]:
        return {"claim_id": self.claim_id, "field": self.field, "kind": self.kind}


@dataclass(frozen=True)
class EvidenceReference:
    evidence_id: str
    content_hash: str
    span_hash: str | None
    limitation: str | None

    @classmethod
    def from_dict(
        cls, value: Any, path: str, evidence: Mapping[str, Mapping[str, Any]],
    ) -> "EvidenceReference":
        item = _object(value, path)
        _exact_fields(item, {"content_hash", "evidence_id", "limitation", "span_hash"}, path)
        evidence_id = _text(item["evidence_id"], f"{path}.evidence_id")
        content_hash = _text(item["content_hash"], f"{path}.content_hash")
        span_hash = _text(item["span_hash"], f"{path}.span_hash") if item["span_hash"] is not None else None
        limitation = _text(item["limitation"], f"{path}.limitation") if item["limitation"] is not None else None
        record = evidence.get(evidence_id)
        if record is None:
            raise ValueError(f"{path}.evidence_id: unknown evidence revision")
        if content_hash != record["content_hash"]:
            raise ValueError(f"{path}.content_hash: does not match evidence revision")
        record_json = record.get("record_json")
        normalized_record = json.loads(record_json) if isinstance(record_json, str) else record_json
        spans = set(normalized_record.get("excerpt_hashes", ())) if isinstance(normalized_record, dict) else set()
        if span_hash is not None and span_hash not in spans:
            raise ValueError(f"{path}.span_hash: does not belong to evidence revision")
        if span_hash is None and limitation is None:
            raise ValueError(f"{path}: span_hash or explicit limitation required")
        return cls(evidence_id, content_hash, span_hash, limitation)

    def as_dict(self) -> dict[str, Any]:
        return normalize({
            "content_hash": self.content_hash,
            "evidence_id": self.evidence_id,
            "limitation": self.limitation,
            "span_hash": self.span_hash,
        })


@dataclass(frozen=True)
class SynthesisTrace:
    method: str
    narrative: str
    evidence_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, path: str, available: set[str]) -> "SynthesisTrace":
        item = _object(value, path)
        _exact_fields(item, {"evidence_ids", "method", "narrative"}, path)
        method = _text(item["method"], f"{path}.method")
        if method not in SYNTHESIS_METHODS:
            raise ValueError(f"{path}.method: unsupported synthesis method")
        evidence_ids = _texts(item["evidence_ids"], f"{path}.evidence_ids")
        if not set(evidence_ids).issubset(available):
            raise ValueError(f"{path}.evidence_ids: must reference candidate evidence")
        return cls(method, _text(item["narrative"], f"{path}.narrative"), evidence_ids)

    def as_dict(self) -> dict[str, Any]:
        return {"evidence_ids": list(self.evidence_ids), "method": self.method, "narrative": self.narrative}


@dataclass(frozen=True)
class CandidateClaim:
    """One candidate field, its epistemic label, and the evidence backing it.

    Per-field citations EXTEND this structure instead of living in a new
    sibling ``field -> evidence_references`` map. CandidateClaim is already the
    field-keyed per-field structure: it carries ``field`` plus the ``Claim``
    that labels it. A parallel map would duplicate the same key space and could
    drift out of sync with the labels — a field could end up asserting
    creative_suggestion here while carrying prior-art citations there, which is
    exactly the citation-hygiene defect this field exists to close.

    ``evidence_references`` is REQUIRED but may be empty: a field with no
    evidentiary support states that explicitly rather than by omission, and
    _exact_fields then rejects any pre-change candidate input loudly instead of
    silently defaulting it to the candidate-level blob. Every reference must
    also appear in the candidate-level ``evidence_references``, so the report's
    _cited_ids and appendix stay complete.
    """

    field: str
    claim: Claim
    evidence_references: tuple[EvidenceReference, ...] = ()

    @classmethod
    def from_dict(
        cls, value: Any, path: str, evidence: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> "CandidateClaim":
        item = _object(value, path)
        _exact_fields(item, {"claim", "evidence_references", "field"}, path)
        raw_references = item["evidence_references"]
        if not isinstance(raw_references, list):
            raise ValueError(f"{path}.evidence_references: array required")
        references = tuple(
            EvidenceReference.from_dict(
                reference, f"{path}.evidence_references[{index}]", evidence or {},
            )
            for index, reference in enumerate(raw_references)
        )
        if len({reference.evidence_id for reference in references}) != len(references):
            raise ValueError(f"{path}.evidence_references: duplicate evidence revisions are not allowed")
        return cls(
            _text(item["field"], f"{path}.field"),
            claim_from_dict(dict(_object(item["claim"], f"{path}.claim")), f"{path}.claim"),
            references,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim.as_dict(),
            "evidence_references": [reference.as_dict() for reference in self.evidence_references],
            "field": self.field,
        }


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    domain: str
    title: str
    technical_problem: str
    mechanism: str
    required_inputs: tuple[str, ...]
    components: tuple[str, ...]
    interactions: tuple[str, ...]
    transformations: tuple[str, ...]
    outputs: tuple[str, ...]
    expected_effects: tuple[str, ...]
    implementation_example: str
    measurable_validation: str
    unresolved_dependencies: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    profile_references: tuple[ProfileReference, ...]
    evidence_references: tuple[EvidenceReference, ...]
    synthesis_trace: SynthesisTrace
    claims: tuple[CandidateClaim, ...]
    profile_revision_hash: str
    research_revision_hash: str
    evaluation_config_hash: str

    @classmethod
    def from_dict(
        cls,
        value: Any,
        path: str,
        *,
        profile_claim_categories: Mapping[tuple[str, str], frozenset[str]],
        evidence: Mapping[str, Mapping[str, Any]],
        profile_revision_hash: str,
        research_revision_hash: str,
        evaluation_config_hash: str,
    ) -> "Candidate":
        item = _object(value, path)
        fields = {
            "claims", "components", "domain", "evidence_references", "expected_effects",
            "implementation_example", "interactions", "mechanism", "measurable_validation",
            "outputs", "profile_references", "required_inputs", "synthesis_trace",
            "technical_problem", "title", "transformations", "unresolved_dependencies",
            "unresolved_questions",
        }
        _exact_fields(item, fields, path)
        raw_profile_refs = item["profile_references"]
        if not isinstance(raw_profile_refs, list):
            raise ValueError(f"{path}.profile_references: array required")
        raw_identities = [
            (
                _text(_object(ref, f"{path}.profile_references[{index}]").get("field"), f"{path}.profile_references[{index}].field"),
                _text(_object(ref, f"{path}.profile_references[{index}]").get("claim_id"), f"{path}.profile_references[{index}].claim_id"),
            )
            for index, ref in enumerate(raw_profile_refs)
        ]
        if len(set(raw_identities)) != len(raw_identities):
            raise ValueError(f"{path}.profile_references: distinct authoritative fact references are required")
        profile_refs = tuple(
            ProfileReference.from_dict(
                ref, f"{path}.profile_references[{index}]", profile_claim_categories,
            )
            for index, ref in enumerate(raw_profile_refs)
        )
        if not profile_refs or {ref.kind for ref in profile_refs} != PROFILE_REF_KINDS:
            raise ValueError(f"{path}.profile_references: problem and capability references required")
        evidence_refs = tuple(
            EvidenceReference.from_dict(ref, f"{path}.evidence_references[{index}]", evidence)
            for index, ref in enumerate(item["evidence_references"])
        ) if isinstance(item["evidence_references"], list) else ()
        if not evidence_refs:
            raise ValueError(f"{path}.evidence_references: at least one evidence revision required")
        if len({ref.evidence_id for ref in evidence_refs}) != len(evidence_refs):
            raise ValueError(f"{path}.evidence_references: duplicate evidence revisions are not allowed")
        available = {ref.evidence_id for ref in evidence_refs}
        claims = tuple(
            CandidateClaim.from_dict(entry, f"{path}.claims[{index}]", evidence)
            for index, entry in enumerate(item["claims"])
        ) if isinstance(item["claims"], list) else ()
        for index, entry in enumerate(claims):
            unbound = sorted(
                reference.evidence_id for reference in entry.evidence_references
                if reference.evidence_id not in available
            )
            if unbound:
                raise ValueError(
                    f"{path}.claims[{index}].evidence_references: not referenced by the candidate: {', '.join(unbound)}"
                )
        required_claim_fields = {"technical_problem", "mechanism", "expected_effects", "synthesis_trace"}
        if not required_claim_fields.issubset({entry.field for entry in claims}):
            raise ValueError(f"{path}.claims: required candidate fields must carry epistemic labels")
        if not any(entry.claim.label is EpistemicLabel.CREATIVE_SUGGESTION for entry in claims):
            raise ValueError(f"{path}.claims: a creative_suggestion label is required")
        for index, entry in enumerate(claims):
            source_id = entry.claim.source_id
            if entry.claim.label in {EpistemicLabel.SOURCE_FACT, EpistemicLabel.SOURCE_INFERENCE} and (
                not source_id or not source_id.startswith("ev_")
            ):
                raise ValueError(f"{path}.claims[{index}]: source claims require a current evidence revision")
            if source_id and source_id.startswith("ev_"):
                reference = next((ref for ref in evidence_refs if ref.evidence_id == source_id), None)
                if reference is None:
                    raise ValueError(f"{path}.claims[{index}]: source evidence is not referenced")
                if entry.claim.label in {EpistemicLabel.SOURCE_FACT, EpistemicLabel.SOURCE_INFERENCE} and (
                    entry.claim.content_hash != reference.content_hash
                    or entry.claim.span_hash != reference.span_hash
                ):
                    raise ValueError(f"{path}.claims[{index}]: source claim does not match exact evidence revision and span")
        body = normalize({
            "claims": [entry.as_dict() for entry in claims],
            "components": list(_texts(item["components"], f"{path}.components")),
            "domain": _text(item["domain"], f"{path}.domain"),
            "evaluation_config_hash": evaluation_config_hash,
            "evidence_references": [ref.as_dict() for ref in evidence_refs],
            "expected_effects": list(_texts(item["expected_effects"], f"{path}.expected_effects")),
            "implementation_example": _text(item["implementation_example"], f"{path}.implementation_example"),
            "interactions": list(_texts(item["interactions"], f"{path}.interactions")),
            "mechanism": _text(item["mechanism"], f"{path}.mechanism"),
            "measurable_validation": _text(item["measurable_validation"], f"{path}.measurable_validation"),
            "outputs": list(_texts(item["outputs"], f"{path}.outputs")),
            "profile_references": [ref.as_dict() for ref in profile_refs],
            "profile_revision_hash": profile_revision_hash,
            "required_inputs": list(_texts(item["required_inputs"], f"{path}.required_inputs")),
            "research_revision_hash": research_revision_hash,
            "synthesis_trace": SynthesisTrace.from_dict(item["synthesis_trace"], f"{path}.synthesis_trace", available).as_dict(),
            "technical_problem": _text(item["technical_problem"], f"{path}.technical_problem"),
            "title": _text(item["title"], f"{path}.title"),
            "transformations": list(_texts(item["transformations"], f"{path}.transformations")),
            "unresolved_dependencies": list(_texts(item["unresolved_dependencies"], f"{path}.unresolved_dependencies", allow_empty=True)),
            "unresolved_questions": list(_texts(item["unresolved_questions"], f"{path}.unresolved_questions", allow_empty=True)),
        })
        candidate_id = "ca_" + digest(body)[:20]
        return cls(
            candidate_id=candidate_id,
            domain=body["domain"], title=body["title"], technical_problem=body["technical_problem"],
            mechanism=body["mechanism"], required_inputs=tuple(body["required_inputs"]),
            components=tuple(body["components"]), interactions=tuple(body["interactions"]),
            transformations=tuple(body["transformations"]), outputs=tuple(body["outputs"]),
            expected_effects=tuple(body["expected_effects"]), implementation_example=body["implementation_example"],
            measurable_validation=body["measurable_validation"],
            unresolved_dependencies=tuple(body["unresolved_dependencies"]),
            unresolved_questions=tuple(body["unresolved_questions"]), profile_references=profile_refs,
            evidence_references=evidence_refs,
            synthesis_trace=SynthesisTrace.from_dict(item["synthesis_trace"], f"{path}.synthesis_trace", available),
            claims=claims, profile_revision_hash=profile_revision_hash,
            research_revision_hash=research_revision_hash, evaluation_config_hash=evaluation_config_hash,
        )

    def as_dict(self) -> dict[str, Any]:
        return normalize({
            "candidate_id": self.candidate_id, "claims": [entry.as_dict() for entry in self.claims],
            "components": list(self.components), "domain": self.domain,
            "evaluation_config_hash": self.evaluation_config_hash,
            "evidence_references": [ref.as_dict() for ref in self.evidence_references],
            "expected_effects": list(self.expected_effects), "implementation_example": self.implementation_example,
            "interactions": list(self.interactions), "mechanism": self.mechanism,
            "measurable_validation": self.measurable_validation, "outputs": list(self.outputs),
            "profile_references": [ref.as_dict() for ref in self.profile_references],
            "profile_revision_hash": self.profile_revision_hash, "required_inputs": list(self.required_inputs),
            "research_revision_hash": self.research_revision_hash,
            "synthesis_trace": self.synthesis_trace.as_dict(), "technical_problem": self.technical_problem,
            "title": self.title, "transformations": list(self.transformations),
            "unresolved_dependencies": list(self.unresolved_dependencies),
            "unresolved_questions": list(self.unresolved_questions),
        })


@dataclass(frozen=True)
class IdeationRun:
    run_id: str
    prior_state: str
    next_state: str
    artifact: ArtifactRevision
    candidate_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ids": [self.artifact.revision_id], "candidate_ids": list(self.candidate_ids),
            "command": "ideate", "next_state": self.next_state, "prior_state": self.prior_state,
            "replayed": self.replayed, "run_id": self.run_id, "status": "candidates_ready",
            "transition_event_ids": list(self.event_ids),
        }


def _private_exports(run_root: Path, name: str, *, create: bool) -> Path:
    root = Path(run_root).absolute()
    if not root.is_dir() or stat.S_ISLNK(root.lstat().st_mode):
        raise ValueError("ideation_export: safe run directory required")
    exports = root / name
    if exports.exists() and (stat.S_ISLNK(exports.lstat().st_mode) or not exports.is_dir()):
        raise ValueError("ideation_export: unsafe export directory")
    if create:
        exports.mkdir(mode=0o700, exist_ok=True)
        try:
            os.chmod(exports, 0o700, follow_symlinks=False)
        except OSError:
            pass
    return exports


def _state_with_exports(
    connection: sqlite3.Connection, run_root: Path, *, create_ideation: bool,
) -> tuple[StateStore, Path]:
    root = Path(run_root).absolute()
    ideation_exports = _private_exports(root, "ideation-exports", create=create_ideation)
    own = (ideation_exports,) if ideation_exports.exists() else ()
    directories = workspace_export_directories(connection, root, own)
    return StateStore(connection, export_directories=directories), ideation_exports


def _current_artifact(connection: sqlite3.Connection, run_id: str, kind: str) -> ArtifactRevision:
    row = connection.execute(
        "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
        "WHERE ca.run_id=? AND ca.kind=? AND ar.stale=0",
        (run_id, kind),
    ).fetchone()
    if row is None:
        raise StateError(f"current {kind} artifact is required")
    return ArtifactRevision(
        row["revision_id"], row["run_id"], row["kind"], row["content_hash"],
        json.loads(row["content_json"]), row["schema_version"], row["created_at"], bool(row["stale"]),
    )


def _profile_field_category(field: str) -> str | None:
    normalized = normalize(field).casefold()
    problem_markers = ("problem", "constraint", "challenge", "pain", "need", "opportunity")
    capability_markers = ("capability", "competenc", "skill", "expertise", "experience", "domain", "interest")
    if normalized == "project_summary" or any(marker in normalized for marker in problem_markers):
        return "problem"
    if any(marker in normalized for marker in capability_markers):
        return "capability"
    return None


def _profile_claim_categories(profile: Mapping[str, Any]) -> dict[tuple[str, str], frozenset[str]]:
    if profile.get("profile_version") != "profile-v1" or profile.get("state") != "profile_ready":
        raise ValueError("profile_context: current profile_ready export required")
    if profile.get("conflicts") or not isinstance(profile.get("facts"), Mapping):
        raise ValueError("profile_context: unresolved conflicts or malformed facts")
    categories: dict[tuple[str, str], set[str]] = {}
    for field, entry in profile["facts"].items():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("claims"), list):
            raise ValueError("profile_context: malformed fact claims")
        category = _profile_field_category(str(field))
        for claim in entry["claims"]:
            parsed = claim_from_dict(dict(_object(claim, "profile_context.claim")), "profile_context.claim")
            serialized = parsed.as_dict()
            if claim.get("claim_id") and claim["claim_id"] != serialized["claim_id"]:
                raise ValueError("profile_context.claim: claim_id mismatch")
            if category is not None:
                categories.setdefault((str(field), serialized["claim_id"]), set()).add(category)
    return {identity: frozenset(values) for identity, values in categories.items()}


def _profile_domain(profile: Mapping[str, Any]) -> str:
    facts = profile["facts"]
    entry = facts.get("technical_domain") if isinstance(facts, Mapping) else None
    return _text(entry.get("value"), "profile_context.technical_domain") if isinstance(entry, Mapping) else ""


def _research_evidence(research: ArtifactRevision) -> dict[str, Mapping[str, Any]]:
    records = research.content.get("evidence")
    if not isinstance(records, list):
        raise ValueError("research_bundle: evidence list required")
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("evidence_id"), str):
            raise ValueError("research_bundle: malformed evidence record")
        result[record["evidence_id"]] = record
    return result


def run_ideation(
    connection: sqlite3.Connection,
    *,
    profile_connection: sqlite3.Connection,
    run_root: Path,
    run_id: str,
    profile: Mapping[str, Any],
    candidate_input: Mapping[str, Any],
    config: EvaluationConfig,
    domain_decision_id: str | None = None,
    fault_at: FaultInjector = None,
) -> IdeationRun:
    """Validate and publish one deterministic candidate-set revision without model or network access."""

    canaries = credential_canaries()
    assert_canaries_absent(
        candidate_input, canaries,
        boundary="candidate_input",
    )
    authoritative_profile = profile_payload(profile_connection)
    assert_canaries_absent(
        authoritative_profile, canaries,
        boundary="profile_context",
    )
    if canonical_json(profile) != canonical_json(authoritative_profile):
        raise ValueError("profile_context: supplied export does not match authoritative profile database")
    profile = authoritative_profile

    state, _exports = _state_with_exports(connection, run_root, create_ideation=False)
    prior = state.snapshot(run_id)
    if prior.state not in {RunState.RESEARCH_COMPLETE, RunState.RESEARCH_INCOMPLETE, RunState.IDEATION_RUNNING, RunState.CANDIDATES_READY}:
        raise StateError("ideation requires research_complete or research_incomplete")
    research = _current_artifact(connection, run_id, "research_bundle")
    evidence = _research_evidence(research)
    profile_claim_categories = _profile_claim_categories(profile)
    profile_revision_id = _text(profile.get("profile_revision"), "profile_context.profile_revision")
    profile_hash = digest(profile)
    config_payload = config.as_dict()
    config_hash = config.content_hash
    profile_context_payload = normalize({
        "profile": profile,
        "profile_revision_hash": profile_hash,
        "profile_revision_id": profile_revision_id,
        "version": "profile-context-v1",
    })
    assert_canaries_absent(
        {"evaluation_config": config_payload, "profile_context": profile_context_payload},
        canaries,
        boundary="ideation_context",
    )
    existing_profile_context = connection.execute(
        "SELECT ar.content_json FROM artifact_revisions ar "
        "JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
        "WHERE ca.run_id=? AND ca.kind='profile_context' AND ar.stale=0",
        (run_id,),
    ).fetchone()
    if (
        existing_profile_context is not None
        and canonical_json(json.loads(existing_profile_context["content_json"])) != canonical_json(profile_context_payload)
    ):
        raise ValueError("profile_context: current run is bound to a different profile revision")
    request = _object(candidate_input, "candidate_input")
    _exact_fields(request, {"candidates", "schema_version"}, "candidate_input")
    if request["schema_version"] != "candidate-input-v1" or not isinstance(request["candidates"], list):
        raise ValueError("candidate_input: candidate-input-v1 with candidates array required")
    candidates = tuple(
        Candidate.from_dict(
            value, f"candidate_input.candidates[{index}]",
            profile_claim_categories=profile_claim_categories,
            evidence=evidence, profile_revision_hash=profile_hash,
            research_revision_hash=research.content_hash, evaluation_config_hash=config_hash,
        )
        for index, value in enumerate(request["candidates"])
    )
    if not candidates:
        raise ValueError("candidate_input.candidates: at least one candidate required")
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise ValueError("candidate_input.candidates: duplicate candidates are not allowed")

    profile_context = state.add_revision(
        run_id, "profile_context", profile_context_payload, schema_version="profile-context-v1",
    )
    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.candidate_id))
    request_context = normalize({
        "candidate_ids": [candidate.candidate_id for candidate in ordered],
        "candidate_input_hash": digest(request),
        "evaluation_config_hash": config_hash,
        "profile_revision_hash": profile_hash,
        "research_revision_hash": research.content_hash,
        "run_id": run_id,
        "version": "ideation-request-v1",
    })
    request_fingerprint = "ir_" + digest(request_context)[:20]
    canonical_domain = _profile_domain(profile)
    proposed_domains = sorted({candidate.domain for candidate in candidates if candidate.domain != canonical_domain})
    if proposed_domains:
        request_revision = state.add_revision(
            run_id, "ideation_request", request_context, schema_version="ideation-request-v1",
            dependencies=(profile_context.revision_id, research.revision_id),
        )
        scope = {
            "evaluation_config_hash": config_hash,
            "new_domain_hashes": [digest(domain) for domain in proposed_domains],
            "old_domain_hash": digest(canonical_domain),
            "profile_revision_hash": profile_hash,
            "purpose": "candidate ideation",
            "request_fingerprint": request_fingerprint,
            "research_revision_hash": research.content_hash,
        }
        suspended_operation = f"ideation.publish:{request_fingerprint}"
        if domain_decision_id:
            row = connection.execute(
                "SELECT gd.action,gd.stale,gd.subject_revision_hash,gd.suspended_operation,"
                "gd.used_at,gd.consumed_by_event_id,ge.approval_scope_json "
                "FROM gate_decisions gd JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
                "WHERE gd.decision_id=? AND gd.run_id=? AND ge.kind='domain_pivot'",
                (domain_decision_id, run_id),
            ).fetchone()
            if row is None or row["action"] != "approve" or row["stale"] or row["subject_revision_hash"] != request_revision.content_hash or row["suspended_operation"] != suspended_operation:
                raise StateError("domain decision does not match the exact current ideation request")
            if row["used_at"]:
                replay = connection.execute(
                    "SELECT event_id FROM idempotency_records WHERE run_id=? AND operation=?",
                    (run_id, suspended_operation),
                ).fetchone()
                if replay is None or replay["event_id"] != row["consumed_by_event_id"]:
                    raise StateError("domain decision was used by a different operation")
            else:
                state.consume_decision(
                    domain_decision_id, suspended_operation=suspended_operation,
                    subject_revision_hash=request_revision.content_hash,
                    approval_scope=json.loads(row["approval_scope_json"]),
                )
        else:
            gate = state.suspend_gate(
                run_id, GateKind.DOMAIN_PIVOT, suspended_operation=suspended_operation,
                subject_revision_hash=request_revision.content_hash, approval_scope=scope,
                return_state=prior.state, actor="ideation-cli", reason="candidate domain differs from current profile",
            )
            raise DomainPivotRequiredError(gate)

    context = normalize({
        "evaluation_config": config_payload, "evaluation_config_hash": config_hash,
        "request_fingerprint": request_fingerprint,
        "profile_context_revision_id": profile_context.revision_id,
        "profile_revision_hash": profile_hash,
        "research_revision_hash": research.content_hash, "version": "ideation-context-v1",
    })
    payload = {
        "candidates": [candidate.as_dict() for candidate in ordered],
        "context_hash": digest(context), "evaluation_config_hash": config_hash,
        "profile_revision_hash": profile_hash, "research_revision_hash": research.content_hash,
        "run_id": run_id, "version": "candidate-set-v1",
    }
    input_hash = digest({"context": context, "payload": payload, "research_revision_id": research.revision_id})
    started = state.transition(
        run_id, RunState.IDEATION_RUNNING, actor="ideation-cli", reason="validated ideation context",
        operation="ideation.start", idempotency_key=input_hash, artifact_kind="ideation_context",
        artifact_content=context, artifact_schema_version="ideation-context-v1",
        dependencies=(profile_context.revision_id, research.revision_id,), fault_at=fault_at,
    )
    if started.artifact is None:
        raise RuntimeError("ideation start did not publish its context")
    state, exports = _state_with_exports(connection, run_root, create_ideation=True)
    finished, _export = state.publish_transition(
        run_id, RunState.CANDIDATES_READY, actor="ideation-cli", reason="candidate set persisted",
        operation=f"ideation.publish:{request_fingerprint}" if domain_decision_id else "ideation.publish",
        idempotency_key=input_hash, artifact_kind="candidate_set",
        artifact_content=payload, artifact_schema_version="candidate-set-v1",
        dependencies=(profile_context.revision_id, started.artifact.revision_id, research.revision_id),
        export_directory=exports,
        evidence_hashes=tuple(ref.evidence_id for candidate in ordered for ref in candidate.evidence_references),
        consumed_decision_id=domain_decision_id,
        fault_at=fault_at,
    )
    if finished.artifact is None:
        raise RuntimeError("ideation finish did not publish its candidate set")
    return IdeationRun(
        run_id, prior.state.value, finished.snapshot.state.value, finished.artifact,
        tuple(candidate.candidate_id for candidate in ordered),
        tuple(dict.fromkeys((started.event_id, finished.event_id))), started.replayed or finished.replayed,
    )


def candidate_map(revision: ArtifactRevision) -> dict[str, Mapping[str, Any]]:
    if revision.kind != "candidate_set" or revision.schema_version != "candidate-set-v1":
        raise ValueError("candidate_set: current candidate-set-v1 artifact required")
    candidates = revision.content.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate_set: candidates array required")
    result = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("candidate_id"), str):
            raise ValueError("candidate_set: malformed candidate")
        result[candidate["candidate_id"]] = candidate
    return result


__all__ = [
    "CANDIDATE_SCHEMA_VERSION", "Candidate", "DomainPivotRequiredError", "EvidenceReference",
    "IdeationRun", "candidate_map", "run_ideation", "_current_artifact", "_private_exports",
    "_research_evidence", "_state_with_exports", "_text", "_texts", "_object", "_exact_fields",
]
