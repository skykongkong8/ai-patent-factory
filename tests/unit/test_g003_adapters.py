import tempfile
import unittest
from pathlib import Path

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KIPRIS_HOST, KiprisAdapter
from patent_factory.adapters.manual_web import ManualWebAdapter
from patent_factory.models import AdapterFailureKind, QueryEnvelope


def envelope(adapter="kipris", version="plus-xml-v1", capability="word_search", host=KIPRIS_HOST, projection=None, **overrides):
    values = {
        "run_id": "run", "adapter": adapter, "adapter_version": version,
        "capability": capability, "allowed_scheme": "https", "allowed_host": host,
        "deadline_seconds": 3, "page": 1, "page_cap": 5, "result_budget": 30,
        "byte_budget": 100_000, "retry_budget": 0, "retry_ownership": "research_runner",
        "query_projection": projection or {"word": "센서", "year": 0, "patent": True, "utility": True},
    }
    values.update(overrides)
    return QueryEnvelope(**values)


class KiprisAdapterTests(unittest.TestCase):
    def test_confirmed_xml_normalizes_and_paginates_without_persisting_key(self):
        body = Path("tests/fixtures/kipris/word-search-v1.xml").read_bytes()
        calls = []

        def transport(url, timeout, budget):
            calls.append((url, timeout, budget))
            return TransportResponse(200, {"Content-Type": "application/xml"}, body)

        result = KiprisAdapter("CANARY-SECRET", transport=transport).search(envelope())
        self.assertTrue(result.successful)
        self.assertEqual(result.next_cursor, "2")
        self.assertEqual(result.records[0].source_locator, "kr-patent:1020240012345")
        self.assertEqual(result.records[0].classifications, ("G06F 1/00",))
        self.assertNotIn("CANARY-SECRET", repr(result))
        self.assertIn("getWordSearch", calls[0][0])

    def test_missing_key_and_rejected_target_make_zero_network_calls(self):
        calls = []
        transport = lambda *args: calls.append(args)
        missing = KiprisAdapter(None, transport=transport).search(envelope())
        denied = KiprisAdapter("secret", transport=transport).search(envelope(host="example.com"))
        self.assertEqual((missing.failure.kind, denied.failure.kind),
                         (AdapterFailureKind.AUTH, AdapterFailureKind.ACCESS_DENIED))
        self.assertEqual(calls, [])

    def test_application_error_malformed_and_entity_payload_are_failures_without_records(self):
        payloads = (
            b"<response><successYN>N</successYN><resultCode>30</resultCode><resultMsg>bad</resultMsg></response>",
            b"<not-closed>",
            b'<!DOCTYPE x [<!ENTITY y "z">]><response>&y;</response>',
        )
        expected = (AdapterFailureKind.AUTH, AdapterFailureKind.MALFORMED, AdapterFailureKind.MALFORMED)
        for body, kind in zip(payloads, expected):
            with self.subTest(kind=kind):
                adapter = KiprisAdapter("secret", transport=lambda *_: TransportResponse(200, {}, body))
                result = adapter.search(envelope())
                self.assertEqual(result.failure.kind, kind)
                self.assertEqual(result.records, ())

    def test_singleton_fields_directly_under_items_are_normalized(self):
        body = b"""<response><successYN>Y</successYN><body><items>
          <inventionTitle>singleton</inventionTitle><applicationNumber>10-2025-0000001</applicationNumber>
          <applicationDate>20250101</applicationDate><ipcNumber>H04L 1/00</ipcNumber>
        </items><numOfRows>1</numOfRows><pageNo>1</pageNo><totalCount>1</totalCount></body></response>"""
        adapter = KiprisAdapter("secret", transport=lambda *_: TransportResponse(200, {}, body))
        result = adapter.search(envelope())
        self.assertTrue(result.successful)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].source_locator, "kr-patent:1020250000001")

    def test_oversize_timeout_rate_limit_and_unsupported_are_normalized(self):
        cases = (
            (lambda *_: TransportResponse(200, {}, b"x" * 10), envelope(byte_budget=5), AdapterFailureKind.OVERSIZE),
            (lambda *_: (_ for _ in ()).throw(TimeoutError()), envelope(), AdapterFailureKind.TIMEOUT),
            (lambda *_: TransportResponse(429, {}, b""), envelope(), AdapterFailureKind.RATE_LIMIT),
            (lambda *_: TransportResponse(200, {}, b"<response/>"), envelope(capability="unknown"), AdapterFailureKind.UNSUPPORTED),
        )
        for transport, query, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(KiprisAdapter("secret", transport=transport).search(query).failure.kind, expected)


class ManualWebAdapterTests(unittest.TestCase):
    def test_import_requires_https_allowlist_json_and_provenance(self):
        record = {
            "canonical_url": "https://example.com/public/1", "identifier": "public-1",
            "title": "공개 기술 문서", "content_hash": "a" * 64, "language": "ko",
            "provenance": "user_import", "limitations": ["manual import"],
        }
        query = envelope(adapter="manual_web", version="import-v1", capability="import", host="example.com",
                         projection={"content_type": "application/json", "records": [record]})
        result = ManualWebAdapter(("example.com",)).search(query)
        self.assertTrue(result.successful)
        self.assertEqual(result.records[0].canonical_url, "https://example.com/public/1")

        for change, kind in (({"canonical_url": "http://example.com/1"}, AdapterFailureKind.ACCESS_DENIED),
                             ({"provenance": ""}, AdapterFailureKind.MALFORMED)):
            bad = {**record, **change}
            query = envelope(adapter="manual_web", version="import-v1", capability="import", host="example.com",
                             projection={"content_type": "application/json", "records": [bad]})
            failed = ManualWebAdapter(("example.com",)).search(query)
            self.assertEqual(failed.failure.kind, kind)
            self.assertEqual(failed.records, ())


if __name__ == "__main__":
    unittest.main()
