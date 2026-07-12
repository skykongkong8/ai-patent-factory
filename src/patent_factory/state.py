from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .artifacts import ArtifactError, ArtifactExport, export_immutable_json, recover_artifact_exports
from .database import FaultInjector, consistent_snapshot, immediate_transaction, inject_fault, utc_now
from .models import (
    ArtifactRevision,
    GateDecision,
    GateEnvelope,
    GateKind,
    RunSnapshot,
    RunState,
    TERMINAL_STATES,
    TransitionResult,
)
from .provenance import canonical_json, digest


class StateError(RuntimeError):
    code = "invalid_state"


class StaleRevisionError(StateError):
    code = "stale_revision"


class GateMismatchError(StateError):
    code = "gate_mismatch"


ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.NEW: frozenset({RunState.PROFILE_PENDING}),
    RunState.PROFILE_PENDING: frozenset({RunState.CONFLICT_RESOLUTION_REQUIRED, RunState.SENSITIVE_DISCLOSURE_REQUIRED, RunState.PROFILE_READY}),
    RunState.CONFLICT_RESOLUTION_REQUIRED: frozenset({RunState.PROFILE_PENDING, RunState.PROFILE_READY, RunState.STOPPED}),
    RunState.PROFILE_READY: frozenset({RunState.CREDENTIAL_REQUIRED, RunState.RESEARCH_READY}),
    RunState.CREDENTIAL_REQUIRED: frozenset({RunState.PROFILE_READY, RunState.RESEARCH_READY, RunState.RESEARCH_RUNNING, RunState.STOPPED}),
    RunState.RESEARCH_READY: frozenset({RunState.CREDENTIAL_REQUIRED, RunState.RESEARCH_RUNNING, RunState.DOMAIN_PIVOT_REQUIRED}),
    RunState.RESEARCH_RUNNING: frozenset({RunState.RESEARCH_COMPLETE, RunState.RESEARCH_INCOMPLETE, RunState.CREDENTIAL_REQUIRED}),
    RunState.RESEARCH_COMPLETE: frozenset({RunState.DOMAIN_PIVOT_REQUIRED, RunState.IDEATION_RUNNING}),
    RunState.RESEARCH_INCOMPLETE: frozenset({RunState.CREDENTIAL_REQUIRED, RunState.RESEARCH_RUNNING, RunState.DOMAIN_PIVOT_REQUIRED, RunState.IDEATION_RUNNING, RunState.INSUFFICIENT_EVIDENCE}),
    RunState.DOMAIN_PIVOT_REQUIRED: frozenset({RunState.RESEARCH_READY, RunState.RESEARCH_RUNNING, RunState.RESEARCH_COMPLETE, RunState.RESEARCH_INCOMPLETE, RunState.IDEATION_RUNNING, RunState.STOPPED}),
    RunState.IDEATION_RUNNING: frozenset({RunState.CANDIDATES_READY, RunState.INSUFFICIENT_EVIDENCE, RunState.DOMAIN_PIVOT_REQUIRED}),
    RunState.CANDIDATES_READY: frozenset({RunState.FINALISTS_READY, RunState.DOMAIN_PIVOT_REQUIRED, RunState.INSUFFICIENT_EVIDENCE}),
    RunState.INSUFFICIENT_EVIDENCE: frozenset({RunState.RESEARCH_RUNNING, RunState.STOPPED}),
    RunState.FINALISTS_READY: frozenset({RunState.AUDIT_RUNNING}),
    RunState.AUDIT_RUNNING: frozenset({RunState.COVERAGE_INSUFFICIENT, RunState.DECISION_REQUIRED, RunState.AUDIT_APPROVED}),
    RunState.COVERAGE_INSUFFICIENT: frozenset({RunState.RESEARCH_RUNNING, RunState.AUDIT_RUNNING, RunState.STOPPED}),
    RunState.DECISION_REQUIRED: frozenset({RunState.STOPPED, RunState.AUDIT_APPROVED, RunState.RESEARCH_RUNNING, RunState.IDEATION_RUNNING}),
    RunState.AUDIT_APPROVED: frozenset({RunState.DRAFT_READY}),
    RunState.DRAFT_READY: frozenset({RunState.SENSITIVE_DISCLOSURE_REQUIRED, RunState.REVIEW_REQUIRED}),
    RunState.SENSITIVE_DISCLOSURE_REQUIRED: frozenset({RunState.DRAFT_READY, RunState.REVIEWED, RunState.VALIDATED, RunState.STOPPED}),
    RunState.REVIEW_REQUIRED: frozenset({RunState.REVISION_REQUIRED, RunState.REVIEWED}),
    RunState.REVISION_REQUIRED: frozenset({RunState.DRAFT_READY, RunState.REVIEW_REQUIRED}),
    RunState.REVIEWED: frozenset({RunState.SENSITIVE_DISCLOSURE_REQUIRED, RunState.VALIDATED, RunState.REVISION_REQUIRED}),
    RunState.VALIDATED: frozenset({RunState.SENSITIVE_DISCLOSURE_REQUIRED, RunState.COMPLETE, RunState.REVISION_REQUIRED}),
}

