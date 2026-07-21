from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters.base import SearchAdapter
from .config import SimilarityConfig
from .corpus import build_retained_corpus
from .models import GateKind, QueryEnvelope, RunState
from .privacy import assert_canaries_absent, credential_canaries
from .provenance import digest, normalize
from .research import CredentialRequiredError, PlannedQuery, ResearchStore
from .similarity import (
    canonical_feature_map,
    risk_label,
    score_pair,
    summarize_candidate,
    validate_feature_map,
    validate_pair_score,
)
from .state import StateError, StateStore, workspace_export_directories


AdapterFactory = Callable[[Mapping[str, Any], int, str], SearchAdapter]


def feature_map_id(finalist_id: str, feature_map: Mapping[str, Any]) -> str:
    return "fm_" + digest({
        "finalist_id": normalize(finalist_id),
        "feature_map": canonical_feature_map(feature_map),
    })[:20]


def validate_audit_artifact(value: Mapping[str, Any], config: SimilarityConfig) -> None:
    """Authoritative cross-field validator for a durable audit-batch-v1 artifact."""

    required = {
        "corpus_set_hash", "feature_map_set_hash", "finalist_set_hash", "results",
        "run_id", "scorer_config_hash", "version",
    }
    if not isinstance(value, Mapping) or set(value) != required or value["version"] != "audit-batch-v1":
        raise ValueError("audit_artifact: exact audit-batch-v1 fields required")
    if not isinstance(value["results"], list) or len(value["results"]) < 3:
        raise ValueError("audit_artifact.results: at least three finalist results required")
    result_fields = {
        "candidate_id", "closest_reference_id", "corpus_hash", "counterargument", "coverage",
        "finalist_id", "outcome", "pair_scores", "r_hi", "r_obs", "upper_bound_reference_id",
    }
    for result in value["results"]:
        if not isinstance(result, Mapping) or set(result) != result_fields:
            raise ValueError("audit_artifact.result: exact fields required")
        if not isinstance(normalize(result["counterargument"]), str) or not normalize(result["counterargument"]):
            raise ValueError("audit_artifact.result: nonempty counterargument required")
        scores = result["pair_scores"]
        if not isinstance(scores, list):
            raise ValueError("audit_artifact.result: pair_scores array required")
        for score in scores:
            exact = validate_pair_score(score)
            if score.get("label") != risk_label(exact["r_obs"], config):
                raise ValueError("audit_artifact.pair_score: label does not match exact observed risk")
        summary = summarize_candidate(scores, config)
        expected = {
            "closest_reference_id": summary["observed_reference_id"],
            "coverage": summary["coverage"],
            "outcome": summary["outcome"],
            "r_hi": summary["r_hi"],
            "r_obs": summary["r_obs"],
            "upper_bound_reference_id": summary["upper_bound_reference_id"],
        }
        if any(result[name] != expected[name] for name in expected):
            raise ValueError("audit_artifact.result: summary does not match validated pair scores")


@dataclass(frozen=True)
class AuditRetrieval:
    run_id: str
    query_set_revision_id: str
    corpus_set_revision_id: str
    corpus_hashes: tuple[str, ...]
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ids": [self.query_set_revision_id, self.corpus_set_revision_id],
            "command": "audit.retrieve", "corpus_hashes": list(self.corpus_hashes),
            "next_state": RunState.AUDIT_RUNNING.value, "replayed": self.replayed,
            "run_id": self.run_id, "status": "audit_running",
        }


@dataclass(frozen=True)
class AuditRun:
    run_id: str
    state: str
    artifact_revision_id: str
    gate_id: str | None
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ids": [self.artifact_revision_id], "command": "audit.score",
            "gate_id": self.gate_id, "next_state": self.state, "replayed": self.replayed,
            "run_id": self.run_id, "status": self.state,
        }


def _artifact(connection: sqlite3.Connection, run_id: str, kind: str):
    row = connection.execute(
        "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
        "WHERE ar.run_id=? AND ca.kind=? AND ar.stale=0", (run_id, kind),
    ).fetchone()
    if row is None:
        raise StateError(f"audit requires current {kind}")
    return row, json.loads(row["content_json"])


