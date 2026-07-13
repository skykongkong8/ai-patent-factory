from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .adapters.base import SearchAdapter
from .database import FaultInjector, immediate_transaction, inject_fault, utc_now
from .models import AdapterResult, QueryEnvelope
from .provenance import canonical_json, digest, evidence_revision_id, normalize


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
                "response_hash,failure_kind,failure_json,terms_note,coverage_json,next_cursor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, envelope.run_id, query_id, envelope.adapter, envelope.adapter_version, at, status,
                 result.response_hash, failure_kind, canonical_json(result.failure.as_dict()) if result.failure else None,
                 result.terms_note, canonical_json(dict(result.coverage)), result.next_cursor),
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
                for rank, record in enumerate(result.records, start=1):
                    record_data = record.as_dict()
                    evidence_id = evidence_revision_id(record.source_locator, record.content_hash)
                    self.connection.execute(
                        "INSERT INTO evidence_records(run_id,evidence_id,source_type,source_locator,original_identifier,title,"
                        "canonical_url,content_hash,language,record_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(run_id,evidence_id) DO NOTHING",
                        (envelope.run_id, evidence_id, record.source_type, record.source_locator,
                         record.original_identifier, record.title, record.canonical_url, record.content_hash,
                         record.language, canonical_json(record_data), at),
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
        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in self.connection.execute(sql, (run_id,))]

        return {
            "adapter_events": rows("SELECT * FROM adapter_events WHERE run_id=? ORDER BY retrieved_at,event_id"),
            "coverage_limitations": rows("SELECT * FROM coverage_limitations WHERE run_id=? ORDER BY created_at,limitation_id"),
            "edges": rows("SELECT * FROM research_edges WHERE run_id=? ORDER BY query_id,source_rank,evidence_id"),
            "evidence": rows("SELECT * FROM evidence_records WHERE run_id=? ORDER BY evidence_id"),
            "observations": rows("SELECT * FROM retrieval_observations WHERE run_id=? ORDER BY retrieved_at,observation_id"),
            "queries": rows("SELECT * FROM research_queries WHERE run_id=? ORDER BY created_at,query_id"),
            "run_id": run_id,
        }
