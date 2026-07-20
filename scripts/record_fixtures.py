"""Opt-in LIVE fixture recorder — turn real service responses into test oracles.

Every adapter fixture in this repo but one was hand-authored, and two live-path
defects (#38, #39) shipped behind a suite that validated an invented XML shape.
The enabling cause was mechanical: nothing could record. `live_kipris_smoke.py`
drives the CLI by subprocess and never sees a response body;
`check_serpapi_quota.py` only hits the account endpoint. This script closes that
gap by calling the adapters directly through `recording_transport`.

Coverage is deliberately ADVERSARIAL, not merely genuine. A recorded happy-path
body satisfies "≥1 real response per capability" while certifying nothing: #39
was itself a happy-path-shaped bug. So this records empty result sets, error
envelopes, and paginated bodies as well.

Requires an explicit --confirm-live flag plus the relevant credential. Recorded
bytes are credential-scrubbed and have volatile fields pinned, so the output is
byte-stable on replay and safe to commit.

Usage:
    set -a && . ./.env && set +a
    PYTHONPATH=src python3 scripts/record_fixtures.py --confirm-live --target kipris
    PYTHONPATH=src python3 scripts/record_fixtures.py --confirm-live --target serpapi
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patent_factory.adapters.base import recording_transport  # noqa: E402
from patent_factory.adapters.google_patents import GooglePatentsAdapter  # noqa: E402
from patent_factory.adapters.kipris import KiprisAdapter  # noqa: E402
from patent_factory.models import QueryEnvelope  # noqa: E402
from patent_factory.privacy import credential_canaries, environment_secret  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

# Volatile server-side fields, pinned so a recording replays byte-identically.
KIPRIS_PINS = (
    (rb"<responseTime>[^<]*</responseTime>", b"<responseTime>2026-07-20 00:00:00.0000</responseTime>"),
)
SERPAPI_PINS = (
    (rb'"(processed_at|created_at|total_time_taken|time_taken_displayed|raw_html_file|json_endpoint)":\s*[^,}]+',
     rb'"\1": "PINNED"'),
    (rb'"id":\s*"[0-9a-f]{16}"', b'"id": "PINNED"'),
)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def kipris_envelope(capability: str, projection: dict, *, result_budget: int = 10) -> QueryEnvelope:
    return QueryEnvelope(
        run_id="record", adapter="kipris", adapter_version="plus-xml-v1",
        capability=capability, allowed_scheme="https", allowed_host="plus.kipris.or.kr",
        deadline_seconds=15, page=1, page_cap=5, result_budget=result_budget,
        byte_budget=1_000_000, retry_budget=0, retry_ownership="recorder",
        query_projection=projection,
    )


def record_kipris(args) -> list[dict]:
    key = environment_secret("KIPRIS_PLUS_API_KEY")
    if not key:
        raise SystemExit("KIPRIS_PLUS_API_KEY is not exported; run: set -a && . ./.env && set +a")
    canaries = credential_canaries()
    targets = [
        # (fixture name, capability, projection, what shape this is meant to prove)
        ("bibliography-summary-live-v1.xml", "bibliography_summary",
         {"application_number": args.application_number}, "bibliography_summary happy path"),
        ("word-search-live-empty-v1.xml", "word_search",
         {"word": args.empty_term, "year": 0, "patent": True, "utility": True},
         "empty result set"),
    ]
    recorded = []
    for name, capability, projection, purpose in targets:
        destination = FIXTURES / "kipris" / name
        adapter = KiprisAdapter(
            key,
            transport=recording_transport(
                KiprisAdapter(key)._transport, destination, canaries=canaries, pins=KIPRIS_PINS,
            ),
        )
        result = adapter.search(kipris_envelope(capability, projection))
        recorded.append({
            "capability": capability,
            "failure": result.failure.kind.value if result.failure else None,
            "fixture": str(destination.relative_to(ROOT)),
            "purpose": purpose,
            "records": len(result.records),
            "bytes": destination.stat().st_size if destination.exists() else 0,
        })
    return recorded


def record_serpapi(args) -> list[dict]:
    key = environment_secret("SERPAPI_API_KEY")
    if not key:
        raise SystemExit("SERPAPI_API_KEY is not exported; run: set -a && . ./.env && set +a")
    canaries = credential_canaries()
    targets = [
        ("organic-results-live-v1.json", {"query": args.query, "page": 1},
         "google_patents happy path with real field set"),
        ("organic-results-live-page2-v1.json", {"query": args.query, "page": 2},
         "paginated body — exercises has_more/next_cursor, which has zero positive coverage"),
    ]
    recorded = []
    for name, projection, purpose in targets:
        destination = FIXTURES / "google_patents" / name
        base = GooglePatentsAdapter(key)
        adapter = GooglePatentsAdapter(
            key,
            transport=recording_transport(
                base._transport, destination, canaries=canaries, pins=SERPAPI_PINS,
            ),
        )
        envelope = QueryEnvelope(
            run_id="record", adapter=base.name, adapter_version=base.version,
            capability="word_search", allowed_scheme="https", allowed_host="serpapi.com",
            deadline_seconds=20, page=projection["page"], page_cap=5, result_budget=10,
            byte_budget=2_000_000, retry_budget=0, retry_ownership="recorder",
            # The adapter's projection key is "word", not "query"
            # (google_patents.py:217). Anything else is rejected before egress.
            query_projection={"word": projection["query"]},
        )
        result = adapter.search(envelope)
        recorded.append({
            "failure": result.failure.kind.value if result.failure else None,
            "fixture": str(destination.relative_to(ROOT)),
            "purpose": purpose,
            "records": len(result.records),
            "searches_spent": 1,
            "bytes": destination.stat().st_size if destination.exists() else 0,
        })
    return recorded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true", required=True)
    parser.add_argument("--target", choices=("kipris", "serpapi"), required=True)
    parser.add_argument("--application-number", default="1020160062884",
                        help="a real number already present in the recorded word-search fixture")
    parser.add_argument("--empty-term", default="zzzqxwv존재하지않는검색어zzz",
                        help="a term expected to return zero results")
    parser.add_argument("--query", default="on-device neural network quantization")
    args = parser.parse_args()
    recorded = record_kipris(args) if args.target == "kipris" else record_serpapi(args)
    emit({"recorded": recorded, "status": "recorded", "target": args.target})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
