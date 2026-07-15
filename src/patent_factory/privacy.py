from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar

from .provenance import digest, normalize


class PrivacyError(ValueError):
    """A redaction-safe privacy boundary failure."""


class DataClass(StrEnum):
    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"
    INTERNAL_PUBLIC_DERIVED = "internal_public_derived"
    INTERNAL_REDACTED = "internal_redacted"
    PUBLIC_REDACTED = "public_redacted"


DEFAULT_RETENTION: Mapping[DataClass, str] = {
    DataClass.RESTRICTED: "environment_or_private_run_only",
    DataClass.CONFIDENTIAL: "run_lifetime",
    DataClass.INTERNAL_PUBLIC_DERIVED: "content_hash_revision",
    DataClass.INTERNAL_REDACTED: "30_days",
    DataClass.PUBLIC_REDACTED: "repository_lifetime",
}

SECRET_FIELD_MARKERS = ("api_key", "apikey", "authorization", "password", "secret", "token")
PROPRIETARY_FIELD_MARKERS = ("private", "proprietary", "raw_document", "source_span")


@dataclass(frozen=True)
class EgressApproval:
    decision_id: str
    subject_revision_hash: str
    recipient: str
    model_class: str
    purpose: str
    approval_scope: str
    approved_data_classes: tuple[DataClass, ...]
    current: bool = True

    def validate(self) -> None:
        if not self.current:
            raise PrivacyError("egress_denied: stale_decision")
        for field_name in ("decision_id", "subject_revision_hash", "recipient", "model_class", "purpose", "approval_scope"):
            if not normalize(getattr(self, field_name)):
                raise PrivacyError(f"egress_approval.{field_name}: required")
        if not self.approved_data_classes:
            raise PrivacyError("egress_approval.approved_data_classes: non-empty scope required")
        if any(not isinstance(item, DataClass) for item in self.approved_data_classes):
            raise PrivacyError("egress_approval.approved_data_classes: invalid data class")


@dataclass(frozen=True)
class EgressManifest:
    manifest_id: str
    decision_id: str
    subject_revision_hash: str
    recipient: str
    model_class: str
    purpose: str
    approval_scope: str
    data_classes: tuple[str, ...]
    content_hashes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_scope": self.approval_scope,
            "content_hashes": list(self.content_hashes),
            "data_classes": list(self.data_classes),
            "decision_id": self.decision_id,
            "manifest_id": self.manifest_id,
            "model_class": self.model_class,
            "purpose": self.purpose,
            "recipient": self.recipient,
            "subject_revision_hash": self.subject_revision_hash,
        }


