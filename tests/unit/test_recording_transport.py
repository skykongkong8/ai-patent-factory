"""`recording_transport` — turn a live call into a fixture, safely.

Recording must happen at the transport boundary: it is the only place the raw
response bytes exist. `scripts/live_kipris_smoke.py` drives the CLI by subprocess
and never sees a body; `scripts/check_serpapi_quota.py` only hits the account
endpoint. That gap is why every adapter fixture in this repo but one was
hand-authored, and why two live-path defects shipped behind a green suite.
"""

import tempfile
import unittest
from pathlib import Path

from patent_factory.adapters.base import TransportResponse, recording_transport

LIVE_BODY = (
    b"<response><header><responseTime>2026-07-20 11:22:33.4444</responseTime>"
    b"</header><body><items/></body><count><totalCount>7</totalCount></count></response>"
)


def transport_returning(body, calls=None):
    def transport(url, timeout, byte_budget):
        if calls is not None:
            calls.append((url, timeout, byte_budget))
        return TransportResponse(200, {"Content-Type": "application/xml"}, body)

    return transport


class RecordingTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.destination = Path(self.temporary.name) / "nested" / "recorded.xml"

    def tearDown(self):
        self.temporary.cleanup()

    def test_records_the_exact_response_bytes(self):
        recording_transport(transport_returning(LIVE_BODY), self.destination)("https://x", 1.0, 10_000)
        self.assertEqual(self.destination.read_bytes(), LIVE_BODY)

    def test_response_is_passed_through_unmodified(self):
        # Recording must not perturb the run it observes.
        response = recording_transport(
            transport_returning(LIVE_BODY), self.destination,
            canaries=("secret-key",), pins=((rb"<responseTime>[^<]*</responseTime>", b"<responseTime>PINNED</responseTime>"),),
        )("https://x", 1.0, 10_000)
        self.assertEqual(response.body, LIVE_BODY)
        self.assertEqual(response.status, 200)

    def test_arguments_reach_the_inner_transport(self):
        calls = []
        recording_transport(transport_returning(LIVE_BODY, calls), self.destination)("https://x", 2.5, 999)
        self.assertEqual(calls, [("https://x", 2.5, 999)])

    def test_credentials_are_scrubbed_from_the_recording(self):
        leaky = b"<response><serviceKey>sk-live-abc123</serviceKey></response>"
        recording_transport(
            transport_returning(leaky), self.destination, canaries=("sk-live-abc123",),
        )("https://x", 1.0, 10_000)
        recorded = self.destination.read_bytes()
        self.assertNotIn(b"sk-live-abc123", recorded)
        self.assertIn(b"[REDACTED]", recorded)

    def test_volatile_fields_are_pinned_for_replay_determinism(self):
        recording_transport(
            transport_returning(LIVE_BODY), self.destination,
            pins=((rb"<responseTime>[^<]*</responseTime>", b"<responseTime>2026-07-20 00:00:00.0000</responseTime>"),),
        )("https://x", 1.0, 10_000)
        recorded = self.destination.read_bytes()
        self.assertIn(b"<responseTime>2026-07-20 00:00:00.0000</responseTime>", recorded)
        self.assertNotIn(b"11:22:33", recorded)

    def test_a_credential_that_survives_scrubbing_is_a_hard_error(self):
        # A pin that reintroduces the secret must not silently produce a leaky
        # fixture. Better to lose the recording than to commit a key.
        def sneaky_pin_transport(url, timeout, byte_budget):
            return TransportResponse(200, {}, b"<a>PLACEHOLDER</a>")

        with self.assertRaisesRegex(ValueError, "credential survived scrubbing"):
            recording_transport(
                sneaky_pin_transport, self.destination, canaries=("leak",),
                pins=((rb"PLACEHOLDER", b"leak"),),
            )("https://x", 1.0, 10_000)

    def test_destination_is_owner_only(self):
        recording_transport(transport_returning(LIVE_BODY), self.destination)("https://x", 1.0, 10_000)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)

    def test_works_for_any_adapter_since_the_transport_signature_is_shared(self):
        # KiprisAdapter and GooglePatentsAdapter both take transport=(url, timeout,
        # byte_budget) -> TransportResponse, so one wrapper covers both.
        json_body = b'{"organic_results": []}'
        recording_transport(transport_returning(json_body), self.destination)("https://serpapi.com", 1.0, 10_000)
        self.assertEqual(self.destination.read_bytes(), json_body)


if __name__ == "__main__":
    unittest.main()
