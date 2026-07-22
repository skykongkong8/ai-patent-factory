---
description: Explain every finalist's dossier after the similarity audit and resolve the always-raised post-audit checkpoint gate.
---

# /checkpoint — human decision after audit (step 6)

Every `audit score` now stops here — clean or breaching — before `/draft`. Follow
`.claude/skills/checkpoint/SKILL.md` and `AGENTS.md`. Re-runnable in a fresh session
while the gate is pending: nothing here is cached in chat, everything is re-read from
the CLI.

## Where you provide input

- `workspace/requests/gate-decision-input-v2.json` — the scaffolded draft you and the
  user complete. Template and field notes are in `workspace/README.md`.

## Steps

0. Confirm the pending gate.

```bash
python3 -m patent_factory run status --run RUN --run-id RUN_ID
python3 -m patent_factory gate inspect --run RUN --run-id RUN_ID --gate-id GATE_ID
```

`gate inspect`'s `kind` must be `post_audit_checkpoint`; its `approval_scope` already
carries every finalist's frozen audit verdict (`r_hi`, `r_obs`, `coverage`,
`closest_reference_id`, `upper_bound_reference_id`, `counterargument`, `outcome`) — it
cannot change while the gate is open.

1. Compose one dossier per finalist, from CLI exports only:

```bash
python3 -m patent_factory run show --run RUN --run-id RUN_ID --kind candidate_set
python3 -m patent_factory run show --run RUN --run-id RUN_ID --kind finalist_set
```

   - **Invention story** (`candidate_set`) — `technical_problem`, `mechanism`, and the
     `synthesis_trace` narrative + `evidence_ids` (what was combined/adapted from
     retrieved prior art, and the creative delta).
   - **Why it's a finalist** (`finalist_set`) — the three axis scores
     (`differentiation`, `technical_feasibility`, `utility_significance`) with
     `rationale`, plus `selection_rationale`.
   - **Audit verdict** (the gate's own `approval_scope.finalist_bindings`) — `r_hi`,
     `r_obs`, `coverage`, `closest_reference_id`, `upper_bound_reference_id`,
     `counterargument`, `outcome`. `closest_reference_id` is `null` ONLY when no
     retained corpus record exists for the finalist at all — render "no reference
     retained for this finalist" in that case, never a fabricated id. On the ordinary
     `coverage_insufficient` path (thin coverage, not an empty corpus),
     `closest_reference_id` AND `upper_bound_reference_id` are REAL evidence ids: the
     closest observed reference just stayed below the excessive threshold, while
     `upper_bound_reference_id` (at `coverage`) names the reference that keeps
     coverage too thin to clear. Render both — never claim "no closest reference
     within the retrieved corpus" when one was in fact found.

   Similarity and counterargument framing are a research aid only — never a
   patentability, novelty, validity, or non-infringement/FTO conclusion.

2. Present the full dossier and elicit **exactly one** top-level decision from the
   user — `approve`, `re_ideate`, `re_research`, or `stop` — plus per-finalist
   `{interesting, boring}` feedback for **all three** finalists (required on every
   action, including a clean approve). On `re_research`, also elicit a bounded `plan`
   (what additional research is needed — offline query terms / needed-research notes).
   Never choose the action or invent feedback on the user's behalf.

3. Scaffold the draft, have the user complete every `TODO(agent)` field, then decide.

```bash
python3 -m patent_factory scaffold gate-decision --run RUN --run-id RUN_ID \
  --gate-id GATE_ID --out workspace/requests/gate-decision-input-v2.json
python3 -m patent_factory gate decide --run RUN --run-id RUN_ID --gate-id GATE_ID \
  --input workspace/requests/gate-decision-input-v2.json
```

`gate decide` itself rejects any surviving `TODO(agent)` marker (core-enforced, not a
scaffold-side convention) — the user must supply/approve `action`, `reason`, `actor`,
every per-finalist `feedback`, and (only on `approve` with breaching finalists) one
`{action: retain_with_warning, finalist_id, reason}` entry per breaching finalist.
**`warning` is core-derived, never user-authored:** the core writes a fixed warning
string onto the persisted decision automatically; submitting a `warning` key in
`decisions[]` is rejected outright (the composition rule accepts only the exact
`{action, finalist_id, reason}` fields).

**`plan` caveat:** the scaffold's `needed_research` TODO string already says this, but
it is easy to miss — `plan` must become `{}` for every action EXCEPT `re_research`,
including `approve` (the most common path). Leaving the scaffolded TODO in place, or
adding real content, both make `gate decide` fail on an otherwise-complete draft.

4. Report the stdout JSON `status`/`next_state` verbatim, then branch:

   - **`approve` → `next_state: audit_approved`** — continue to **`/draft`**.
   - **`re_ideate` → `next_state: ideation_running`** — continue to **`/ideate`**; the
     skill must read this decision's `feedback` — via
     `run show --run RUN --run-id RUN_ID --kind gate_resolution` if it is still
     current, or the durable `<run>/decision-exports/ar_*.json` file once it is not
     (see the note below) — and author genuinely different candidates
     (drop/deprioritize the "boring" directions, extend the "interesting" ones).
     Re-authoring byte-identical candidates does not raise an error — it silently
     replays the stale ideation context instead of producing anything new.
   - **`re_research` → `next_state: research_running`** — continue to **`/research`**,
     offline only (`research fixture` / `research normalize-web` + `research manual`);
     author the second pass from the decided `plan.needed_research`, read the same way
     as `feedback` above. Live `research kipris`/`research serpapi` on this second pass
     is out of scope, deferred to
     [issue #48](https://github.com/skykongkong8/ai-patent-factory/issues/48).

   **Reading the resolution in a fresh session:** the gate resolution is a DAG
   descendant of `audit_batch`, so the FIRST `ideate`/`research` publish after the
   decision invalidates it — `run show`'s `ar.stale=0` filter then can no longer find
   it. The durable fallback is the exported file at
   `<run>/decision-exports/ar_<revision_id>.json`: staleness only updates
   `artifact_revisions`/`current_artifacts` rows, never the exported bytes on disk, so
   this file always has the decision, before or after invalidation.
   - **`stop` → `next_state: stopped`** — the run ends here.

## Stop conditions (do not bypass)

- Never auto-decide, time out, or auto-approve. This command only scaffolds clerical
  bindings; every judgment field stays `TODO(agent)` until the user fills it in.
- On `approve` with any breaching finalist (`outcome: decision_required`), `decisions`
  needs exactly one `{action: retain_with_warning, finalist_id, reason}` entry per
  breaching finalist — never a user-supplied `warning` field; the core derives and
  attaches that string itself. Every other action (`re_ideate`/`re_research`/`stop`)
  keeps `decisions` empty — the per-finalist signal for those branches lives only in
  `feedback`.
- No patentability, novelty, validity, or non-infringement/FTO conclusion anywhere in
  the dossier.
- Do not edit the decision-set export or SQLite, and do not fabricate a `gate_id` or
  `decision_id`.
