from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .paths import owner_only_file
from .provenance import canonical_json


class ArtifactError(ValueError):
    """A redaction-safe immutable artifact failure."""


@dataclass(frozen=True)
class ArtifactExport:
    artifact_id: str
    content_hash: str
    path: str
    reused: bool
    size: int


def canonical_json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _ensure_safe_target(path: Path) -> None:
    if not path.is_absolute():
        raise ArtifactError("artifact_path: absolute contained path required")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ArtifactError("artifact_path: symbolic link rejected")
    parent = path.parent
    if not parent.exists() or not stat.S_ISDIR(parent.stat(follow_symlinks=False).st_mode):
        raise ArtifactError("artifact_path: existing directory required")
    if path.exists() and not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise ArtifactError("artifact_path: regular file required")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _result(path: Path, payload: bytes, *, reused: bool) -> ArtifactExport:
    content_hash = hashlib.sha256(payload).hexdigest()
    return ArtifactExport(
        artifact_id="ar_" + content_hash[:16],
        content_hash=content_hash,
        path=str(path),
        reused=reused,
        size=len(payload),
    )


def export_immutable(
    path: Path,
    payload: bytes,
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> ArtifactExport:
    """Publish bytes once using a same-directory, fsync-backed no-clobber operation."""

    _ensure_safe_target(path)
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            raise ArtifactError("artifact_conflict: immutable path already contains different bytes")
        return _result(path, payload, reused=True)

    hook = fault_hook or (lambda stage: None)
    handle, temporary_name = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    published_by_this_call = False
    try:
        hook("temporary_created")
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            hook("payload_written")
            stream.flush()
            os.fsync(stream.fileno())
        owner_only_file(temporary)
        hook("file_fsynced")
        try:
            os.link(temporary, path, follow_symlinks=False)
            published_by_this_call = True
        except FileExistsError:
            existing = path.read_bytes()
            if existing != payload:
                raise ArtifactError("artifact_conflict: concurrent immutable publish")
        hook("published")
        owner_only_file(path)
        _fsync_directory(path.parent)
        hook("directory_fsynced")
        temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        return _result(path, payload, reused=False)
    except BaseException:
        # A successfully linked target is complete even if cleanup or the durability
        # acknowledgement is interrupted. Recovery removes only the private temp link.
        if temporary.exists() and not published_by_this_call:
            temporary.unlink(missing_ok=True)
        raise


def export_immutable_json(
    path: Path,
    value: Any,
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> ArtifactExport:
    return export_immutable(path, canonical_json_bytes(value), fault_hook=fault_hook)


def recover_artifact_exports(
    directory: Path,
    registered: Mapping[Path, tuple[str, int]] | None = None,
) -> tuple[str, ...]:
    """Reconcile interrupted and registered exports under one private directory."""

    if directory.is_symlink() or not directory.is_dir():
        raise ArtifactError("artifact_recovery: safe directory required")
    removed: list[str] = []
    for temporary in sorted(directory.glob(".artifact-*.tmp"), key=lambda item: item.name):
        mode = temporary.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ArtifactError("artifact_recovery: unsafe temporary entry")
        temporary.unlink()
        removed.append(temporary.name)
    if registered is not None:
        normalized = {path.absolute(): expected for path, expected in registered.items()}
        for path, (expected_hash, expected_size) in normalized.items():
            if path.parent != directory.absolute():
                raise ArtifactError("artifact_recovery: registered path outside configured directory")
            try:
                _ensure_safe_target(path)
                payload = path.read_bytes()
            except FileNotFoundError:
                raise ArtifactError("artifact_recovery: registered export missing") from None
            if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_hash:
                raise ArtifactError("artifact_recovery: registered export mismatch")
        for published in sorted(directory.glob("ar_*"), key=lambda item: item.name):
            _ensure_safe_target(published)
            if published.suffix not in {".json", ".md"}:
                raise ArtifactError("artifact_recovery: unsupported published artifact suffix")
            if published.absolute() not in normalized:
                published.unlink()
                removed.append(published.name)
    if removed:
        _fsync_directory(directory)
    return tuple(removed)
