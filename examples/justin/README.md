# Justin — the golden end-to-end scenario

A redacted, fully public mock inventor ("Justin", an on-device AI researcher)
used as the living integration scenario proposed in issue #23. The e2e journey
test (`tests/e2e/test_full_journey.py`) drives the REAL CLI from `init` through
`profile → run start → research normalize-web + manual → scaffold/ideate →
scaffold/shortlist → scaffold/audit retrieve → audit score → scaffold/draft (en)
→ review → validate` and asserts the rendered English report byte-matches
`expected-report-en.md`.

Regenerate the golden after an intentional renderer change:

    JUSTIN_GOLDEN_REGENERATE=1 PYTHONPATH=src python3 -m unittest tests.e2e.test_full_journey

Assets:
- `background.md` — profile input (`field: value` lines; no personal identifiers)
- `web-rows.json` — `web-rows-v1` public web metadata for `research normalize-web`
- `expected-report-en.md` — the committed golden English report
