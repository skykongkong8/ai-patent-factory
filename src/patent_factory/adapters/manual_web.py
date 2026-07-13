from __future__ import annotations

import urllib.parse
from typing import Any

from patent_factory.models import (
    AdapterFailure,
    AdapterFailureKind,
    AdapterRecord,
    AdapterResult,
    QueryEnvelope,
)
from patent_factory.provenance import canonical_json, digest, normalize

TERMS_NOTE = "User-supplied normalized metadata; original source terms and access limits remain applicable."


def _failure(kind: AdapterFailureKind, message: str) -> AdapterResult:
    return AdapterResult((), None, TERMS_NOTE, {"usable": 0}, failure=AdapterFailure(kind, message))


class ManualWebAdapter:
    name = "manual_web"
    version = "import-v1"

    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self._allowed_hosts = frozenset(host.casefold() for host in allowed_hosts)

    def search(self, envelope: QueryEnvelope) -> AdapterResult:
        try:
            envelope.validate()
            if envelope.adapter != self.name or envelope.adapter_version != self.version or envelope.capability != "import":
                return _failure(AdapterFailureKind.UNSUPPORTED, "manual adapter identity or capability mismatch")
            if envelope.allowed_host.casefold() not in self._allowed_hosts:
                return _failure(AdapterFailureKind.ACCESS_DENIED, "manual source host is not allowlisted")
            projection = dict(envelope.query_projection)
            if set(projection) != {"content_type", "records"} or projection["content_type"] != "application/json":
                return _failure(AdapterFailureKind.UNSUPPORTED, "manual import requires application/json")
            encoded = canonical_json(projection["records"]).encode("utf-8")
            if len(encoded) > envelope.byte_budget:
                return _failure(AdapterFailureKind.OVERSIZE, "manual import exceeds byte budget")
            if not isinstance(projection["records"], list):
                return _failure(AdapterFailureKind.MALFORMED, "manual records must be a list")
            records: list[AdapterRecord] = []
            for item in projection["records"][: envelope.result_budget]:
                if not isinstance(item, dict):
                    return _failure(AdapterFailureKind.MALFORMED, "manual record must be an object")
                url = urllib.parse.urlsplit(str(item.get("canonical_url", "")))
                if url.scheme != "https" or not url.hostname or url.hostname.casefold() not in self._allowed_hosts:
                    return _failure(AdapterFailureKind.ACCESS_DENIED, "manual record URL is outside the HTTPS allowlist")
                if url.username or url.password or url.fragment:
                    return _failure(AdapterFailureKind.ACCESS_DENIED, "manual record URL contains forbidden components")
                title = normalize(item.get("title", ""))
                identifier = normalize(item.get("identifier", ""))
                content_hash = normalize(item.get("content_hash", ""))
                if not title or not identifier or not content_hash or not normalize(item.get("provenance", "")):
                    return _failure(AdapterFailureKind.MALFORMED, "manual record provenance and identity are required")
                locator = urllib.parse.urlunsplit(("https", url.hostname.casefold(), url.path or "/", url.query, ""))
                records.append(AdapterRecord(
                    source_type="manual_web", source_locator=locator, original_identifier=identifier,
                    title=title, content_hash=content_hash, language=normalize(item.get("language", "und")),
                    canonical_url=locator, excerpt_hashes=tuple(sorted(set(item.get("excerpt_hashes", ())))),
                    interpretations=tuple(item.get("interpretations", ())),
                    limitations=tuple(item.get("limitations", ())),
                ))
        except (TypeError, ValueError):
            return _failure(AdapterFailureKind.MALFORMED, "manual import envelope is malformed")
        result = AdapterResult(tuple(records), digest(encoded.decode("utf-8")), TERMS_NOTE,
                               {"received": len(records), "usable": len(records)})
        result.validate()
        return result
