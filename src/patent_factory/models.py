from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .provenance import digest, normalize


class RunState(StrEnum):
    NEW = "new"
    PROFILE_PENDING = "profile_pending"
    CONFLICT_RESOLUTION_REQUIRED = "conflict_resolution_required"
    SENSITIVE_DISCLOSURE_REQUIRED = "sensitive_disclosure_required"
    PROFILE_READY = "profile_ready"
    CREDENTIAL_REQUIRED = "credential_required"
    RESEARCH_READY = "research_ready"
    RESEARCH_RUNNING = "research_running"
    RESEARCH_COMPLETE = "research_complete"
    RESEARCH_INCOMPLETE = "research_incomplete"
    DOMAIN_PIVOT_REQUIRED = "domain_pivot_required"
    IDEATION_RUNNING = "ideation_running"
    CANDIDATES_READY = "candidates_ready"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FINALISTS_READY = "finalists_ready"
    AUDIT_RUNNING = "audit_running"
    COVERAGE_INSUFFICIENT = "coverage_insufficient"
    DECISION_REQUIRED = "decision_required"
    AUDIT_APPROVED = "audit_approved"
    DRAFT_READY = "draft_ready"
    REVIEW_REQUIRED = "review_required"
    REVISION_REQUIRED = "revision_required"
    REVIEWED = "reviewed"
    VALIDATED = "validated"
    COMPLETE = "complete"
    STOPPED = "stopped"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({RunState.COMPLETE, RunState.STOPPED, RunState.CANCELLED})


class GateKind(StrEnum):
    CONFLICT_RESOLUTION = "conflict_resolution"
    CREDENTIAL = "credential"
    SENSITIVE_DISCLOSURE = "sensitive_disclosure"
    DOMAIN_PIVOT = "domain_pivot"
    COVERAGE = "coverage"
    EXCESSIVE_SIMILARITY = "excessive_similarity"


class AdapterFailureKind(StrEnum):
    AUTH = "auth"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    ACCESS_DENIED = "access_denied"
    MALFORMED = "malformed"
    OVERSIZE = "oversize"
    UNSUPPORTED = "unsupported"
    NETWORK = "network"
    INTERNAL = "internal"


@dataclass(frozen=True)
class QueryEnvelope:
    run_id: str
    adapter: str
    adapter_version: str
    capability: str
    allowed_scheme: str
    allowed_host: str
    deadline_seconds: float
    page: int
    page_cap: int
    result_budget: int
    byte_budget: int
    retry_budget: int
    retry_ownership: str
    query_projection: Mapping[str, Any]
    cursor: str | None = None

    def validate(self) -> None:
        required = (
            self.run_id,
            self.adapter,
            self.adapter_version,
            self.capability,
            self.allowed_host,
            self.retry_ownership,
        )
        if any(not normalize(item) for item in required):
            raise ValueError("query_envelope: required field missing")
        if self.allowed_scheme != "https":
            raise ValueError("query_envelope.allowed_scheme: https required")
        if not 0 < self.deadline_seconds <= 60:
            raise ValueError("query_envelope.deadline_seconds: must be in (0,60]")
        if not 1 <= self.page <= self.page_cap <= 100:
            raise ValueError("query_envelope.page: outside page budget")
        if not 1 <= self.result_budget <= 500:
            raise ValueError("query_envelope.result_budget: must be between 1 and 500")
        if not 1 <= self.byte_budget <= 10_000_000:
            raise ValueError("query_envelope.byte_budget: outside byte budget")
        if not 0 <= self.retry_budget <= 3:
            raise ValueError("query_envelope.retry_budget: outside retry budget")
        if not isinstance(self.query_projection, Mapping):
            raise ValueError("query_envelope.query_projection: object required")

    def request_body(self) -> dict[str, Any]:
        self.validate()
        return {
            "adapter": normalize(self.adapter), "adapter_version": normalize(self.adapter_version),
            "allowed_host": normalize(self.allowed_host).casefold(), "allowed_scheme": self.allowed_scheme,
            "byte_budget": self.byte_budget, "capability": normalize(self.capability),
            "cursor": normalize(self.cursor) if self.cursor else None, "deadline_seconds": self.deadline_seconds,
            "page": self.page, "page_cap": self.page_cap,
            "query_projection": normalize(dict(self.query_projection)), "result_budget": self.result_budget,
            "retry_budget": self.retry_budget, "retry_ownership": normalize(self.retry_ownership),
            "run_id": normalize(self.run_id),
        }

    @property
    def request_fingerprint(self) -> str:
        return "rq_" + digest(self.request_body())[:20]


