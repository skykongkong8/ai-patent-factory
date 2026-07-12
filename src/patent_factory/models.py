from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


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
