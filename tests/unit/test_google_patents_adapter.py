import json
import unittest
import urllib.error
from pathlib import Path

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.google_patents import (
    SERPAPI_HOST,
    GooglePatentsAdapter,
    serpapi_account,
)
from patent_factory.models import AdapterFailureKind, QueryEnvelope

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/google_patents/organic-results-v1.json"


def envelope(**overrides):
    values = {
        "run_id": "run", "adapter": "google_patents", "adapter_version": "serpapi-v1",
        "capability": "word_search", "allowed_scheme": "https", "allowed_host": SERPAPI_HOST,
        "deadline_seconds": 3, "page": 1, "page_cap": 5, "result_budget": 30,
        "byte_budget": 1_000_000, "retry_budget": 0, "retry_ownership": "research_runner",
        "query_projection": {"word": "센서"},
    }
    values.update(overrides)
    return QueryEnvelope(**values)


def _raise_http(code):
    def transport(url, timeout, budget):
        raise urllib.error.HTTPError(url, code, "x", {}, None)
    return transport


class GooglePatentsAdapterTests(unittest.TestCase):
    def test_success_normalizes_to_gpatent_locators_without_leaking_key(self):
        body = FIXTURE.read_bytes()
        calls = []

        def transport(url, timeout, budget):
            calls.append(url)
            return TransportResponse(200, {"Content-Type": "application/json"}, body,
                                     final_url="https://serpapi.com/search")

        result = GooglePatentsAdapter("CANARY-KEY", transport=transport).search(envelope())
        self.assertIsNone(result.failure)
        self.assertEqual(len(result.records), 2)
        first, second = result.records
        self.assertEqual(first.source_locator, "gpatent:KR102000001B1")
        self.assertEqual(first.source_type, "google_patent")
        self.assertEqual(first.provenance, "serpapi_google_patents")
        self.assertEqual(first.applicant, "Redacted Assignee Co.")
        self.assertEqual(first.canonical_url, "https://patents.google.com/patent/KR102000001B1/en")
        # publication_number derived from patent_id when the field is absent; assignee list joined.
        self.assertEqual(second.source_locator, "gpatent:KR1020240000002A")
        self.assertEqual(second.applicant, "First Assignee, Second Assignee")
        self.assertEqual(result.coverage["total_count"], 2)
        self.assertIn("engine=google_patents", calls[0])
        self.assertIn("api_key=CANARY-KEY", calls[0])
        self.assertNotIn("CANARY-KEY", repr(result))

    def test_result_budget_truncates(self):
        body = FIXTURE.read_bytes()
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(
            envelope(result_budget=1)
        )
        self.assertEqual(len(result.records), 1)

    def test_missing_credential_is_auth_without_network(self):
        calls = []
        adapter = GooglePatentsAdapter(
            None, transport=lambda *a: calls.append(a) or TransportResponse(200, {}, b"{}")
        )
        result = adapter.search(envelope())
        self.assertEqual(result.failure.kind, AdapterFailureKind.AUTH)
        self.assertEqual(calls, [])

    def test_quota_error_is_rate_limit(self):
        body = json.dumps({"error": "You've run out of searches for this month."}).encode()
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(envelope())
        self.assertEqual(result.failure.kind, AdapterFailureKind.RATE_LIMIT)

    def test_invalid_key_error_is_auth(self):
        body = json.dumps({"error": "Invalid API key, your API key should be here."}).encode()
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(envelope())
        self.assertEqual(result.failure.kind, AdapterFailureKind.AUTH)

    def test_throttle_is_rate_limited_but_distinct_from_quota_exhaustion(self):
        throttle = json.dumps({
            "error": "You are sending requests too fast, you've exceeded the hourly throughput limit."
        }).encode()
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, throttle)).search(envelope())
        self.assertEqual(result.failure.kind, AdapterFailureKind.RATE_LIMIT)
        self.assertTrue(result.failure.retryable)
        self.assertIn("throttled", result.failure.message)
        self.assertNotIn("quota", result.failure.message)

        exhausted = json.dumps({"error": "You've run out of searches for this month."}).encode()
        quota = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, exhausted)).search(envelope())
        self.assertEqual(quota.failure.kind, AdapterFailureKind.RATE_LIMIT)
        self.assertIn("quota exhausted", quota.failure.message)

    def test_unrecognized_request_error_stays_malformed(self):
        # Guards the closed marker lists against over-broad matching.
        for text in ("Invalid engine parameter.", "Unsupported country code exceeded limits."):
            body = json.dumps({"error": text}).encode()
            result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(envelope())
            self.assertEqual(result.failure.kind, AdapterFailureKind.MALFORMED)

    def test_processing_or_missing_status_is_not_terminal_success(self):
        for body in (
            json.dumps({"search_metadata": {"status": "Processing"}, "organic_results": []}).encode(),
            json.dumps({"organic_results": []}).encode(),
        ):
            result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(envelope())
            self.assertEqual(result.failure.kind, AdapterFailureKind.MALFORMED)

    def test_non_object_containers_are_malformed(self):
        for body in (
            json.dumps({"search_metadata": "weird"}).encode(),
            json.dumps({"search_metadata": {"status": "Success"}, "serpapi_pagination": ["x"]}).encode(),
            json.dumps({"search_metadata": {"status": "Success"}, "search_information": [1]}).encode(),
        ):
            result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(envelope())
            self.assertEqual(result.failure.kind, AdapterFailureKind.MALFORMED)

    def test_offsite_patent_link_falls_back_to_canonical_url(self):
        body = json.dumps({
            "search_metadata": {"status": "Success"},
            "organic_results": [{
                "publication_number": "KR102000001B1",
                "title": "Fixture title",
                "patent_link": "http://evil.example/track?x=1",
            }],
        }).encode()
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(envelope())
        self.assertIsNone(result.failure)
        self.assertEqual(
            result.records[0].canonical_url,
            "https://patents.google.com/patent/KR102000001B1/en",
        )

    def test_accepted_patent_link_is_reserialized_without_smuggled_bytes(self):
        body = json.dumps({
            "search_metadata": {"status": "Success"},
            "organic_results": [{
                "publication_number": "KR102000001B1",
                "title": "Fixture title",
                "patent_link": "https://PATENTS.GOOGLE.COM/patent/KR10\t2000001B1/en\nextra",
            }],
        }).encode()
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(envelope())
        self.assertIsNone(result.failure)
        url = result.records[0].canonical_url
        self.assertNotIn("\t", url)
        self.assertNotIn("\n", url)
        self.assertTrue(url.startswith("https://patents.google.com/"))

    def test_priority_date_is_never_substituted_for_filing_date(self):
        body = json.dumps({
            "search_metadata": {"status": "Success"},
            "organic_results": [{
                "publication_number": "KR102000001B1",
                "title": "Fixture title",
                "priority_date": "2023-01-01",
            }],
        }).encode()
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, body)).search(envelope())
        self.assertIsNone(result.failure)
        record = result.records[0]
        self.assertIsNone(record.filing_date)
        self.assertTrue(any("priority date" in note for note in record.limitations))

    def test_http_status_mapping(self):
        self.assertEqual(GooglePatentsAdapter("k", transport=_raise_http(401)).search(envelope()).failure.kind,
                         AdapterFailureKind.AUTH)
        self.assertEqual(GooglePatentsAdapter("k", transport=_raise_http(429)).search(envelope()).failure.kind,
                         AdapterFailureKind.RATE_LIMIT)

    def test_redirect_off_allowlist_is_access_denied(self):
        body = FIXTURE.read_bytes()
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(
            200, {}, body, final_url="https://evil.example/search")).search(envelope())
        self.assertEqual(result.failure.kind, AdapterFailureKind.ACCESS_DENIED)

    def test_host_outside_allowlist_is_denied(self):
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, b"{}")).search(
            envelope(allowed_host="patents.google.com")
        )
        self.assertEqual(result.failure.kind, AdapterFailureKind.ACCESS_DENIED)

    def test_malformed_json_is_malformed(self):
        result = GooglePatentsAdapter("k", transport=lambda *a: TransportResponse(200, {}, b"{not json")).search(
            envelope()
        )
        self.assertEqual(result.failure.kind, AdapterFailureKind.MALFORMED)

    def test_account_quota_parses_without_leaking_key(self):
        body = (ROOT / "tests/fixtures/serpapi/account-ok.json").read_bytes()
        calls = []

        def transport(url, timeout, budget):
            calls.append(url)
            return TransportResponse(200, {}, body, final_url="https://serpapi.com/account.json")

        account = serpapi_account("CANARY-KEY", transport=transport)
        self.assertEqual(account["total_searches_left"], 248)
        self.assertEqual(account["plan_renewal_date"], "2026-08-01")
        self.assertIn("api_key=CANARY-KEY", calls[0])

    def test_account_oversized_body_raises_value_error_not_overflow(self):
        def transport(url, timeout, budget):
            return TransportResponse(200, {}, b"x" * (budget + 1), final_url="https://serpapi.com/account.json")

        with self.assertRaises(ValueError):
            serpapi_account("k", transport=transport, byte_budget=64)


if __name__ == "__main__":
    unittest.main()
