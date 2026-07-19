---
description: Retrieve finalist-specific KIPRIS corpora and score simrisk-v1.0.0 risk.
---

# /audit — score similarity risk (step 5)

Retrieve a per-finalist KIPRIS corpus and score similarity risk. Follow
`.claude/skills/research/SKILL.md` and `AGENTS.md`.

## Where you provide input

- `workspace/requests/audit-query-input-v1.json` — one query group per finalist.
- `documents/requests/audit-fixture-manifest-v1.json` — the fixture manifest, under the
  documents root.
- `workspace/requests/feature-map-set-input-v1.json` — reviewed feature maps, for
  scoring.

Templates are in `workspace/README.md`.

## Steps

0. Start from `scaffold audit-query` (the current `finalist_set_hash` and one
   ko+en query pair per finalist pre-filled; you author the search terms), then
   after retrieval help assemble the reviewed feature-map set. Give every
   feature an optional human-readable `description` — the report renders it in
   place of the raw `df_`/`mf_` feature IDs in sections 6 and 8.

```bash
python3 -m patent_factory scaffold audit-query --run RUN --run-id RUN_ID \
  --out workspace/requests/audit-query-input-v1.json
```
1. Retrieve, then score.

```bash
python3 -m patent_factory audit retrieve --run RUN --run-id RUN_ID --query-input AUDIT_QUERY_INPUT --fixture-manifest FIXTURE_MANIFEST
# …or, with KIPRIS_PLUS_API_KEY configured and the user's approval, live retrieval:
python3 -m patent_factory audit retrieve --run RUN --run-id RUN_ID --query-input AUDIT_QUERY_INPUT --live
python3 -m patent_factory audit score --run RUN --run-id RUN_ID --feature-input FEATURE_MAP_SET_INPUT
```

On `status: credential_required` (exit 5), preserve `gate_id` and stop; resume
the exact same request with `--decision-id` after the user decides the gate.

2. Report the stdout JSON `status`/`next_state` and coverage verbatim. The scorer is
   `simrisk-v1.0.0`; do not recompute scores, corpus, feature maps, or labels.
3. On success, suggest the next step — **`/draft`** to render the report.

## Stop conditions (do not bypass)

- Stop immediately on `credential_required`, `coverage_insufficient`,
  `decision_required`, any other `*_required`, `stopped`, or `error`.
- Auto-approval happens only in the core, when coverage is sufficient and `R_hi < 75`.
  Do not make the retain/refine/replace/research/stop decision for the user, and do not
  zero-fill incomplete coverage.