GATE_STATES = {
    GateKind.CONFLICT_RESOLUTION: RunState.CONFLICT_RESOLUTION_REQUIRED,
    GateKind.CREDENTIAL: RunState.CREDENTIAL_REQUIRED,
    GateKind.SENSITIVE_DISCLOSURE: RunState.SENSITIVE_DISCLOSURE_REQUIRED,
    GateKind.DOMAIN_PIVOT: RunState.DOMAIN_PIVOT_REQUIRED,
    GateKind.COVERAGE: RunState.COVERAGE_INSUFFICIENT,
    GateKind.EXCESSIVE_SIMILARITY: RunState.DECISION_REQUIRED,
}

GATE_ACTIONS: dict[GateKind, frozenset[str]] = {
    GateKind.CONFLICT_RESOLUTION: frozenset({"choose_source", "choose_value", "retain_unresolved", "stop"}),
    GateKind.CREDENTIAL: frozenset({"configure_and_verify", "approve", "degrade", "stop"}),
    GateKind.SENSITIVE_DISCLOSURE: frozenset({"approve", "redact", "stop"}),
    GateKind.DOMAIN_PIVOT: frozenset({"approve", "reject", "stop"}),
    GateKind.COVERAGE: frozenset({"expand", "retry", "stop"}),
    GateKind.EXCESSIVE_SIMILARITY: frozenset({"retain_with_warning", "refine", "replace", "stop"}),
}

AUTHORIZING_GATE_ACTIONS: dict[GateKind, frozenset[str]] = {
    GateKind.CONFLICT_RESOLUTION: frozenset({"choose_source", "choose_value"}),
    GateKind.CREDENTIAL: frozenset({"configure_and_verify", "approve"}),
    GateKind.SENSITIVE_DISCLOSURE: frozenset({"approve"}),
    GateKind.DOMAIN_PIVOT: frozenset({"approve"}),
    GateKind.COVERAGE: frozenset(),
    GateKind.EXCESSIVE_SIMILARITY: frozenset({"retain_with_warning"}),
}

GATE_STATE_SET = frozenset(GATE_STATES.values())


def _as_revision(row: sqlite3.Row | None) -> ArtifactRevision | None:
    if row is None:
        return None
    return ArtifactRevision(row["revision_id"], row["run_id"], row["kind"], row["content_hash"], json.loads(row["content_json"]), row["schema_version"], row["created_at"], bool(row["stale"]))


