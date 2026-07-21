"""Fixture provenance and edge-shape coverage — issue #43, item 5.

Three properties are enforced here:

1. **Completeness** — every file under tests/fixtures is registered. An
   unregistered fixture fails, so a new hand-authored oracle cannot appear
   silently.
2. **Integrity** — each fixture's bytes match a pinned sha256, so a "correction"
   back toward an invented shape is caught. (The structural assertion in
   test_kipris_live_shape.py is KEPT alongside this: a hash fires on every
   legitimate re-record and explains nothing, while the structural test says
   exactly what went wrong.)
3. **Edge-shape coverage** — every network adapter capability covers its
   required shapes, and **each shape is proven by replaying the recorded bytes
   through the real adapter**, never by trusting the manifest's own label.

Property 3 is the one that matters. Origin alone is not enough: a recorded
happy-path body satisfies "one real response per capability" while certifying
nothing, and #39 — pipe-delimited ipcNumber scoring 0.00 instead of 1.00 — was
itself a happy-path-shaped bug. If these predicates read the manifest's `shapes`
list instead of the bytes, this file would be exactly the kind of self-certifying
test that #43 is about.

`manual_web` is excluded from coverage by an explicit predicate, not by
omission: it makes no network call and can never have a recorded response.
"""

import hashlib
import json
import unittest
from pathlib import Path

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.google_patents import GooglePatentsAdapter
from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.models import QueryEnvelope

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))
VALID_ORIGINS = set(MANIFEST["origins"])


def registered():
    return {item["path"]: item for item in MANIFEST["fixtures"]}


def on_disk():
    return {
        str(path.relative_to(FIXTURES))
        for path in FIXTURES.rglob("*")
        if path.is_file() and path.name != "PROVENANCE.json"
    }


def kipris_envelope(capability, projection):
    return QueryEnvelope(
        run_id="provenance", adapter="kipris", adapter_version="plus-xml-v1",
        capability=capability, allowed_scheme="https", allowed_host="plus.kipris.or.kr",
        deadline_seconds=10, page=1, page_cap=5, result_budget=10, byte_budget=2_000_000,
        retry_budget=0, retry_ownership="test", query_projection=projection,
    )


def google_envelope(page=1):
    return QueryEnvelope(
        run_id="provenance", adapter="google_patents", adapter_version="serpapi-v1",
        capability="word_search", allowed_scheme="https", allowed_host="serpapi.com",
        deadline_seconds=15, page=page, page_cap=5, result_budget=10, byte_budget=2_000_000,
        retry_budget=0, retry_ownership="test", query_projection={"word": "quantization"},
    )


def replay(path):
    """Parse a fixture's bytes through its real adapter and return the result."""

    entry = registered()[path]
    body = (FIXTURES / path).read_bytes()
    transport = lambda *_: TransportResponse(200, {}, body)  # noqa: E731
    if entry["adapter"] == "kipris":
        adapter = KiprisAdapter("key", transport=transport)
        projection = (
            {"application_number": "1020160062884"}
            if entry["capability"] == "bibliography_summary"
            else {"word": "x", "year": 0, "patent": True, "utility": True}
        )
        return adapter.search(kipris_envelope(entry["capability"], projection))
    page = 2 if "page2" in path else 1
    return GooglePatentsAdapter("key", transport=transport).search(google_envelope(page))


# Each predicate reads the PARSED RESULT, never the manifest.
SHAPE_PREDICATES = {
    "happy_path": lambda result: bool(result.records) and result.failure is None,
    "empty": lambda result: not result.records and result.failure is None,
    "error": lambda result: result.failure is not None,
    "paginated": lambda result: result.next_cursor is not None,
    "multi_ipc": lambda result: any(len(r.classifications) > 1 for r in result.records),
    "status_variant": lambda result: len({
        r.register_status for r in result.records if r.register_status
    }) >= 2,
}

# `alternate_date_format` cannot be judged from the parsed result: canonical_date
# normalizes every valid date to YYYY-MM-DD, so `result.filing_date` is non-digit
# for the plain 8-digit form too. It must be asserted against the RAW BYTES — the
# fixture has to actually carry a non-8-digit date, which is the whole point of
# recording bibliography_summary (it emits 2016.05.23 where word_search emits
# 20160523). Keyed by path so it reads the file, not the adapter output.
import re as _re  # noqa: E402

_DOTTED_OR_DASHED_DATE = _re.compile(rb"\b\d{4}[.\-/]\d{2}[.\-/]\d{2}\b")
BYTE_SHAPE_PREDICATES = {
    "alternate_date_format": lambda raw: bool(_DOTTED_OR_DASHED_DATE.search(raw)),
}


class FixtureCompletenessTests(unittest.TestCase):
    def test_every_fixture_on_disk_is_registered(self):
        unregistered = on_disk() - set(registered())
        self.assertEqual(unregistered, set(), f"unregistered fixtures: {sorted(unregistered)}")

    def test_every_registered_fixture_exists(self):
        missing = set(registered()) - on_disk()
        self.assertEqual(missing, set(), f"registered but absent: {sorted(missing)}")

    def test_every_entry_declares_a_known_origin_and_a_reason(self):
        for path, entry in registered().items():
            with self.subTest(fixture=path):
                self.assertIn(entry["origin"], VALID_ORIGINS)
                self.assertTrue(entry.get("note"), "every fixture must say why it exists")


