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

Never directly edit candidate/finalist/report exports or SQLite. Stop on domain pivot,
evidence, coverage, excessive-risk, disclosure, revision, and other gates. This skill may
recommend but never make the user's pivot or excessive-risk decision. Do not load private
inputs into hosted context without a current exact egress approval and minimized
manifest.

## Next

`/ideate` → `/shortlist` → (`/audit`) → `/draft`. After a persisted shortlist, the next
step is `/audit`; after an approved audit, `/draft`.
