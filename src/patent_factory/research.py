from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .adapters.base import SearchAdapter
from .database import FaultInjector, immediate_transaction, inject_fault, utc_now
from .models import AdapterResult, GateEnvelope, GateKind, QueryEnvelope, RunState
from .provenance import canonical_json, digest, evidence_revision_id, normalize
from .privacy import assert_canaries_absent, credential_canaries
from .state import StateStore, workspace_export_directories


@dataclass(frozen=True)
class ResearchBudget:
    max_depth: int = 1
    max_calls: int = 12
    per_adapter_results: int = 30
    retry_budget: int = 0
    page_cap: int = 5
    byte_budget: int = 1_000_000

    def validate(self) -> None:
        if not 0 <= self.max_depth <= 3:
            raise ValueError("research_budget.max_depth: must be between 0 and 3")
        if not 1 <= self.max_calls <= 100:
            raise ValueError("research_budget.max_calls: must be between 1 and 100")
        if not 1 <= self.per_adapter_results <= 500:
            raise ValueError("research_budget.per_adapter_results: must be between 1 and 500")
        if not 0 <= self.retry_budget <= 3:
            raise ValueError("research_budget.retry_budget: must be between 0 and 3")
        if not 1 <= self.page_cap <= 100 or not 1 <= self.byte_budget <= 10_000_000:
            raise ValueError("research_budget: page or byte budget is invalid")


@dataclass(frozen=True)
class PlannedQuery:
    envelope: QueryEnvelope
    origin_query: str
    term: str
    term_kind: str
    depth: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "origin_query": normalize(self.origin_query),
            "term": normalize(self.term),
            "term_kind": self.term_kind,
        }


@dataclass(frozen=True)
class ResearchExecution:
    run_id: str
    query_id: str
    event_id: str
    observation_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    status: str
    failure_kind: str | None
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "evidence_ids": list(self.evidence_ids),
            "failure_kind": self.failure_kind, "observation_ids": list(self.observation_ids),
            "query_id": self.query_id, "run_id": self.run_id, "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, replayed: bool = False) -> "ResearchExecution":
        return cls(
            run_id=value["run_id"], query_id=value["query_id"], event_id=value["event_id"],
            observation_ids=tuple(value["observation_ids"]), evidence_ids=tuple(value["evidence_ids"]),
            status=value["status"], failure_kind=value.get("failure_kind"), replayed=replayed,
        )


@dataclass(frozen=True)
class ResearchRun:
    run_id: str
    prior_state: str
    next_state: str
    execution: ResearchExecution
    bundle: Mapping[str, Any]
    artifact_revision_id: str
    transition_event_ids: tuple[str, ...]
    replayed: bool

    def _incomplete_reason(self) -> str | None:
        """Say WHY a run is incomplete, so `incomplete` is not a dead end.

        A run whose adapter succeeded but contributed no NEW evidence is
        `incomplete` purely because every record deduplicated against evidence
        already in the run. Without this, re-importing after an
        excessive-similarity `replace` reroute returns `incomplete`/exit 4 with a
        non-zero `evidence_count` and no indication that the fix is "supply a
        reference that is not already here".
        """

        if self.next_state == RunState.RESEARCH_COMPLETE.value:
            return None
        if self.execution.status == "success" and not self.execution.evidence_ids:
            return (
                "no_new_evidence: the adapter succeeded but every record already exists in "
                "this run, so nothing was added. Supply at least one reference not already "
                "retrieved, or proceed with the evidence you have."
            )
        return f"adapter_status_{self.execution.status}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_status": {
                "failure_kind": self.execution.failure_kind,
                "status": self.execution.status,
            },
            "artifact_ids": [self.artifact_revision_id],
            "command": "research",
            "evidence_count": len(self.execution.evidence_ids),
            "incomplete_reason": self._incomplete_reason(),
            "manifest": self.bundle["manifest"],
            "next_state": self.next_state,
            "prior_state": self.prior_state,
            "query_id": self.execution.query_id,
            "replayed": self.replayed,
            "run_id": self.run_id,
            "status": "complete" if self.next_state == RunState.RESEARCH_COMPLETE.value else "incomplete",
            "transition_event_ids": list(self.transition_event_ids),
        }


class CredentialRequiredError(RuntimeError):
    """A credential-bound research request was transactionally suspended."""

    def __init__(self, gate: GateEnvelope) -> None:
        super().__init__("credential_required: configure and approve the exact research request")
        self.gate = gate


class LiveResearchReentryRefusedError(RuntimeError):
    """A live (credential-requiring) research verb was refused on a stale re_research re-entry."""

    code = "live_research_reentry_refused_issue_48"

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"run {run_id}: live research verbs are refused on a research_running state "
            "entered via a re_research checkpoint resolution — this second pass is "
            "offline-only (fixture/normalize-web/manual), deferred to issue #48"
        )
        self.run_id = run_id


