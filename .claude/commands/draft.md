---
description: Render the private 11-section report (English default, Korean optional) with citation and decision bindings.
---

# /draft — render the report (step 6)

Render the private 11-section report in English (default) or Korean. Follow `CLAUDE.md`, `AGENTS.md`, and
`.claude/skills/ideation/SKILL.md`. Pass only a `report-input-v1` bound to the current
approved artifact hash.

## Where you provide input

Author `workspace/requests/report-input-v2.json` (template in `workspace/README.md`).
The report renders in English (`"language": "en"`, the default) or Korean
(`"language": "ko"`); the legacy `report-input-v1` shape is accepted and means
Korean. The core renders the eleven sections and the citation/decision bindings —
this wrapper never writes or edits `draft.md` or the report export.

## Steps

0. Ask which report language the user wants (default English), then start from
   `scaffold report` (renderable profile fields pre-filled; you author the
   drafter identity, date, and questions):

```bash
python3 -m patent_factory scaffold report --language en \
  --out workspace/requests/report-input-v2.json
```
1. Run the CLI verb.

```bash
python3 -m patent_factory draft --run RUN --run-id RUN_ID --input REPORT_INPUT
```

2. Report the stdout JSON `status`/`next_state` verbatim and note the report hash for
   review.
3. On success, suggest the next step — **`/review`** for the independent reviewer pass.

## Stop conditions (do not bypass)

- Stop on unresolved evidence, stale audit/decision, `coverage_insufficient`,
  `decision_required`, any other `*_required`, `stopped`, or `error`.
- Do not add patentability, novelty, validity, or non-infringement/FTO conclusions.