class StateStore:
    def __init__(self, connection: sqlite3.Connection, *, export_directories: Iterable[Path] = ()):
        self.connection = connection
        directories = tuple(Path(directory).absolute() for directory in export_directories)
        self.export_directories = frozenset(directories)
        if not directories:
            return
        registered = {
            Path(row["path"]).absolute(): (row["byte_hash"], row["byte_size"])
            for row in self.connection.execute("SELECT path,byte_hash,byte_size FROM artifact_exports")
        }
        configured = set(directories)
        if any(path.parent not in configured for path in registered):
            raise StateError("artifact registry path is outside configured export directories")
        for directory in directories:
            recover_artifact_exports(
                directory,
                {
                    path: expected
                    for path, expected in registered.items()
                    if path.parent == directory
                },
            )

    def _snapshot(self, run_id: str) -> RunSnapshot:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise StateError("run_not_found")
        pointers = {item["kind"]: item["revision_id"] for item in self.connection.execute("SELECT kind,revision_id FROM current_artifacts WHERE run_id=? ORDER BY kind", (run_id,))}
        return RunSnapshot(run_id, RunState(row["state"]), row["state_version"], pointers, row["created_at"], row["updated_at"])

    def snapshot(self, run_id: str) -> RunSnapshot:
        if self.connection.in_transaction:
            return self._snapshot(run_id)
        with consistent_snapshot(self.connection):
            return self._snapshot(run_id)

    def create_run(self, run_id: str, *, actor: str = "system", reason: str = "run created", fault_at: FaultInjector = None) -> RunSnapshot:
        now = utc_now()
        with immediate_transaction(self.connection):
            self.connection.execute("INSERT INTO runs VALUES(?, 'new', 0, ?, ?)", (run_id, now, now))
            inject_fault(fault_at, "after_state")
            event_id = "te_" + digest({"run_id":run_id,"actor":actor,"state":"new","reason":reason,"at":now})[:20]
            self.connection.execute("INSERT INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)", (event_id,run_id,actor,"new","new",reason,"[]",None,now))
            inject_fault(fault_at, "after_event")
            return self._snapshot(run_id)

    def _require_current_hash(self, run_id: str, content_hash: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id WHERE ar.run_id=? AND ar.content_hash=? AND ar.stale=0",
            (run_id,content_hash),
        ).fetchone()
        if row is None:
            raise StaleRevisionError("subject revision is not current")
        return row

    def _invalidate_from(self, run_id: str, revision_id: str) -> set[str]:
        rows = self.connection.execute(
            "WITH RECURSIVE descendants(id) AS (SELECT downstream_revision_id FROM artifact_dependencies WHERE run_id=? AND upstream_revision_id=? UNION SELECT ad.downstream_revision_id FROM artifact_dependencies ad JOIN descendants d ON ad.upstream_revision_id=d.id WHERE ad.run_id=?) SELECT id FROM descendants",
            (run_id,revision_id,run_id),
        ).fetchall()
        ids = {row[0] for row in rows}
        if ids:
            marks = ",".join("?" for _ in ids)
            self.connection.execute(f"UPDATE artifact_revisions SET stale=1 WHERE revision_id IN ({marks})", tuple(ids))
            self.connection.execute(f"DELETE FROM current_artifacts WHERE revision_id IN ({marks})", tuple(ids))
        hashes = {row[0] for row in self.connection.execute("SELECT content_hash FROM artifact_revisions WHERE revision_id=?", (revision_id,))}
        if ids:
            marks = ",".join("?" for _ in ids)
            hashes.update(row[0] for row in self.connection.execute(f"SELECT content_hash FROM artifact_revisions WHERE revision_id IN ({marks})", tuple(ids)))
        for value in hashes:
            self.connection.execute("UPDATE gate_decisions SET stale=1 WHERE run_id=? AND subject_revision_hash=?", (run_id,value))
            self.connection.execute("UPDATE gate_envelopes SET status='superseded' WHERE run_id=? AND subject_revision_hash=? AND status='pending'", (run_id,value))
        return ids

    def _check_dependencies(self, run_id: str, dependencies: Iterable[str], revision_id: str) -> tuple[str, ...]:
        items = tuple(dict.fromkeys(dependencies))
        for upstream in items:
            row = self.connection.execute("SELECT run_id,stale FROM artifact_revisions WHERE revision_id=?", (upstream,)).fetchone()
            if row is None or row["run_id"] != run_id:
                raise StateError("dependency must belong to the run")
            if row["stale"]:
                raise StaleRevisionError("stale revision cannot be a dependency")
            cycle = self.connection.execute(
                "WITH RECURSIVE descendants(id) AS (SELECT downstream_revision_id FROM artifact_dependencies WHERE upstream_revision_id=? UNION SELECT ad.downstream_revision_id FROM artifact_dependencies ad JOIN descendants d ON ad.upstream_revision_id=d.id) SELECT 1 FROM descendants WHERE id=?",
                (revision_id,upstream),
            ).fetchone()
            if upstream == revision_id or cycle:
                raise StateError("artifact dependency cycle")
        return items

    def _validate_direct_transition(self, prior: RunState, target: RunState) -> None:
        if target in GATE_STATE_SET:
            raise GateMismatchError("mandatory gate state requires a gate envelope")
        if prior in GATE_STATE_SET and target is not RunState.CANCELLED:
            raise GateMismatchError("mandatory gate state requires a gate decision")
        if target not in ALLOWED_TRANSITIONS.get(prior, frozenset()) and not (
            target is RunState.CANCELLED and prior not in TERMINAL_STATES
        ):
            raise StateError(f"illegal transition: {prior.value} -> {target.value}")

    def _activate_revision(self, run_id: str, kind: str, revision_id: str, fault_at: FaultInjector) -> None:
        prior = self.connection.execute("SELECT revision_id FROM current_artifacts WHERE run_id=? AND kind=?", (run_id,kind)).fetchone()
        if prior and prior[0] != revision_id:
            self._invalidate_from(run_id, prior[0])
        inject_fault(fault_at, "after_invalidation")
        self.connection.execute("INSERT INTO current_artifacts VALUES(?,?,?) ON CONFLICT(run_id,kind) DO UPDATE SET revision_id=excluded.revision_id", (run_id,kind,revision_id))
        inject_fault(fault_at, "after_pointer")

    def _add_revision(self, run_id: str, kind: str, content: dict[str, Any], schema_version: str, dependencies: Iterable[str], fault_at: FaultInjector, *, activate: bool = True) -> ArtifactRevision:
        content_json = canonical_json(content)
        dependency_ids = tuple(sorted(set(dependencies)))
        content_hash = digest({
            "content": content,
            "dependencies": dependency_ids,
            "schema_version": schema_version,
        })
        existing = self.connection.execute("SELECT * FROM artifact_revisions WHERE run_id=? AND kind=? AND content_hash=?", (run_id,kind,content_hash)).fetchone()
        if existing:
            if existing["stale"]:
                raise StaleRevisionError("immutable revision was previously invalidated")
            if activate:
                self._activate_revision(run_id,kind,existing["revision_id"],fault_at)
            return _as_revision(existing)  # type: ignore[return-value]
        now = utc_now()
        revision_id = "ar_" + digest({"run_id":run_id,"kind":kind,"content_hash":content_hash,"schema_version":schema_version})[:20]
        deps = self._check_dependencies(run_id, dependency_ids, revision_id)
        self.connection.execute("INSERT INTO artifact_revisions VALUES(?,?,?,?,?,?,?,0)", (revision_id,run_id,kind,content_json,content_hash,schema_version,now))
        inject_fault(fault_at, "after_revision")
        for upstream in deps:
            self.connection.execute("INSERT INTO artifact_dependencies VALUES(?,?,?)", (run_id,upstream,revision_id))
        inject_fault(fault_at, "after_dependency")
        if activate:
            self._activate_revision(run_id,kind,revision_id,fault_at)
        return ArtifactRevision(revision_id,run_id,kind,content_hash,json.loads(content_json),schema_version,now)

    def add_revision(self, run_id: str, kind: str, content: dict[str, Any], *, schema_version: str = "1", dependencies: Iterable[str] = (), fault_at: FaultInjector = None) -> ArtifactRevision:
        with immediate_transaction(self.connection):
            self._snapshot(run_id)
            return self._add_revision(run_id,kind,content,schema_version,dependencies,fault_at)

    def _published_replay(self, run_id: str, operation: str, idempotency_key: str) -> tuple[TransitionResult, ArtifactExport] | None:
        record = self.connection.execute(
            "SELECT * FROM idempotency_records WHERE run_id=? AND operation=? AND idempotency_key=?",
            (run_id, operation, idempotency_key),
        ).fetchone()
        if record is None:
            return None
        artifact = _as_revision(
            self.connection.execute(
                "SELECT * FROM artifact_revisions WHERE revision_id=?",
                (record["artifact_revision_id"],),
            ).fetchone()
        )
        if artifact is None:
            raise StateError("published idempotency record has no artifact")
        export = self.connection.execute(
            "SELECT path,byte_hash,byte_size FROM artifact_exports WHERE revision_id=?",
            (artifact.revision_id,),
        ).fetchone()
        if export is None:
            raise StateError("published idempotency record has no export")
        replay_export = ArtifactExport(
            "ar_" + export["byte_hash"][:16],
            export["byte_hash"],
            export["path"],
            True,
            export["byte_size"],
        )
        return TransitionResult(self._snapshot(run_id), record["event_id"], artifact, True), replay_export

    def transition(self, run_id: str, next_state: RunState | str, *, actor: str, reason: str, evidence_hashes: Iterable[str] = (), operation: str, idempotency_key: str, artifact_kind: str | None = None, artifact_content: dict[str, Any] | None = None, artifact_schema_version: str = "1", dependencies: Iterable[str] = (), fault_at: FaultInjector = None) -> TransitionResult:
        target = RunState(next_state)
        with immediate_transaction(self.connection):
            prior_record = self.connection.execute("SELECT * FROM idempotency_records WHERE run_id=? AND operation=? AND idempotency_key=?", (run_id,operation,idempotency_key)).fetchone()
            if prior_record:
                artifact = _as_revision(self.connection.execute("SELECT * FROM artifact_revisions WHERE revision_id=?", (prior_record["artifact_revision_id"],)).fetchone()) if prior_record["artifact_revision_id"] else None
                return TransitionResult(self._snapshot(run_id),prior_record["event_id"],artifact,True)
            prior = self._snapshot(run_id)
            self._validate_direct_transition(prior.state, target)
            artifact = None
            if artifact_kind is not None:
                if artifact_content is None:
                    raise ValueError("artifact_content is required with artifact_kind")
                artifact = self._add_revision(run_id,artifact_kind,artifact_content,artifact_schema_version,dependencies,fault_at)
            hashes = set(evidence_hashes)
            if artifact:
                hashes.add(artifact.content_hash)
            now = utc_now()
            event_id = "te_" + digest({"run_id":run_id,"actor":actor,"prior":prior.state.value,"next":target.value,"reason":reason,"hashes":sorted(hashes),"at":now})[:20]
            self.connection.execute("INSERT INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)", (event_id,run_id,actor,prior.state.value,target.value,reason,canonical_json(sorted(hashes)),artifact.revision_id if artifact else None,now))
            inject_fault(fault_at,"after_event")
            self.connection.execute("UPDATE runs SET state=?,state_version=state_version+1,updated_at=? WHERE run_id=?", (target.value,now,run_id))
            inject_fault(fault_at,"after_state")
            self.connection.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)", (run_id,operation,idempotency_key,event_id,artifact.revision_id if artifact else None,target.value,now))
            inject_fault(fault_at,"after_idempotency")
            return TransitionResult(self._snapshot(run_id),event_id,artifact)

    def publish_transition(self, run_id: str, next_state: RunState | str, *, actor: str, reason: str, operation: str, idempotency_key: str, artifact_kind: str, artifact_content: dict[str, Any], export_directory: Path, artifact_schema_version: str = "1", dependencies: Iterable[str] = (), evidence_hashes: Iterable[str] = (), consumed_decision_id: str | None = None, export_fault_hook: Callable[[str], None] | None = None, fault_at: FaultInjector = None) -> tuple[TransitionResult, ArtifactExport]:
        target = RunState(next_state)
        directory = Path(export_directory).absolute()
        if directory not in self.export_directories:
            raise ArtifactError("artifact_path: export directory is not configured")
        replay = self._published_replay(run_id, operation, idempotency_key)
        if replay is not None:
            return replay
        dependency_ids = tuple(sorted(set(dependencies)))
        artifact_hash = digest({
            "content": artifact_content,
            "dependencies": dependency_ids,
            "schema_version": artifact_schema_version,
        })
        revision_id = "ar_" + digest({"run_id":run_id,"kind":artifact_kind,"content_hash":artifact_hash,"schema_version":artifact_schema_version})[:20]
        export_path = directory / f"{revision_id}.json"
        with immediate_transaction(self.connection):
            replay = self._published_replay(run_id, operation, idempotency_key)
            if replay is not None:
                return replay
            prior = self._snapshot(run_id)
            self._validate_direct_transition(prior.state, target)
            inject_fault(fault_at,"before_export")
            exported = export_immutable_json(export_path,artifact_content,fault_hook=export_fault_hook)
            inject_fault(fault_at,"after_export_publish")
            inject_fault(fault_at,"before_database")
            decision_hashes: set[str] = set()
            if consumed_decision_id:
                decision = self.connection.execute(
                    "SELECT gd.*,ge.kind AS gate_kind FROM gate_decisions gd "
                    "JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
                    "WHERE gd.decision_id=? AND gd.run_id=?",
                    (consumed_decision_id,run_id),
                ).fetchone()
                if (
                    decision is None
                    or decision["stale"]
                    or not decision["consumed_at"]
                    or decision["used_at"]
                    or decision["consumed_by_event_id"]
                    or decision["suspended_operation"] != operation
                    or decision["action"] not in AUTHORIZING_GATE_ACTIONS[GateKind(decision["gate_kind"])]
                ):
                    raise GateMismatchError("a current consumed approval for the exact operation is required")
                self._require_current_hash(run_id,decision["subject_revision_hash"])
                decision_hashes.update((decision["subject_revision_hash"],decision["approval_scope_hash"]))
            artifact = self._add_revision(run_id,artifact_kind,artifact_content,artifact_schema_version,dependency_ids,fault_at,activate=False)
            now = utc_now()
            export_id = "ex_" + digest({"revision_id":artifact.revision_id,"path":exported.path,"byte_hash":exported.content_hash})[:20]
            self.connection.execute("INSERT OR IGNORE INTO artifact_exports VALUES(?,?,?,?,?,?,?)",(export_id,artifact.revision_id,run_id,exported.path,exported.content_hash,exported.size,now))
            registry = self.connection.execute("SELECT path,byte_hash,byte_size FROM artifact_exports WHERE revision_id=?",(artifact.revision_id,)).fetchone()
            if tuple(registry) != (exported.path,exported.content_hash,exported.size):
                raise StateError("artifact export registry conflict")
            inject_fault(fault_at,"after_export_registry")
            self._activate_revision(run_id,artifact_kind,artifact.revision_id,fault_at)
            hashes = set(evidence_hashes) | decision_hashes | {artifact.content_hash,exported.content_hash}
            event_id = "te_" + digest({"run_id":run_id,"actor":actor,"prior":prior.state.value,"next":target.value,"reason":reason,"hashes":sorted(hashes),"at":now})[:20]
            self.connection.execute("INSERT INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)",(event_id,run_id,actor,prior.state.value,target.value,reason,canonical_json(sorted(hashes)),artifact.revision_id,now))
            inject_fault(fault_at,"after_event")
            if consumed_decision_id:
                claimed = self.connection.execute(
                    "UPDATE gate_decisions SET used_at=?,consumed_by_event_id=? "
                    "WHERE decision_id=? AND stale=0 AND consumed_at IS NOT NULL "
                    "AND used_at IS NULL AND consumed_by_event_id IS NULL",
                    (now,event_id,consumed_decision_id),
                )
                if claimed.rowcount != 1:
                    raise GateMismatchError("approval was already used")
                inject_fault(fault_at,"after_decision_claim")
            self.connection.execute("UPDATE runs SET state=?,state_version=state_version+1,updated_at=? WHERE run_id=?",(target.value,now,run_id))
            inject_fault(fault_at,"after_state")
            self.connection.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)",(run_id,operation,idempotency_key,event_id,artifact.revision_id,target.value,now))
            inject_fault(fault_at,"after_idempotency")
            result = TransitionResult(self._snapshot(run_id),event_id,artifact)
            return result,exported

    def suspend_gate(self, run_id: str, kind: GateKind | str, *, suspended_operation: str, subject_revision_hash: str, approval_scope: dict[str, Any], return_state: RunState | str, actor: str, reason: str, fault_at: FaultInjector = None) -> GateEnvelope:
        gate_kind = GateKind(kind)
        gate_state = GATE_STATES[gate_kind]
        desired_return = RunState(return_state)
        with immediate_transaction(self.connection):
            prior = self._snapshot(run_id)
            self._require_current_hash(run_id,subject_revision_hash)
            if desired_return != prior.state and desired_return not in ALLOWED_TRANSITIONS.get(gate_state,frozenset()):
                raise GateMismatchError("return state is not permitted for this gate")
            if gate_state not in ALLOWED_TRANSITIONS.get(prior.state,frozenset()):
                raise StateError(f"gate {gate_kind.value} cannot suspend {prior.state.value}")
            scope_json = canonical_json(approval_scope)
            scope_hash = digest(approval_scope)
            now = utc_now()
            gate_id = "ge_" + digest({"run_id":run_id,"kind":gate_kind.value,"state":prior.state.value,"operation":suspended_operation,"subject":subject_revision_hash,"scope":scope_hash,"return":desired_return.value})[:20]
            self.connection.execute("INSERT INTO gate_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending')", (gate_id,run_id,gate_kind.value,gate_state.value,prior.state.value,suspended_operation,subject_revision_hash,scope_json,scope_hash,desired_return.value,now))
            inject_fault(fault_at,"after_gate")
            event_id = "te_" + digest({"gate_id":gate_id,"actor":actor,"at":now})[:20]
            self.connection.execute("INSERT INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)", (event_id,run_id,actor,prior.state.value,gate_state.value,reason,canonical_json([subject_revision_hash,scope_hash]),None,now))
            inject_fault(fault_at,"after_event")
            self.connection.execute("UPDATE runs SET state=?,state_version=state_version+1,updated_at=? WHERE run_id=?", (gate_state.value,now,run_id))
            inject_fault(fault_at,"after_state")
            return GateEnvelope(gate_id,run_id,gate_kind,prior.state,suspended_operation,subject_revision_hash,json.loads(scope_json),scope_hash,desired_return,now)

    def decide_gate(self, gate_id: str, *, action: str, actor: str, reason: str, subject_revision_hash: str, approval_scope: dict[str, Any], suspended_operation: str | None = None, return_state: RunState | str | None = None, fault_at: FaultInjector = None) -> tuple[GateDecision, TransitionResult]:
        with immediate_transaction(self.connection):
            envelope = self.connection.execute("SELECT * FROM gate_envelopes WHERE gate_id=?", (gate_id,)).fetchone()
            if envelope is None or envelope["status"] != "pending":
                raise GateMismatchError("gate is not pending")
            if subject_revision_hash != envelope["subject_revision_hash"] or digest(approval_scope) != envelope["approval_scope_hash"]:
                raise GateMismatchError("decision does not match current subject and scope")
            if suspended_operation is not None and suspended_operation != envelope["suspended_operation"]:
                raise GateMismatchError("decision cannot change suspended operation")
            if return_state is not None and RunState(return_state).value != envelope["return_state"]:
                raise GateMismatchError("decision cannot change return state")
            self._require_current_hash(envelope["run_id"],subject_revision_hash)
            prior = self._snapshot(envelope["run_id"])
            if prior.state.value != envelope["gate_state"]:
                raise GateMismatchError("run is not at the recorded gate state")
            gate_kind = GateKind(envelope["kind"])
            if action not in GATE_ACTIONS[gate_kind]:
                raise GateMismatchError(f"action is not allowed for {gate_kind.value}")
            target = RunState.STOPPED if action == "stop" else RunState(envelope["return_state"])
            now = utc_now()
            decision_id = "gd_" + digest({"gate_id":gate_id,"action":action,"actor":actor,"subject":subject_revision_hash,"scope":envelope["approval_scope_hash"],"at":now})[:20]
            self.connection.execute(
                "INSERT INTO gate_decisions "
                "(decision_id,gate_id,run_id,action,actor,subject_revision_hash,approval_scope_hash,suspended_operation,return_state,reason,created_at,stale,consumed_at,used_at,consumed_by_event_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,NULL)",
                (decision_id,gate_id,envelope["run_id"],action,actor,subject_revision_hash,envelope["approval_scope_hash"],envelope["suspended_operation"],target.value,reason,now),
            )
            inject_fault(fault_at,"after_decision")
            self.connection.execute("UPDATE gate_envelopes SET status='decided' WHERE gate_id=?", (gate_id,))
            event_id = "te_" + digest({"decision_id":decision_id,"prior":prior.state.value,"next":target.value})[:20]
            self.connection.execute("INSERT INTO transition_events VALUES(?,?,?,?,?,?,?,?,?)", (event_id,envelope["run_id"],actor,prior.state.value,target.value,reason,canonical_json([subject_revision_hash,envelope["approval_scope_hash"]]),None,now))
            inject_fault(fault_at,"after_event")
            self.connection.execute("UPDATE runs SET state=?,state_version=state_version+1,updated_at=? WHERE run_id=?", (target.value,now,envelope["run_id"]))
            inject_fault(fault_at,"after_state")
            decision = GateDecision(decision_id,gate_id,envelope["run_id"],action,actor,subject_revision_hash,envelope["approval_scope_hash"],envelope["suspended_operation"],target,reason,now)
            result = TransitionResult(self._snapshot(envelope["run_id"]),event_id,suspended_operation=envelope["suspended_operation"])
            return decision,result

    def consume_decision(self, decision_id: str, *, suspended_operation: str, subject_revision_hash: str, approval_scope: dict[str, Any]) -> GateDecision:
        with immediate_transaction(self.connection):
            row = self.connection.execute(
                "SELECT gd.*,ge.kind AS gate_kind FROM gate_decisions gd "
                "JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id WHERE gd.decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None or row["stale"] or row["consumed_at"] or row["used_at"]:
                raise GateMismatchError("decision is unavailable")
            if row["suspended_operation"] != suspended_operation or row["subject_revision_hash"] != subject_revision_hash or row["approval_scope_hash"] != digest(approval_scope):
                raise GateMismatchError("decision does not authorize this operation, subject, and scope")
            if row["action"] not in AUTHORIZING_GATE_ACTIONS[GateKind(row["gate_kind"])]:
                raise GateMismatchError("decision action does not authorize the guarded operation")
            now = utc_now()
            self.connection.execute("UPDATE gate_decisions SET consumed_at=? WHERE decision_id=?", (now,decision_id))
            return GateDecision(row["decision_id"],row["gate_id"],row["run_id"],row["action"],row["actor"],row["subject_revision_hash"],row["approval_scope_hash"],row["suspended_operation"],RunState(row["return_state"]),row["reason"],row["created_at"],False,now)
