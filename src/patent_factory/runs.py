from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .database import FaultInjector, profile_payload
from .models import ArtifactRevision, RunState
from .privacy import assert_canaries_absent, environment_secret
from .paths import enforce_private_directory
from .provenance import canonical_json, digest, normalize
from .state import StateError, StateStore, workspace_export_directories


RUN_START_VERSION = "run-start-v1"
PROFILE_CONTEXT_VERSION = "profile-context-v1"
StageFault = tuple[str, FaultInjector] | None


@dataclass(frozen=True)
class RunStart:
    run_id: str
    prior_state: str
    next_state: str
    profile_revision: str
    artifact: ArtifactRevision
    event_ids: tuple[str, ...]
    export_path: str
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ids": [self.artifact.revision_id],
            "command": "run.start",
            "event_ids": list(self.event_ids),
            "export_path": self.export_path,
            "next_state": self.next_state,
            "prior_state": self.prior_state,
            "profile_revision": self.profile_revision,
            "replayed": self.replayed,
            "run_id": self.run_id,
            "status": self.next_state,
            "version": RUN_START_VERSION,
        }


@dataclass(frozen=True)
class PreparedRunProfile:
    revision: str
    content_hash: str
    context: Mapping[str, Any]


def _fault(value: StageFault, stage: str) -> FaultInjector:
    return value[1] if value is not None and value[0] == stage else None


def _state(connection: sqlite3.Connection, run_root: Path) -> tuple[StateStore, Path]:
    root = Path(run_root).absolute()
    enforce_private_directory(root, "run_start run directory")
    exports = root / "bootstrap-exports"
    exports.mkdir(mode=0o700, exist_ok=True)
    enforce_private_directory(exports, "run_start bootstrap export directory")
    directories = workspace_export_directories(connection, root, (exports,))
    return StateStore(connection, export_directories=directories), exports


def prepare_run_profile(
    profile_connection: sqlite3.Connection, profile: Mapping[str, Any],
) -> PreparedRunProfile:
    """Validate and bind an exported profile to its authoritative database."""

    authoritative = profile_payload(profile_connection)
    secret = environment_secret("KIPRIS_PLUS_API_KEY")
    assert_canaries_absent(
        authoritative, (secret,) if secret else (), boundary="run_start.profile_context",
    )
    if canonical_json(profile) != canonical_json(authoritative):
        raise ValueError("run_start: supplied profile export does not match authoritative profile database")
    if (
        authoritative.get("profile_version") != "profile-v1"
        or authoritative.get("state") != "profile_ready"
        or authoritative.get("conflicts")
        or not isinstance(authoritative.get("facts"), Mapping)
    ):
        raise ValueError("run_start: authoritative profile must be current and profile_ready")
    profile_revision = normalize(authoritative.get("profile_revision"))
    if not isinstance(profile_revision, str) or not profile_revision:
        raise ValueError("run_start: authoritative profile revision is missing")
    profile_hash = digest(authoritative)
    profile_context = normalize({
        "profile": authoritative,
        "profile_revision_hash": profile_hash,
        "profile_revision_id": profile_revision,
        "version": PROFILE_CONTEXT_VERSION,
    })
    assert_canaries_absent(
        profile_context, (secret,) if secret else (), boundary="run_start.profile_context",
    )
    return PreparedRunProfile(profile_revision, profile_hash, profile_context)


def start_run(
    connection: sqlite3.Connection,
    *,
    profile_connection: sqlite3.Connection,
    run_root: Path,
    run_id: str,
    profile: Mapping[str, Any],
    prepared_profile: PreparedRunProfile | None = None,
    fault_at: StageFault = None,
) -> RunStart:
    """Bind one private run to an exact authoritative profile and make research ready."""

    normalized_run_id = normalize(run_id)
    if not isinstance(normalized_run_id, str) or not normalized_run_id:
        raise ValueError("run_start.run_id: non-empty string required")
    prepared = prepared_profile or prepare_run_profile(profile_connection, profile)
    profile_revision = prepared.revision
    profile_hash = prepared.content_hash
    profile_context = prepared.context

    state, exports = _state(connection, run_root)
    try:
        prior_state = state.snapshot(normalized_run_id).state.value
    except StateError as exc:
        if str(exc) != "run_not_found":
            raise
        prior_state = RunState.NEW.value
    created = state.ensure_run(
        normalized_run_id, actor="run-start-cli", reason="private run initialized",
        fault_at=_fault(fault_at, "create"),
    )
    identity = digest({"profile_hash": profile_hash, "run_id": normalized_run_id})
    pending = state.transition(
        normalized_run_id, RunState.PROFILE_PENDING, actor="run-start-cli",
        reason="authoritative profile binding started", operation="run.start.profile_pending",
        idempotency_key=identity, fault_at=_fault(fault_at, "profile_pending"),
    )
    ready = state.transition(
        normalized_run_id, RunState.PROFILE_READY, actor="run-start-cli",
        reason="authoritative profile verified", operation="run.start.profile_ready",
        idempotency_key=identity, evidence_hashes=(profile_hash,),
        fault_at=_fault(fault_at, "profile_ready"),
    )
    published, exported = state.publish_transition(
        normalized_run_id, RunState.RESEARCH_READY, actor="run-start-cli",
        reason="profile context registered and research enabled",
        operation="run.start.profile_context", idempotency_key=identity,
        artifact_kind="profile_context", artifact_content=profile_context,
        artifact_schema_version=PROFILE_CONTEXT_VERSION, evidence_hashes=(profile_hash,),
        export_directory=exports, fault_at=_fault(fault_at, "profile_context"),
    )
    if published.artifact is None:
        raise RuntimeError("run_start: profile context publication produced no artifact")
    event_ids = tuple(dict.fromkeys((
        created.event_id, pending.event_id, ready.event_id, published.event_id,
    )))
    return RunStart(
        normalized_run_id, prior_state, published.snapshot.state.value, profile_revision,
        published.artifact, event_ids, exported.path,
        all(item.replayed for item in (created, pending, ready, published)),
    )


__all__ = [
    "PROFILE_CONTEXT_VERSION", "RUN_START_VERSION", "PreparedRunProfile", "RunStart",
    "prepare_run_profile", "start_run",
]
