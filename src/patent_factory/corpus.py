from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping

from .provenance import digest, normalize


def _patent_identity(record: Mapping[str, Any]) -> str:
    candidates = (
        record.get("application_number"), record.get("publication_number"),
        record.get("original_identifier"), record.get("source_locator"),
    )
    for candidate in candidates:
        value = re.sub(r"[^0-9A-Za-z]", "", normalize(candidate or "")).upper()
        if value:
            return value
    raise ValueError("corpus record: application or publication identity required")


def build_retained_corpus(
    *,
    finalist_id: str,
    query_group_id: str,
    hits: Iterable[Mapping[str, Any]],
    failures: Iterable[Mapping[str, Any]] = (),
    limit: int = 100,
) -> dict[str, Any]:
    if limit != 100:
        raise ValueError("corpus.limit: simrisk-v1.0.0 requires 100")
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    query_hits: dict[tuple[str, str], set[str]] = defaultdict(set)
    page_attempts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for raw in hits:
        item = normalize(dict(raw))
        required = {"content_hash", "evidence_id", "logical_query_id", "query_id", "record", "source_rank"}
        if set(item) != required or not isinstance(item["record"], dict):
            raise ValueError("corpus hit: exact fields required")
        if isinstance(item["source_rank"], bool) or not isinstance(item["source_rank"], int) or item["source_rank"] < 1:
            raise ValueError("corpus hit.source_rank: positive integer required")
        identity = _patent_identity(item["record"])
        key = identity, item["content_hash"]
        query_hits[key].add(item["logical_query_id"])
        page_attempts[key].add(item["query_id"])
        current = grouped.get(key)
        candidate = {
            "application_identity": identity, "best_source_rank": item["source_rank"],
            "content_hash": item["content_hash"], "evidence_id": item["evidence_id"],
            "logical_query_ids": [], "query_ids": [], "record": item["record"],
        }
        if current is None or (candidate["best_source_rank"], candidate["evidence_id"]) < (current["best_source_rank"], current["evidence_id"]):
            grouped[key] = candidate
        else:
            current["best_source_rank"] = min(current["best_source_rank"], item["source_rank"])
    records = []
    for key, item in grouped.items():
        item["logical_query_ids"] = sorted(query_hits[key])
        item["query_ids"] = sorted(page_attempts[key])
        item["query_hit_count"] = len(item["logical_query_ids"])
        records.append(item)
    records.sort(key=lambda item: (-item["query_hit_count"], item["best_source_rank"], item["application_identity"], item["content_hash"]))
    if len(records) > limit:
        boundary = records[limit - 1]
        substantive = (boundary["query_hit_count"], boundary["best_source_rank"])
        retained = records[:limit]
        retained.extend(
            item for item in records[limit:]
            if (item["query_hit_count"], item["best_source_rank"]) == substantive
        )
    else:
        retained = records
    retained_keys = {(item["application_identity"], item["content_hash"]) for item in retained}
    excluded = [{
        "application_identity": item["application_identity"],
        "best_source_rank": item["best_source_rank"],
        "content_hash": item["content_hash"],
        "evidence_id": item["evidence_id"],
        "logical_query_ids": item["logical_query_ids"],
        "query_hit_count": item["query_hit_count"],
        "query_ids": item["query_ids"],
        "reason_code": "below_retention_boundary",
    } for item in records if (item["application_identity"], item["content_hash"]) not in retained_keys]
    payload = {
        "excluded_count": len(excluded), "excluded_records": excluded, "failures": sorted(
            (normalize(dict(item)) for item in failures), key=lambda item: (item.get("query_id", ""), item.get("kind", ""))
        ),
        "finalist_id": normalize(finalist_id), "query_group_id": normalize(query_group_id),
        "records": retained, "retained_count": len(retained), "version": "retained-corpus-v1",
    }
    payload["corpus_hash"] = digest(payload)
    return payload
