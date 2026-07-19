from __future__ import annotations

import re
import urllib.parse
from dataclasses import replace
from typing import Any, Iterable, Mapping

from patent_factory.models import (
    AdapterFailure,
    AdapterFailureKind,
    AdapterRecord,
    AdapterResult,
    QueryEnvelope,
)
from patent_factory.provenance import canonical_json, digest, normalize

TERMS_NOTE = "User-supplied normalized metadata; original source terms and access limits remain applicable."
MANUAL_RECORD_FIELDS = frozenset({
    "canonical_url", "identifier", "title", "content_hash", "language", "provenance",
    "excerpt_hashes", "interpretations", "limitations",
})


def _failure(kind: AdapterFailureKind, message: str) -> AdapterResult:
    return AdapterResult((), None, TERMS_NOTE, {"usable": 0}, failure=AdapterFailure(kind, message))


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"manual record {field} must be a string list")
    return [normalize(item) for item in value if normalize(item)]


def sanitize_manual_records(
    records: Any,
    allowed_hosts: Iterable[str],
) -> list[dict[str, Any]]:
    """Validate a closed public-metadata schema before fingerprinting or persistence."""

    if not isinstance(records, list):
        raise ValueError("manual records must be a list")
    hosts = frozenset(normalize(host).casefold() for host in allowed_hosts if normalize(host))
    sanitized: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, Mapping):
            raise ValueError("manual record must be an object")
        if set(item) - MANUAL_RECORD_FIELDS:
            raise ValueError("manual record contains unsupported fields")
        url = urllib.parse.urlsplit(str(item.get("canonical_url", "")))
        if url.scheme != "https" or not url.hostname or url.hostname.casefold() not in hosts:
            raise PermissionError("manual record URL is outside the HTTPS allowlist")
        if url.username or url.password or url.fragment or (url.port not in (None, 443)):
            raise PermissionError("manual record URL contains forbidden components")
        title = normalize(item.get("title", ""))
        identifier = normalize(item.get("identifier", ""))
        content_hash = normalize(item.get("content_hash", "")).casefold()
        provenance = normalize(item.get("provenance", ""))
        if not title or not identifier or not provenance:
            raise ValueError("manual record provenance and identity are required")
        if re.fullmatch(r"[0-9a-f]{64}", content_hash) is None:
            raise ValueError("manual record content_hash must be normalized SHA-256 hex")
        locator = urllib.parse.urlunsplit(("https", url.hostname.casefold(), url.path or "/", url.query, ""))
        sanitized.append({
            "canonical_url": locator,
            "content_hash": content_hash,
            "excerpt_hashes": sorted(set(_strings(item.get("excerpt_hashes"), "excerpt_hashes"))),
            "identifier": identifier,
            "interpretations": _strings(item.get("interpretations"), "interpretations"),
            "language": normalize(item.get("language", "und")) or "und",
            "limitations": _strings(item.get("limitations"), "limitations"),
            "provenance": provenance,
            "title": title,
        })
    return sanitized


class ManualWebAdapter:
    name = "manual_web"
    version = "import-v1"

    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self._allowed_hosts = frozenset(host.casefold() for host in allowed_hosts)

    def prepare_envelope(self, envelope: QueryEnvelope) -> QueryEnvelope:
        envelope.validate()
        if envelope.adapter != self.name or envelope.adapter_version != self.version or envelope.capability != "import":
            raise ValueError("manual adapter identity or capability mismatch")
        if envelope.allowed_host.casefold() not in self._allowed_hosts:
            raise PermissionError("manual source host is not allowlisted")
        projection = dict(envelope.query_projection)
        if set(projection) != {"content_type", "records"} or projection["content_type"] != "application/json":
            raise ValueError("manual import requires application/json")
        sanitized = sanitize_manual_records(projection["records"], self._allowed_hosts)
        return replace(
            envelope,
            query_projection={"content_type": "application/json", "records": sanitized},
        )

    def search(self, envelope: QueryEnvelope) -> AdapterResult:
        try:
            if envelope.adapter != self.name or envelope.adapter_version != self.version or envelope.capability != "import":
                return _failure(AdapterFailureKind.UNSUPPORTED, "manual adapter identity or capability mismatch")
            envelope = self.prepare_envelope(envelope)
            projection = dict(envelope.query_projection)
            sanitized = projection["records"]
            encoded = canonical_json(sanitized).encode("utf-8")
            if len(encoded) > envelope.byte_budget:
                return _failure(AdapterFailureKind.OVERSIZE, "manual import exceeds byte budget")
            if len(sanitized) > envelope.result_budget:
                return _failure(
                    AdapterFailureKind.OVERSIZE,
                    f"manual import exceeds result budget ({len(sanitized)} records > "
                    f"{envelope.result_budget}): raise --result-budget or split the import",
                )
            records: list[AdapterRecord] = []
            for item in sanitized:
                records.append(AdapterRecord(
                    source_type="manual_web", source_locator=item["canonical_url"],
                    original_identifier=item["identifier"], title=item["title"],
                    content_hash=item["content_hash"], language=item["language"],
                    provenance=item["provenance"], canonical_url=item["canonical_url"],
                    excerpt_hashes=tuple(item["excerpt_hashes"]),
                    interpretations=tuple(item.get("interpretations", ())),
                    limitations=tuple(item.get("limitations", ())),
                ))
        except PermissionError as error:
            return _failure(AdapterFailureKind.ACCESS_DENIED, str(error))
        except (TypeError, ValueError):
            return _failure(AdapterFailureKind.MALFORMED, "manual import envelope is malformed")
        result = AdapterResult(tuple(records), digest(encoded.decode("utf-8")), TERMS_NOTE,
                               {"received": len(sanitized), "usable": len(records)})
        result.validate()
        return result
