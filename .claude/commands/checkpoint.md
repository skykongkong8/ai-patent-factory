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
carries every finalist's frozen audit verdict (`r_hi`, `r_obs`, `closest_reference_id`,
`counterargument`, `outcome`) — it cannot change while the gate is open.

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
     `r_obs`, `closest_reference_id`, `counterargument`, `outcome`. When `outcome` is
     `coverage_insufficient`, `closest_reference_id` is `null` — render "no closest
     reference within the retrieved corpus (coverage insufficient)"; never print a null
     id or invent a reference.

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
every per-finalist `feedback`, and (only on `approve` with breaching finalists) each
`retain_with_warning` entry's `warning`.

4. Report the stdout JSON `status`/`next_state` verbatim, then branch:

   - **`approve` → `next_state: audit_approved`** — continue to **`/draft`**.
   - **`re_ideate` → `next_state: ideation_running`** — continue to **`/ideate`**; the
     skill must read this decision's `feedback` from the decision-set export and author
     genuinely different candidates (drop/deprioritize the "boring" directions, extend
     the "interesting" ones). Re-authoring byte-identical candidates does not raise an
     error — it silently replays the stale ideation context instead of producing
     anything new.
   - **`re_research` → `next_state: research_running`** — continue to **`/research`**,
     offline only (`research fixture` / `research normalize-web` + `research manual`);
     author the second pass from the decided `plan.needed_research`. Live
     `research kipris`/`research serpapi` on this second pass is out of scope, deferred
     to [issue #48](https://github.com/skykongkong8/ai-patent-factory/issues/48).
   - **`stop` → `next_state: stopped`** — the run ends here.

## Stop conditions (do not bypass)

- Never auto-decide, time out, or auto-approve. This command only scaffolds clerical
  bindings; every judgment field stays `TODO(agent)` until the user fills it in.
- On `approve` with any breaching finalist (`outcome: decision_required`), `decisions`
  needs exactly one `retain_with_warning` entry per breaching finalist with a
  non-empty `warning`. Every other action (`re_ideate`/`re_research`/`stop`) keeps
  `decisions` empty — the per-finalist signal for those branches lives only in
  `feedback`.
- No patentability, novelty, validity, or non-infringement/FTO conclusion anywhere in
  the dossier.
- Do not edit the decision-set export or SQLite, and do not fabricate a `gate_id` or
  `decision_id`.
