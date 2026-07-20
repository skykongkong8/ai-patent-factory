---
name: ideation
description: Prepare evidence-bound candidate, shortlist, and report inputs while leaving every transition and gate to the JSON CLI core.
---

# Ideation workflow

This workflow prepares the versioned inputs for `/ideate`, `/shortlist`, and `/draft`.

## Inputs & where they live

Author the versioned request objects under `workspace/requests/`:
`candidate-input-v1.json`, `shortlist-input-v1.json`, and `report-input-v1.json`
(templates and field notes in `workspace/README.md`). They are inputs to `ideate`,
`shortlist`, and `draft`; they are not authoritative state or substitutes for CLI
exports. ID/hash bindings are copied from prior-stage output, never invented.

## Rules

Every candidate traces to a user problem/capability and evidence. Preserve all six
epistemic labels. `agent_inference` requires rationale; hypotheses and creative
suggestions must not become facts. Each finalist needs independent differentiation,
technical-feasibility, and utility-significance axes with score, rationale, confidence,
supporting and contrary evidence, gaps, and coverage limitations. If three defensible
finalists are unavailable, preserve `insufficient_evidence` and stop.

## `synthesis_trace` — the creative delta

Every candidate carries a required `synthesis_trace` object. It is the record of what
you actually contributed on top of the retrieved prior art, and it is the only place
that discipline is captured — there is no separate "novelty delta" field, and you must
not invent one.

- `method` — exactly one of `modify`, `combine`, `adapt`, `constrain`, `transfer`.
  Anything else is rejected.
- `evidence_ids` — one or more evidence IDs, each of which must also appear in the
  candidate's own `evidence_references`. The trace is hash-bound to real retrieved
  documents; it cannot cite something the candidate did not trace.
- `narrative` — prose naming which researched mechanisms were combined/adapted and
  what the delta is. Aim for roughly a **10–30% creative delta**: enough that the
  candidate is not a restatement of a retrieved document, little enough that it stays
  anchored to the evidence.

The 10–30% figure is an **ideation heuristic for how much to invent, not a measurement
and not a novelty claim**. Never write it, or any derived percentage, into a report as
if it were a measured property of the invention. Describe concrete differentiating
features instead.

Note the ordering constraint: `synthesis_trace.evidence_ids` is restricted to
research-phase evidence the candidate already references. The audit's *closest* prior
art is discovered later, during `/audit`, so a trace authored at `/ideate` time cannot
be required to cite it. The report renders that relationship descriptively instead.

Never directly edit candidate/finalist/report exports or SQLite. Stop on domain pivot,
evidence, coverage, excessive-risk, disclosure, revision, and other gates. This skill may
recommend but never make the user's pivot or excessive-risk decision. Do not load private
inputs into hosted context without a current exact egress approval and minimized
manifest.

## Next

`/ideate` → `/shortlist` → (`/audit`) → `/draft`. After a persisted shortlist, the next
step is `/audit`; after an approved audit, `/draft`.
