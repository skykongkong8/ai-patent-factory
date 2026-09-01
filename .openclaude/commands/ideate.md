---
description: Run the divergence-first ideation workbench, then persist promoted candidates.
---

# /ideate - propose candidates (step 3)

Turn research evidence into a divergence-first creative workbench, validate each
stage, then publish 1 to 12 promoted, evidence-bound `ca_*` candidates through
the existing JSON CLI. Target at least 3 when the evidence supports a real
shortlist. Follow the installed `ideation` skill. Do not load
private source, profile, evidence spans, or workbench briefs into hosted model
context without a current exact egress approval.

## Where work happens

Use a run-local workbench under `workspace/requests/ideation/RUN_ID/`:

```text
workspace/requests/ideation/RUN_ID/
  brief-v1.json
  history/
  sessions/SESSION_ID/
    ideas.jsonl
    relations.jsonl
    clusters-v1.json
  promoted/
    candidate-input-v1.json
    lineage-v1.json
  notes.md
```

Workbench notes, raw idea nodes, relations, and clusters are non-authoritative.
They help you diverge, combine, and select. Only the final `ideate` CLI call
publishes candidate state.

## Steps

Execute exactly one unfinished stage per invocation. Return the command result
and stop at its checkpoint; resume the next stage in a fresh invocation.

0. Scaffold the minimized, hash-bound workbench brief.

```bash
python3 -m patent_factory scaffold ideation-workbench \
  --run RUN --run-id RUN_ID \
  --profile-database PROFILE_DATABASE \
  --out workspace/requests/ideation/RUN_ID/brief-v1.json
```

   The private brief deliberately contains identifiers and hashes, not profile
   values, research titles/interpretations, evidence text, or checkpoint feedback
   prose. It is not authorization to send private semantics to Claude. Generate
   only from user-supplied/public material or a separately approved minimized
   egress payload; otherwise stop before hosted generation.

1. Create independent divergence sessions under `sessions/`. Each `ideas.jsonl`
   row is an `idea-node-v1` with a workbench-local idea ID, the session ID, the
   exact brief hash, lens, technical problem, rough mechanism, validation path,
   limitations, and optional evidence/profile references. Do not use `ca_*`
   IDs for raw ideas.

2. Validate the divergence stage before combining ideas.

```bash
python3 -m patent_factory scaffold ideation-workbench \
  --run RUN --run-id RUN_ID \
  --profile-database PROFILE_DATABASE \
  --validate workspace/requests/ideation/RUN_ID \
  --stage diverge
```

3. Author `relations.jsonl` and `clusters-v1.json`. Relations may derive,
   combine, contrast, revise, or park ideas. Keep references known, acyclic for
   derivation-style edges, and do not treat promotion as a relation.

4. Validate the entangle stage.

```bash
python3 -m patent_factory scaffold ideation-workbench \
  --run RUN --run-id RUN_ID \
  --profile-database PROFILE_DATABASE \
  --validate workspace/requests/ideation/RUN_ID \
  --stage entangle
```

5. Choose a pool size from 1 to 12 and scaffold the promoted candidate input.

```bash
python3 -m patent_factory scaffold candidate \
  --run RUN --run-id RUN_ID \
  --profile-database PROFILE_DATABASE \
  --count COUNT \
  --out workspace/requests/ideation/RUN_ID/promoted/candidate-input-v1.json
```

   Fill `promoted/candidate-input-v1.json`, then author
   `promoted/lineage-v1.json`. The candidate input must have 1 to 12 candidates,
   no `TODO(agent)` markers, and valid evidence/profile bindings. The lineage
   file binds each zero-based candidate index to source idea IDs, session IDs,
   relevant relation IDs, and the canonical candidate input hash produced by
   `patent_factory.provenance.digest` over the filled JSON object. Counts 1 or 2
   are valid but necessarily lead `/shortlist` to explicit insufficient evidence.

6. Validate promotion before publishing.

```bash
python3 -m patent_factory scaffold ideation-workbench \
  --run RUN --run-id RUN_ID \
  --profile-database PROFILE_DATABASE \
  --validate workspace/requests/ideation/RUN_ID \
  --stage promote
```

   Validation advisories do not block publication. Review `single_lens_workbench`,
   `no_cross_session_relation`, and `single_synthesis_method` before continuing;
   vary the promoted pool when the warning reflects accidental monoculture.

7. Publish the promoted candidates with the existing authoritative verb.

```bash
python3 -m patent_factory ideate \
  --run RUN --run-id RUN_ID \
  --profile PROFILE \
  --profile-database PROFILE_DATABASE \
  --input workspace/requests/ideation/RUN_ID/promoted/candidate-input-v1.json
```

8. Report the stdout JSON `status`/`next_state` verbatim. On success, suggest
   the next step: **`/shortlist`**.

## Stop conditions

- Stop on `domain_pivot_required`, `insufficient_evidence`, any other
  `*_required`, `stopped`, or `error`. Preserve IDs, hashes, and gate details.
- Workbench validation errors exit before publishing and do not authorize repair
  by guessing. Fix the workbench file that failed and re-run the same validation.
- A `re_ideate` checkpoint decision seeds the next workbench brief with the
  resolution hash, per-finalist feedback hashes, and finalist/candidate IDs. It
  does not copy feedback prose across the private boundary. Re-submitting
  byte-identical promoted candidate content is a hard `StateError`, not a silent replay.
- Never copy candidate JSON or an export to imitate a state transition.
