---
description: Persist three finalists (three axes each) or explicit insufficient evidence.
---

# /shortlist — pick finalists (step 4)

Select three defensible finalists from the candidates. Follow
`.claude/skills/ideation/SKILL.md` and pass only a reviewed `shortlist-input-v1`.

## Where you provide input

Author `workspace/requests/shortlist-input-v1.json` (template in `workspace/README.md`).
Each finalist needs the three independent axes — `differentiation`,
`technical_feasibility`, `utility_significance` — with score, rationale, confidence,
supporting and contrary evidence references, coverage assessment, and gaps.

## Steps

0. Help the user assemble `shortlist-input-v1` from the candidate output.
1. Run the CLI verb.

```bash
python3 -m patent_factory shortlist --run RUN --run-id RUN_ID --input SHORTLIST_INPUT
```

2. Report the stdout JSON `status`/`next_state` verbatim.
3. On success, suggest the next step — **`/audit`** to score similarity risk per
   finalist.

## Stop conditions (do not bypass)

- If `status` is `insufficient_evidence`, stop — do not manufacture weak finalists;
  preserve the insufficiency instead.
- `*_required`, `stopped`, and `error` are not permission to auto-proceed.
- Never directly edit SQLite, the candidate/finalist exports, or a state pointer.
