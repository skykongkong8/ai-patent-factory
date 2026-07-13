from __future__ import annotations

import hashlib
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from patent_factory.models import (
    AdapterFailure,
    AdapterFailureKind,
    AdapterRecord,
    AdapterResult,
    QueryEnvelope,
)
from patent_factory.provenance import digest, normalize

from .base import TransportResponse, bounded_body, normalized_patent_number

KIPRIS_HOST = "plus.kipris.or.kr"
KIPRIS_BASE_URL = "https://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice"
TERMS_NOTE = "Normalized metadata only; raw KIPRIS responses are not cached or redistributed."
Transport = Callable[[str, float, int], TransportResponse]


def _default_transport(url: str, timeout: float, byte_budget: int) -> TransportResponse:
    request = urllib.request.Request(url, headers={"Accept": "application/xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(byte_budget + 1)
        return TransportResponse(response.status, dict(response.headers.items()), bounded_body(body, byte_budget))


def _failure(kind: AdapterFailureKind, message: str, *, retryable: bool = False) -> AdapterResult:
    return AdapterResult((), None, TERMS_NOTE, {"usable": 0}, failure=AdapterFailure(kind, message, retryable))


def _text(node: ET.Element, name: str) -> str | None:
    found = node.find(f".//{name}")
    return normalize(found.text) if found is not None and found.text else None


def _result_nodes(root: ET.Element) -> tuple[ET.Element, ...]:
    items = root.find(".//items")
    if items is None:
        return ()
    grouped = tuple(items.findall("./item"))
    if grouped:
        return grouped
    # The confirmed KIPRIS family also emits singleton fields directly under
    # <items>; treat that container as one record without guessing other shapes.
    if items.find("./applicationNumber") is not None:
        return (items,)
    return ()


class KiprisAdapter:
    name = "kipris"
    version = "plus-xml-v1"

    def __init__(self, service_key: str | None, *, transport: Transport | None = None) -> None:
        self._service_key = service_key
        self._transport = transport or _default_transport

    def _parameters(self, envelope: QueryEnvelope) -> tuple[str, dict[str, Any]]:
        projection = dict(envelope.query_projection)
        if envelope.capability == "word_search":
            allowed = {"word", "year", "patent", "utility", "num_of_rows"}
            if set(projection) - allowed or not normalize(projection.get("word", "")):
                raise ValueError("word_search projection is invalid")
            year = int(projection.get("year", 0))
            rows = int(projection.get("num_of_rows", min(30, envelope.result_budget)))
            if not 0 <= year <= 10 or not 1 <= rows <= min(500, envelope.result_budget):
                raise ValueError("word_search budget is invalid")
            return "getWordSearch", {
                "word": normalize(projection["word"]), "year": year,
                "patent": str(bool(projection.get("patent", True))).lower(),
                "utility": str(bool(projection.get("utility", True))).lower(),
                "numOfRows": rows, "pageNo": envelope.page,
            }
        if envelope.capability == "bibliography_summary":
            if set(projection) != {"application_number"} or not normalize(projection["application_number"]):
                raise ValueError("bibliography_summary projection is invalid")
            return "getBibliographySumryInfoSearch", {"applicationNumber": projection["application_number"]}
        raise LookupError("unsupported KIPRIS capability")

    def search(self, envelope: QueryEnvelope) -> AdapterResult:
        try:
            envelope.validate()
            if envelope.adapter != self.name or envelope.adapter_version != self.version:
                return _failure(AdapterFailureKind.UNSUPPORTED, "adapter identity mismatch")
            if envelope.allowed_scheme != "https" or envelope.allowed_host.casefold() != KIPRIS_HOST:
                return _failure(AdapterFailureKind.ACCESS_DENIED, "target is outside the KIPRIS allowlist")
            if not self._service_key:
                return _failure(AdapterFailureKind.AUTH, "KIPRIS credential is missing")
            operation, parameters = self._parameters(envelope)
        except LookupError as error:
            return _failure(AdapterFailureKind.UNSUPPORTED, str(error))
        except (TypeError, ValueError) as error:
            return _failure(AdapterFailureKind.MALFORMED, str(error))

        # ServiceKey is process-only. It is added after fingerprinting and never returned.
        parameters["ServiceKey"] = self._service_key
        url = f"{KIPRIS_BASE_URL}/{operation}?{urllib.parse.urlencode(parameters)}"
        try:
            response = self._transport(url, envelope.deadline_seconds, envelope.byte_budget)
            body = bounded_body(response.body, envelope.byte_budget)
        except OverflowError:
            return _failure(AdapterFailureKind.OVERSIZE, "KIPRIS response exceeds byte budget")
        except (TimeoutError, socket.timeout):
            return _failure(AdapterFailureKind.TIMEOUT, "KIPRIS request timed out", retryable=True)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                return _failure(AdapterFailureKind.RATE_LIMIT, "KIPRIS rate limit response", retryable=True)
            if error.code in {401, 403}:
                return _failure(AdapterFailureKind.AUTH, "KIPRIS credential was rejected")
            return _failure(AdapterFailureKind.NETWORK, f"KIPRIS HTTP status {error.code}", retryable=error.code >= 500)
        except (urllib.error.URLError, OSError):
            return _failure(AdapterFailureKind.NETWORK, "KIPRIS network request failed", retryable=True)
        except Exception:
            return _failure(AdapterFailureKind.INTERNAL, "KIPRIS transport failed")

        if response.status == 429:
            return _failure(AdapterFailureKind.RATE_LIMIT, "KIPRIS rate limit response", retryable=True)
        if response.status in {401, 403}:
            return _failure(AdapterFailureKind.AUTH, "KIPRIS credential was rejected")
        if not 200 <= response.status < 300:
            return _failure(AdapterFailureKind.NETWORK, f"KIPRIS HTTP status {response.status}", retryable=response.status >= 500)
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            return _failure(AdapterFailureKind.MALFORMED, "unsafe XML declaration rejected")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return _failure(AdapterFailureKind.MALFORMED, "malformed KIPRIS XML")
        if (_text(root, "successYN") or "Y").upper() == "N":
            code = _text(root, "resultCode") or "unknown"
            kind = AdapterFailureKind.AUTH if code == "30" else AdapterFailureKind.MALFORMED
            return _failure(kind, f"KIPRIS application error {code}")

        records: list[AdapterRecord] = []
        for item in _result_nodes(root):
            application = _text(item, "applicationNumber")
            title = _text(item, "inventionTitle")
            if not application or not title:
                return _failure(AdapterFailureKind.MALFORMED, "KIPRIS item misses required identity fields")
            number = normalized_patent_number(application)
            abstract = _text(item, "astrtCont")
            classifications = tuple(sorted({value for name in ("ipcNumber", "cpcNumber") if (value := _text(item, name))}))
            normalized_record = {
                "abstract": abstract, "applicant": _text(item, "applicantName"),
                "application_number": number, "classifications": classifications,
                "filing_date": _text(item, "applicationDate"), "title": title,
            }
            records.append(AdapterRecord(
                source_type="kipris_patent", source_locator=f"kr-patent:{number}",
                original_identifier=application, title=title, content_hash=digest(normalized_record),
                language="ko", filing_date=normalized_record["filing_date"],
                applicant=normalized_record["applicant"], abstract=abstract,
                classifications=classifications,
                limitations=("Normalized KIPRIS metadata; not a patentability conclusion.",),
            ))
            if len(records) >= envelope.result_budget:
                break
        total = int(_text(root, "totalCount") or len(records))
        rows = int(_text(root, "numOfRows") or max(1, len(records)))
        page = int(_text(root, "pageNo") or envelope.page)
        next_cursor = str(page + 1) if page < envelope.page_cap and page * rows < total else None
        result = AdapterResult(
            tuple(records), hashlib.sha256(body).hexdigest(), TERMS_NOTE,
            {"received": len(records), "total_count": total, "usable": len(records)}, next_cursor=next_cursor,
        )
        result.validate()
        return result
