---
description: Validate and persist three or more evidence-bound candidate proposals.
---

# /ideate — propose candidates (step 3)

Turn research evidence into ≥3 evidence-bound candidate proposals. Follow
`.claude/skills/ideation/SKILL.md`. Do not load private source into model context
without a current exact egress approval.

## Where you provide input

Author `workspace/requests/candidate-input-v1.json` (template and field notes in
`workspace/README.md`). Each candidate traces to profile facts and research evidence and
preserves all six epistemic labels; `agent_inference` needs a `rationale` and must not
read as fact. IDs and hashes are copied from earlier outputs, never invented.

## Steps

0. Start from a hash-bound draft — `scaffold candidate` pre-fills every evidence
   binding (IDs, content/span hashes, profile references) so you only author the
   inventive prose; the result JSON includes the evidence hash table for label
   upgrades. Never fake a binding.

```bash
python3 -m patent_factory scaffold candidate --run RUN --run-id RUN_ID \
  --out workspace/requests/candidate-input-v1.json
```
1. Run the CLI verb.

```bash
python3 -m patent_factory ideate --run RUN --run-id RUN_ID --profile PROFILE --profile-database PROFILE_DATABASE --input CANDIDATE_INPUT
```

2. Report the stdout JSON `status`/`next_state` verbatim.
3. On success, suggest the next step — **`/shortlist`** to pick three finalists.

## Stop conditions (do not bypass)

- Stop on `domain_pivot_required`, `insufficient_evidence`, any other `*_required`,
  `stopped`, or `error`. Preserve `gate_id`, but do not approve a pivot or create a
  `decision_id`.
- Resume only after the user completes a decision for the exact current topic, using
  the same input and the core-issued `--decision-id`.
- Do not copy candidate JSON or an export to imitate a state transition.