@dataclass(frozen=True)
class AdapterFailure:
    kind: AdapterFailureKind
    message: str
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "message": normalize(self.message), "retryable": self.retryable}


@dataclass(frozen=True)
class AdapterRecord:
    source_type: str
    source_locator: str
    original_identifier: str
    title: str
    content_hash: str
    language: str
    canonical_url: str | None = None
    filing_date: str | None = None
    applicant: str | None = None
    abstract: str | None = None
    classifications: tuple[str, ...] = ()
    excerpt_hashes: tuple[str, ...] = ()
    interpretations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def validate(self) -> None:
        for name in ("source_type", "source_locator", "original_identifier", "title", "content_hash", "language"):
            if not normalize(getattr(self, name)):
                raise ValueError(f"adapter_record.{name}: required")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return normalize({
            "abstract": self.abstract, "applicant": self.applicant, "canonical_url": self.canonical_url,
            "classifications": list(self.classifications), "content_hash": self.content_hash,
            "excerpt_hashes": list(self.excerpt_hashes), "filing_date": self.filing_date,
            "interpretations": list(self.interpretations), "language": self.language,
            "limitations": list(self.limitations), "original_identifier": self.original_identifier,
            "source_locator": self.source_locator, "source_type": self.source_type, "title": self.title,
        })


@dataclass(frozen=True)
class AdapterResult:
    records: tuple[AdapterRecord, ...]
    response_hash: str | None
    terms_note: str
    coverage: Mapping[str, Any]
    next_cursor: str | None = None
    rate_limit: Mapping[str, Any] | None = None
    failure: AdapterFailure | None = None

    @property
    def successful(self) -> bool:
        return self.failure is None

    def validate(self) -> None:
        if not normalize(self.terms_note):
            raise ValueError("adapter_result.terms_note: required")
        if not isinstance(self.coverage, Mapping):
            raise ValueError("adapter_result.coverage: object required")
        if self.failure is not None:
            if self.records:
                raise ValueError("adapter_result: failure cannot contain evidence records")
            return
        if not self.response_hash:
            raise ValueError("adapter_result.response_hash: required for success")
        for record in self.records:
            record.validate()


@dataclass(frozen=True)
class ArtifactRevision:
    revision_id: str
    run_id: str
    kind: str
    content_hash: str
    content: dict[str, Any]
    schema_version: str
    created_at: str
    stale: bool = False


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    state: RunState
    state_version: int
    current_revisions: dict[str, str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TransitionResult:
    snapshot: RunSnapshot
    event_id: str
    artifact: ArtifactRevision | None = None
    replayed: bool = False
    suspended_operation: str | None = None


@dataclass(frozen=True)
class GateEnvelope:
    gate_id: str
    run_id: str
    kind: GateKind
    suspended_state: RunState
    suspended_operation: str
    subject_revision_hash: str
    approval_scope: dict[str, Any]
    approval_scope_hash: str
    return_state: RunState
    created_at: str
    status: str = "pending"


@dataclass(frozen=True)
class GateDecision:
    decision_id: str
    gate_id: str
    run_id: str
    action: str
    actor: str
    subject_revision_hash: str
    approval_scope_hash: str
    suspended_operation: str
    return_state: RunState
    reason: str
    created_at: str
    stale: bool = False
    consumed_at: str | None = None
    used_at: str | None = None
    consumed_by_event_id: str | None = None
