from __future__ import annotations

import os
import stat
from pathlib import Path


class PathPolicyError(ValueError):
    """A private path violated the repository containment policy."""


def _relative(path: Path, label: str) -> Path:
    if path.is_absolute():
        raise PathPolicyError(f"{label} rejected: absolute paths are not allowed")
    if any(part == ".." for part in path.parts):
        raise PathPolicyError(f"{label} rejected: parent traversal is not allowed")
    return path


def _check_existing_chain(path: Path, label: str) -> None:
    current = Path.cwd()
    for part in _relative(path, label).parts:
        if part in ("", "."):
            continue
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise PathPolicyError(f"{label} rejected: symbolic link: {current}")


def private_root(path: Path, label: str, *, create: bool = False) -> Path:
    path = _relative(path, label)
    _check_existing_chain(path, label)
    absolute = Path.cwd() / path
    if create:
        current = Path.cwd()
        for part in path.parts:
            if part in ("", "."):
                continue
            current = current / part
            if current.exists():
                if not stat.S_ISDIR(current.stat(follow_symlinks=False).st_mode):
                    raise PathPolicyError(f"{label} rejected: directory required")
            else:
                current.mkdir(mode=0o700)
            try:
                current.chmod(0o700)
            except OSError:
                pass
    if not absolute.exists() or not stat.S_ISDIR(absolute.stat(follow_symlinks=False).st_mode):
        raise PathPolicyError(f"{label} rejected: directory required")
    try:
        absolute.chmod(0o700)
    except OSError:
        pass
    return absolute.resolve(strict=True)


def contained_input(path: Path, root: Path, label: str, *, directory: bool = False) -> Path:
    path = _relative(path, label)
    _check_existing_chain(path, label)
    absolute = Path.cwd() / path
    resolved = absolute.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathPolicyError(f"{label} rejected: path outside configured root") from exc
    mode = absolute.stat(follow_symlinks=False).st_mode
    expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if not expected:
        kind = "directory" if directory else "regular file"
        raise PathPolicyError(f"{label} rejected: {kind} required")
    return resolved


def contained_directory(path: Path, root: Path, label: str) -> Path:
    """Resolve an existing directory relative to an already trusted absolute root."""
    path = _relative(path, label)
    current = Path(root).resolve(strict=True)
    for part in path.parts:
        if part in ("", "."):
            continue
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise PathPolicyError(f"{label} rejected: directory required") from None
        if stat.S_ISLNK(mode):
            raise PathPolicyError(f"{label} rejected: symbolic link: {current}")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(Path(root).resolve(strict=True))
    except ValueError as exc:
        raise PathPolicyError(f"{label} rejected: path outside configured root") from exc
    if not stat.S_ISDIR(current.stat(follow_symlinks=False).st_mode):
        raise PathPolicyError(f"{label} rejected: directory required")
    return resolved


def contained_output(path: Path, root: Path, label: str) -> Path:
    path = _relative(path, label)
    _check_existing_chain(path, label)
    absolute = Path.cwd() / path
    try:
        absolute.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PathPolicyError(f"{label} rejected: path outside configured root") from exc
    parent = absolute.parent
    if not parent.exists() or not stat.S_ISDIR(parent.stat(follow_symlinks=False).st_mode):
        raise PathPolicyError(f"{label} rejected: existing parent directory required")
    if absolute.exists() and not stat.S_ISREG(absolute.stat(follow_symlinks=False).st_mode):
        raise PathPolicyError(f"{label} rejected: regular file required")
    return absolute


def owner_only_file(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR, follow_symlinks=False)
    except OSError:
        pass
