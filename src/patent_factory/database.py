from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .paths import owner_only_file
from .profile import IncomingFact, PROFILE_VERSION, atomic_write_profile
from .provenance import canonical_json, digest

SCHEMA_VERSION = 5
BUSY_TIMEOUT_MS = 250

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS ingestion_batches (batch_id TEXT PRIMARY KEY, input_mode TEXT NOT NULL, input_digest TEXT NOT NULL, input_count INTEGER NOT NULL, outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'conflict')), changes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS profile_facts (field TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile_claims (field TEXT NOT NULL REFERENCES profile_facts(field), claim_id TEXT NOT NULL, claim_json TEXT NOT NULL, batch_id TEXT NOT NULL REFERENCES ingestion_batches(batch_id), PRIMARY KEY (field, claim_id));
CREATE TABLE IF NOT EXISTS profile_conflicts (conflict_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES ingestion_batches(batch_id), field TEXT NOT NULL, existing_value_json TEXT NOT NULL, incoming_value_json TEXT NOT NULL, incoming_source_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile_state (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), status TEXT NOT NULL, revision TEXT NOT NULL);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL, state_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_revisions (revision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, kind TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL, schema_version TEXT NOT NULL, created_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0,1)), UNIQUE(run_id,kind,content_hash));
CREATE INDEX IF NOT EXISTS artifact_revisions_run_hash ON artifact_revisions(run_id,content_hash);
CREATE TABLE IF NOT EXISTS current_artifacts (run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, kind TEXT NOT NULL, revision_id TEXT NOT NULL REFERENCES artifact_revisions(revision_id), PRIMARY KEY(run_id,kind));
CREATE TABLE IF NOT EXISTS artifact_dependencies (run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, upstream_revision_id TEXT NOT NULL REFERENCES artifact_revisions(revision_id), downstream_revision_id TEXT NOT NULL REFERENCES artifact_revisions(revision_id), PRIMARY KEY(upstream_revision_id,downstream_revision_id), CHECK(upstream_revision_id<>downstream_revision_id));
CREATE INDEX IF NOT EXISTS artifact_dependencies_upstream ON artifact_dependencies(run_id,upstream_revision_id);
CREATE TABLE IF NOT EXISTS transition_events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, actor TEXT NOT NULL, prior_state TEXT NOT NULL, next_state TEXT NOT NULL, reason TEXT NOT NULL, evidence_hashes_json TEXT NOT NULL, artifact_revision_id TEXT REFERENCES artifact_revisions(revision_id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency_records (run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, operation TEXT NOT NULL, idempotency_key TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES transition_events(event_id), artifact_revision_id TEXT REFERENCES artifact_revisions(revision_id), state_after TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,operation,idempotency_key));
CREATE TABLE IF NOT EXISTS gate_envelopes (gate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, kind TEXT NOT NULL, gate_state TEXT NOT NULL, suspended_state TEXT NOT NULL, suspended_operation TEXT NOT NULL, subject_revision_hash TEXT NOT NULL, approval_scope_json TEXT NOT NULL, approval_scope_hash TEXT NOT NULL, return_state TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','decided','superseded')));
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_gate_per_run ON gate_envelopes(run_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS gate_decisions (decision_id TEXT PRIMARY KEY, gate_id TEXT NOT NULL UNIQUE REFERENCES gate_envelopes(gate_id), run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, action TEXT NOT NULL, actor TEXT NOT NULL, subject_revision_hash TEXT NOT NULL, approval_scope_hash TEXT NOT NULL, suspended_operation TEXT NOT NULL, return_state TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0,1)), consumed_at TEXT);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS artifact_exports (export_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL UNIQUE REFERENCES artifact_revisions(revision_id) ON DELETE CASCADE, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, path TEXT NOT NULL UNIQUE, byte_hash TEXT NOT NULL, byte_size INTEGER NOT NULL CHECK(byte_size >= 0), created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS artifact_exports_run ON artifact_exports(run_id,revision_id);
"""

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS research_queries (
  query_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  request_fingerprint TEXT NOT NULL,
  envelope_json TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, request_fingerprint)
);
CREATE INDEX IF NOT EXISTS research_queries_run ON research_queries(run_id, created_at, query_id);
CREATE TABLE IF NOT EXISTS adapter_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  query_id TEXT NOT NULL REFERENCES research_queries(query_id) ON DELETE CASCADE,
  adapter TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('success','failure')),
  response_hash TEXT,
  failure_kind TEXT,
  failure_json TEXT,
  terms_note TEXT NOT NULL,
  coverage_json TEXT NOT NULL,
  next_cursor TEXT
);
CREATE INDEX IF NOT EXISTS adapter_events_query ON adapter_events(run_id, query_id, retrieved_at, event_id);
CREATE TABLE IF NOT EXISTS evidence_records (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_locator TEXT NOT NULL,
  original_identifier TEXT NOT NULL,
  title TEXT NOT NULL,
  canonical_url TEXT,
  content_hash TEXT NOT NULL,
  language TEXT NOT NULL,
  record_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, evidence_id),
  UNIQUE(run_id, source_locator, content_hash)
);
CREATE INDEX IF NOT EXISTS evidence_records_locator ON evidence_records(run_id, source_locator);
CREATE TABLE IF NOT EXISTS retrieval_observations (
  observation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  query_id TEXT NOT NULL REFERENCES research_queries(query_id) ON DELETE CASCADE,
  event_id TEXT NOT NULL REFERENCES adapter_events(event_id) ON DELETE CASCADE,
  evidence_id TEXT,
  retrieved_at TEXT NOT NULL,
  response_hash TEXT,
  access_status TEXT NOT NULL CHECK(access_status IN ('success','failure')),
  terms_note TEXT NOT NULL,
  FOREIGN KEY(run_id, evidence_id) REFERENCES evidence_records(run_id, evidence_id) ON DELETE CASCADE,
  UNIQUE(event_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS research_edges (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  query_id TEXT NOT NULL REFERENCES research_queries(query_id) ON DELETE CASCADE,
  observation_id TEXT NOT NULL REFERENCES retrieval_observations(observation_id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL,
  source_rank INTEGER NOT NULL CHECK(source_rank >= 1),
  PRIMARY KEY(query_id, observation_id, evidence_id),
  FOREIGN KEY(run_id, evidence_id) REFERENCES evidence_records(run_id, evidence_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS coverage_limitations (
  limitation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  query_id TEXT NOT NULL REFERENCES research_queries(query_id) ON DELETE CASCADE,
  event_id TEXT NOT NULL REFERENCES adapter_events(event_id) ON DELETE CASCADE,
  failure_kind TEXT NOT NULL,
  limitation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(event_id, failure_kind)
);
CREATE TABLE IF NOT EXISTS research_operations (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  query_id TEXT NOT NULL REFERENCES research_queries(query_id) ON DELETE CASCADE,
  event_id TEXT NOT NULL REFERENCES adapter_events(event_id) ON DELETE CASCADE,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, idempotency_key)
);
"""

class DatabaseCorruptError(RuntimeError):
    code = "database_corrupt"


class RunBusyError(RuntimeError):
    code = "run_busy"
    retryable = True

    def __init__(self) -> None:
        super().__init__("run_busy: retry the operation")


class InjectedFailure(RuntimeError):
    pass


FaultInjector = str | Callable[[str], None] | None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def inject_fault(injector: FaultInjector, boundary: str) -> None:
    if callable(injector):
        injector(boundary)
    elif injector == boundary:
        raise InjectedFailure(f"injected failure at {boundary}")


def _is_busy(error: sqlite3.OperationalError) -> bool:
    return "locked" in str(error).lower() or "busy" in str(error).lower()


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _migrate_v4(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(gate_decisions)")}
    if "used_at" not in columns:
        connection.execute("ALTER TABLE gate_decisions ADD COLUMN used_at TEXT")
    if "consumed_by_event_id" not in columns:
        connection.execute(
            "ALTER TABLE gate_decisions ADD COLUMN consumed_by_event_id TEXT REFERENCES transition_events(event_id)"
        )


def _migrate(connection: sqlite3.Connection, version: int, fault_at: FaultInjector) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        if version == 0:
            _execute_script(connection, SCHEMA_V1)
            connection.execute("PRAGMA user_version=1")
            inject_fault(fault_at, "migration_v1")
            version = 1
        if version == 1:
            _execute_script(connection, SCHEMA_V2)
            connection.execute("PRAGMA user_version=2")
            inject_fault(fault_at, "migration_v2")
            version = 2
        if version == 2:
            _execute_script(connection, SCHEMA_V3)
            connection.execute("PRAGMA user_version=3")
            inject_fault(fault_at, "migration_v3")
            version = 3
        if version == 3:
            _migrate_v4(connection)
            connection.execute("PRAGMA user_version=4")
            inject_fault(fault_at, "migration_v4")
            version = 4
        if version == 4:
            _execute_script(connection, SCHEMA_V5)
            connection.execute("PRAGMA user_version=5")
            inject_fault(fault_at, "migration_v5")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def connect_database(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS, fault_at: FaultInjector = None) -> sqlite3.Connection:
    if not 1 <= busy_timeout_ms <= 5_000:
        raise ValueError("busy_timeout_ms must be between 1 and 5000")
    connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < 0 or version > SCHEMA_VERSION:
        connection.close()
        raise ValueError(f"database: unsupported schema version {version}")
    try:
        _migrate(connection, version, fault_at)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DatabaseCorruptError("database_corrupt: integrity check failed")
    except BaseException:
        connection.close()
        raise
    owner_only_file(path)
    return connection


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        if _is_busy(error):
            raise RunBusyError() from None
        raise
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


@contextmanager
def consistent_snapshot(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN")
    try:
        connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


@dataclass(frozen=True)
class IngestionResult:
    batch_id: str
    changes: int
    conflicts: tuple[dict[str, str], ...]
    fact_count: int
    input_count: int
    profile_revision: str
    status: str


def _batch_payload(input_mode: str, facts: list[IncomingFact]) -> dict[str, Any]:
    return {"input_mode": input_mode, "facts": [{"field": fact.field, "value": fact.value, "claim": fact.claim.as_dict()} for fact in facts]}


def _revision(connection: sqlite3.Connection) -> str:
    facts = [tuple(row) for row in connection.execute("SELECT field,value_json FROM profile_facts ORDER BY field")]
    claims = [tuple(row) for row in connection.execute("SELECT field,claim_id,claim_json FROM profile_claims ORDER BY field,claim_id")]
    return "pr_" + digest({"facts": facts, "claims": claims})[:16]


def _conflicts_for_batch(connection: sqlite3.Connection, batch_id: str) -> tuple[dict[str, str], ...]:
    rows = connection.execute("SELECT conflict_id,field,existing_value_json,incoming_value_json,incoming_source_id FROM profile_conflicts WHERE batch_id=? ORDER BY field,conflict_id", (batch_id,))
    return tuple({"conflict_id": row["conflict_id"], "existing_value_hash": digest(json.loads(row["existing_value_json"])), "field": row["field"], "incoming_value_hash": digest(json.loads(row["incoming_value_json"])), "incoming_source_id": row["incoming_source_id"]} for row in rows)


def _result(connection: sqlite3.Connection, batch_id: str) -> IngestionResult:
    batch = connection.execute("SELECT * FROM ingestion_batches WHERE batch_id=?", (batch_id,)).fetchone()
    state = connection.execute("SELECT status FROM profile_state WHERE singleton=1").fetchone()[0]
    return IngestionResult(batch_id, batch["changes"], _conflicts_for_batch(connection, batch_id), connection.execute("SELECT count(*) FROM profile_facts").fetchone()[0], batch["input_count"], _revision(connection), state)


def ingest(connection: sqlite3.Connection, input_mode: str, incoming: Iterable[IncomingFact]) -> IngestionResult:
    facts = [fact.normalized() for fact in incoming]
    input_digest = digest(_batch_payload(input_mode, facts))
    batch_id = "ib_" + input_digest[:16]
    with immediate_transaction(connection):
        if connection.execute("SELECT 1 FROM ingestion_batches WHERE batch_id=?", (batch_id,)).fetchone():
            return replace(_result(connection, batch_id), changes=0)
        conflicts: list[tuple[str, str, str, str]] = []
        pending: dict[str, str] = {}
        for fact in facts:
            incoming_json = canonical_json(fact.value)
            row = connection.execute("SELECT value_json FROM profile_facts WHERE field=?", (fact.field,)).fetchone()
            existing_json = pending.get(fact.field, row[0] if row else incoming_json)
            if existing_json != incoming_json:
                conflicts.append((fact.field, existing_json, incoming_json, fact.claim.source_id or ""))
            pending[fact.field] = existing_json
        if conflicts:
            connection.execute("INSERT INTO ingestion_batches VALUES(?,?,?,?, 'conflict',0)", (batch_id,input_mode,input_digest,len(facts)))
            for field, existing_json, incoming_json, source_id in conflicts:
                conflict_id = "cf_" + digest({"batch_id":batch_id,"field":field,"existing":existing_json,"incoming":incoming_json,"source_id":source_id})[:16]
                connection.execute("INSERT OR IGNORE INTO profile_conflicts VALUES(?,?,?,?,?,?)", (conflict_id,batch_id,field,existing_json,incoming_json,source_id))
            revision = _revision(connection)
            connection.execute("INSERT INTO profile_state VALUES(1,'conflict_resolution_required',?) ON CONFLICT(singleton) DO UPDATE SET status=excluded.status,revision=excluded.revision", (revision,))
        else:
            changes = 0
            connection.execute("INSERT INTO ingestion_batches VALUES(?,?,?,?, 'applied',0)", (batch_id,input_mode,input_digest,len(facts)))
            for fact in facts:
                connection.execute("INSERT OR IGNORE INTO profile_facts VALUES(?,?)", (fact.field,canonical_json(fact.value)))
                claim = fact.claim.as_dict()
                before = connection.total_changes
                connection.execute("INSERT OR IGNORE INTO profile_claims VALUES(?,?,?,?)", (fact.field,claim["claim_id"],canonical_json(claim),batch_id))
                changes += connection.total_changes-before
            connection.execute("UPDATE ingestion_batches SET changes=? WHERE batch_id=?", (changes,batch_id))
            revision = _revision(connection)
            status = "conflict_resolution_required" if connection.execute("SELECT 1 FROM profile_conflicts LIMIT 1").fetchone() else "profile_ready"
            connection.execute("INSERT INTO profile_state VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET status=excluded.status,revision=excluded.revision", (status,revision))
        return _result(connection,batch_id)


def export_profile(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for row in connection.execute("SELECT field,value_json FROM profile_facts ORDER BY field"):
        claims = [json.loads(item[0]) for item in connection.execute("SELECT claim_json FROM profile_claims WHERE field=? ORDER BY claim_id", (row["field"],))]
        facts[row["field"]] = {"claims":claims,"value":json.loads(row["value_json"])}
    conflicts = [{"conflict_id":row["conflict_id"],"existing_value_hash":digest(json.loads(row["existing_value_json"])),"field":row["field"],"incoming_value_hash":digest(json.loads(row["incoming_value_json"])),"incoming_source_id":row["incoming_source_id"]} for row in connection.execute("SELECT conflict_id,field,existing_value_json,incoming_value_json,incoming_source_id FROM profile_conflicts ORDER BY field,conflict_id")]
    state = connection.execute("SELECT status,revision FROM profile_state WHERE singleton=1").fetchone()
    payload = {"conflicts":conflicts,"facts":facts,"profile_revision":state["revision"] if state else _revision(connection),"profile_version":PROFILE_VERSION,"state":state["status"] if state else "profile_pending"}
    atomic_write_profile(path,payload)
    return payload
