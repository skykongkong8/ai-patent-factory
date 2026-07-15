from __future__ import annotations

import hashlib
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
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

SERPAPI_HOST = "serpapi.com"
SERPAPI_SEARCH_URL = "https://serpapi.com/search"
SERPAPI_ACCOUNT_URL = "https://serpapi.com/account.json"
GOOGLE_PATENTS_HOST = "patents.google.com"
ENGINE = "google_patents"
TERMS_NOTE = "Normalized metadata only; raw SerpApi/Google Patents responses are not cached or redistributed."
Transport = Callable[[str, float, int], TransportResponse]

# SerpApi phrases that indicate the monthly search allowance is spent rather than
# a malformed request or an invalid key. Kept as a small closed list so a genuine
# malformed response is never silently reclassified as a recoverable quota state.
_QUOTA_MARKERS = ("run out of searches", "ran out of searches", "exceeded", "plan searches", "monthly search")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


def _default_transport(url: str, timeout: float, byte_budget: int) -> TransportResponse:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        body = response.read(byte_budget + 1)
        return TransportResponse(
            response.status,
            dict(response.headers.items()),
            bounded_body(body, byte_budget),
            final_url=response.geturl(),
        )


def _failure(kind: AdapterFailureKind, message: str, *, retryable: bool = False) -> AdapterResult:
    return AdapterResult(
        (), None, TERMS_NOTE, {"usable": 0},
        failure=AdapterFailure(kind, message, retryable),
    )


def _allowed_final_url(value: str) -> bool:
    try:
        final = urllib.parse.urlsplit(value)
        return bool(
            final.scheme == "https"
            and final.hostname
            and final.hostname.casefold() == SERPAPI_HOST
            and not final.username
            and not final.password
            and not final.fragment
            and final.port in (None, 443)
        )
    except ValueError:
        return False


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    text = normalize(value) if isinstance(value, str) else None
    return text or None


def _assignee(value: Any) -> str | None:
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (list, tuple)):
        joined = ", ".join(normalize(item) for item in value if isinstance(item, str) and normalize(item))
        return joined or None
    return None


def _publication_number(result: dict[str, Any]) -> str | None:
    direct = _text(result.get("publication_number"))
    if direct:
        return direct
    # patent_id / patent_link commonly carry "patent/<PUB>/en".
    for key in ("patent_id", "patent_link"):
        raw = _text(result.get(key))
        if not raw:
            continue
        parts = [segment for segment in raw.replace("https://", "").split("/") if segment]
        if "patent" in parts:
            index = parts.index("patent")
            if index + 1 < len(parts):
                return parts[index + 1]
    return None


def serpapi_account(
    api_key: str,
    *,
    transport: Transport | None = None,
    deadline: float = 10.0,
    byte_budget: int = 200_000,
) -> dict[str, Any]:
    """Read the free SerpApi account quota without spending a search or returning the key."""

    if not api_key:
        raise ValueError("serpapi account query requires a credential")
    call = transport or _default_transport
    url = f"{SERPAPI_ACCOUNT_URL}?{urllib.parse.urlencode({'api_key': api_key})}"
    response = call(url, deadline, byte_budget)
    if response.final_url and not _allowed_final_url(response.final_url):
        raise ValueError("serpapi account redirect left the allowlist")
    if response.status != 200:
        raise ValueError(f"serpapi account query failed with status {response.status}")
    try:
        data = json.loads(bounded_body(response.body, byte_budget))
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("serpapi account response was not valid JSON") from error
    if not isinstance(data, dict):
        raise ValueError("serpapi account response was not an object")
    return {
        "total_searches_left": _int_or_none(data.get("total_searches_left")),
        "plan_searches_left": _int_or_none(data.get("plan_searches_left")),
        "this_month_usage": _int_or_none(data.get("this_month_usage")),
        "searches_per_month": _int_or_none(data.get("searches_per_month")),
        "plan_renewal_date": _text(data.get("plan_renewal_date")),
    }


