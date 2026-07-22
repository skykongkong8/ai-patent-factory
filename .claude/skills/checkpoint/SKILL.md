---
name: checkpoint
description: Compose the post-audit dossier, elicit one user decision per gate, and resolve the unified post_audit_checkpoint gate without ever deciding for the user.
---

# Checkpoint workflow

This workflow covers `/checkpoint`: the always-raised human decision point after every
`audit score` — clean or breaching — before `/draft`. It subsumes the prior
excessive-similarity gate: per-finalist retain/refine/replace semantics surface inside
it only when a finalist actually breaches (`outcome: decision_required`).

## Inputs & where they live

Author `workspace/requests/gate-decision-input-v2.json` (template and field notes in
`workspace/README.md`). `scaffold gate-decision` pre-fills every hash/ID binding from
the current pending gate (`gate_id`, `subject_revision_hash`, `approval_scope`, and —
when breaches exist — one `retain_with_warning` skeleton per breaching finalist); every
judgment field (`action`, `actor`, `reason`, each finalist's `feedback.interesting` /
`feedback.boring`, and — only for `re_research` — `plan`) is left `TODO(agent)`.
`gate decide`'s **core** validation rejects any surviving `TODO(agent)` marker, so an
unedited draft can never resolve the gate — this is enforced in the CLI, not merely a
scaffold-side convention.

## Dossier composition

Compose one dossier entry per finalist, from CLI exports only — never from memory or
invention:

- **Invention story** — `run show --kind candidate_set`: `technical_problem`,
  `mechanism`, and the `synthesis_trace` narrative + `evidence_ids` (which researched
  mechanisms were combined/adapted, and what the creative delta is).
- **Why it's a finalist** — `run show --kind finalist_set`: the three axis scores
  (`differentiation`, `technical_feasibility`, `utility_significance`) with
  `rationale`, plus `selection_rationale`.
- **Audit verdict** — `gate inspect`'s `approval_scope.finalist_bindings` (one row per
  finalist, already frozen for the life of the pending gate — a gate freezes further
  mutation, so nothing here can go stale mid-review): `r_hi`, `r_obs`, `coverage`,
  `closest_reference_id`, `upper_bound_reference_id`, `counterargument`, `outcome`.
  `closest_reference_id` is `null` ONLY when no retained corpus record exists for the
  finalist at all (render "no reference retained for this finalist" there, never a
  fabricated id). On the ordinary `coverage_insufficient` path (thin coverage, not an
  empty corpus), `closest_reference_id` AND `upper_bound_reference_id` are REAL
  evidence ids: the closest observed reference just stayed below the excessive
  threshold, while `upper_bound_reference_id` (at `coverage`) names the reference that
  keeps coverage too thin to clear. Render both — never claim no reference was found
  when one was.

Similarity and counterargument framing are a research aid only — never a
patentability, novelty, validity, or non-infringement/FTO conclusion.

## Eliciting the decision

Present the full dossier, then elicit exactly one top-level action — `approve`,
`re_ideate`, `re_research`, or `stop` — plus per-finalist `{interesting, boring}`
feedback for all three finalists (required on every action, including a clean
approve). On `re_research`, also elicit a bounded `plan` (what additional research is
needed — offline query terms / needed-research notes). Never choose the action or
invent the feedback on the user's behalf; this skill only scaffolds and explains.

On `approve` with breaching finalists (any `outcome: decision_required`), the
composition rule requires exactly one `{action: retain_with_warning, finalist_id,
reason}` decision per breaching finalist; `re_ideate` / `re_research` / `stop` must
keep `decisions` empty — the per-finalist signal for those branches lives only in
`feedback`. **`warning` is core-derived, never user-authored:** the core attaches a
fixed warning string to the persisted decision automatically — submitting a `warning`
key is rejected outright, since the composition rule accepts only the exact
`{action, finalist_id, reason}` fields.

## Re-entry consumption (feedback and plan are read, not replayed)

`gate decide` only *records* `feedback`/`plan` in the decision-set export; it does not
itself change ideation or research. The re-entered stage must *read* the export and
author input that actually reflects it, or the feedback is recorded but never applied.

**How to read the resolution:** `run show --run RUN --run-id RUN_ID --kind
gate_resolution` while it is still current. The resolution is a DAG descendant of
`audit_batch`, so the FIRST `ideate`/`research` publish after the decision invalidates
it — `run show`'s `ar.stale=0` filter then can no longer find it, including across a
fresh session (nothing here is cached in chat). The durable fallback is the exported
file at `<run>/decision-exports/ar_<revision_id>.json`: `_invalidate_from` only flips
`artifact_revisions`/`current_artifacts` rows, it never touches the exported bytes on
disk, so this file always has the decision, before or after invalidation.

- **`re_ideate` → `/ideate`.** Read the resolved decision's `feedback` (above); author
  a genuinely different `candidate-input-v1.json` — drop or deprioritize the "boring"
  directions, extend the "interesting" ones. Re-authoring byte-identical candidates
  does not raise an error; it silently replays the stale ideation context (the
  upstream artifact the re-branch does not stale), so vary substance, not just
  wording.
- **`re_research` → `/research`.** Read the decided `plan.needed_research` the same
  way; author the offline second pass (`research fixture` / `research normalize-web` +
  `research manual`) targeting those terms. Live `research kipris` / `research serpapi`
  is out of scope for this second pass, deferred to
  [issue #48](https://github.com/skykongkong8/ai-patent-factory/issues/48); the
  one-research-op-per-run policy for the *direct* (non-gate) path is unchanged.

## Rules

Read-only against exports (`run status`, `run show`, `gate inspect`); only
`scaffold gate-decision` and `gate decide` touch state, and only the core validates and
transitions. Never fabricate a `gate_id`, `decision_id`, hash binding, or approval.
Keep drafter ≠ reviewer separation intact — this workflow never runs `/draft`'s or
`/review`'s identity.

## Next

`approve` → `/draft`. `re_ideate` → `/ideate` (re-author from feedback).
`re_research` → `/research` (offline second pass from the plan). `stop` → the run ends.
