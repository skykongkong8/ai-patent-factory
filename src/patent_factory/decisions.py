from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .database import FaultInjector
from .models import ArtifactRevision, GateKind, RunState
from .privacy import assert_canaries_absent, credential_canaries
from .provenance import digest, normalize
from .state import (
    GATE_ACTIONS, GateMismatchError, StateError, StateStore, gate_action_target,
    workspace_export_directories,
)


@dataclass(frozen=True)
class DecisionRun:
    run_id: str
    gate_id: str
    decision_id: str
    artifact_revision_id: str
    action: str
    next_state: str
    replayed: bool
    report_revision_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        artifact_ids = [self.artifact_revision_id]
        if self.report_revision_id:
            artifact_ids.append(self.report_revision_id)
        return {
            "action": self.action, "artifact_ids": artifact_ids,
            "command": "gate.decide", "decision_id": self.decision_id,
            "gate_id": self.gate_id, "next_state": self.next_state,
            "replayed": self.replayed, "run_id": self.run_id,
            "status": self.next_state,
        }


def _artifact(connection: sqlite3.Connection, run_id: str, kind: str) -> tuple[sqlite3.Row, dict[str, Any]]:
    row = connection.execute(
        "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
        "WHERE ar.run_id=? AND ca.kind=? AND ar.stale=0", (run_id, kind),
    ).fetchone()
    if row is None:
        raise StateError(f"gate decision requires current {kind}")
    return row, json.loads(row["content_json"])


