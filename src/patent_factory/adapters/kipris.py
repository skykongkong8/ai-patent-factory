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

from .base import TransportResponse, bounded_body, canonical_date, normalized_patent_number

KIPRIS_HOST = "plus.kipris.or.kr"
KIPRIS_BASE_URL = "https://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice"
TERMS_NOTE = "Normalized metadata only; raw KIPRIS responses are not cached or redistributed."
Transport = Callable[[str, float, int], TransportResponse]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


def _default_transport(url: str, timeout: float, byte_budget: int) -> TransportResponse:
    request = urllib.request.Request(url, headers={"Accept": "application/xml"})
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        body = response.read(byte_budget + 1)
        return TransportResponse(
            response.status,
            dict(response.headers.items()),
            bounded_body(body, byte_budget),
            final_url=response.geturl(),
        )


def _failure(
    kind: AdapterFailureKind,
    message: str,
    *,
    retryable: bool = False,
    rate_limit: dict[str, str] | None = None,
) -> AdapterResult:
    return AdapterResult(
        (), None, TERMS_NOTE, {"usable": 0},
        rate_limit=rate_limit,
        failure=AdapterFailure(kind, message, retryable),
    )


def _rate_limit(headers: Any) -> dict[str, str] | None:
    normalized = {str(key).casefold(): normalize(value) for key, value in dict(headers).items()}
    result = {
        name: normalized[header]
        for name, header in (("limit", "x-ratelimit-limit"), ("remaining", "x-ratelimit-remaining"),
                             ("reset", "x-ratelimit-reset"), ("retry_after", "retry-after"))
        if normalized.get(header)
    }
    return result or None


def _allowed_final_url(value: str) -> bool:
    try:
        final = urllib.parse.urlsplit(value)
        return bool(
            final.scheme == "https"
            and final.hostname
            and final.hostname.casefold() == KIPRIS_HOST
            and not final.username
            and not final.password
            and not final.fragment
            and final.port in (None, 443)
        )
    except ValueError:
        return False


def _text(node: ET.Element, name: str) -> str | None:
    found = node.find(f".//{name}")
    return normalize(found.text) if found is not None and found.text else None


def _result_nodes(root: ET.Element) -> tuple[ET.Element, ...]:
    items = root.find(".//items")
    if items is None:
        return ()
    return tuple(items.findall("./item"))