def refuse_stale_re_research_reentry(connection: sqlite3.Connection, run_id: str) -> None:
    """Refuse a live research verb iff the run's current research_running state was
    entered via a `re_research` checkpoint resolution with no research since (finding #12).

    `SETUP.md`, `research/SKILL.md`, and `checkpoint.md` all describe the
    second in-run research pass as offline-only (issue #48 defers live
    support), but nothing enforced it: `run_research`'s offline path and the
    live kipris/serpapi paths accept `RunState.RESEARCH_RUNNING` identically.

    Discriminator (RC5), anchored to concrete persisted events rather than a
    fragile clock comparison alone: refuse iff the latest `gate_decisions`
    row for this run with `action='re_research'` exists AND no
    `transition_events` row with `next_state='research_complete'` has a
    LATER `created_at` than that resolution's (both are written from the
    same `now` value inside `publish_gate_resolution`'s one transaction, so
    the anchor and its own transition_event always agree). This keeps a
    legitimate first-pass retry allowed (no re_research resolution exists at
    all), refuses the second pass immediately after `re_research`, and
    allows a later cycle-back (re_research -> offline publish -> a
    subsequent COVERAGE-expand re-enters research_running by a different
    route) since a research_complete transition now exists after the anchor.
    """
    anchor = connection.execute(
        "SELECT created_at FROM gate_decisions WHERE run_id=? AND action='re_research' "
        "ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if anchor is None:
        return
    published_since = connection.execute(
        "SELECT 1 FROM transition_events WHERE run_id=? AND next_state=? AND created_at>? LIMIT 1",
        (run_id, RunState.RESEARCH_COMPLETE.value, anchor["created_at"]),
    ).fetchone()
    if published_since is None:
        raise LiveResearchReentryRefusedError(run_id)


@dataclass(frozen=True)
class ResearchBatchRun:
    run_id: str
    prior_state: str
    next_state: str
    executions: tuple[ResearchExecution, ...]
    bundle: Mapping[str, Any]
    artifact_revision_id: str
    transition_event_ids: tuple[str, ...]
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        succeeded = sum(
            1 for item in self.executions if item.status == "success" and item.evidence_ids
        )
        failure_kinds = sorted({
            item.failure_kind for item in self.executions if item.failure_kind
        })
        return {
            "adapter_status": {
                "failure_kinds": failure_kinds,
                "status": "success" if succeeded else "failure",
            },
            "artifact_ids": [self.artifact_revision_id],
            "command": "research",
            "evidence_count": len({
                evidence_id for item in self.executions for evidence_id in item.evidence_ids
            }),
            "manifest": self.bundle["manifest"],
            "next_state": self.next_state,
            "planned_count": len(self.executions),
            "prior_state": self.prior_state,
            "queries": [
                {
                    "evidence_count": len(item.evidence_ids),
                    "failure_kind": item.failure_kind,
                    "query_id": item.query_id,
                    "status": item.status,
                }
                for item in self.executions
            ],
            "replayed": self.replayed,
            "run_id": self.run_id,
            "status": "complete" if self.next_state == RunState.RESEARCH_COMPLETE.value else "incomplete",
            "succeeded_count": succeeded,
            "transition_event_ids": list(self.transition_event_ids),
        }


def _terms(values: Iterable[str]) -> tuple[str, ...]:
    normalized = {normalize(value) for value in values if normalize(value)}
    return tuple(sorted(normalized, key=lambda value: (value.casefold(), value)))


def plan_keyword_queries(
    *,
    run_id: str,
    origin_query: str,
    korean_synonyms: Sequence[str] = (),
    english_synonyms: Sequence[str] = (),
    discovered_terms: Sequence[str] = (),
    classifications: Sequence[str] = (),
    applicants: Sequence[str] = (),
    inventors: Sequence[str] = (),
    budget: ResearchBudget = ResearchBudget(),
    adapter: str = "kipris",
    adapter_version: str = "plus-xml-v1",
    allowed_host: str = "plus.kipris.or.kr",
) -> tuple[PlannedQuery, ...]:
    budget.validate()
    origin = normalize(origin_query)
    if not origin:
        raise ValueError("origin_query: required")
    groups = (
        ("origin", 0, (origin,)),
        ("synonym_ko", 1, _terms(korean_synonyms)),
        ("synonym_en", 1, _terms(english_synonyms)),
        ("discovered", 2, _terms(discovered_terms)),
        ("classification", 1, _terms(classifications)),
        ("applicant", 1, _terms(applicants)),
        ("inventor", 1, _terms(inventors)),
    )
    planned: list[PlannedQuery] = []
    seen: set[str] = set()
    for kind, depth, values in groups:
        if depth > budget.max_depth:
            continue
        for term in values:
            identity = term.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            envelope = QueryEnvelope(
                run_id=run_id, adapter=adapter, adapter_version=adapter_version,
                capability="word_search", allowed_scheme="https", allowed_host=allowed_host,
                deadline_seconds=10, page=1, page_cap=budget.page_cap,
                result_budget=budget.per_adapter_results, byte_budget=budget.byte_budget,
                retry_budget=budget.retry_budget, retry_ownership="research_runner",
                query_projection={"word": term, "year": 0, "patent": True, "utility": True},
            )
            planned.append(PlannedQuery(envelope, origin, term, kind, depth))
            if len(planned) >= budget.max_calls:
                return tuple(planned)
    return tuple(planned)


def plan_bibliography_queries(
    *,
    run_id: str,
    application_numbers: Sequence[str],
    budget: ResearchBudget = ResearchBudget(),
    adapter: str = "kipris",
    adapter_version: str = "plus-xml-v1",
    allowed_host: str = "plus.kipris.or.kr",
) -> tuple[PlannedQuery, ...]:
    """Plan one bibliography-summary lookup per application number.

    Kept separate from `plan_keyword_queries` rather than parameterized by
    capability: the two capabilities take different projections entirely
    (`{"word": ...}` versus `{"application_number": ...}`), so a shared planner
    would have to branch on capability at every step and could emit a projection
    the adapter rejects.
    """

    budget.validate()
    numbers = tuple(dict.fromkeys(
        normalized for value in application_numbers if (normalized := normalize(value))
    ))
    if not numbers:
        raise ValueError("application_numbers: at least one value required")
    planned: list[PlannedQuery] = []
    for number in numbers:
        envelope = QueryEnvelope(
            run_id=run_id, adapter=adapter, adapter_version=adapter_version,
            capability="bibliography_summary", allowed_scheme="https", allowed_host=allowed_host,
            deadline_seconds=10, page=1, page_cap=budget.page_cap,
            result_budget=budget.per_adapter_results, byte_budget=budget.byte_budget,
            retry_budget=budget.retry_budget, retry_ownership="research_runner",
            query_projection={"application_number": number},
        )
        planned.append(PlannedQuery(envelope, number, number, "bibliography", 0))
        if len(planned) >= budget.max_calls:
            break
    return tuple(planned)


class ResearchStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _prior(self, run_id: str, idempotency_key: str) -> tuple[str, ResearchExecution] | None:
        row = self.connection.execute(
            "SELECT query_id,result_json FROM research_operations WHERE run_id=? AND idempotency_key=?",
            (run_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return row["query_id"], ResearchExecution.from_dict(json.loads(row["result_json"]), replayed=True)

    def execute(
        self,
        adapter: SearchAdapter,
        query: PlannedQuery | QueryEnvelope,
        *,
        idempotency_key: str,
        retrieved_at: str | None = None,
        fault_at: FaultInjector = None,
    ) -> ResearchExecution:
        envelope = query.envelope if isinstance(query, PlannedQuery) else query
        prepare = getattr(adapter, "prepare_envelope", None)
        if callable(prepare):
            envelope = prepare(envelope)
            if not isinstance(envelope, QueryEnvelope):
                raise TypeError("adapter prepare_envelope must return QueryEnvelope")
            if isinstance(query, PlannedQuery):
                query = replace(query, envelope=envelope)
        plan = query.as_dict() if isinstance(query, PlannedQuery) else {}
        envelope.validate()
        if not normalize(idempotency_key):
            raise ValueError("idempotency_key: required")
        query_id = "qu_" + digest({"run_id": envelope.run_id, "fingerprint": envelope.request_fingerprint})[:20]
        prior = self._prior(envelope.run_id, idempotency_key)
        if prior:
            if prior[0] != query_id:
                raise ValueError("idempotency_key reused for a different query")
            return prior[1]

        result = adapter.search(envelope)
        result.validate()
        canaries = credential_canaries()
        assert_canaries_absent(
            {
                "coverage": dict(result.coverage), "failure": result.failure.as_dict() if result.failure else None,
                "next_cursor": result.next_cursor, "rate_limit": dict(result.rate_limit) if result.rate_limit else None,
                "records": [record.as_dict() for record in result.records], "terms_note": result.terms_note,
            },
            canaries, boundary="adapter_response",
        )
        at = retrieved_at or utc_now()
        event_id = "ae_" + digest({
            "run_id": envelope.run_id, "query_id": query_id, "idempotency_key": idempotency_key,
            "retrieved_at": at,
        })[:20]
        status = "success" if result.successful else "failure"
        failure_kind = result.failure.kind.value if result.failure else None
        observation_ids: list[str] = []
        evidence_ids: list[str] = []

        with immediate_transaction(self.connection):
            concurrent = self._prior(envelope.run_id, idempotency_key)
            if concurrent:
                if concurrent[0] != query_id:
                    raise ValueError("idempotency_key reused for a different query")
                return concurrent[1]
            self.connection.execute(
                "INSERT INTO research_queries(query_id,run_id,request_fingerprint,envelope_json,plan_json,created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,request_fingerprint) DO NOTHING",
                (query_id, envelope.run_id, envelope.request_fingerprint, canonical_json(envelope.request_body()),
                 canonical_json(plan), at),
            )
            inject_fault(fault_at, "after_research_query")
            self.connection.execute(
                "INSERT INTO adapter_events(event_id,run_id,query_id,adapter,adapter_version,retrieved_at,status,"
                "response_hash,failure_kind,failure_json,terms_note,coverage_json,next_cursor,rate_limit_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, envelope.run_id, query_id, envelope.adapter, envelope.adapter_version, at, status,
                 result.response_hash, failure_kind, canonical_json(result.failure.as_dict()) if result.failure else None,
                 result.terms_note, canonical_json(dict(result.coverage)), result.next_cursor,
                 canonical_json(dict(result.rate_limit)) if result.rate_limit else None),
            )
            inject_fault(fault_at, "after_adapter_event")

            if result.failure:
                observation_id = "ob_" + digest({"event_id": event_id, "failure": failure_kind})[:20]
                self.connection.execute(
                    "INSERT INTO retrieval_observations(observation_id,run_id,query_id,event_id,evidence_id,retrieved_at,"
                    "response_hash,access_status,terms_note) VALUES(?,?,?,?,NULL,?,?,?,?)",
                    (observation_id, envelope.run_id, query_id, event_id, at, result.response_hash, "failure", result.terms_note),
                )
                observation_ids.append(observation_id)
                limitation_id = "li_" + digest({"event_id": event_id, "failure": failure_kind})[:20]
                self.connection.execute(
                    "INSERT INTO coverage_limitations VALUES(?,?,?,?,?,?,?)",
                    (limitation_id, envelope.run_id, query_id, event_id, failure_kind,
                     normalize(result.failure.message), at),
                )
                inject_fault(fault_at, "after_coverage_limitation")
            else:
                seen_evidence: set[str] = set()
                for rank, record in enumerate(result.records, start=1):
                    record_data = record.as_dict()
                    evidence_id = evidence_revision_id(record.source_locator, record.content_hash)
                    if evidence_id in seen_evidence:
                        continue
                    seen_evidence.add(evidence_id)
                    self.connection.execute(
                        "INSERT INTO evidence_records(run_id,evidence_id,source_type,source_locator,original_identifier,title,"
                        "canonical_url,content_hash,language,record_json,created_at,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(run_id,evidence_id) DO NOTHING",
                        (envelope.run_id, evidence_id, record.source_type, record.source_locator,
                         record.original_identifier, record.title, record.canonical_url, record.content_hash,
                         record.language, canonical_json(record_data), at, record.provenance),
                    )
                    evidence_ids.append(evidence_id)
                    inject_fault(fault_at, "after_evidence_record")
                    observation_id = "ob_" + digest({"event_id": event_id, "evidence_id": evidence_id})[:20]
                    self.connection.execute(
                        "INSERT INTO retrieval_observations VALUES(?,?,?,?,?,?,?,?,?)",
                        (observation_id, envelope.run_id, query_id, event_id, evidence_id, at,
                         result.response_hash, "success", result.terms_note),
                    )
                    observation_ids.append(observation_id)
                    self.connection.execute(
                        "INSERT INTO research_edges VALUES(?,?,?,?,?)",
                        (envelope.run_id, query_id, observation_id, evidence_id, rank),
                    )
                    inject_fault(fault_at, "after_research_edge")
                if not result.records:
                    observation_id = "ob_" + digest({"event_id": event_id, "empty": True})[:20]
                    self.connection.execute(
                        "INSERT INTO retrieval_observations VALUES(?,?,?,?,NULL,?,?,?,?)",
                        (observation_id, envelope.run_id, query_id, event_id, at,
                         result.response_hash, "success", result.terms_note),
                    )
                    observation_ids.append(observation_id)
                    inject_fault(fault_at, "after_empty_observation")

            execution = ResearchExecution(
                envelope.run_id, query_id, event_id, tuple(observation_ids), tuple(evidence_ids),
                status, failure_kind,
            )
            self.connection.execute(
                "INSERT INTO research_operations VALUES(?,?,?,?,?,?)",
                (envelope.run_id, idempotency_key, query_id, event_id, canonical_json(execution.as_dict()), at),
            )
            inject_fault(fault_at, "after_research_operation")
        return execution

    def manifest(self, run_id: str) -> dict[str, Any]:
        """Build the research stage's own manifest — never the whole run.

        `audit.py` retrieves its similarity corpus through this same store
        (`store.execute`, `audit.py:306-308`), tagging every query it plans with
        a `term_kind` of `audit_{language}`. Those rows share this run_id and
        land in the same tables, so an unfiltered read here would hand the
        audit's own search terms and evidence back to the research stage on any
        route that calls `manifest()` again after an audit has run (the
        COVERAGE-expand re-entry). `term_kind` is only recorded on
        `research_queries.plan_json` (`PlannedQuery.as_dict()`), so every other
        table is scoped by joining back to the research-stage query_id set;
        `evidence_records` has no query_id at all, so it is scoped through
        `research_edges` — an evidence row is kept if ANY of its edges comes
        from a research-stage query, since the same content-addressed record
        can legitimately surface from both stages.
        """

        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in self.connection.execute(sql, (run_id,))]

        all_queries = rows("SELECT * FROM research_queries WHERE run_id=? ORDER BY created_at,query_id")
        research_query_ids = {
            row["query_id"] for row in all_queries
            if not str(json.loads(row["plan_json"]).get("term_kind", "")).startswith("audit_")
        }

        def scoped(sql: str) -> list[dict[str, Any]]:
            return [row for row in rows(sql) if row["query_id"] in research_query_ids]

        edges = scoped("SELECT * FROM research_edges WHERE run_id=? ORDER BY query_id,source_rank,evidence_id")
        research_evidence_ids = {row["evidence_id"] for row in edges}

        return {
            "adapter_events": scoped("SELECT * FROM adapter_events WHERE run_id=? ORDER BY retrieved_at,event_id"),
            "coverage_limitations": scoped("SELECT * FROM coverage_limitations WHERE run_id=? ORDER BY created_at,limitation_id"),
            "edges": edges,
            "evidence": [
                row for row in rows("SELECT * FROM evidence_records WHERE run_id=? ORDER BY evidence_id")
                if row["evidence_id"] in research_evidence_ids
            ],
            "observations": scoped("SELECT * FROM retrieval_observations WHERE run_id=? ORDER BY retrieved_at,observation_id"),
            "queries": [row for row in all_queries if row["query_id"] in research_query_ids],
            "run_id": run_id,
        }


def research_bundle(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Build the single deterministic payload registered by StateStore publication."""

    return {
        "adapter_events": manifest["adapter_events"],
        "coverage_limitations": manifest["coverage_limitations"],
        "edges": manifest["edges"],
        "evidence": manifest["evidence"],
        "observations": manifest["observations"],
        "queries": manifest["queries"],
        "run_id": manifest["run_id"],
        "version": "research-bundle-v1",
    }


def _private_export_directory(run_root: Path, *, create: bool) -> tuple[Path, Path]:
    root = Path(run_root).absolute()
    if not root.is_dir() or stat.S_ISLNK(root.lstat().st_mode):
        raise ValueError("research_export: safe run directory required")
    exports = root / "research-exports"
    if exports.exists() and (stat.S_ISLNK(exports.lstat().st_mode) or not exports.is_dir()):
        raise ValueError("research_export: unsafe export directory")
    if create:
        exports.mkdir(mode=0o700, exist_ok=True)
        try:
            os.chmod(exports, 0o700, follow_symlinks=False)
        except OSError:
            pass
    return root, exports


def _credential_scope(
    envelope: QueryEnvelope,
    *,
    auth_attempt: str,
    credential_name: str,
) -> dict[str, Any]:
    return {
        "adapter": normalize(envelope.adapter),
        "adapter_version": normalize(envelope.adapter_version),
        "allowed_host": normalize(envelope.allowed_host).casefold(),
        "auth_attempt": auth_attempt,
        "capability": normalize(envelope.capability),
        "credential_name": credential_name,
        "request_fingerprint": envelope.request_fingerprint,
    }


def _batch_credential_scope(
    envelopes: Sequence[QueryEnvelope],
    *,
    auth_attempt: str,
    credential_name: str,
) -> dict[str, Any]:
    first = envelopes[0]
    return {
        "adapter": normalize(first.adapter),
        "adapter_version": normalize(first.adapter_version),
        "allowed_host": normalize(first.allowed_host).casefold(),
        "auth_attempt": auth_attempt,
        "capability": normalize(first.capability),
        "credential_name": credential_name,
        "query_count": len(envelopes),
        "request_fingerprint": digest({
            "fingerprints": [envelope.request_fingerprint for envelope in envelopes],
        }),
    }


def _verify_and_consume_credential_decision(
    connection: sqlite3.Connection,
    state: StateStore,
    *,
    run_id: str,
    credential_decision_id: str,
    credential_operation: str,
    subject_revision_hash: str,
    idempotency_key: str,
) -> None:
    """Bind a user decision to the exact suspended request, consuming it once."""

    row = connection.execute(
        "SELECT ge.approval_scope_json,gd.stale,gd.subject_revision_hash,"
        "gd.suspended_operation,gd.used_at,gd.consumed_by_event_id FROM gate_decisions gd "
        "JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
        "WHERE gd.decision_id=? AND gd.run_id=?",
        (credential_decision_id, run_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("credential decision is unavailable")
    approval_scope = json.loads(row["approval_scope_json"])
    if (
        row["stale"]
        or row["subject_revision_hash"] != subject_revision_hash
        or row["suspended_operation"] != credential_operation
    ):
        raise RuntimeError("credential decision does not match the current request")
    if row["used_at"]:
        replay = connection.execute(
            "SELECT event_id FROM idempotency_records "
            "WHERE run_id=? AND operation=? AND idempotency_key=?",
            (run_id, credential_operation, idempotency_key),
        ).fetchone()
        if replay is None or replay["event_id"] != row["consumed_by_event_id"]:
            raise RuntimeError("credential decision was used by a different operation")
    else:
        state.consume_decision(
            credential_decision_id,
            suspended_operation=credential_operation,
            subject_revision_hash=subject_revision_hash,
            approval_scope=approval_scope,
        )


def run_research(
    connection: sqlite3.Connection,
    *,
    run_root: Path,
    run_id: str,
    adapter: SearchAdapter,
    query: PlannedQuery | QueryEnvelope,
    idempotency_key: str,
    retrieved_at: str | None = None,
    credential_decision_id: str | None = None,
    fault_at: FaultInjector = None,
) -> ResearchRun:
    """Execute one bounded research operation through the authoritative state machine."""

    envelope = query.envelope if isinstance(query, PlannedQuery) else query
    prepare = getattr(adapter, "prepare_envelope", None)
    if callable(prepare):
        envelope = prepare(envelope)
        if not isinstance(envelope, QueryEnvelope):
            raise TypeError("adapter prepare_envelope must return QueryEnvelope")
        if isinstance(query, PlannedQuery):
            query = replace(query, envelope=envelope)
        else:
            query = envelope
    envelope.validate()
    if envelope.run_id != normalize(run_id):
        raise ValueError("research run_id does not match the query envelope")
    root, exports = _private_export_directory(run_root, create=False)
    own = (exports,) if exports.exists() else ()
    state = StateStore(connection, export_directories=workspace_export_directories(connection, root, own))
    prior = state.snapshot(run_id)
    if prior.state is RunState.CREDENTIAL_REQUIRED:
        raise RuntimeError("credential_required: a current decision must resume the suspended request")

    credential_operation = f"research.execute:{idempotency_key}"
    requires_credential = bool(getattr(adapter, "requires_credential", False))
    credential_name = normalize(getattr(adapter, "credential_name", ""))
    if requires_credential and not credential_name:
        raise ValueError("credential-requiring adapter must declare its credential name")
    request_revision = None
    if requires_credential:
        if prior.state is RunState.RESEARCH_RUNNING:
            # Phase-4 validation (guard symmetry): this single-query entry
            # point is generic over ANY credential-requiring adapter, not
            # only the CLI's own kipris/serpapi callers — the CLI-level
            # SerpAPI preflight is one caller, not the only one. Guarding
            # here too makes `run_research` self-protecting regardless of
            # caller, matching `run_research_batch`'s identical guard.
            refuse_stale_re_research_reentry(connection, run_id)
        if prior.state not in {RunState.RESEARCH_READY, RunState.RESEARCH_RUNNING}:
            state.transition(
                run_id, RunState.RESEARCH_RUNNING, actor="research-cli", reason="state check",
                operation="research.start", idempotency_key=idempotency_key,
            )
        request_revision = state.add_revision(
            run_id,
            "research_request",
            {
                "plan": query.as_dict() if isinstance(query, PlannedQuery) else {},
                "request": envelope.request_body(),
            },
            schema_version="research-request-v1",
        )
        if credential_decision_id:
            _verify_and_consume_credential_decision(
                connection,
                state,
                run_id=run_id,
                credential_decision_id=credential_decision_id,
                credential_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                idempotency_key=idempotency_key,
            )
        if not bool(getattr(adapter, "credential_present", False)):
            scope = _credential_scope(
                envelope,
                auth_attempt=credential_decision_id or "preflight",
                credential_name=credential_name,
            )
            gate = state.suspend_gate(
                run_id,
                GateKind.CREDENTIAL,
                suspended_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                approval_scope=scope,
                return_state=prior.state,
                actor="research-cli",
                reason="required adapter credential is unavailable",
            )
            raise CredentialRequiredError(gate)

    transition_event_ids: list[str] = []
    if prior.state is not RunState.RESEARCH_RUNNING:
        started = state.transition(
            run_id,
            RunState.RESEARCH_RUNNING,
            actor="research-cli",
            reason="bounded research started",
            operation="research.start",
            idempotency_key=idempotency_key,
        )
        transition_event_ids.append(started.event_id)
    else:
        started = None
    execution_key = (
        f"{idempotency_key}:credential:{credential_decision_id}"
        if credential_decision_id else idempotency_key
    )
    execution = ResearchStore(connection).execute(
        adapter,
        query,
        idempotency_key=execution_key,
        retrieved_at=retrieved_at,
    )
    if requires_credential and execution.failure_kind == "auth":
        if request_revision is None:
            raise RuntimeError("credential adapter has no request revision")
        scope = _credential_scope(
            envelope,
            auth_attempt=credential_decision_id or "remote_auth",
            credential_name=credential_name,
        )
        gate = state.suspend_gate(
            run_id,
            GateKind.CREDENTIAL,
            suspended_operation=credential_operation,
            subject_revision_hash=request_revision.content_hash,
            approval_scope=scope,
            return_state=RunState.RESEARCH_RUNNING,
            actor="research-cli",
            reason="adapter rejected the configured credential",
        )
        raise CredentialRequiredError(gate)
    manifest = ResearchStore(connection).manifest(run_id)
    payload = research_bundle(manifest)
    target = (
        RunState.RESEARCH_COMPLETE
        if execution.status == "success" and execution.evidence_ids
        else RunState.RESEARCH_INCOMPLETE
    )
    _root, exports = _private_export_directory(root, create=True)
    state = StateStore(connection, export_directories=workspace_export_directories(connection, root, (exports,)))
    final_operation = credential_operation if credential_decision_id else "research.finish"
    finished, exported = state.publish_transition(
        run_id,
        target,
        actor="research-cli",
        reason="bounded research persisted",
        operation=final_operation,
        idempotency_key=idempotency_key,
        evidence_hashes=execution.evidence_ids,
        artifact_kind="research_bundle",
        artifact_content=payload,
        artifact_schema_version="research-bundle-v1",
        export_directory=exports,
        dependencies=(request_revision.revision_id,) if request_revision else (),
        consumed_decision_id=credential_decision_id,
        fault_at=fault_at,
    )
    if finished.artifact is None:
        raise RuntimeError("research finish transition did not produce its bundle revision")
    transition_event_ids.append(finished.event_id)
    bundle = {
        **payload,
        "manifest": {
            "artifact_id": exported.artifact_id,
            "byte_hash": exported.content_hash,
            "byte_size": exported.size,
            "path": Path(exported.path).relative_to(root).as_posix(),
        },
    }
    return ResearchRun(
        run_id,
        prior.state.value,
        finished.snapshot.state.value,
        execution,
        bundle,
        finished.artifact.revision_id,
        tuple(transition_event_ids),
        bool(started and started.replayed and execution.replayed and finished.replayed),
    )


def run_research_batch(
    connection: sqlite3.Connection,
    *,
    run_root: Path,
    run_id: str,
    adapter: SearchAdapter,
    queries: Sequence[PlannedQuery],
    idempotency_key: str,
    retrieved_at: str | None = None,
    credential_decision_id: str | None = None,
    fault_at: FaultInjector = None,
) -> ResearchBatchRun:
    """Execute a bounded batch of planned queries in one research session.

    Mirrors run_research's authoritative handling, but performs every planned
    query between the single start transition and the single finish
    publication — the same store pattern the audit retrieval loop uses. A
    non-auth source failure is recorded as an adapter event and coverage
    limitation and the batch continues; an auth failure suspends the exact
    batch behind a credential gate.
    """

    if not queries:
        raise ValueError("research batch requires at least one planned query")
    if len(queries) > 100:
        raise ValueError("research batch exceeds the maximum of 100 planned queries")
    if not normalize(idempotency_key):
        raise ValueError("idempotency_key: required")
    prepare = getattr(adapter, "prepare_envelope", None)
    resolved: list[PlannedQuery] = []
    for query in queries:
        envelope = query.envelope
        if callable(prepare):
            envelope = prepare(envelope)
            if not isinstance(envelope, QueryEnvelope):
                raise TypeError("adapter prepare_envelope must return QueryEnvelope")
            query = replace(query, envelope=envelope)
        envelope.validate()
        if envelope.run_id != normalize(run_id):
            raise ValueError("research run_id does not match a query envelope")
        resolved.append(query)

    root, exports = _private_export_directory(run_root, create=False)
    own = (exports,) if exports.exists() else ()
    state = StateStore(connection, export_directories=workspace_export_directories(connection, root, own))
    prior = state.snapshot(run_id)
    if prior.state is RunState.CREDENTIAL_REQUIRED:
        raise RuntimeError("credential_required: a current decision must resume the suspended request")

    credential_operation = f"research.execute:{idempotency_key}"
    requires_credential = bool(getattr(adapter, "requires_credential", False))
    credential_name = normalize(getattr(adapter, "credential_name", ""))
    if requires_credential and not credential_name:
        raise ValueError("credential-requiring adapter must declare its credential name")
    request_revision = None
    if requires_credential:
        if prior.state is RunState.RESEARCH_RUNNING:
            refuse_stale_re_research_reentry(connection, run_id)
        if prior.state not in {RunState.RESEARCH_READY, RunState.RESEARCH_RUNNING}:
            state.transition(
                run_id, RunState.RESEARCH_RUNNING, actor="research-cli", reason="state check",
                operation="research.start", idempotency_key=idempotency_key,
            )
        request_revision = state.add_revision(
            run_id,
            "research_request",
            {
                "plan": [query.as_dict() for query in resolved],
                "requests": [query.envelope.request_body() for query in resolved],
            },
            schema_version="research-request-v1",
        )
        if credential_decision_id:
            _verify_and_consume_credential_decision(
                connection,
                state,
                run_id=run_id,
                credential_decision_id=credential_decision_id,
                credential_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                idempotency_key=idempotency_key,
            )
        if not bool(getattr(adapter, "credential_present", False)):
            scope = _batch_credential_scope(
                [query.envelope for query in resolved],
                auth_attempt=credential_decision_id or "preflight",
                credential_name=credential_name,
            )
            gate = state.suspend_gate(
                run_id,
                GateKind.CREDENTIAL,
                suspended_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                approval_scope=scope,
                return_state=prior.state,
                actor="research-cli",
                reason="required adapter credential is unavailable",
            )
            raise CredentialRequiredError(gate)

    transition_event_ids: list[str] = []
    if prior.state is not RunState.RESEARCH_RUNNING:
        started = state.transition(
            run_id,
            RunState.RESEARCH_RUNNING,
            actor="research-cli",
            reason="bounded research batch started",
            operation="research.start",
            idempotency_key=idempotency_key,
        )
        transition_event_ids.append(started.event_id)
    else:
        started = None
    base_key = (
        f"{idempotency_key}:credential:{credential_decision_id}"
        if credential_decision_id else idempotency_key
    )
    store = ResearchStore(connection)
    executions: list[ResearchExecution] = []
    for index, query in enumerate(resolved):
        execution = store.execute(
            adapter,
            query,
            idempotency_key=f"{base_key}:q{index:02d}",
            retrieved_at=retrieved_at,
        )
        executions.append(execution)
        if requires_credential and execution.failure_kind == "auth":
            if request_revision is None:
                raise RuntimeError("credential adapter has no request revision")
            scope = _batch_credential_scope(
                [query.envelope for query in resolved],
                auth_attempt=credential_decision_id or "remote_auth",
                credential_name=credential_name,
            )
            gate = state.suspend_gate(
                run_id,
                GateKind.CREDENTIAL,
                suspended_operation=credential_operation,
                subject_revision_hash=request_revision.content_hash,
                approval_scope=scope,
                return_state=RunState.RESEARCH_RUNNING,
                actor="research-cli",
                reason="adapter rejected the configured credential",
            )
            raise CredentialRequiredError(gate)
    manifest = ResearchStore(connection).manifest(run_id)
    payload = research_bundle(manifest)
    succeeded = any(item.status == "success" and item.evidence_ids for item in executions)
    target = RunState.RESEARCH_COMPLETE if succeeded else RunState.RESEARCH_INCOMPLETE
    _root, exports = _private_export_directory(root, create=True)
    state = StateStore(connection, export_directories=workspace_export_directories(connection, root, (exports,)))
    final_operation = credential_operation if credential_decision_id else "research.finish"
    evidence_hashes = tuple(dict.fromkeys(
        evidence_id for item in executions for evidence_id in item.evidence_ids
    ))
    finished, exported = state.publish_transition(
        run_id,
        target,
        actor="research-cli",
        reason="bounded research batch persisted",
        operation=final_operation,
        idempotency_key=idempotency_key,
        evidence_hashes=evidence_hashes,
        artifact_kind="research_bundle",
        artifact_content=payload,
        artifact_schema_version="research-bundle-v1",
        export_directory=exports,
        dependencies=(request_revision.revision_id,) if request_revision else (),
        consumed_decision_id=credential_decision_id,
        fault_at=fault_at,
    )
    if finished.artifact is None:
        raise RuntimeError("research finish transition did not produce its bundle revision")
    transition_event_ids.append(finished.event_id)
    bundle = {
        **payload,
        "manifest": {
            "artifact_id": exported.artifact_id,
            "byte_hash": exported.content_hash,
            "byte_size": exported.size,
            "path": Path(exported.path).relative_to(root).as_posix(),
        },
    }
    return ResearchBatchRun(
        run_id,
        prior.state.value,
        finished.snapshot.state.value,
        tuple(executions),
        bundle,
        finished.artifact.revision_id,
        tuple(transition_event_ids),
        bool(
            started and started.replayed
            and all(item.replayed for item in executions)
            and finished.replayed
        ),
    )
