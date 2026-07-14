from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .database import FaultInjector
from .models import GateEnvelope, GateKind, RunState
from .paths import contained_directory
from .provenance import digest, normalize
from .report import _current_artifact, _report_state, validate_report_artifact
from .review import validate_review_artifact
from .state import GateMismatchError, StateError, StateStore
from .validation import validate_validation_artifact


SHARE_INPUT_VERSION = "external-report-share-v1"
MANAGED_SHARE_DIRECTORY = ".patent-factory-shares"


class SensitiveDisclosureRequiredError(RuntimeError):
    def __init__(self, gate: GateEnvelope) -> None:
        super().__init__("sensitive_disclosure_required: decide the exact external report share")
        self.gate = gate


@dataclass(frozen=True)
class ShareRun:
    run_id: str
    report_hash: str
    receipt_revision_id: str
    export_path: str
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ids": [self.receipt_revision_id], "command": "share",
            "export_path": self.export_path, "next_state": RunState.COMPLETE.value,
            "replayed": self.replayed, "report_hash": self.report_hash,
            "run_id": self.run_id, "status": RunState.COMPLETE.value,
        }


def _text(value: Any, path: str) -> str:
    item = normalize(value)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{path}: non-empty string required")
    return item


def validate_share_input(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {"destination", "purpose", "recipient", "report_hash", "schema_version", "sensitive_fields"}
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != SHARE_INPUT_VERSION:
        raise ValueError("share_input: exact external-report-share-v1 fields required")
    fields = value["sensitive_fields"]
    if not isinstance(fields, list) or any(not isinstance(item, str) or not normalize(item) for item in fields):
        raise ValueError("share_input.sensitive_fields: string array required")
    if fields != sorted(set(fields)):
        raise ValueError("share_input.sensitive_fields: sorted unique fields required")
    return normalize({
        "destination": _text(value["destination"], "share_input.destination"),
        "purpose": _text(value["purpose"], "share_input.purpose"),
        "recipient": _text(value["recipient"], "share_input.recipient"),
        "report_hash": _text(value["report_hash"], "share_input.report_hash"),
        "schema_version": SHARE_INPUT_VERSION, "sensitive_fields": fields,
    })


def _safe_destination(run_root: Path, value: str) -> Path:
    root = Path(run_root).absolute()
    destination = contained_directory(Path(value), root.parent, "external share destination")
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("external share destination must be outside the owner-only run")
    return destination


def _managed_share_directory(destination: Path) -> Path:
    """Create or validate the only caller-destination subtree we may recover."""
    directory = destination / MANAGED_SHARE_DIRECTORY
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        raise ValueError("external share managed directory is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("external share managed directory must be a non-symbolic-link directory")
    if metadata.st_uid != os.geteuid():
        raise ValueError("external share managed directory must be owned by the current user")
    try:
        os.chmod(directory, 0o700, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("external share managed directory must be owner-only") from exc
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("external share managed directory must be owner-only")
    return directory


def _completed_share_replay(
    connection: sqlite3.Connection, *, run_id: str, operation: str,
    scope: Mapping[str, Any], report_hash: str,
) -> ShareRun | None:
    row = connection.execute(
        "SELECT ar.revision_id,ar.content_json,ar.stale,ae.path FROM idempotency_records ir "
        "JOIN artifact_revisions ar ON ar.revision_id=ir.artifact_revision_id "
        "JOIN artifact_exports ae ON ae.revision_id=ar.revision_id "
        "WHERE ir.run_id=? AND ir.operation=? AND ir.state_after='complete' "
        "AND ar.kind='share_receipt' ORDER BY ir.created_at DESC LIMIT 1",
        (run_id, operation),
    ).fetchone()
    if row is None:
        return None
    receipt = json.loads(row["content_json"])
    if (
        row["stale"]
        or receipt.get("approval_scope_hash") != digest(scope)
        or receipt.get("report_hash") != report_hash
        or receipt.get("version") != "external-share-receipt-v1"
    ):
        raise StateError("completed share replay record is stale or scope-mismatched")
    return ShareRun(run_id, report_hash, row["revision_id"], row["path"], True)


def share_report(
    connection: sqlite3.Connection, *, run_root: Path, run_id: str,
    share_input: Mapping[str, Any], decision_id: str | None = None,
    fault_at: FaultInjector = None,
) -> ShareRun:
    request = validate_share_input(share_input)
    destination = _safe_destination(run_root, request["destination"])
    base_state, _private_exports = _report_state(connection, run_root)
    prior = base_state.snapshot(run_id)
    if prior.state not in {RunState.COMPLETE, RunState.SENSITIVE_DISCLOSURE_REQUIRED}:
        raise StateError("external report share requires a completed private report")
    report_row, report = _current_artifact(connection, run_id, "report")
    review_row, review = _current_artifact(connection, run_id, "review")
    validation_row, validation = _current_artifact(connection, run_id, "validation")
    validate_report_artifact(report)
    validate_review_artifact(review, report=report)
    validate_validation_artifact(validation)
    if request["report_hash"] != report_row["content_hash"]:
        raise StateError("share input must bind the current report artifact hash")
    report_fields = sorted(item["field"] for item in report["sensitive_disclosures"])
    if request["sensitive_fields"] != report_fields:
        raise ValueError("share input sensitive fields must exactly match the current report")
    fields = [{
        "field": item["field"], "reason": item["reason"], "text_hash": item["text_hash"],
    } for item in sorted(report["sensitive_disclosures"], key=lambda item: item["field"])]
    scope = normalize({
        "content_hash": digest({"markdown": report["markdown"]}),
        "destination": str(destination), "fields": fields, "purpose": request["purpose"],
        "recipient": request["recipient"], "report_hash": report_row["content_hash"],
        "review_hash": review_row["content_hash"], "validation_hash": validation_row["content_hash"],
    })
    operation = f"report.share:{digest(scope)}"
    replay = _completed_share_replay(
        connection, run_id=run_id, operation=operation, scope=scope,
        report_hash=report_row["content_hash"],
    )
    if replay is not None:
        return replay
    if decision_id is None:
        if prior.state is RunState.SENSITIVE_DISCLOSURE_REQUIRED:
            row = connection.execute(
                "SELECT * FROM gate_envelopes WHERE run_id=? AND kind='sensitive_disclosure' AND status='pending'",
                (run_id,),
            ).fetchone()
            if row is None or row["subject_revision_hash"] != report_row["content_hash"] or json.loads(row["approval_scope_json"]) != scope:
                raise GateMismatchError("pending sensitive disclosure gate does not match this share")
            gate = GateEnvelope(
                row["gate_id"], run_id, GateKind.SENSITIVE_DISCLOSURE,
                RunState(row["suspended_state"]), row["suspended_operation"], row["subject_revision_hash"],
                scope, row["approval_scope_hash"], RunState(row["return_state"]), row["created_at"], row["status"],
            )
        else:
            gate = base_state.suspend_gate(
                run_id, GateKind.SENSITIVE_DISCLOSURE, suspended_operation=operation,
                subject_revision_hash=report_row["content_hash"], approval_scope=scope,
                return_state=RunState.COMPLETE, actor="share-cli",
                reason="external report share requires exact disclosure approval",
            )
        raise SensitiveDisclosureRequiredError(gate)
    row = connection.execute(
        "SELECT gd.*,ge.approval_scope_json,ge.kind FROM gate_decisions gd JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
        "WHERE gd.decision_id=? AND gd.run_id=?", (decision_id, run_id),
    ).fetchone()
    if (
        row is None or row["kind"] != "sensitive_disclosure" or row["action"] != "approve"
        or row["subject_revision_hash"] != report_row["content_hash"]
        or row["suspended_operation"] != operation or json.loads(row["approval_scope_json"]) != scope
    ):
        raise GateMismatchError("share approval does not match the exact report, recipient, destination, purpose, and fields")
    managed_destination = _managed_share_directory(destination)
    base_state.consume_decision(
        decision_id, suspended_operation=operation,
        subject_revision_hash=report_row["content_hash"], approval_scope=scope,
    )
    directories = set(base_state.export_directories) | {managed_destination}
    state = StateStore(connection, export_directories=tuple(sorted(directories)))
    receipt = normalize({
        "approval_scope_hash": digest(scope), "destination": str(destination),
        "purpose": request["purpose"], "recipient": request["recipient"],
        "report_hash": report_row["content_hash"], "run_id": run_id,
        "version": "external-share-receipt-v1",
    })
    result, exported = state.publish_transition(
        run_id, RunState.COMPLETE, actor="share-cli", reason="approved external report share published",
        operation=operation, idempotency_key=digest({"decision_id": decision_id, "scope": scope}),
        artifact_kind="share_receipt", artifact_content=receipt,
        artifact_schema_version="external-share-receipt-v1",
        dependencies=(report_row["revision_id"], review_row["revision_id"], validation_row["revision_id"]),
        export_directory=managed_destination, export_payload=(report["markdown"] + "\n").encode("utf-8"),
        export_suffix=".md", consumed_decision_id=decision_id, fault_at=fault_at,
    )
    if result.artifact is None:
        raise RuntimeError("external share produced no receipt artifact")
    return ShareRun(run_id, report_row["content_hash"], result.artifact.revision_id, exported.path, result.replayed)


__all__ = [
    "SHARE_INPUT_VERSION", "SensitiveDisclosureRequiredError", "ShareRun", "share_report",
    "validate_share_input",
]