def _exports(connection: sqlite3.Connection, run_root: Path) -> tuple[StateStore, Path]:
    root = Path(run_root).absolute()
    if not root.is_dir() or stat.S_ISLNK(root.lstat().st_mode):
        raise ValueError("decision_export: safe run directory required")
    directory = root / "decision-exports"
    if directory.exists() and (not directory.is_dir() or stat.S_ISLNK(directory.lstat().st_mode)):
        raise ValueError("decision_export: unsafe export directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700, follow_symlinks=False)
    except OSError:
        pass
    directories = workspace_export_directories(connection, root, (directory,))
    return StateStore(connection, export_directories=directories), directory


def inspect_gate(connection: sqlite3.Connection, run_id: str, gate_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM gate_envelopes WHERE gate_id=? AND run_id=?", (gate_id, run_id),
    ).fetchone()
    if row is None:
        raise GateMismatchError("gate is unavailable")
    return {
        "actions": sorted(GATE_ACTIONS[GateKind(row["kind"])]),
        "approval_scope": json.loads(row["approval_scope_json"]),
        "approval_scope_hash": row["approval_scope_hash"], "gate_id": gate_id,
        "kind": row["kind"], "return_state": row["return_state"],
        "run_id": run_id, "status": row["status"],
        "subject_revision_hash": row["subject_revision_hash"],
        "suspended_operation": row["suspended_operation"],
    }


def _text(value: Any, path: str) -> str:
    item = normalize(value)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{path}: non-empty string required")
    return item


_TODO_MARKER = "TODO(agent)"


def _reject_todo_marker(value: Any, path: str) -> None:
    """Reject any leftover scaffold placeholder (core-side, not scaffold-side).

    ``scaffold gate-decision`` stubs every judgment field with a ``TODO(agent)``
    marker (scaffold.TODO); ``count_todos`` only counts them client-side. An
    unedited draft must fail here, in ``resolve_gate``, or AC-9's "user-authored"
    guarantee would be a convention instead of an invariant.
    """
    if isinstance(value, str):
        if _TODO_MARKER in value:
            raise ValueError(f"{path}: unedited TODO(agent) marker must be resolved before deciding")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_todo_marker(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_todo_marker(item, f"{path}[{index}]")


def _durable_resolution_replay(
    connection: sqlite3.Connection, *, run_id: str, gate_id: str,
    envelope: sqlite3.Row, decision_input: Mapping[str, Any],
) -> DecisionRun | None:
    operation = f"gate.resolve:{gate_id}"
    row = connection.execute(
        "SELECT ir.state_after,ir.artifact_revision_id,ar.kind,ar.content_json,"
        "gd.decision_id,gd.action,gd.actor,gd.reason,gd.subject_revision_hash,gd.approval_scope_hash "
        "FROM idempotency_records ir "
        "JOIN artifact_revisions ar ON ar.revision_id=ir.artifact_revision_id "
        "JOIN gate_decisions gd ON gd.gate_id=? AND gd.run_id=ir.run_id "
        "WHERE ir.run_id=? AND ir.operation=? AND ir.idempotency_key=?",
        (gate_id, run_id, operation, digest(decision_input)),
    ).fetchone()
    if row is None:
        return None
    expected_kind = (
        "sensitive_gate_resolution"
        if GateKind(envelope["kind"]) is GateKind.SENSITIVE_DISCLOSURE
        else "gate_resolution"
    )
    resolution = json.loads(row["content_json"])
    if (
        row["kind"] != expected_kind
        or resolution.get("decision_id") != row["decision_id"]
        or resolution.get("gate_id") != gate_id
        or resolution.get("action") != row["action"]
        or resolution.get("actor") != row["actor"]
        or resolution.get("reason") != row["reason"]
        or resolution.get("subject_revision_hash") != row["subject_revision_hash"]
        or resolution.get("approval_scope_hash") != row["approval_scope_hash"]
        or row["action"] != decision_input["action"]
        or row["actor"] != decision_input["actor"]
        or row["reason"] != decision_input["reason"]
        or row["subject_revision_hash"] != envelope["subject_revision_hash"]
        or row["approval_scope_hash"] != envelope["approval_scope_hash"]
    ):
        raise StateError("gate resolution replay record is malformed")
    report_revision_id = None
    next_state = row["state_after"]
    if GateKind(envelope["kind"]) is GateKind.SENSITIVE_DISCLOSURE and row["action"] == "redact":
        redaction_rows = connection.execute(
            "SELECT ir.state_after,ir.artifact_revision_id,ar.kind,ar.content_json "
            "FROM idempotency_records ir "
            "JOIN artifact_revisions ar ON ar.revision_id=ir.artifact_revision_id "
            "WHERE ir.run_id=? AND ir.operation=?",
            (run_id, f"report.redact:{row['decision_id']}"),
        ).fetchall()
        if len(redaction_rows) != 1:
            raise StateError("redaction replay record is missing or ambiguous")
        redaction = redaction_rows[0]
        report = json.loads(redaction["content_json"])
        histories = report.get("redactions", [])
        if (
            redaction["kind"] != "report"
            or not isinstance(histories, list)
            or not histories
            or any(
                not isinstance(item, Mapping)
                or item.get("decision_id") != row["decision_id"]
                or item.get("prior_report_hash") != envelope["subject_revision_hash"]
                for item in histories
            )
        ):
            raise StateError("redaction replay record is malformed")
        report_revision_id = redaction["artifact_revision_id"]
        next_state = redaction["state_after"]
    return DecisionRun(
        run_id, gate_id, row["decision_id"], row["artifact_revision_id"],
        row["action"], next_state, True, report_revision_id,
    )


def _resolution(
    connection: sqlite3.Connection, run_id: str, envelope: sqlite3.Row,
    request: Mapping[str, Any],
) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    kind = GateKind(envelope["kind"])
    action = _text(request["action"], "decision.action")
    if action not in GATE_ACTIONS[kind]:
        raise GateMismatchError(f"action is not allowed for {kind.value}")
    entries = request.get("decisions", [])
    plan = request.get("plan", {})
    if not isinstance(entries, list) or not isinstance(plan, Mapping):
        raise ValueError("decision: decisions array and plan object required")
    subject = connection.execute(
        "SELECT * FROM artifact_revisions WHERE run_id=? AND content_hash=? AND stale=0",
        (run_id, envelope["subject_revision_hash"]),
    ).fetchone()
    if subject is None:
        raise GateMismatchError("decision subject is stale")
    dependencies = [subject["revision_id"]]
    payload: dict[str, Any] = {
        "action": action, "actor": _text(request["actor"], "decision.actor"),
        "approval_scope_hash": envelope["approval_scope_hash"], "gate_id": envelope["gate_id"],
        "gate_kind": kind.value, "reason": _text(request["reason"], "decision.reason"),
        "run_id": run_id, "subject_revision_hash": envelope["subject_revision_hash"],
        "suspended_operation": envelope["suspended_operation"], "version": "decision-set-v1",
    }
    if kind is GateKind.EXCESSIVE_SIMILARITY:
        audit_row, audit = _artifact(connection, run_id, "audit_batch")
        finalist_row, finalists = _artifact(connection, run_id, "finalist_set")
        corpus_row, corpora = _artifact(connection, run_id, "corpus_set")
        feature_row, features = _artifact(connection, run_id, "feature_map_set")
        config_row, _config = _artifact(connection, run_id, "scorer_config")
        if audit_row["content_hash"] != envelope["subject_revision_hash"]:
            raise GateMismatchError("excessive decision does not bind the current audit")
        scope = json.loads(envelope["approval_scope_json"])
        current_bindings = [{
            "corpus_hash": {entry["finalist_id"]: entry for entry in corpora["corpora"]}[item["finalist_id"]]["corpus_hash"],
            "finalist_hash": digest({entry["finalist_id"]: entry for entry in finalists["finalists"]}[item["finalist_id"]]),
            "finalist_id": item["finalist_id"],
            "map_id": {entry["finalist_id"]: entry for entry in features["maps"]}[item["finalist_id"]]["map_id"],
        } for item in audit["results"] if item["outcome"] == "decision_required"]
        if (
            audit.get("corpus_set_hash") != corpus_row["content_hash"]
            or scope.get("corpus_set_hash") != corpus_row["content_hash"]
            or scope.get("finalist_set_hash") != finalist_row["content_hash"]
            or scope.get("feature_map_set_hash") != feature_row["content_hash"]
            or scope.get("scorer_config_hash") != config_row["content_hash"]
            or scope.get("decision_bindings") != current_bindings
        ):
            raise GateMismatchError("excessive decision bindings are stale")
        affected = sorted(result["finalist_id"] for result in audit["results"] if result["outcome"] == "decision_required")
        if action == "stop":
            if entries:
                raise ValueError("decision.decisions: stop cannot include partial finalist decisions")
            resolved: list[dict[str, Any]] = []
        else:
            if action not in {"retain_with_warning", "refine", "replace"}:
                raise GateMismatchError("excessive decision action is invalid")
            if any(not isinstance(item, Mapping) or set(item) != {"action", "finalist_id", "reason"} for item in entries):
                raise ValueError("decision.decisions: exact finalist decision fields required")
            ids = [item["finalist_id"] for item in entries]
            if len(ids) != len(set(ids)) or sorted(ids) != affected:
                raise ValueError("decision.decisions: exactly one current decision per excessive finalist required")
            allowed = {"retain_with_warning", "refine", "replace"}
            if any(item["action"] not in allowed for item in entries):
                raise ValueError("decision.decisions: retain_with_warning, refine, or replace required")
            aggregate = "replace" if any(item["action"] == "replace" for item in entries) else "refine" if any(item["action"] == "refine" for item in entries) else "retain_with_warning"
            if action != aggregate:
                raise ValueError("decision.action: must match policy-derived finalist branch")
            finalist_map = {item["finalist_id"]: item for item in finalists["finalists"]}
            corpus_map = {item["finalist_id"]: item for item in corpora["corpora"]}
            feature_map = {item["finalist_id"]: item for item in features["maps"]}
            resolved = []
            for item in sorted(entries, key=lambda value: value["finalist_id"]):
                finalist_id = item["finalist_id"]
                resolved.append({
                    "action": item["action"], "candidate_id": finalist_map[finalist_id]["candidate_id"],
                    "corpus_hash": corpus_map[finalist_id]["corpus_hash"],
                    "feature_map_id": feature_map[finalist_id]["map_id"],
                    "finalist_hash": digest(finalist_map[finalist_id]), "finalist_id": finalist_id,
                    "reason": _text(item["reason"], f"decision.decisions.{finalist_id}.reason"),
                    "warning": "Retained despite excessive provisional similarity risk within the retrieved corpus."
                    if item["action"] == "retain_with_warning" else None,
                })
        payload.update({
            "audit_hash": audit_row["content_hash"], "decisions": resolved,
            "corpus_set_hash": corpus_row["content_hash"],
            "feature_map_set_hash": feature_row["content_hash"],
            "finalist_set_hash": finalist_row["content_hash"], "plan": {},
            "scorer_config_hash": config_row["content_hash"],
        })
        dependencies.extend((finalist_row["revision_id"], corpus_row["revision_id"], feature_row["revision_id"], config_row["revision_id"]))
    elif kind is GateKind.POST_AUDIT_CHECKPOINT:
        audit_row, audit = _artifact(connection, run_id, "audit_batch")
        finalist_row, finalists = _artifact(connection, run_id, "finalist_set")
        corpus_row, corpora = _artifact(connection, run_id, "corpus_set")
        feature_row, features = _artifact(connection, run_id, "feature_map_set")
        config_row, _config = _artifact(connection, run_id, "scorer_config")
        if audit_row["content_hash"] != envelope["subject_revision_hash"]:
            raise GateMismatchError("checkpoint decision does not bind the current audit")
        scope = json.loads(envelope["approval_scope_json"])
        finalist_map = {item["finalist_id"]: item for item in finalists["finalists"]}
        corpus_map = {item["finalist_id"]: item for item in corpora["corpora"]}
        feature_map = {item["finalist_id"]: item for item in features["maps"]}
        current_bindings = [{
            "candidate_id": item["candidate_id"], "closest_reference_id": item["closest_reference_id"],
            "corpus_hash": item["corpus_hash"], "counterargument": item["counterargument"],
            "coverage": item["coverage"],
            "finalist_hash": digest(finalist_map[item["finalist_id"]]), "finalist_id": item["finalist_id"],
            "map_id": feature_map[item["finalist_id"]]["map_id"], "outcome": item["outcome"],
            "r_hi": item["r_hi"], "r_obs": item["r_obs"],
            "upper_bound_reference_id": item["upper_bound_reference_id"],
        } for item in audit["results"]]
        affected = sorted(result["finalist_id"] for result in audit["results"] if result["outcome"] == "decision_required")
        coverage_insufficient_ids = sorted(
            result["finalist_id"] for result in audit["results"] if result["outcome"] == "coverage_insufficient"
        )
        if (
            audit.get("corpus_set_hash") != corpus_row["content_hash"]
            or scope.get("corpus_set_hash") != corpus_row["content_hash"]
            or scope.get("finalist_set_hash") != finalist_row["content_hash"]
            or scope.get("feature_map_set_hash") != feature_row["content_hash"]
            or scope.get("scorer_config_hash") != config_row["content_hash"]
            or scope.get("finalist_bindings") != current_bindings
            or scope.get("affected_finalist_ids") != affected
        ):
            raise GateMismatchError("checkpoint decision bindings are stale")
        all_finalist_ids = sorted(finalist_map)
        feedback = request.get("feedback", [])
        if not isinstance(feedback, list) or any(
            not isinstance(item, Mapping) or set(item) != {"boring", "finalist_id", "interesting"}
            for item in feedback
        ):
            raise ValueError("decision.feedback: exact per-finalist fields required")
        feedback_ids = [item["finalist_id"] for item in feedback]
        if len(feedback_ids) != len(set(feedback_ids)) or sorted(feedback_ids) != all_finalist_ids:
            raise ValueError("decision.feedback: exactly one entry per current finalist required")
        resolved_feedback = [{
            "boring": _text(item["boring"], f"decision.feedback.{item['finalist_id']}.boring"),
            "finalist_id": item["finalist_id"],
            "interesting": _text(item["interesting"], f"decision.feedback.{item['finalist_id']}.interesting"),
        } for item in sorted(feedback, key=lambda value: value["finalist_id"])]
        # Blocker #2: this prose becomes durable, hash-bound Section 9
        # content that can never be re-authored once persisted (unlike
        # report-input text). Screen it at gate decide, while the operator
        # can still fix it — run_review's later scan is too late.
        from .validation import _scan_prohibited_language

        _scan_prohibited_language(payload["reason"], "ko")
        for item in resolved_feedback:
            _scan_prohibited_language(item["interesting"], "ko")
            _scan_prohibited_language(item["boring"], "ko")
        if action == "stop":
            if entries:
                raise ValueError("decision.decisions: stop cannot include finalist decisions")
            resolved = []
        elif action == "approve":
            if coverage_insufficient_ids:
                # Blocker #1: decision_required takes gate-routing precedence
                # over coverage_insufficient (audit.py), so a batch can carry
                # both outcomes. `affected` only lists decision_required
                # finalists, so approving the breach alone used to strand a
                # coverage_insufficient rider at AUDIT_APPROVED with no path
                # to /draft (report.py rejects any non-exact-approved audit)
                # and no path back (ALLOWED_TRANSITIONS forbids re-deciding).
                # Reject here, while re_ideate/re_research are still reachable.
                raise ValueError(
                    "decision.action: approve cannot proceed while "
                    f"{', '.join(coverage_insufficient_ids)} remain coverage_insufficient — "
                    "resolve with re_research (broaden the corpus) or re_ideate "
                    "(replace the finalist) before approving"
                )
            if not affected:
                if entries:
                    raise ValueError("decision.decisions: a clean approve cannot include finalist decisions")
                resolved = []
            else:
                if any(not isinstance(item, Mapping) or set(item) != {"action", "finalist_id", "reason"} for item in entries):
                    raise ValueError("decision.decisions: exact finalist decision fields required")
                ids = [item["finalist_id"] for item in entries]
                if len(ids) != len(set(ids)) or sorted(ids) != affected:
                    raise ValueError("decision.decisions: exactly one current decision per breaching finalist required")
                if any(item["action"] != "retain_with_warning" for item in entries):
                    raise ValueError("decision.decisions: approve requires retain_with_warning for every breaching finalist")
                resolved = []
                for item in sorted(entries, key=lambda value: value["finalist_id"]):
                    finalist_id = item["finalist_id"]
                    reason_text = _text(item["reason"], f"decision.decisions.{finalist_id}.reason")
                    _scan_prohibited_language(reason_text, "ko")
                    resolved.append({
                        "action": "retain_with_warning", "candidate_id": finalist_map[finalist_id]["candidate_id"],
                        "corpus_hash": corpus_map[finalist_id]["corpus_hash"],
                        "feature_map_id": feature_map[finalist_id]["map_id"],
                        "finalist_hash": digest(finalist_map[finalist_id]), "finalist_id": finalist_id,
                        "reason": reason_text,
                        "warning": "Retained despite excessive provisional similarity risk within the retrieved corpus.",
                    })
        else:
            # re_ideate / re_research: the fate for every finalist (including
            # breaching ones) is carried by feedback[], never by decisions[].
            if entries:
                raise ValueError("decision.decisions: this action must not include finalist decisions")
            resolved = []
        if action == "re_research":
            if not plan:
                raise ValueError("decision.plan: re_research requires a bounded plan")
            payload.update({"plan": dict(plan), "plan_hash": digest(plan)})
        else:
            if plan:
                raise ValueError("decision.plan: only re_research accepts a plan")
            payload["plan"] = {}
        payload.update({
            "audit_hash": audit_row["content_hash"], "decisions": resolved,
            "corpus_set_hash": corpus_row["content_hash"], "feedback": resolved_feedback,
            "feature_map_set_hash": feature_row["content_hash"],
            "finalist_set_hash": finalist_row["content_hash"],
            "scorer_config_hash": config_row["content_hash"],
        })
        dependencies.extend((finalist_row["revision_id"], corpus_row["revision_id"], feature_row["revision_id"], config_row["revision_id"]))
    elif kind is GateKind.COVERAGE:
        if entries:
            raise ValueError("decision.decisions: coverage uses one branch action")
        if action not in {"expand", "retry", "stop"}:
            raise GateMismatchError("coverage decision action is invalid")
        if action != "stop" and not plan:
            raise ValueError("decision.plan: expand or retry requires a bounded plan")
        payload.update({"decisions": [], "plan": dict(plan), "plan_hash": digest(plan)})
    else:
        if entries or plan:
            raise ValueError("decision: this gate accepts only its action and reason")
        payload.update({"decisions": [], "plan": {}})
    payload["next_state"] = gate_action_target(kind, action, RunState(envelope["return_state"])).value
    return action, payload, tuple(sorted(set(dependencies)))


def resolve_gate(
    connection: sqlite3.Connection, *, run_root: Path, run_id: str,
    decision_input: Mapping[str, Any], fault_at: FaultInjector = None,
) -> DecisionRun:
    canaries = credential_canaries()
    assert_canaries_absent(decision_input, canaries, boundary="decision_input")
    if not isinstance(decision_input, Mapping) or "gate_id" not in decision_input:
        raise ValueError("decision: exact gate-decision-input-v1 fields required")
    gate_id = _text(decision_input["gate_id"], "decision.gate_id")
    envelope = connection.execute("SELECT * FROM gate_envelopes WHERE gate_id=? AND run_id=?", (gate_id, run_id)).fetchone()
    if envelope is None:
        raise GateMismatchError("gate is unavailable")
    # Choice C1: dispatch the exact input-schema version on the gate's OWN
    # kind, not on caller-supplied shape — a v1 payload can never satisfy a
    # checkpoint gate and a v2 payload can never satisfy any other gate (R8).
    if GateKind(envelope["kind"]) is GateKind.POST_AUDIT_CHECKPOINT:
        required = {
            "action", "actor", "approval_scope", "decisions", "feedback",
            "gate_id", "plan", "reason", "schema_version", "subject_revision_hash",
        }
        if set(decision_input) != required or decision_input.get("schema_version") != "gate-decision-input-v2":
            raise ValueError("decision: exact gate-decision-input-v2 fields required")
        # Core sentinel rejection (RF#5): count_todos only runs scaffold-side
        # (scaffold.py), so an unedited draft must be rejected here or AC-9's
        # "user-authored" guarantee is a convention, not an invariant.
        for field in ("action", "actor", "reason", "feedback", "plan", "decisions"):
            _reject_todo_marker(decision_input.get(field), f"decision.{field}")
    else:
        required = {"action", "actor", "approval_scope", "decisions", "gate_id", "plan", "reason", "schema_version", "subject_revision_hash"}
        if set(decision_input) != required or decision_input.get("schema_version") != "gate-decision-input-v1":
            raise ValueError("decision: exact gate-decision-input-v1 fields required")
    if decision_input["subject_revision_hash"] != envelope["subject_revision_hash"] or decision_input["approval_scope"] != json.loads(envelope["approval_scope_json"]):
        raise GateMismatchError("decision does not match current subject and scope")
    replay = _durable_resolution_replay(
        connection, run_id=run_id, gate_id=gate_id,
        envelope=envelope, decision_input=decision_input,
    )
    if replay is not None:
        return replay
    action, payload, dependencies = _resolution(connection, run_id, envelope, decision_input)
    store, exports = _exports(connection, run_root)
    decision, result, _export = store.publish_gate_resolution(
        gate_id, action=action, actor=payload["actor"], reason=payload["reason"],
        subject_revision_hash=envelope["subject_revision_hash"],
        approval_scope=json.loads(envelope["approval_scope_json"]), artifact_content=payload,
        dependencies=dependencies, export_directory=exports,
        idempotency_key=digest(decision_input),
        artifact_kind="sensitive_gate_resolution" if GateKind(envelope["kind"]) is GateKind.SENSITIVE_DISCLOSURE else "gate_resolution",
        fault_at=fault_at,
    )
    assert result.artifact is not None
    report_revision_id = None
    next_state = result.snapshot.state.value
    replayed = result.replayed
    if GateKind(envelope["kind"]) is GateKind.SENSITIVE_DISCLOSURE and action == "redact":
        from .report import apply_sensitive_redaction

        redacted = apply_sensitive_redaction(
            connection, run_root=run_root, run_id=run_id,
            decision_id=decision.decision_id, reason=payload["reason"], fault_at=fault_at,
        )
        report_revision_id = redacted.artifact.revision_id
        next_state = redacted.next_state
        replayed = replayed or redacted.replayed
    return DecisionRun(run_id, gate_id, decision.decision_id, result.artifact.revision_id,
                       action, next_state, replayed, report_revision_id)
