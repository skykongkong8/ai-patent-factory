---
description: Independent reviewer pass, deterministic validate, and guarded external share.
---

# /review — independent review, validate & share (step 7)

Run an independent reviewer pass, then deterministic validation, then (optionally) a
guarded external share. Follow `.claude/skills/patent-review/SKILL.md`. The reviewer's
identity and pass must differ from the drafter's.

## Where you provide input

- `workspace/requests/review-input-v1.json` — reviewer identity ≠ drafter; `report_hash`
  from the `/draft` output; all seven checks present.
- `workspace/requests/external-report-share-v1.json` — only if sharing externally.

Templates are in `workspace/README.md`.

## Steps

0. In a separate reviewer pass, help assemble `review-input-v1` against the exact report
   hash.
1. Review, then validate only after the review reports `reviewed`.

```bash
python3 -m patent_factory review --run RUN --run-id RUN_ID --input REVIEW_INPUT
python3 -m patent_factory validate --run RUN --run-id RUN_ID
```

2. Report `status`/`next_state` verbatim; only `validate`'s `complete` counts as done.
3. If the user wants to share externally, run it as a separate, guarded operation.

```bash
python3 -m patent_factory share --run RUN --run-id RUN_ID --input SHARE_INPUT
```

## Stop conditions (do not bypass)

- If the review is `revision_required`, do not `validate` — stop and return to
  **`/draft`**. Do not bypass any `*_required`, `stopped`, or `error`.
- On `sensitive_disclosure_required`, preserve `gate_id`, `subject_revision_hash`, and
  the exact scope, and stop. Do not choose approve/redact/stop or create an approval.
- Resume `share` only after the user decides with a current `gate-decision-input-v1`
  and the core-issued `decision_id`, by adding `--decision-id` to the same share input.
  Copying a file is not sharing.
