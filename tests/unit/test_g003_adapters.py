import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KIPRIS_HOST, KiprisAdapter, _default_transport
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
    def test_default_transport_never_follows_http_redirect(self):
        hits = {"redirect": 0, "sink": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    hits["redirect"] += 1
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/sink")
                    self.end_headers()
                else:
                    hits["sink"] += 1
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"sink")

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as captured:
                _default_transport(
                    f"http://127.0.0.1:{server.server_port}/redirect", 2, 1_000,
                )
            self.assertEqual(captured.exception.code, 302)
            self.assertEqual(hits, {"redirect": 1, "sink": 0})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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
            b"<response><body><totalCount>0</totalCount></body></response>",
            b"<response><successYN>Y</successYN></response>",
            b"<response><successYN>Y</successYN><body><totalCount>NaN</totalCount><numOfRows>1</numOfRows><pageNo>1</pageNo></body></response>",
            b"<response><successYN>Y</successYN><body><totalCount>0</totalCount><numOfRows>1</numOfRows><pageNo>1</pageNo></body></response>",
        )
        expected = (
            AdapterFailureKind.AUTH, AdapterFailureKind.MALFORMED, AdapterFailureKind.MALFORMED,
            AdapterFailureKind.MALFORMED, AdapterFailureKind.MALFORMED, AdapterFailureKind.MALFORMED,
            AdapterFailureKind.MALFORMED,
        )
        for body, kind in zip(payloads, expected):
            with self.subTest(kind=kind):
                adapter = KiprisAdapter("secret", transport=lambda *_: TransportResponse(200, {}, body))
                result = adapter.search(envelope())
                self.assertEqual(result.failure.kind, kind)
                self.assertEqual(result.records, ())

    def test_explicit_empty_success_and_rate_limit_metadata_are_preserved(self):
        body = (b"<response><successYN>Y</successYN><body><items/>"
                b"<numOfRows>30</numOfRows><pageNo>1</pageNo><totalCount>0</totalCount>"
                b"</body></response>")
        result = KiprisAdapter(
            "secret",
            transport=lambda *_: TransportResponse(
                200, {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "49"}, body,
            ),
        ).search(envelope())
        self.assertTrue(result.successful)
        self.assertEqual(result.records, ())
        self.assertEqual(result.rate_limit, {"limit": "50", "remaining": "49"})

    def test_redirected_final_target_is_rejected(self):
        body = Path("tests/fixtures/kipris/word-search-v1.xml").read_bytes()
        result = KiprisAdapter(
            "secret",
            transport=lambda *_: TransportResponse(200, {}, body, final_url="https://example.com/stolen"),
        ).search(envelope())
        self.assertEqual(result.failure.kind, AdapterFailureKind.ACCESS_DENIED)
        self.assertEqual(result.records, ())
        invalid_port = KiprisAdapter(
            "secret",
            transport=lambda *_: TransportResponse(200, {}, body, final_url="https://plus.kipris.or.kr:bad/x"),
        ).search(envelope())
        self.assertEqual(invalid_port.failure.kind, AdapterFailureKind.ACCESS_DENIED)

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

    def test_confirmed_bibliography_summary_operation_uses_application_number(self):
        body = b"""<response><successYN>Y</successYN><body><items><item>
          <inventionTitle>summary</inventionTitle><applicationNumber>10-2025-0000002</applicationNumber>
        </item></items><numOfRows>1</numOfRows><pageNo>1</pageNo><totalCount>1</totalCount>
        </body></response>"""
        calls = []

        def transport(url, *_):
            calls.append(url)
            return TransportResponse(200, {"Content-Type": "application/xml"}, body)

        query = envelope(
            capability="bibliography_summary",
            projection={"application_number": "10-2025-0000002"},
        )
        result = KiprisAdapter("secret", transport=transport).search(query)
        self.assertTrue(result.successful)
        self.assertEqual(result.records[0].source_locator, "kr-patent:1020250000002")
        self.assertIn("getBibliographySumryInfoSearch", calls[0])
        self.assertIn("applicationNumber=10-2025-0000002", calls[0])

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

    def test_closed_schema_hash_normalization_and_explicit_provenance(self):
        base = {
            "canonical_url": "https://example.com/public/1", "identifier": "public-1",
            "title": "public", "content_hash": "A" * 64, "language": "ko",
            "provenance": "user_import", "limitations": [],
        }
        good = envelope(
            adapter="manual_web", version="import-v1", capability="import", host="example.com",
            projection={"content_type": "application/json", "records": [base]},
        )
        result = ManualWebAdapter(("example.com",)).search(good)
        self.assertTrue(result.successful)
        self.assertEqual(result.records[0].content_hash, "a" * 64)
        self.assertEqual(result.records[0].provenance, "user_import")

        for changed in ({**base, "raw_document": "MANUAL-PRIVATE-CANARY"},
                        {**base, "content_hash": "not-a-sha256"}):
            with self.subTest(fields=tuple(changed)):
                query = envelope(
                    adapter="manual_web", version="import-v1", capability="import", host="example.com",
                    projection={"content_type": "application/json", "records": [changed]},
                )
                failed = ManualWebAdapter(("example.com",)).search(query)
                self.assertEqual(failed.failure.kind, AdapterFailureKind.MALFORMED)
                self.assertNotIn("MANUAL-PRIVATE-CANARY", repr(failed))

    def test_result_budget_excess_fails_instead_of_silently_truncating(self):
        records = [
            {
                "canonical_url": f"https://example.com/public/{index}", "identifier": f"public-{index}",
                "title": f"public {index}", "content_hash": "a" * 64, "language": "ko",
                "provenance": "user_import", "limitations": [],
            }
            for index in range(3)
        ]
        exceeded = envelope(
            adapter="manual_web", version="import-v1", capability="import", host="example.com",
            projection={"content_type": "application/json", "records": records}, result_budget=2,
        )
        failed = ManualWebAdapter(("example.com",)).search(exceeded)
        self.assertEqual(failed.failure.kind, AdapterFailureKind.OVERSIZE)
        self.assertEqual(failed.records, ())
        self.assertIn("result budget", failed.failure.message)

        within = envelope(
            adapter="manual_web", version="import-v1", capability="import", host="example.com",
            projection={"content_type": "application/json", "records": records}, result_budget=3,
        )
        result = ManualWebAdapter(("example.com",)).search(within)
        self.assertTrue(result.successful)
        self.assertEqual(len(result.records), 3)
        self.assertEqual(result.coverage["received"], 3)
        self.assertEqual(result.coverage["usable"], 3)


if __name__ == "__main__":
    unittest.main()
