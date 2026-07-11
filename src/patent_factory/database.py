from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .paths import owner_only_file
from .profile import IncomingFact, PROFILE_VERSION, atomic_write_profile
from .provenance import canonical_json, digest

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS ingestion_batches (
    batch_id TEXT PRIMARY KEY,
    input_mode TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    input_count INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'conflict')),
    changes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_facts (
    field TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_claims (
    field TEXT NOT NULL REFERENCES profile_facts(field),
    claim_id TEXT NOT NULL,
    claim_json TEXT NOT NULL,
    batch_id TEXT NOT NULL REFERENCES ingestion_batches(batch_id),
    PRIMARY KEY (field, claim_id)
);
CREATE TABLE IF NOT EXISTS profile_conflicts (
    conflict_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES ingestion_batches(batch_id),
    field TEXT NOT NULL,
    existing_value_json TEXT NOT NULL,
    incoming_value_json TEXT NOT NULL,
    incoming_source_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    status TEXT NOT NULL,
    revision TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class IngestionResult:
    batch_id: str
    changes: int
    conflicts: tuple[dict[str, str], ...]
    fact_count: int
    input_count: int
    profile_revision: str
    status: str


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        connection.close()
        raise ValueError(f"database: unsupported schema version {version}")
    connection.executescript(SCHEMA)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()
    owner_only_file(path)
    return connection


def _batch_payload(input_mode: str, facts: list[IncomingFact]) -> dict[str, Any]:
    return {
        "input_mode": input_mode,
        "facts": [
            {"field": fact.field, "value": fact.value, "claim": fact.claim.as_dict()}
            for fact in facts
        ],
    }


def _revision(connection: sqlite3.Connection) -> str:
    facts = [tuple(row) for row in connection.execute("SELECT field, value_json FROM profile_facts ORDER BY field")]
    claims = [tuple(row) for row in connection.execute("SELECT field, claim_id, claim_json FROM profile_claims ORDER BY field, claim_id")]
    return "pr_" + digest({"facts": facts, "claims": claims})[:16]


def _conflicts_for_batch(connection: sqlite3.Connection, batch_id: str) -> tuple[dict[str, str], ...]:
    rows = connection.execute(
        "SELECT conflict_id, field, existing_value_json, incoming_value_json, incoming_source_id "
        "FROM profile_conflicts WHERE batch_id = ? ORDER BY field, conflict_id",
        (batch_id,),
    )
    return tuple({
        "conflict_id": row["conflict_id"],
        "existing_value_hash": digest(json.loads(row["existing_value_json"])),
        "field": row["field"],
        "incoming_value_hash": digest(json.loads(row["incoming_value_json"])),
        "incoming_source_id": row["incoming_source_id"],
    } for row in rows)


def _result(connection: sqlite3.Connection, batch_id: str) -> IngestionResult:
    batch = connection.execute("SELECT * FROM ingestion_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    conflicts = _conflicts_for_batch(connection, batch_id)
    status = connection.execute("SELECT status FROM profile_state WHERE singleton = 1").fetchone()[0]
    return IngestionResult(
        batch_id=batch_id,
        changes=batch["changes"],
        conflicts=conflicts,
        fact_count=connection.execute("SELECT count(*) FROM profile_facts").fetchone()[0],
        input_count=batch["input_count"],
        profile_revision=_revision(connection),
        status=status,
    )


def ingest(connection: sqlite3.Connection, input_mode: str, incoming: Iterable[IncomingFact]) -> IngestionResult:
    facts = [fact.normalized() for fact in incoming]
    payload = _batch_payload(input_mode, facts)
    input_digest = digest(payload)
    batch_id = "ib_" + input_digest[:16]
    connection.execute("BEGIN IMMEDIATE")
    try:
        prior = connection.execute("SELECT 1 FROM ingestion_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if prior is not None:
            result = replace(_result(connection, batch_id), changes=0)
            connection.commit()
            return result

        conflicts: list[tuple[str, str, str, str]] = []
        pending: dict[str, str] = {}
        for fact in facts:
            incoming_json = canonical_json(fact.value)
            row = connection.execute("SELECT value_json FROM profile_facts WHERE field = ?", (fact.field,)).fetchone()
            existing_json = pending.get(fact.field, row[0] if row else incoming_json)
            if existing_json != incoming_json:
                conflicts.append((fact.field, existing_json, incoming_json, fact.claim.source_id or ""))
            pending[fact.field] = existing_json

        if conflicts:
            connection.execute(
                "INSERT INTO ingestion_batches VALUES (?, ?, ?, ?, 'conflict', 0)",
                (batch_id, input_mode, input_digest, len(facts)),
            )
            for field, existing_json, incoming_json, source_id in conflicts:
                conflict_id = "cf_" + digest({
                    "batch_id": batch_id, "field": field, "existing": existing_json,
                    "incoming": incoming_json, "source_id": source_id,
                })[:16]
                connection.execute(
                    "INSERT OR IGNORE INTO profile_conflicts VALUES (?, ?, ?, ?, ?, ?)",
                    (conflict_id, batch_id, field, existing_json, incoming_json, source_id),
                )
            revision = _revision(connection)
            connection.execute(
                "INSERT INTO profile_state VALUES (1, 'conflict_resolution_required', ?) "
                "ON CONFLICT(singleton) DO UPDATE SET status=excluded.status, revision=excluded.revision",
                (revision,),
            )
        else:
            changes = 0
            connection.execute(
                "INSERT INTO ingestion_batches VALUES (?, ?, ?, ?, 'applied', 0)",
                (batch_id, input_mode, input_digest, len(facts)),
            )
            for fact in facts:
                value_json = canonical_json(fact.value)
                connection.execute("INSERT OR IGNORE INTO profile_facts VALUES (?, ?)", (fact.field, value_json))
                claim = fact.claim.as_dict()
                before = connection.total_changes
                connection.execute(
                    "INSERT OR IGNORE INTO profile_claims VALUES (?, ?, ?, ?)",
                    (fact.field, claim["claim_id"], canonical_json(claim), batch_id),
                )
                changes += connection.total_changes - before
            connection.execute("UPDATE ingestion_batches SET changes = ? WHERE batch_id = ?", (changes, batch_id))
            revision = _revision(connection)
            unresolved = connection.execute("SELECT 1 FROM profile_conflicts LIMIT 1").fetchone()
            status = "conflict_resolution_required" if unresolved else "profile_ready"
            connection.execute(
                "INSERT INTO profile_state VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET status=excluded.status, revision=excluded.revision",
                (status, revision),
            )
        result = _result(connection, batch_id)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def export_profile(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for row in connection.execute("SELECT field, value_json FROM profile_facts ORDER BY field"):
        claims = [
            json.loads(item[0]) for item in connection.execute(
                "SELECT claim_json FROM profile_claims WHERE field = ? ORDER BY claim_id", (row["field"],)
            )
        ]
        facts[row["field"]] = {"claims": claims, "value": json.loads(row["value_json"])}
    conflicts = [{
        "conflict_id": row["conflict_id"],
        "existing_value_hash": digest(json.loads(row["existing_value_json"])),
        "field": row["field"],
        "incoming_value_hash": digest(json.loads(row["incoming_value_json"])),
        "incoming_source_id": row["incoming_source_id"],
    } for row in connection.execute(
        "SELECT conflict_id, field, existing_value_json, incoming_value_json, incoming_source_id "
        "FROM profile_conflicts ORDER BY field, conflict_id"
    )]
    state = connection.execute("SELECT status, revision FROM profile_state WHERE singleton = 1").fetchone()
    payload = {
        "conflicts": conflicts,
        "facts": facts,
        "profile_revision": state["revision"] if state else _revision(connection),
        "profile_version": PROFILE_VERSION,
        "state": state["status"] if state else "profile_pending",
    }
    atomic_write_profile(path, payload)
    return payload