class FixtureIntegrityTests(unittest.TestCase):
    def test_recorded_bytes_match_their_pinned_hash(self):
        for path, entry in registered().items():
            with self.subTest(fixture=path):
                actual = hashlib.sha256((FIXTURES / path).read_bytes()).hexdigest()
                self.assertEqual(
                    actual, entry["sha256"],
                    f"{path} changed. If this was a deliberate re-record, update PROVENANCE.json "
                    f"and say so in the note; if not, the oracle just drifted.",
                )


class EdgeShapeCoverageTests(unittest.TestCase):
    """The control that stops a happy-path-only corpus from passing as coverage."""

    def allowlisted(self):
        return {item["key"] for item in MANIFEST["xfail_allowlist"]}

    def test_every_network_capability_covers_its_required_shapes(self):
        covered = {}
        for path, entry in registered().items():
            if not entry.get("adapter") or not entry.get("capability"):
                continue
            key = f"{entry['adapter']}/{entry['capability']}"
            covered.setdefault(key, set()).update(entry.get("shapes", []))
        for key, required in MANIFEST["required_shapes"].items():
            for shape in required:
                with self.subTest(capability=key, shape=shape):
                    if f"{key}:{shape}" in self.allowlisted():
                        self.skipTest("dated xfail; see PROVENANCE.json xfail_allowlist")
                    self.assertIn(
                        shape, covered.get(key, set()),
                        f"{key} has no fixture covering the {shape} shape",
                    )

    def test_declared_shapes_are_proven_by_the_bytes_not_the_label(self):
        # If a manifest entry claims a shape its bytes do not exhibit, that is a
        # mislabelled fixture — the failure mode this whole file exists to stop.
        for path, entry in registered().items():
            for shape in entry.get("shapes", []):
                with self.subTest(fixture=path, shape=shape):
                    if shape in BYTE_SHAPE_PREDICATES:
                        raw = (FIXTURES / path).read_bytes()
                        self.assertTrue(
                            BYTE_SHAPE_PREDICATES[shape](raw),
                            f"{path} claims '{shape}' but its raw bytes do not carry it",
                        )
                        continue
                    predicate = SHAPE_PREDICATES.get(shape)
                    self.assertIsNotNone(predicate, f"no predicate defined for shape {shape}")
                    self.assertTrue(
                        predicate(replay(path)),
                        f"{path} claims shape '{shape}' but its bytes do not exhibit it",
                    )

    def test_every_required_shape_has_an_executable_predicate(self):
        # A required shape with no predicate would be satisfiable by a label alone.
        for required in MANIFEST["required_shapes"].values():
            for shape in required:
                with self.subTest(shape=shape):
                    self.assertTrue(
                        shape in SHAPE_PREDICATES or shape in BYTE_SHAPE_PREDICATES,
                        f"no predicate (parsed or byte-level) defined for shape {shape}",
                    )

    def test_offline_adapters_are_excluded_explicitly(self):
        self.assertIn("manual_web", MANIFEST["offline_adapters"])
        self.assertNotIn("manual_web", MANIFEST["network_adapters"])

    def test_success_path_of_a_network_capability_is_never_certified_by_invented_bytes(self):
        for path, entry in registered().items():
            if "happy_path" in entry.get("shapes", []):
                with self.subTest(fixture=path):
                    self.assertEqual(
                        entry["origin"], "recorded",
                        f"{path} certifies a success path, so it must be a real recording",
                    )

    def test_no_invented_fixture_is_the_sole_success_oracle_for_a_capability(self):
        # The #45 failure was invented bytes being the ONLY thing certifying a
        # live capability. A controlled hand-authored fixture is fine for a
        # targeted unit assertion (a specific multi-assignee join, a locator
        # edge) — but ONLY once a real recording also backs that capability's
        # happy path. This asks the BYTES (does it parse successfully?), then
        # requires a recorded happy_path sibling for the same capability.
        #
        # It closes the `shapes: []` escape the earlier guard missed: a fixture
        # can no longer be an invented success oracle by declaring no shapes,
        # because the recorded-sibling requirement is keyed on what the bytes do,
        # not on what the manifest claims.
        recorded_happy = {
            f"{entry['adapter']}/{entry['capability']}"
            for entry in registered().values()
            if entry.get("adapter") and entry["origin"] == "recorded"
            and "happy_path" in entry.get("shapes", [])
        }
        for path, entry in registered().items():
            if not entry.get("adapter") or not entry.get("capability"):
                continue
            if entry["origin"] in {"recorded", "derived"}:
                continue
            result = replay(path)
            if result.records and result.failure is None:
                key = f"{entry['adapter']}/{entry['capability']}"
                with self.subTest(fixture=path):
                    self.assertIn(
                        key, recorded_happy,
                        f"{path} is an invented fixture that parses successfully, and no "
                        f"recorded happy_path fixture backs {key}: it is the sole success "
                        "oracle, which is exactly the #45 defect.",
                    )

    def test_xfail_allowlist_entries_are_dated_and_reviewable(self):
        for item in MANIFEST["xfail_allowlist"]:
            with self.subTest(key=item["key"]):
                self.assertTrue(item["reason"])
                self.assertTrue(item["added"])
                self.assertTrue(item["review_by"], "an allowlist entry with no review date is permanent")


if __name__ == "__main__":
    unittest.main()
