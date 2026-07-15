---
description: Render the private Korean 11-section report with citation and decision bindings.
---

# /draft — render the report (step 6)

Render the private Korean 11-section report. Follow `CLAUDE.md`, `AGENTS.md`, and
`.claude/skills/ideation/SKILL.md`. Pass only a `report-input-v1` bound to the current
approved artifact hash.

## Where you provide input

Author `workspace/requests/report-input-v1.json` (template in `workspace/README.md`).
The core renders the eleven Korean sections and the citation/decision bindings — this
wrapper never writes or edits `draft.md` or the report export.

## Steps

0. Help the user assemble `report-input-v1` (drafter identity, `report_date`, profile
   fields, handoff questions).
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
