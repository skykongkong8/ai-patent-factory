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
- `expected-report-en.md` — the **renderer regression fixture**. Its content is
  deterministic stub text (`agent-completed …`) driven through the real CLI with
  offline fixture research; it exists to catch renderer changes byte-for-byte,
  **not** to demonstrate the product's output. Do not read it as a sample report.
- `live-sample-report-en.md` — the **real sample deliverable**. Produced by an
  actual live run (`research kipris` → agent-authored ideation → `audit retrieve
  --live` → `draft` → `review` → `validate` → `complete`) against the real KIPRIS
  Plus service on 2026-07-20: 154 research records, a 301-record live audit
  corpus over 6 credentialed queries, three genuinely distinct on-device-AI
  finalist ideas each with a populated `synthesis_trace` describing its delta
  over retrieved prior art, and real KIPRIS-reported legal-status tokens
  (공개/등록/소멸/…) rendered verbatim with observation dates. This is what the
  pipeline actually produces; it carries the standard research-aid disclaimers
  and asserts no patentability or novelty conclusion.