def _exports(run_root: Path) -> Path:
    root = Path(run_root).absolute()
    if not root.is_dir() or stat.S_ISLNK(root.lstat().st_mode):
        raise ValueError("audit_export: safe run directory required")
    directory = root / "audit-exports"
    if directory.exists() and (not directory.is_dir() or stat.S_ISLNK(directory.lstat().st_mode)):
        raise ValueError("audit_export: unsafe export directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700, follow_symlinks=False)
    except OSError:
        pass
    return directory


def _publishing_state(connection: sqlite3.Connection, run_root: Path) -> tuple[StateStore, Path]:
    root, audit_exports = Path(run_root).absolute(), _exports(run_root)
    directories = workspace_export_directories(connection, root, (audit_exports,))
    return StateStore(connection, export_directories=directories), audit_exports


def _query_input(value: Mapping[str, Any], finalists: Mapping[str, Mapping[str, Any]], finalist_hash: str) -> list[dict[str, Any]]:
    if set(value) != {"finalist_set_hash", "groups", "schema_version"} or value["schema_version"] != "audit-query-input-v1":
        raise ValueError("audit_query_input: exact audit-query-input-v1 fields required")
    if value["finalist_set_hash"] != finalist_hash or not isinstance(value["groups"], list):
        raise ValueError("audit_query_input: current finalist set hash required")
    if len(value["groups"]) != len(finalists):
        raise ValueError("audit_query_input.groups: exactly one group per finalist required")
    prepared = []
    seen_finalists: set[str] = set()
    for raw in value["groups"]:
        if not isinstance(raw, Mapping) or set(raw) != {"finalist_id", "queries"}:
            raise ValueError("audit_query_input.groups: exact fields required")
        finalist_id = raw["finalist_id"]
        if finalist_id not in finalists or finalist_id in seen_finalists or not isinstance(raw["queries"], list):
            raise ValueError("audit_query_input.groups: unknown finalist or invalid queries")
        seen_finalists.add(finalist_id)
        queries = []
        for query in raw["queries"]:
            if not isinstance(query, Mapping) or set(query) != {"language", "term"} or query["language"] not in {"ko", "en"} or not normalize(query["term"]):
                raise ValueError("audit_query_input.queries: Korean/English term required")
            queries.append({"language": query["language"], "term": normalize(query["term"])})
        if {item["language"] for item in queries} != {"ko", "en"}:
            raise ValueError("audit_query_input.groups: each finalist requires Korean and English queries")
        group_id = "aq_" + digest({"finalist_set_hash": finalist_hash, "finalist_id": finalist_id, "queries": queries})[:20]
        prepared.append({"finalist_id": finalist_id, "query_group_id": group_id, "queries": queries})
    if {item["finalist_id"] for item in prepared} != set(finalists):
        raise ValueError("audit_query_input.groups: exactly one group per finalist required")
    if len({item["query_group_id"] for item in prepared}) != len(prepared):
        raise ValueError("audit_query_input.groups: query group identities must be unique")
    return sorted(prepared, key=lambda item: item["finalist_id"])


def run_audit_retrieval(
    connection: sqlite3.Connection, *, run_root: Path, run_id: str,
    query_input: Mapping[str, Any], config: SimilarityConfig, adapter_factory: AdapterFactory,
    credential_decision_id: str | None = None, retrieved_at: str | None = None,
) -> AuditRetrieval:
    canaries = credential_canaries()
    assert_canaries_absent(query_input, canaries, boundary="audit_query_input")
    finalist_row, finalist_set = _artifact(connection, run_id, "finalist_set")
    finalists = {item["finalist_id"]: item for item in finalist_set["finalists"]}
    groups = _query_input(query_input, finalists, finalist_row["content_hash"])
    state = StateStore(connection)
    prior = state.snapshot(run_id)
    if prior.state not in {RunState.FINALISTS_READY, RunState.AUDIT_RUNNING}:
        raise StateError("audit retrieval requires finalists_ready")
    if prior.state is RunState.FINALISTS_READY:
        first_group, first_query = groups[0], groups[0]["queries"][0]
        preflight = adapter_factory(first_query, 1, first_group["finalist_id"])
        request_hash = digest({"finalist_set_hash": finalist_row["content_hash"], "query_input": query_input})
        credential_operation = f"audit.retrieve:{request_hash}"
        credential_missing = bool(getattr(preflight, "requires_credential", False)) and not bool(getattr(preflight, "credential_present", False))
        if credential_decision_id and credential_missing:
            raise StateError("credential remains unavailable for the approved audit request")
        if credential_decision_id:
            row = connection.execute(
                "SELECT gd.action,gd.stale,gd.subject_revision_hash,gd.suspended_operation,gd.used_at,ge.approval_scope_json "
                "FROM gate_decisions gd JOIN gate_envelopes ge ON ge.gate_id=gd.gate_id "
                "WHERE gd.decision_id=? AND gd.run_id=? AND ge.kind='credential'",
                (credential_decision_id, run_id),
            ).fetchone()
            if row is None or row["action"] not in {"configure_and_verify", "approve"} or row["stale"] or row["subject_revision_hash"] != finalist_row["content_hash"] or row["suspended_operation"] != credential_operation or row["used_at"]:
                raise StateError("credential decision does not match the exact current audit request")
            state.consume_decision(
                credential_decision_id, suspended_operation=credential_operation,
                subject_revision_hash=finalist_row["content_hash"],
                approval_scope=json.loads(row["approval_scope_json"]),
            )
        if credential_missing:
            gate = state.suspend_gate(
                run_id, GateKind.CREDENTIAL, suspended_operation=credential_operation,
                subject_revision_hash=finalist_row["content_hash"],
                approval_scope={
                    "adapter": normalize(getattr(preflight, "name", "kipris")),
                    "credential_name": normalize(getattr(preflight, "credential_name", "KIPRIS_PLUS_API_KEY")),
                    "finalist_set_hash": finalist_row["content_hash"], "query_input_hash": digest(query_input),
                },
                return_state=RunState.FINALISTS_READY, actor="audit-cli",
                reason="required final-audit KIPRIS credential is unavailable",
            )
            raise CredentialRequiredError(gate)
    try:
        current_config_row, current_config_content = _artifact(connection, run_id, "scorer_config")
    except StateError:
        current_config_row, current_config_content = None, None
    configured = (
        current_config_content == config.as_dict()
        or isinstance(current_config_content, Mapping)
        and current_config_content.get("config") == config.as_dict()
        and current_config_content.get("finalist_set_hash") == finalist_row["content_hash"]
    )
    if configured:
        config_revision = type("Revision", (), {
            "revision_id": current_config_row["revision_id"], "content_hash": current_config_row["content_hash"],
        })()
    else:
        config_revision = state.add_revision(
            run_id, "scorer_config", {
                "config": config.as_dict(), "config_hash": config.content_hash,
                "finalist_set_hash": finalist_row["content_hash"],
                "supersedes": current_config_row["content_hash"] if current_config_row else None,
                "version": "scorer-config-v1",
            }, schema_version="scorer-config-v1",
        )
    query_payload = {
        "config_hash": config.content_hash, "finalist_set_hash": finalist_row["content_hash"],
        "groups": groups, "run_id": run_id, "version": "audit-query-set-v1",
    }
    operation_hash = digest(query_payload)
    if prior.state is RunState.FINALISTS_READY:
        start_operation = credential_operation if credential_decision_id else "audit.retrieve.start"
        started = state.transition(
            run_id, RunState.AUDIT_RUNNING, actor="audit-cli", reason="finalist-specific KIPRIS audit started",
            operation=start_operation, idempotency_key=operation_hash,
            artifact_kind="audit_query_set", artifact_content=query_payload,
            artifact_schema_version="audit-query-set-v1", dependencies=(finalist_row["revision_id"], config_revision.revision_id),
            consumed_decision_id=credential_decision_id,
        )
        query_revision = started.artifact
    else:
        try:
            query_row, current = _artifact(connection, run_id, "audit_query_set")
        except StateError:
            # A changed finalist or scorer configuration invalidates the prior
            # audit descendants. Rebuild from the new current upstreams while
            # retaining immutable history and never reviving stale decisions.
            query_revision = state.add_revision(
                run_id, "audit_query_set", query_payload, schema_version="audit-query-set-v1",
                dependencies=(finalist_row["revision_id"], config_revision.revision_id),
            )
        else:
            if current != query_payload:
                raise StateError("audit retrieval already runs for a different current query set")
            query_revision = type("Revision", (), {"revision_id": query_row["revision_id"]})()

    store = ResearchStore(connection)
    corpora = []
    for group in groups:
        query_ids: list[str] = []
        logical_queries: dict[str, str] = {}
        failures: list[dict[str, Any]] = []
        for query_index, query in enumerate(group["queries"]):
            logical_query_id = "lq_" + digest({
                "query_group_id": group["query_group_id"], "query_index": query_index,
                "language": query["language"], "term": query["term"],
            })[:20]
            logical_received = 0
            for page in range(1, config.page_cap + 1):
                remaining = config.results_per_query - logical_received
                if remaining <= 0:
                    break
                binding = {
                    "purpose": "final_similarity_audit", "finalist_set_hash": finalist_row["content_hash"],
                    "finalist_id": group["finalist_id"], "query_group_id": group["query_group_id"],
                }
                envelope = QueryEnvelope(
                    run_id=run_id, adapter="kipris", adapter_version="plus-xml-v1", capability="word_search",
                    allowed_scheme="https", allowed_host="plus.kipris.or.kr", deadline_seconds=10,
                    page=page, page_cap=config.page_cap, result_budget=remaining,
                    byte_budget=1_000_000, retry_budget=0, retry_ownership="audit_runner",
                    query_projection={"word": query["term"], "year": 0, "patent": True, "utility": True, "num_of_rows": remaining},
                    cursor=str(page) if page > 1 else None, audit_binding=binding,
                )
                planned = PlannedQuery(envelope, query["term"], query["term"], f"audit_{query['language']}", 0)
                execution = store.execute(
                    adapter_factory(query, page, group["finalist_id"]), planned,
                    idempotency_key=f"audit:{operation_hash}:{group['finalist_id']}:{query_index}:{page}",
                    retrieved_at=retrieved_at,
                )
                query_ids.append(execution.query_id)
                logical_queries[execution.query_id] = logical_query_id
                if execution.failure_kind:
                    failures.append({"kind": execution.failure_kind, "query_id": execution.query_id})
                    break
                event = connection.execute("SELECT next_cursor,coverage_json FROM adapter_events WHERE event_id=?", (execution.event_id,)).fetchone()
                coverage = json.loads(event["coverage_json"])
                logical_received += min(remaining, int(coverage.get("received", len(execution.evidence_ids))))
                if not event["next_cursor"]:
                    break
        marks = ",".join("?" for _ in query_ids)
        rows = connection.execute(
            f"SELECT re.query_id,re.source_rank,er.evidence_id,er.content_hash,er.record_json "
            f"FROM research_edges re JOIN evidence_records er ON er.run_id=re.run_id AND er.evidence_id=re.evidence_id "
            f"WHERE re.run_id=? AND re.query_id IN ({marks}) ORDER BY re.query_id,re.source_rank,er.evidence_id",
            (run_id, *query_ids),
        ).fetchall() if query_ids else ()
        hits = [{
            "query_id": row["query_id"], "source_rank": row["source_rank"], "evidence_id": row["evidence_id"],
            "logical_query_id": logical_queries[row["query_id"]],
            "content_hash": row["content_hash"], "record": json.loads(row["record_json"]),
        } for row in rows]
        corpora.append(build_retained_corpus(
            finalist_id=group["finalist_id"], query_group_id=group["query_group_id"],
            hits=hits, failures=failures, limit=config.corpus_limit,
        ))
    corpus_payload = {
        "config_hash": config.content_hash, "corpora": corpora,
        "finalist_set_hash": finalist_row["content_hash"], "run_id": run_id, "version": "corpus-set-v1",
    }
    corpus_revision = state.add_revision(
        run_id, "corpus_set", corpus_payload, schema_version="corpus-set-v1",
        dependencies=(query_revision.revision_id, config_revision.revision_id),
    )
    return AuditRetrieval(
        run_id, query_revision.revision_id, corpus_revision.revision_id,
        tuple(item["corpus_hash"] for item in corpora), prior.state is RunState.AUDIT_RUNNING,
    )


def _candidate_text(candidate: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("technical_problem", "mechanism", "required_inputs", "components", "interactions", "transformations", "outputs", "expected_effects", "implementation_example")
    parts = []
    for field in fields:
        value = candidate.get(field)
        parts.extend(value if isinstance(value, list) else [value] if value else [])
    return {"title": candidate["title"], "abstract": " ".join(parts)}


FEATURE_SOURCE_FIELDS = {
    "problem": {"technical_problem"},
    "inputs": {"required_inputs", "components"},
    "mechanism": {"mechanism", "components", "interactions"},
    "transformations": {"transformations"},
    "outputs": {"outputs"},
    "technical_effects": {"expected_effects", "measurable_validation"},
}


def _candidate_span_hashes(candidate: Mapping[str, Any], category: str) -> set[str]:
    spans: set[str] = set()
    for field in FEATURE_SOURCE_FIELDS[category]:
        raw = candidate.get(field)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            if isinstance(value, str) and normalize(value):
                spans.add(digest({"field": field, "text": normalize(value)}))
    return spans


def run_audit_scoring(
    connection: sqlite3.Connection, *, run_root: Path, run_id: str,
    feature_input: Mapping[str, Any], config: SimilarityConfig,
) -> AuditRun:
    canaries = credential_canaries()
    assert_canaries_absent(feature_input, canaries, boundary="feature_map_input")
    state = StateStore(connection)
    snapshot = state.snapshot(run_id)
    replay_states = {RunState.AUDIT_APPROVED, RunState.COVERAGE_INSUFFICIENT, RunState.DECISION_REQUIRED}
    if snapshot.state not in replay_states | {RunState.AUDIT_RUNNING}:
        raise StateError("audit scoring requires audit_running or an exact completed replay")
    finalist_row, finalist_set = _artifact(connection, run_id, "finalist_set")
    candidate_row, candidate_set = _artifact(connection, run_id, "candidate_set")
    corpus_row, corpus_set = _artifact(connection, run_id, "corpus_set")
    config_row, config_content = _artifact(connection, run_id, "scorer_config")
    required = {"corpus_set_hash", "finalist_set_hash", "maps", "schema_version"}
    if set(feature_input) != required or feature_input["schema_version"] != "feature-map-set-input-v1":
        raise ValueError("feature_map_input: exact feature-map-set-input-v1 fields required")
    if feature_input["finalist_set_hash"] != finalist_row["content_hash"] or feature_input["corpus_set_hash"] != corpus_row["content_hash"]:
        raise ValueError("feature_map_input: current finalist and corpus hashes required")
    resolved_config = config_content.get("config") if isinstance(config_content, Mapping) and "config" in config_content else config_content
    if resolved_config != config.as_dict() or not isinstance(feature_input["maps"], list):
        raise ValueError("feature_map_input: scorer configuration mismatch")
    raw_maps = feature_input["maps"]
    if any(
        not isinstance(item, Mapping) or set(item) != {"feature_map", "finalist_id", "map_id"}
        for item in raw_maps
    ):
        raise ValueError("feature_map_input.maps: exact map wrapper fields required")
    canonical_maps = [{
        "feature_map": canonical_feature_map(item["feature_map"]),
        "finalist_id": item["finalist_id"],
        "map_id": item["map_id"],
    } for item in raw_maps]
    if snapshot.state in replay_states:
        feature_row, feature_set = _artifact(connection, run_id, "feature_map_set")
        audit_row, audit_batch = _artifact(connection, run_id, "audit_batch")
        validate_audit_artifact(audit_batch, config)
        if (
            feature_set.get("maps") != canonical_maps
            or feature_set.get("finalist_set_hash") != feature_input["finalist_set_hash"]
            or feature_set.get("corpus_set_hash") != feature_input["corpus_set_hash"]
            or audit_batch.get("feature_map_set_hash") != feature_row["content_hash"]
            or audit_batch.get("scorer_config_hash") != config_row["content_hash"]
        ):
            raise StateError("completed audit replay does not match the exact bound input")
        gate = connection.execute(
            "SELECT gate_id FROM gate_envelopes WHERE run_id=? AND subject_revision_hash=? "
            "ORDER BY created_at DESC LIMIT 1", (run_id, audit_row["content_hash"]),
        ).fetchone()
        return AuditRun(
            run_id, snapshot.state.value, audit_row["revision_id"], gate["gate_id"] if gate else None, True,
        )
    finalists = {item["finalist_id"]: item for item in finalist_set["finalists"]}
    if len(canonical_maps) != len(finalists):
        raise ValueError("feature_map_input.maps: exactly one frozen map per finalist required")
    finalist_ids = [item["finalist_id"] for item in canonical_maps]
    map_ids = [item["map_id"] for item in canonical_maps]
    if len(set(finalist_ids)) != len(finalist_ids) or len(set(map_ids)) != len(map_ids):
        raise ValueError("feature_map_input.maps: duplicate finalist or map identity")
    if set(finalist_ids) != set(finalists):
        raise ValueError("feature_map_input.maps: exact current finalist set required")
    if any(item["map_id"] != feature_map_id(item["finalist_id"], item["feature_map"]) for item in canonical_maps):
        raise ValueError("feature_map_input.maps: map identity does not bind frozen content")
    maps = {item["finalist_id"]: item for item in canonical_maps}
    candidates = {item["candidate_id"]: item for item in candidate_set["candidates"]}
    corpora = {item["finalist_id"]: item for item in corpus_set["corpora"]}
    results = []
    for finalist_id in sorted(finalists):
        finalist, corpus = finalists[finalist_id], corpora[finalist_id]
        candidate = candidates[finalist["candidate_id"]]
        feature_map = maps[finalist_id]["feature_map"]
        validate_feature_map(feature_map, config)
        for feature in feature_map["features"].values():
            candidate_spans = _candidate_span_hashes(candidate, feature["category"])
            if not set(feature["candidate_span_hashes"]).issubset(candidate_spans):
                raise ValueError("feature_map_input: candidate span does not belong to the finalist revision")
        record_by_id = {item["evidence_id"]: item["record"] for item in corpus["records"]}
        if {item["evidence_id"] for item in feature_map["reference_maps"]} != set(record_by_id):
            raise ValueError("feature_map_input: every retained corpus record must be reviewed exactly once")
        for mapping in feature_map["reference_maps"]:
            record = record_by_id[mapping["evidence_id"]]
            field_spans = record.get("field_span_hashes", {})
            inspected = set(mapping["inspected_fields"])
            if not inspected or not inspected.issubset(field_spans) or any(not record.get(field) for field in inspected):
                raise ValueError("feature_map_input: inspected field is not a real retained evidence field")
            spans = {field_spans[field] for field in inspected}
            for decision in mapping["decisions"].values():
                if not set(decision["reference_span_hashes"]).issubset(spans):
                    raise ValueError("feature_map_input: reference span does not belong to evidence revision")
        scores = []
        candidate_text = _candidate_text(candidate)
        for item in corpus["records"]:
            reference = {**item["record"], "evidence_id": item["evidence_id"]}
            scores.append(score_pair(candidate_text, reference, feature_map, config))
        summary = summarize_candidate(scores, config)
        results.append({
            "candidate_id": finalist["candidate_id"], "corpus_hash": corpus["corpus_hash"],
            "counterargument": "Provisional research aid within the retrieved corpus; not a legal novelty conclusion.",
            "coverage": summary["coverage"], "finalist_id": finalist_id,
            "outcome": summary["outcome"], "pair_scores": scores,
            "closest_reference_id": summary["observed_reference_id"],
            "r_hi": summary["r_hi"], "r_obs": summary["r_obs"],
            "upper_bound_reference_id": summary["upper_bound_reference_id"],
        })
    feature_payload = {
        "corpus_set_hash": corpus_row["content_hash"], "finalist_set_hash": finalist_row["content_hash"],
        "maps": [maps[key] for key in sorted(maps)], "run_id": run_id, "version": "feature-map-set-v1",
    }
    feature_dependencies = tuple(sorted({corpus_row["revision_id"], finalist_row["revision_id"]}))
    feature_hash = digest({
        "content": feature_payload,
        "dependencies": feature_dependencies,
        "schema_version": "feature-map-set-v1",
    })
    if any(item["outcome"] == "coverage_insufficient" for item in results):
        target, gate_kind = RunState.COVERAGE_INSUFFICIENT, GateKind.COVERAGE
    else:
        target, gate_kind = RunState.DECISION_REQUIRED, GateKind.POST_AUDIT_CHECKPOINT
    audit_payload = {
        "corpus_set_hash": corpus_row["content_hash"], "feature_map_set_hash": feature_hash,
        "finalist_set_hash": finalist_row["content_hash"], "results": results,
        "run_id": run_id, "scorer_config_hash": config_row["content_hash"],
        "version": "audit-batch-v1",
    }
    validate_audit_artifact(audit_payload, config)
    feature_revision = state.add_revision(
        run_id, "feature_map_set", feature_payload, schema_version="feature-map-set-v1",
        dependencies=feature_dependencies,
    )
    if feature_revision.content_hash != feature_hash:
        raise StateError("feature-map revision hash drifted after audit validation")
    operation_hash = digest(audit_payload)
    dependencies = (finalist_row["revision_id"], corpus_row["revision_id"], feature_revision.revision_id, config_row["revision_id"])
    publishing, exports = _publishing_state(connection, run_root)
    if gate_kind:
        scope = {
            "audit_hash": digest(audit_payload), "outcome": target.value,
            "affected_finalist_ids": [item["finalist_id"] for item in results if item["outcome"] == target.value],
            "decision_bindings": [{
                "corpus_hash": item["corpus_hash"], "finalist_hash": digest(finalists[item["finalist_id"]]),
                "finalist_id": item["finalist_id"], "map_id": maps[item["finalist_id"]]["map_id"],
            } for item in results if item["outcome"] == target.value],
            "corpus_set_hash": corpus_row["content_hash"],
            "feature_map_set_hash": feature_hash, "finalist_set_hash": finalist_row["content_hash"],
            "scorer_config_hash": config_row["content_hash"],
        }
        finished, _export, gate = publishing.publish_gate_transition(
            run_id, gate_kind, actor="audit-cli", reason="final similarity audit requires a decision",
            operation="audit.finalize", idempotency_key=operation_hash, approval_scope=scope,
            artifact_kind="audit_batch", artifact_content=audit_payload,
            artifact_schema_version="audit-batch-v1", dependencies=dependencies,
            export_directory=exports,
        )
        return AuditRun(run_id, target.value, finished.artifact.revision_id, gate.gate_id, finished.replayed)
    finished, _export = publishing.publish_transition(
        run_id, target, actor="audit-cli", reason="final similarity audit approved",
        operation="audit.finalize", idempotency_key=operation_hash,
        artifact_kind="audit_batch", artifact_content=audit_payload,
        artifact_schema_version="audit-batch-v1", dependencies=dependencies,
        export_directory=exports,
    )
    return AuditRun(run_id, target.value, finished.artifact.revision_id, None, finished.replayed)
