---
name: research
description: Run bounded local research and finalist-specific retrieval without fabricating evidence or broadening egress.
---

# Research workflow

This workflow covers both bounded evidence gathering (`/research`) and finalist-specific
retrieval (`/audit`).

## Inputs & where they live

Local source files (fixtures, manual results, the audit fixture manifest) live under
`documents/`; versioned request objects (e.g. `audit-query-input-v1.json`,
`feature-map-set-input-v1.json`) live under `workspace/requests/`. Templates are in
`documents/README.md` and `workspace/README.md`.

Create the private workflow run only with `python3 -m patent_factory run start ...`; it
binds the authoritative profile and enters `research_ready`. Use only
`python3 -m patent_factory research ...` and `python3 -m patent_factory audit retrieve ...`
for retrieval state changes. Pass paths and bounded query projections; never pass the
whole profile to an adapter. Fixture research is the offline acceptance path. Manual
imports must be user supplied, HTTPS-derived, size bounded, and restricted to explicit
`--allow-host` values.

## Out-of-band web deep research (Google Patents · Naver · arXiv · Papers with Code · GitHub)

The CLI itself never fetches the open web. The driving agent performs the search
out-of-band with its own web tools, then hands the results to the core through the
offline normalizer:

1. Derive Korean AND English keyword combinations from the profile domain and each
   candidate topic (synonyms, classifications, applicant names, discovered terms).
   Never put private profile text beyond approved technical keywords into a search
   query.
2. Search each source in both languages; collect only public metadata per hit:
   `url`, `title`, `identifier` (arXiv id, patent number, repo, …), optional
   `abstract`/`excerpts`/`limitations`/`language`.
3. Save the rows as a `web-rows-v1` JSON under `documents/`, then run
   `python3 -m patent_factory research normalize-web ROWS --out documents/normalized.json
   --allow-host HOST … --source-type arxiv|google_patents|naver|papers_with_code|github|web`
   — a pure offline transform that computes the span/content hashes and enforces the
   HTTPS allowlist.
4. Import with `research manual documents/normalized.json --allow-host HOST …`.
   The source tag is preserved as evidence `provenance`.

Repeat per source; each import is one bounded operation with its own adapter event.

## Rules

Treat each stdout JSON object as authoritative command output. Preserve query,
adapter/version, retrieval time, result/failure, stable evidence IDs, and coverage
limitations. A source failure is an adapter event, never evidence. Never invent fallback
records, silently change query/corpus budgets, scrape an unapproved host, or edit
research exports/SQLite.

Stop on `credential_required`, `research_incomplete`, `coverage_insufficient`, any other
`*_required`, `stopped`, or `error`. A pending/rejected credential or paid-service gate
permits zero network requests. Only a user-provided current `gate-decision-input-v1` may
resolve the exact gate; this skill does not authorize credentials, paid services,
hosted-model egress, or a broader data scope.

## Re-entry after `/checkpoint`

A `re_research` checkpoint decision re-enters this stage for one bounded second
pass. Offline verbs (`fixture` / `normalize-web` + `manual`) run as before. Live
`research kipris` / `research serpapi`
([issue #48](https://github.com/skykongkong8/ai-patent-factory/issues/48)) is
force-gated on this route: even with the key in env the verb suspends with
`credential_required`, and only the USER may approve that gate — its scope shows
the resolution's `plan_hash`, `needed_research`, and the literal second-pass
terms; a plan-unbound or plan-mismatched re-entry is refused outright. The
force-gate covers EVERY attempt of that second pass, retries included: it is
keyed on the live `re_research` resolution, not on the run's current state, so
an approved attempt that ends `research_incomplete` does not fall out of the
guard — the retry is force-gated with its own plan-bound scope, or, from a
state that cannot legally carry a credential gate, refused before any egress. A
plain rerun advances to a fresh attempt key of its own, so it publishes its own
bundle instead of inheriting the previous attempt's — still force-gated, still
salted. `--idempotency-key` pins the coordinate and switches that advance off,
so pinning an already-published one is refused before any egress with
`live_research_reentry_spent_coordinate_issue_48`. This
skill still never runs live/credentialed commands itself. Read the
decided `plan.needed_research` via
`run show --run RUN --run-id RUN_ID --kind gate_resolution` while it is still current,
or the durable `<run>/decision-exports/ar_<revision_id>.json` file once the first
`research` publish after the decision has invalidated it (`run show`'s `ar.stale=0`
filter can no longer find it then; the exported file is unaffected since staleness
only touches `artifact_revisions`/`current_artifacts` rows). Target the offline import
at those terms. See `.claude/skills/checkpoint/SKILL.md`.

## Next

After `/research` reaches `research_complete`, continue with `/ideate`. After
`/audit` scoring, every batch — clean or breaching — stops at the always-raised
`post_audit_checkpoint` gate; continue with `/checkpoint`, which resolves to `/draft`
(`approve`), back to `/ideate` (`re_ideate`), back to this stage (`re_research`), or a
stop.
