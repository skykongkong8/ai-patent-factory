---
name: ideation
description: Prepare a divergence-first workbench, promoted candidates, shortlist, and report inputs while leaving every transition and gate to the JSON CLI core.
---

# Ideation workflow

This workflow prepares the non-authoritative ideation workbench and the versioned
inputs for `/ideate`, `/shortlist`, and `/draft`.

## Workbench first

Start `/ideate` with `scaffold ideation-workbench`, not by writing
`candidate-input-v1.json` directly. The workbench lives under
`workspace/requests/ideation/RUN_ID/`:

```text
brief-v1.json
history/brief-<hash>.json
sessions/<session-id>/ideas.jsonl
sessions/<session-id>/relations.jsonl
sessions/<session-id>/clusters-v1.json
promoted/candidate-input-v1.json
promoted/lineage-v1.json
notes.md
```

The private brief is a minimized, hash-bound snapshot of the current
profile/research context. It contains claim/evidence identities and hashes, not
profile values, research titles/interpretations, evidence text, or feedback prose.
It therefore does not authorize hosted generation and is not semantically complete
by itself. Use only user-supplied/public material or a separately approved minimized
egress payload for hosted generation; otherwise stop before that transfer. If the
brief content changes, keep the previous body in `history/` and preserve existing
sessions and notes. Raw ideas, relations, clusters, and notes are creative workbench
material only; they are not published candidates and they never substitute for CLI
state.

## Stage validations

Treat each scaffold or validation boundary as one bounded invocation. Return its
JSON result and stop; resume the next unfinished stage in a fresh invocation.

Run the workbench validator at each boundary:

```bash
python3 -m patent_factory scaffold ideation-workbench \
  --run RUN --run-id RUN_ID \
  --profile-database PROFILE_DATABASE \
  --validate workspace/requests/ideation/RUN_ID \
  --stage diverge
python3 -m patent_factory scaffold ideation-workbench \
  --run RUN --run-id RUN_ID \
  --profile-database PROFILE_DATABASE \
  --validate workspace/requests/ideation/RUN_ID \
  --stage entangle
python3 -m patent_factory scaffold ideation-workbench \
  --run RUN --run-id RUN_ID \
  --profile-database PROFILE_DATABASE \
  --validate workspace/requests/ideation/RUN_ID \
  --stage promote
```

`diverge` checks the brief, session bindings, JSONL shape, unique workbench idea
IDs, and optional evidence/profile references. `entangle` additionally checks
relation and cluster references plus relation graph rules. `promote` additionally
checks `promoted/candidate-input-v1.json`, `promoted/lineage-v1.json`, candidate
count, TODO removal, candidate input hash binding, and zero-based candidate-index
lineage coverage.

## Raw ideas vs published candidates

Raw idea IDs are local to the workbench and must not start with `ca_`. A raw idea
may be parked, revised, combined, contrasted, or promoted later. Published
candidate IDs are created only by the Python core when the existing `ideate` verb
accepts the promoted `candidate-input-v1.json`.

Every promoted candidate still needs profile references, evidence references, all
required candidate fields, and a `synthesis_trace`. `synthesis_trace.method` is
exactly one of `modify`, `combine`, `adapt`, `constrain`, or `transfer`; its
evidence IDs must also appear in the candidate's own `evidence_references`.
Treat the trace as a creative-accounting record, not a novelty measurement or a
legal conclusion. Do not use a numeric creative-delta target during divergence;
compare mechanisms, inputs, transformations, outputs, validation approaches, and
evidence neighborhoods as advisory portfolio signals instead. The audit closest
prior-art reference is discovered later, so
`/ideate` cannot bind `synthesis_trace` to the closest reference in advance.

Use `scaffold candidate --count COUNT` to create
`promoted/candidate-input-v1.json`, with `COUNT` from 1 through 12, before filling
it. Target at least three genuinely supported candidates; counts one or two remain
valid and deliberately lead the shortlist scaffold toward explicit insufficiency.
Bind `lineage-v1.json` to the canonical `patent_factory.provenance.digest` of the
filled candidate input object, not to its raw file bytes.

## Shortlist and later inputs

After `ideate` publishes candidates, use `scaffold shortlist` and then `/shortlist`.
The shortlist contains finalists and exclusions only; raw workbench ideas and raw
session dispositions do not belong in the shortlist. Each finalist needs the three
fixed axes with score, rationale, confidence, supporting/contrary evidence, gaps,
and coverage limitations. If three defensible finalists are unavailable, preserve
`insufficient_evidence` and stop.

## Re-entry after `/checkpoint`

A `re_ideate` checkpoint decision re-enters this stage. Run
`scaffold ideation-workbench --out .../brief-v1.json` before any new ideate publish;
the scaffold copies the current resolution hash, per-finalist feedback hashes, and
finalist/candidate IDs into the new brief while the resolution is current. It does
not copy `interesting`/`boring` prose. Do not inspect or copy immutable exports to
reconstruct that seed. Re-submitting byte-identical promoted candidate content raises a hard
`StateError`; revise the promoted input before re-running `/ideate`.

## Privacy and authority

Never directly edit candidate/finalist/report exports or SQLite. Stop on domain
pivot, evidence, coverage, excessive-risk, disclosure, revision, and other gates.
This skill may recommend but never make the user's pivot, disclosure, checkpoint,
or excessive-risk decision. Do not load private inputs, profile facts, workbench
briefs, evidence spans, reports, secrets, or canaries into hosted context without a
current exact egress approval and minimized manifest.

## Next

`/ideate` -> `/shortlist` -> `/audit` -> `/checkpoint` -> `/draft`. After a
persisted shortlist, the next step is `/audit`; after `/checkpoint` resolves with
`approve`, the next step is `/draft`.
