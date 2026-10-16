"""Explicit, local-only adoption of pre-catalog profile and run state.

Inspection uses read-only core connections. Adoption never changes a legacy run:
only its ownership and a separate historical profile snapshot are published.
"""
from __future__ import annotations

import json
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .catalog import CatalogError
from .database import (
    BUSY_TIMEOUT_MS, SCHEMA_VERSION, DatabaseCorruptError, connect_database,
    consistent_snapshot, export_profile, immediate_transaction, profile_payload,
    resolve_run_id,
)
from .paths import PathPolicyError, enforce_private_directory
from .provenance import canonical_json, digest
from .runs import run_show, run_status


@contextmanager
def read_legacy_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Read supported old databases without upgrading their bytes or permissions."""

    path = _checked_path(path)
    connection = sqlite3.connect(
        f"file:{quote(str(path))}?mode=ro", uri=True,
        timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 7 or version > SCHEMA_VERSION:
            raise ValueError(f"legacy database: unsupported schema version {version}")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DatabaseCorruptError("database_corrupt: integrity check failed")
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise DatabaseCorruptError('database_corrupt: legacy database could not be read') from exc
    except BaseException:
        connection.close()
        raise
    try:
        yield connection
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptError('database_corrupt: legacy database could not be read') from exc
    finally:
        connection.close()


def _checked_path(path: Path, root: Path | None = None, *, directory: bool = False) -> Path:
    """Validate internal absolute paths without chmod, mkdir, or following links."""

    candidate = Path(path)
    if ".." in candidate.parts:
        raise PathPolicyError("legacy path rejected: parent traversal is not allowed")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    current = Path(candidate.anchor)
    mode = current.lstat().st_mode
    for part in candidate.parts[1:]:
        current /= part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise PathPolicyError("legacy path rejected: symbolic link")
    if root is not None:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise PathPolicyError("legacy path rejected: outside workspace root") from exc
    expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if not expected:
        raise PathPolicyError("legacy path rejected: directory required" if directory else "legacy path rejected: regular file required")
    return candidate


def inspect_legacy(
    workspace_root: Path, *, run: Path | None = None, run_id: str | None = None,
) -> dict[str, Any]:
    """Report only metadata for the global profile and an explicitly chosen run."""

    root = _checked_path(workspace_root, directory=True)
    result: dict[str, Any] = {"command": "legacy.inspect", "status": "reported", "profile": None, "runs": []}
    database = root / "profile.sqlite3"
    if database.exists() or database.is_symlink():
        _checked_path(database, root)
        with read_legacy_database(database) as connection:
            payload = profile_payload(connection)
            result["profile"] = {
                "database": str(database.relative_to(root)),
                "profile_revision": payload["profile_revision"],
                "content_hash": digest(payload),
                "fact_count": len(payload["facts"]),
                "conflict_count": len(payload["conflicts"]),
                "state": payload["state"],
            }
    if run is not None:
        run_root = _checked_path(run, root, directory=True)
        database = _checked_path(run_root / "factory.sqlite3", root)
        with read_legacy_database(database) as connection:
            resolved_id = resolve_run_id(connection, run_id)
            metadata = run_status(connection, resolved_id)
        metadata.pop("command", None)
        metadata["run"] = str(run_root.relative_to(root))
        result["runs"].append(metadata)
    elif run_id is not None:
        raise ValueError("legacy inspect: --run is required with --run-id")
    return result


def _historical_profile(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    shown = run_show(connection, run_id, "profile_context")
    context = shown["content"]
    if shown["schema_version"] != "profile-context-v1" or context.get("version") != "profile-context-v1":
        raise ValueError("legacy adopt: run requires a profile-context-v1 artifact")
    profile = context.get("profile")
    if not isinstance(profile, dict) or digest(profile) != context.get("profile_revision_hash"):
        raise ValueError("legacy adopt: historical profile hash mismatch")
    if (
        profile.get("profile_version") != "profile-v1"
        or profile.get("state") != "profile_ready"
        or profile.get("conflicts")
        or not isinstance(profile.get("facts"), dict)
        or profile.get("profile_revision") != context.get("profile_revision_id")
    ):
        raise ValueError("legacy adopt: historical profile must be profile_ready")
    dependencies = [row[0] for row in connection.execute(
        "SELECT upstream_revision_id FROM artifact_dependencies WHERE run_id=? AND downstream_revision_id=? ORDER BY upstream_revision_id",
        (run_id, shown["revision_id"]),
    )]
    if digest({"content": context, "dependencies": dependencies, "schema_version": shown["schema_version"]}) != shown["content_hash"]:
        raise ValueError("legacy adopt: profile context hash mismatch")
    return profile


def _managed_directory(path: Path, root: Path) -> Path:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink():
            _checked_path(current, root, directory=True)
        else:
            current.mkdir(mode=0o700)
        enforce_private_directory(current, "legacy managed snapshot directory")
    return current


def _materialize_historical_profile(
    root: Path, inventor_id: str, topic_id: str, profile: dict[str, Any],
) -> tuple[Path, Path]:
    """Create an authoritative scoped DB retaining historical claims and revision."""

    directory = _managed_directory(
        root / "inventors" / inventor_id / "topics" / topic_id / "legacy-contexts" / digest(profile), root,
    )
    database, exported = directory / "profile.sqlite3", directory / "profile.json"
    if database.exists() or database.is_symlink():
        _checked_path(database, root)
    if exported.exists() or exported.is_symlink():
        _checked_path(exported, root)
    with closing(connect_database(database)) as connection:
        existing = profile_payload(connection)
        if existing["state"] == "profile_pending" and not existing["facts"]:
            batch_id = "legacy_" + digest(profile)
            claims = sum(len(entry["claims"]) for entry in profile["facts"].values())
            with immediate_transaction(connection):
                connection.execute(
                    "INSERT INTO ingestion_batches VALUES(?,?,?,?, 'applied',?)",
                    (batch_id, "legacy-context", digest(profile), len(profile["facts"]), claims),
                )
                for field, entry in profile["facts"].items():
                    connection.execute("INSERT INTO profile_facts VALUES(?,?)", (field, canonical_json(entry["value"])))
                    for claim in entry["claims"]:
                        connection.execute(
                            "INSERT INTO profile_claims VALUES(?,?,?,?)",
                            (field, claim["claim_id"], canonical_json(claim), batch_id),
                        )
                connection.execute(
                    "INSERT INTO profile_state VALUES(1,?,?)",
                    (profile["state"], profile["profile_revision"]),
                )
        if canonical_json(profile_payload(connection)) != canonical_json(profile):
            raise ValueError("legacy adopt: historical profile snapshot mismatch")
        export_profile(connection, exported)
    return database, exported


def adopt_legacy(
    catalog: Any, *, inventor_id: str, topic_id: str | None = None,
    run: Path | None = None, run_id: str | None = None, profile: bool = False,
) -> dict[str, Any]:
    """Explicitly assign one global profile or one untouched v1 run to its owner."""

    if profile == (run is not None):
        raise ValueError("legacy adopt: select exactly one profile or run")
    inventor = catalog.entity("inventor", inventor_id)
    if inventor["archived"]:
        raise ValueError("legacy adopt: inventor is archived")
    root = _checked_path(catalog.workspace_root, directory=True)
    result: dict[str, Any] = {"command": "legacy.adopt", "status": "adopted", "inventor_id": inventor_id}
    if profile:
        if topic_id is not None or run_id is not None:
            raise ValueError("legacy adopt: profile ownership does not accept topic or run id")
        source_database = _checked_path(root / "profile.sqlite3", root)
        with read_legacy_database(source_database) as source, consistent_snapshot(source):
            payload = profile_payload(source)
            if payload["state"] != "profile_ready" or payload["conflicts"]:
                raise ValueError("legacy adopt: source profile must be profile_ready with no pending conflicts")
            source_relative = source_database.relative_to(Path.cwd())
            source_hash = digest(payload)
            claimed = catalog.register_legacy_source(source_relative, inventor_id, source_hash=source_hash)
            version_id = claimed.get("version_id") or "pv_" + digest({
                "source_path": source_relative.as_posix(), "source_hash": source_hash, "inventor_id": inventor_id,
            })[:24]
            try:
                candidate = catalog.version(version_id)
            except CatalogError as exc:
                if exc.code != "catalog_version_missing":
                    raise
                candidate = None
            if candidate is not None and candidate["status"] == "ready":
                result["profile"] = candidate
                result["replayed"] = True
                return result
            candidate = catalog.prepare_version("profile", inventor_id, replace=True, version_id=version_id)
            catalog.register_legacy_source(source_relative, inventor_id, source_hash=source_hash, version_id=version_id)
            with catalog.edit_version(version_id):
                destination_path = Path(candidate["database"])
                if destination_path.exists() or destination_path.is_symlink():
                    _checked_path(destination_path, root)
                destination = sqlite3.connect(destination_path, isolation_level=None)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
                # Upgrade only the separate managed copy when the source predates the catalog.
                migrated = connect_database(destination_path)
                migrated.close()
                result["profile"] = catalog.finish_version(candidate["version_id"])
        return result

    if not topic_id:
        raise ValueError("legacy adopt: topic is required for a run")
    if not run_id:
        raise ValueError("legacy adopt: exact run id is required")
    topic = catalog.entity("topic", topic_id, inventor_id=inventor_id)
    if topic["archived"]:
        raise ValueError("legacy adopt: topic is archived")
    run_root = _checked_path(run, root, directory=True)
    database = _checked_path(run_root / "factory.sqlite3", root)
    with read_legacy_database(database) as connection, consistent_snapshot(connection):
        resolved = resolve_run_id(connection, run_id)
        historical = _historical_profile(connection, resolved)
    run_relative = run_root.relative_to(Path.cwd())
    existing = catalog.run_binding(run_relative, run_id=resolved)
    if existing is not None and (
        existing["inventor_id"] != inventor_id or existing["topic_id"] != topic_id
    ):
        raise ValueError("legacy adopt: run is already bound to another owner")
    if existing is not None:
        if not existing["legacy"] or not existing.get("legacy_profile_database") or not existing.get("legacy_profile_export"):
            raise ValueError("legacy adopt: existing run binding has no historical profile")
        snapshot_database = _checked_path(Path(existing["legacy_profile_database"]), root)
        snapshot_export = _checked_path(Path(existing["legacy_profile_export"]), root)
        with read_legacy_database(snapshot_database) as connection:
            if canonical_json(profile_payload(connection)) != canonical_json(historical):
                raise ValueError("legacy adopt: historical profile snapshot mismatch")
        if canonical_json(json.loads(snapshot_export.read_text(encoding="utf-8"))) != canonical_json(historical):
            raise ValueError("legacy adopt: historical profile export mismatch")
        result.update(run=existing, topic_id=topic_id, replayed=True)
        return result
    snapshot_database, snapshot_export = _materialize_historical_profile(root, inventor_id, topic_id, historical)
    result["run"] = catalog.bind_run(
        run_relative, resolved, inventor_id, topic_id, None, None, legacy=True,
        legacy_profile_database=snapshot_database.relative_to(Path.cwd()).as_posix(),
        legacy_profile_export=snapshot_export.relative_to(Path.cwd()).as_posix(),
    )
    result["topic_id"] = topic_id
    return result


__all__ = ["adopt_legacy", "inspect_legacy", "read_legacy_database"]