class KiprisAdapter:
    name = "kipris"
    version = "plus-xml-v1"
    credential_name = "KIPRIS_PLUS_API_KEY"

    def __init__(
        self,
        service_key: str | None,
        *,
        transport: Transport | None = None,
        credential_required: bool = True,
    ) -> None:
        self._service_key = service_key
        self._transport = transport or _default_transport
        self.requires_credential = credential_required

    @property
    def credential_present(self) -> bool:
        return bool(self._service_key)

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
                return _failure(
                    AdapterFailureKind.RATE_LIMIT,
                    "KIPRIS rate limit response",
                    retryable=True,
                    rate_limit=_rate_limit(error.headers or {}),
                )
            if error.code in {401, 403}:
                return _failure(AdapterFailureKind.AUTH, "KIPRIS credential was rejected")
            return _failure(AdapterFailureKind.NETWORK, f"KIPRIS HTTP status {error.code}", retryable=error.code >= 500)
        except (urllib.error.URLError, OSError):
            return _failure(AdapterFailureKind.NETWORK, "KIPRIS network request failed", retryable=True)
        except Exception:
            return _failure(AdapterFailureKind.INTERNAL, "KIPRIS transport failed")

        rate_limit = _rate_limit(response.headers)
        if response.final_url and not _allowed_final_url(response.final_url):
            return _failure(AdapterFailureKind.ACCESS_DENIED, "KIPRIS redirect left the allowlist")
        if response.status == 429:
            return _failure(
                AdapterFailureKind.RATE_LIMIT, "KIPRIS rate limit response",
                retryable=True, rate_limit=rate_limit,
            )
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
        application_status = (_text(root, "successYN") or "").upper()
        if application_status == "N":
            code = _text(root, "resultCode") or "unknown"
            kind = AdapterFailureKind.AUTH if code == "30" else AdapterFailureKind.MALFORMED
            return _failure(kind, f"KIPRIS application error {code}")
        if application_status != "Y":
            return _failure(AdapterFailureKind.MALFORMED, "KIPRIS response misses explicit success status")
        body_node = root.find(".//body")
        if body_node is None:
            return _failure(AdapterFailureKind.MALFORMED, "KIPRIS response misses expected body")
        if body_node.find(".//items") is None:
            return _failure(AdapterFailureKind.MALFORMED, "KIPRIS response misses expected items container")

        try:
            # The live service emits pagination in a <count> element that is a
            # SIBLING of <body> — see tests/fixtures/kipris/word-search-live-v1.xml.
            # Only hand-authored fixtures ever nested it inside <body>, and that
            # invented shape is what let #38 ship. Searching from the document
            # root resolves the real shape; the structural guard in
            # tests/unit/test_kipris_live_shape.py stops it drifting back.
            total_text = _text(root, "totalCount")
            rows_text = _text(root, "numOfRows")
            page_text = _text(root, "pageNo")
            if total_text is None or rows_text is None or page_text is None:
                raise ValueError("pagination fields are required")
            total, rows, page = int(total_text), int(rows_text), int(page_text)
            if total < 0 or rows < 1 or page < 1 or page != envelope.page:
                raise ValueError("pagination values are invalid")
            if envelope.cursor is not None and envelope.cursor != str(page):
                raise ValueError("pagination cursor does not match page")
            nodes = _result_nodes(root)
            if total > 0 and not nodes:
                raise ValueError("positive result count has no items")
            records: list[AdapterRecord] = []
            for item in nodes:
                application = _text(item, "applicationNumber")
                title = _text(item, "inventionTitle")
                if not application or not title:
                    raise ValueError("KIPRIS item misses required identity fields")
                number = normalized_patent_number(application)
                if not number:
                    raise ValueError("KIPRIS application number is invalid")
                abstract = _text(item, "astrtCont")
                # The live service packs multiple classification codes into one
                # element separated by "|". Splitting is required for correctness:
                # an unsplit blob never matches a candidate code, so an exact IPC
                # subgroup hit scores "unrelated" instead of "subgroup".
                # cpcNumber is documented by KIPRIS but appears in 0 of the
                # recorded live items; it is read opportunistically, not on
                # evidence that the service emits it.
                classifications = tuple(sorted({
                    part for name in ("ipcNumber", "cpcNumber")
                    if (value := _text(item, name))
                    for raw in value.split("|")
                    if (part := normalize(raw))
                }))
                normalized_record = {
                    "abstract": abstract, "applicant": _text(item, "applicantName"),
                    "application_number": number, "classifications": classifications,
                    "filing_date": canonical_date(_text(item, "applicationDate")), "title": title,
                }
                field_span_hashes = {
                    field: digest({"field": field, "text": value})
                    for field, value in normalized_record.items()
                    if field in {"title", "abstract", "classifications"} and value
                }
                excerpt_hashes = tuple(sorted(field_span_hashes.values()))
                records.append(AdapterRecord(
                    source_type="kipris_patent", source_locator=f"kr-patent:{number}",
                    original_identifier=application, title=title, content_hash=digest(normalized_record),
                    language="ko", provenance="kipris_plus_api",
                    filing_date=normalized_record["filing_date"], applicant=normalized_record["applicant"],
                    abstract=abstract, classifications=classifications,
                    excerpt_hashes=excerpt_hashes, field_span_hashes=field_span_hashes,
                    limitations=("Normalized KIPRIS metadata; not a patentability conclusion.",),
                    # Retrieved and carried, but deliberately absent from
                    # `normalized_record` above: `registerStatus` is mutable
                    # (등록 -> 소멸), so hashing it would re-mint `evidence_id`
                    # every time a reference's status changed upstream.
                    register_status=_text(item, "registerStatus"),
                    register_date=canonical_date(_text(item, "registerDate")),
                    register_number=_text(item, "registerNumber"),
                    open_date=canonical_date(_text(item, "openDate")),
                    publication_date=canonical_date(_text(item, "publicationDate")),
                ))
                if len(records) >= envelope.result_budget:
                    break
        except (ArithmeticError, TypeError, ValueError):
            return _failure(AdapterFailureKind.MALFORMED, "malformed KIPRIS response structure")
        next_cursor = str(page + 1) if page < envelope.page_cap and page * rows < total else None
        result = AdapterResult(
            tuple(records), hashlib.sha256(body).hexdigest(), TERMS_NOTE,
            {"received": len(records), "total_count": total, "usable": len(records)},
            next_cursor=next_cursor, rate_limit=rate_limit,
        )
        try:
            result.validate()
            return result
        except (TypeError, ValueError):
            return _failure(AdapterFailureKind.MALFORMED, "malformed normalized KIPRIS result")
