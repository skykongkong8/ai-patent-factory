"""Immutable effective profiles pinned to one inventor, topic, and version pair."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .database import (
    connect_database, connect_read_only_database, consistent_snapshot, ingest, profile_payload,
)
from .paths import owner_only_file
from .privacy import assert_canaries_absent, credential_canaries
from .profile import IncomingFact, atomic_write_profile
from .provenance import canonical_json, claim_from_dict, digest, normalize, strict_json_loads
from .runs import PreparedRunProfile, prepare_run_profile


PROFILE_CONTEXT_V2 = "profile-context-v2"
_BINDING_FIELDS = ("inventor_id", "topic_id", "profile_version_id", "topic_version_id")


def _binding(value: Mapping[str, Any]) -> dict[str, Any]:
    binding = normalize(dict(value))
    if any(not isinstance(binding.get(field), str) or not binding[field]
           for field in _BINDING_FIELDS):
        raise ValueError("profile context binding requires inventor, topic, and version IDs")
    return binding


def _incoming(profile: Mapping[str, Any], prefix: str = "") -> list[IncomingFact]:
    facts = []
    for field, entry in sorted(profile["facts"].items()):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("claims"), list) or not entry["claims"]:
            raise ValueError("effective profile: fact claims are required")
        for raw in entry["claims"]:
            claim = claim_from_dict(raw, "effective profile.claim")
            if canonical_json(raw) != canonical_json(claim.as_dict()):
                raise ValueError("effective profile: claim does not match canonical provenance")
            facts.append(IncomingFact(prefix + field, entry["value"], claim))
    return facts


def _ready_profile(path: Path) -> dict[str, Any]:
    with connect_read_only_database(path) as connection:
        with consistent_snapshot(connection):
            profile = profile_payload(connection)
            prepare_run_profile(connection, profile)
    return profile


def _check_output_path(path: Path) -> None:
    for component in (path, *path.absolute().parents):
        if component.is_symlink():
            raise ValueError("effective profile: symbolic link output rejected")
    if path.exists() and not path.is_file():
        raise ValueError("effective profile: regular output file required")


def _check_database(path: Path, expected: Mapping[str, Any]) -> None:
    with connect_read_only_database(path) as connection:
        with consistent_snapshot(connection):
            if canonical_json(profile_payload(connection)) != canonical_json(expected):
                raise ValueError("effective profile: existing database contains different content")


def _check_export(path: Path, expected: Mapping[str, Any]) -> None:
    try:
        existing = strict_json_loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("effective profile: existing export is malformed") from exc
    if canonical_json(existing) != canonical_json(expected):
        raise ValueError("effective profile: existing export contains different content")


def prepare_effective_profile(
    profile_database: Path, topic_database: Path,
    output_database: Path, output_export: Path, binding: Mapping[str, Any],
) -> PreparedRunProfile:
    """Build or verify an effective DB through core ingest, never overwrite a snapshot.

    Callers validate configured-root containment and create the private output
    directories. Atomic no-replace publication makes concurrent exact retries safe.
    """

    binding = _binding(binding)
    base = _ready_profile(Path(profile_database))
    topic = _ready_profile(Path(topic_database))
    if any(field.startswith("topic.") for field in base["facts"]):
        raise ValueError("effective profile: reserved topic namespace in common profile")
    incoming = _incoming(base) + _incoming(topic, "topic.")
    assert_canaries_absent(
        {"base_profile": base, "topic_context": topic, "binding": binding},
        credential_canaries(), boundary="run_start.profile_context",
    )
    database, export = Path(output_database), Path(output_export)
    _check_output_path(database)
    _check_output_path(export)
    with tempfile.TemporaryDirectory(prefix=".effective-", dir=database.parent) as temporary:
        staged = Path(temporary) / "profile.sqlite3"
        with connect_database(staged) as connection:
            result = ingest(connection, "effective-profile-v2", incoming)
            if result.status != "profile_ready":
                raise ValueError("effective profile: combined profile must be profile_ready")
            effective = profile_payload(connection)
        if database.exists():
            _check_database(database, effective)
        if export.exists():
            _check_export(export, effective)
        try:
            os.link(staged, database)
        except FileExistsError:
            _check_database(database, effective)
        owner_only_file(database)
        if not export.exists():
            staged_export = Path(temporary) / "profile.json"
            atomic_write_profile(staged_export, effective)
            try:
                os.link(staged_export, export)
            except FileExistsError:
                _check_export(export, effective)
        owner_only_file(export)
    context = normalize({
        "base_profile": base, "base_profile_hash": digest(base), "binding": binding,
        "profile": effective, "profile_revision_hash": digest(effective),
        "profile_revision_id": effective["profile_revision"],
        "topic_context": topic, "topic_context_hash": digest(topic),
        "version": PROFILE_CONTEXT_V2,
    })
    return PreparedRunProfile(effective["profile_revision"], digest(context), context)


def resolve_pinned_profile(
    connection: sqlite3.Connection, profile_connection: sqlite3.Connection, run_id: str,
) -> dict[str, Any]:
    """Verify the supplied effective DB against a run's immutable v2 context.

    Legacy v1 callers retain their existing validation and profile selection.
    """

    supplied = profile_payload(profile_connection)
    row = connection.execute(
        "SELECT ar.revision_id,ar.content_json,ar.content_hash,ar.schema_version FROM artifact_revisions ar "
        "JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
        "WHERE ca.run_id=? AND ca.kind='profile_context' AND ar.stale=0", (run_id,),
    ).fetchone()
    if row is None:
        return supplied
    context = json.loads(row["content_json"])
    if context.get("version") != PROFILE_CONTEXT_V2:
        return supplied
    dependencies = tuple(item[0] for item in connection.execute(
        "SELECT upstream_revision_id FROM artifact_dependencies "
        "WHERE run_id=? AND downstream_revision_id=? ORDER BY upstream_revision_id",
        (run_id, row["revision_id"]),
    ))
    artifact_hash = digest({"content": context, "dependencies": dependencies,
                            "schema_version": row["schema_version"]})
    if row["schema_version"] != PROFILE_CONTEXT_V2 or artifact_hash != row["content_hash"]:
        raise ValueError("pinned profile context hash or schema does not match")
    _binding(context.get("binding", {}))
    for key, hash_key in (("profile", "profile_revision_hash"),
                          ("base_profile", "base_profile_hash"),
                          ("topic_context", "topic_context_hash")):
        if not isinstance(context.get(key), Mapping) or digest(context[key]) != context.get(hash_key):
            raise ValueError("pinned profile context contains an invalid profile hash")
    if (canonical_json(supplied) != canonical_json(context["profile"])
            or supplied.get("profile_revision") != context.get("profile_revision_id")):
        raise ValueError("pinned profile does not match the supplied authoritative database")
    prepare_run_profile(profile_connection, supplied)
    assert_canaries_absent(context, credential_canaries(), boundary="profile_context")
    return supplied


__all__ = ["PROFILE_CONTEXT_V2", "prepare_effective_profile", "resolve_pinned_profile"]