class GooglePatentsAdapter:
    name = "google_patents"
    version = "serpapi-v1"
    credential_name = "SERPAPI_API_KEY"

    def __init__(
        self,
        api_key: str | None,
        *,
        transport: Transport | None = None,
        credential_required: bool = True,
    ) -> None:
        self._api_key = api_key
        self._transport = transport or _default_transport
        self.requires_credential = credential_required

    @property
    def credential_present(self) -> bool:
        return bool(self._api_key)

    def _parameters(self, envelope: QueryEnvelope) -> dict[str, Any]:
        projection = dict(envelope.query_projection)
        if envelope.capability != "word_search":
            raise LookupError("unsupported google_patents capability")
        allowed = {"word", "num", "country", "language", "status", "type", "before", "after", "sort"}
        if set(projection) - allowed or not normalize(projection.get("word", "")):
            raise ValueError("word_search projection is invalid")
        num = int(projection.get("num", min(100, max(10, envelope.result_budget))))
        if not 10 <= num <= 100:
            raise ValueError("word_search num is outside 10..100")
        parameters: dict[str, Any] = {
            "engine": ENGINE,
            "q": normalize(projection["word"]),
            "num": num,
            "page": envelope.page,
            "output": "json",
        }
        for name in ("country", "language", "status", "type", "before", "after", "sort"):
            value = normalize(projection.get(name, ""))
            if value:
                parameters[name] = value
        return parameters

    def search(self, envelope: QueryEnvelope) -> AdapterResult:
        try:
            envelope.validate()
            if envelope.adapter != self.name or envelope.adapter_version != self.version:
                return _failure(AdapterFailureKind.UNSUPPORTED, "adapter identity mismatch")
            if envelope.allowed_scheme != "https" or envelope.allowed_host.casefold() != SERPAPI_HOST:
                return _failure(AdapterFailureKind.ACCESS_DENIED, "target is outside the SerpApi allowlist")
            if not self._api_key:
                return _failure(AdapterFailureKind.AUTH, "SerpApi credential is missing")
            parameters = self._parameters(envelope)
        except LookupError as error:
            return _failure(AdapterFailureKind.UNSUPPORTED, str(error))
        except (TypeError, ValueError) as error:
            return _failure(AdapterFailureKind.MALFORMED, str(error))

        # api_key is process-only. It is added after fingerprinting and never returned.
        parameters["api_key"] = self._api_key
        url = f"{SERPAPI_SEARCH_URL}?{urllib.parse.urlencode(parameters)}"
        try:
            response = self._transport(url, envelope.deadline_seconds, envelope.byte_budget)
            body = bounded_body(response.body, envelope.byte_budget)
        except OverflowError:
            return _failure(AdapterFailureKind.OVERSIZE, "SerpApi response exceeds byte budget")
        except (TimeoutError, socket.timeout):
            return _failure(AdapterFailureKind.TIMEOUT, "SerpApi request timed out", retryable=True)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                return _failure(AdapterFailureKind.RATE_LIMIT, "SerpApi rate limit response", retryable=True)
            if error.code in {401, 403}:
                return _failure(AdapterFailureKind.AUTH, "SerpApi credential was rejected")
            return _failure(AdapterFailureKind.NETWORK, f"SerpApi HTTP status {error.code}", retryable=error.code >= 500)
        except (urllib.error.URLError, OSError):
            return _failure(AdapterFailureKind.NETWORK, "SerpApi network request failed", retryable=True)
        except Exception:
            return _failure(AdapterFailureKind.INTERNAL, "SerpApi transport failed")

        if response.final_url and not _allowed_final_url(response.final_url):
            return _failure(AdapterFailureKind.ACCESS_DENIED, "SerpApi redirect left the allowlist")
        if response.status == 429:
            return _failure(AdapterFailureKind.RATE_LIMIT, "SerpApi rate limit response", retryable=True)
        if response.status in {401, 403}:
            return _failure(AdapterFailureKind.AUTH, "SerpApi credential was rejected")
        if not 200 <= response.status < 300:
            return _failure(AdapterFailureKind.NETWORK, f"SerpApi HTTP status {response.status}", retryable=response.status >= 500)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return _failure(AdapterFailureKind.MALFORMED, "malformed SerpApi JSON")
        if not isinstance(data, dict):
            return _failure(AdapterFailureKind.MALFORMED, "SerpApi response was not an object")

        error_text = _text(data.get("error"))
        if error_text:
            lowered = error_text.casefold()
            if any(marker in lowered for marker in _QUOTA_MARKERS):
                return _failure(AdapterFailureKind.RATE_LIMIT, "SerpApi monthly search quota exhausted", retryable=True)
            if "api key" in lowered or "api_key" in lowered:
                return _failure(AdapterFailureKind.AUTH, "SerpApi credential was rejected")
            return _failure(AdapterFailureKind.MALFORMED, "SerpApi reported a request error")
        status = _text((data.get("search_metadata") or {}).get("status")) or ""
        if status and status.casefold() not in {"success", "processing"}:
            return _failure(AdapterFailureKind.MALFORMED, "SerpApi response misses a success status")

        organic = data.get("organic_results")
        if organic is not None and not isinstance(organic, list):
            return _failure(AdapterFailureKind.MALFORMED, "SerpApi organic_results is not a list")
        information = data.get("search_information") or {}
        total = _int_or_none(information.get("total_results"))

        try:
            records: list[AdapterRecord] = []
            for result in organic or ():
                if not isinstance(result, dict):
                    raise ValueError("SerpApi result is not an object")
                title = _text(result.get("title"))
                pub = _publication_number(result)
                if not title or not pub:
                    raise ValueError("SerpApi item misses required identity fields")
                number = normalized_patent_number(pub)
                if not number:
                    raise ValueError("SerpApi publication number is invalid")
                abstract = _text(result.get("snippet"))
                canonical_url = _text(result.get("patent_link")) or f"https://{GOOGLE_PATENTS_HOST}/patent/{number}/en"
                normalized_record = {
                    "abstract": abstract,
                    "applicant": _assignee(result.get("assignee")),
                    "application_number": number,
                    "classifications": (),
                    "filing_date": _text(result.get("filing_date")) or _text(result.get("priority_date")),
                    "title": title,
                }
                field_span_hashes = {
                    field_name: digest({"field": field_name, "text": value})
                    for field_name, value in normalized_record.items()
                    if field_name in {"title", "abstract"} and value
                }
                excerpt_hashes = tuple(sorted(field_span_hashes.values()))
                records.append(AdapterRecord(
                    source_type="google_patent",
                    source_locator=f"gpatent:{number}",
                    original_identifier=pub,
                    title=title,
                    content_hash=digest(normalized_record),
                    language=_text(result.get("language")) or "und",
                    provenance="serpapi_google_patents",
                    canonical_url=canonical_url,
                    filing_date=normalized_record["filing_date"],
                    applicant=normalized_record["applicant"],
                    abstract=abstract,
                    excerpt_hashes=excerpt_hashes,
                    field_span_hashes=field_span_hashes,
                    limitations=("Normalized Google Patents metadata; not a patentability conclusion.",),
                ))
                if len(records) >= envelope.result_budget:
                    break
        except (ArithmeticError, TypeError, ValueError):
            return _failure(AdapterFailureKind.MALFORMED, "malformed SerpApi response structure")

        received = len(records)
        coverage_total = total if total is not None else received
        has_more = bool((data.get("serpapi_pagination") or {}).get("next"))
        next_cursor = str(envelope.page + 1) if has_more and envelope.page < envelope.page_cap else None
        result = AdapterResult(
            tuple(records), hashlib.sha256(body).hexdigest(), TERMS_NOTE,
            {"received": received, "total_count": coverage_total, "usable": received},
            next_cursor=next_cursor,
        )
        try:
            result.validate()
            return result
        except (TypeError, ValueError):
            return _failure(AdapterFailureKind.MALFORMED, "malformed normalized SerpApi result")