def environment_secret(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Read a secret without returning it in any diagnostic structure."""

    if not name or not name.isascii() or not all(character.isupper() or character.isdigit() or character == "_" for character in name):
        raise PrivacyError("secret_name_invalid")
    return (os.environ if environ is None else environ).get(name)


# Every credential the local pipeline may hold. Leak-canary boundaries scrub all
# of them, so protection does not depend on which single adapter happens to run.
KNOWN_CREDENTIAL_NAMES: tuple[str, ...] = ("KIPRIS_PLUS_API_KEY", "SERPAPI_API_KEY")


def credential_canaries(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the present secret values for every known credential, for canary checks."""

    values: list[str] = []
    for name in KNOWN_CREDENTIAL_NAMES:
        secret = environment_secret(name, environ)
        if secret:
            values.append(secret)
    return tuple(values)


def secret_status(name: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    present = bool(environment_secret(name, environ))
    return {"name": name, "present": present, "status": "configured" if present else "missing"}


def credential_diagnostic(
    name: str,
    environ: Mapping[str, str] | None = None,
    *,
    simulated_invalid: bool = False,
    fixture_usable: bool = False,
) -> dict[str, Any]:
    """Return one redacted, offline-verifiable credential state."""

    if simulated_invalid and fixture_usable:
        raise PrivacyError("credential_diagnostic: conflicting simulation modes")
    status = secret_status(name, environ)
    if fixture_usable:
        return {**status, "mode": "fixture", "status": "fixture_usable"}
    if simulated_invalid:
        return {**status, "mode": "simulated", "status": "simulated_invalid"}
    return {
        **status,
        "mode": "environment",
        "status": "present" if status["present"] else "missing",
    }


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _walk_strings(item)]
    if isinstance(value, (list, tuple, set)):
        return [text for item in value for text in _walk_strings(item)]
    return []


def assert_canaries_absent(value: Any, canaries: Sequence[str], *, boundary: str) -> None:
    texts = _walk_strings(value)
    if any(canary and canary in text for canary in canaries for text in texts):
        raise PrivacyError(f"{boundary}: canary_detected")


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a diagnostic-safe mapping without secret or proprietary field values."""

    redacted: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key).casefold()
        if any(marker in normalized_key for marker in SECRET_FIELD_MARKERS + PROPRIETARY_FIELD_MARKERS):
            redacted[str(key)] = "[REDACTED]"
        elif isinstance(item, Mapping):
            redacted[str(key)] = redact_mapping(item)
        elif isinstance(item, list):
            redacted[str(key)] = [redact_mapping(entry) if isinstance(entry, Mapping) else entry for entry in item]
        else:
            redacted[str(key)] = item
    return redacted


def build_egress_manifest(
    approval: EgressApproval,
    *,
    subject_revision_hash: str,
    recipient: str,
    model_class: str,
    purpose: str,
    approval_scope: str,
    data_classes: Sequence[DataClass],
    content_hashes: Sequence[str],
) -> EgressManifest:
    approval.validate()
    if any(not isinstance(item, DataClass) for item in data_classes):
        raise PrivacyError("egress_denied: invalid_data_class")
    requested = tuple(sorted(set(data_classes), key=lambda item: item.value))
    if not requested:
        raise PrivacyError("egress_denied: empty_data_scope")
    comparisons = {
        "subject_revision_hash": (approval.subject_revision_hash, subject_revision_hash),
        "recipient": (approval.recipient, recipient),
        "model_class": (approval.model_class, model_class),
        "purpose": (approval.purpose, purpose),
        "approval_scope": (approval.approval_scope, approval_scope),
    }
    mismatch = next((name for name, values in comparisons.items() if normalize(values[0]) != normalize(values[1])), None)
    if mismatch:
        raise PrivacyError(f"egress_denied: {mismatch}_mismatch")
    if not set(requested).issubset(approval.approved_data_classes):
        raise PrivacyError("egress_denied: data_class_not_approved")
    normalized_hashes = tuple(sorted({str(normalize(item)) for item in content_hashes if normalize(item)}))
    body = {
        "decision_id": approval.decision_id,
        "subject_revision_hash": subject_revision_hash,
        "recipient": recipient,
        "model_class": model_class,
        "purpose": purpose,
        "approval_scope": approval_scope,
        "data_classes": [item.value for item in requested],
        "content_hashes": list(normalized_hashes),
    }
    return EgressManifest(
        manifest_id="eg_" + digest(body)[:16],
        decision_id=approval.decision_id,
        subject_revision_hash=subject_revision_hash,
        recipient=recipient,
        model_class=model_class,
        purpose=purpose,
        approval_scope=approval_scope,
        data_classes=tuple(item.value for item in requested),
        content_hashes=normalized_hashes,
    )


T = TypeVar("T")


def guarded_hosted_call(
    callback: Callable[[EgressManifest], T],
    *,
    approval: EgressApproval | None,
    subject_revision_hash: str,
    recipient: str,
    model_class: str,
    purpose: str,
    approval_scope: str,
    data_classes: Sequence[DataClass],
    content_hashes: Sequence[str],
    payload: Any,
    canaries: Sequence[str] = (),
) -> tuple[T, EgressManifest]:
    if approval is None:
        raise PrivacyError("egress_denied: approval_required")
    assert_canaries_absent(payload, canaries, boundary="hosted_egress")
    manifest = build_egress_manifest(
        approval,
        subject_revision_hash=subject_revision_hash,
        recipient=recipient,
        model_class=model_class,
        purpose=purpose,
        approval_scope=approval_scope,
        data_classes=data_classes,
        content_hashes=content_hashes,
    )
    return callback(manifest), manifest


@dataclass(frozen=True)
class DeletionReport:
    root: str
    removed: tuple[str, ...]
    failures: tuple[dict[str, str], ...]

    @property
    def complete(self) -> bool:
        return not self.failures


def _contained_run(run_path: Path, workspace_root: Path) -> tuple[Path, Path]:
    root = workspace_root.absolute()
    run = run_path.absolute()
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError:
        raise PrivacyError("delete_run: workspace_not_found") from None
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise PrivacyError("delete_run: unsafe_workspace_root")
    try:
        relative = run.relative_to(root)
    except ValueError as exc:
        raise PrivacyError("delete_run: path_outside_workspace") from exc
    if not relative.parts:
        raise PrivacyError("delete_run: workspace_root_forbidden")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise PrivacyError("delete_run: run_not_found") from None
        if stat.S_ISLNK(mode):
            raise PrivacyError("delete_run: symlink_root_rejected")
    if not stat.S_ISDIR(run.lstat().st_mode):
        raise PrivacyError("delete_run: directory_required")
    return root, run


def delete_run(run_path: Path, workspace_root: Path) -> DeletionReport:
    """Delete one contained run without following links or touching siblings."""

    root, run = _contained_run(run_path, workspace_root)
    removed: list[str] = []
    failures: list[dict[str, str]] = []

    def relative(path: Path) -> str:
        return path.relative_to(root).as_posix()

    def remove(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                for entry in sorted(path.iterdir(), key=lambda item: item.name):
                    remove(entry)
                path.rmdir()
            else:
                path.unlink()
            removed.append(relative(path))
        except OSError as exc:
            failures.append({"code": type(exc).__name__, "path": relative(path)})

    remove(run)
    return DeletionReport(relative(run), tuple(removed), tuple(failures))
