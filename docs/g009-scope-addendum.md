# G009 scope addendum (2026-07-19)

Records the user decisions and delivered scope of the #22/#23/#24 roadmap.
Supersedes the deep-interview spec where noted; everything else in the spec
remains authoritative.

## Decisions

1. **Report language** — supersedes spec §10 ("Korean is the default"):
   the report renders in **English by default**, Korean optional
   (`report-input-v2.language`, parallel frozen policy/template per language;
   `report-input-v1` remains accepted and means Korean).
2. **Live KIPRIS** — the credentialed adapter is now wired into shipped verbs
   (`research kipris`, `audit retrieve --live`) behind the existing credential
   gate; CI stays fully offline (mock transports); one opt-in redacted live
   smoke exists (`scripts/live_kipris_smoke.py --confirm-live`).
3. **Open-web evidence** — retrieval stays in the driving agent (its own web
   tools), per spec §10's manual-handoff model; the offline
   `research normalize-web` verb computes the hashes and enforces the HTTPS
   allowlist, feeding the existing `research manual` import. No k-skill
   dependency.

## Delivered (#24 priorities)

| Item | Surface |
| --- | --- |
| P1 live KIPRIS | `research kipris`, `audit retrieve --live`, credential gate exit 5 |
| P2 web evidence | `research normalize-web` + agent recipe in `.claude/skills/research` |
| P3 scaffolds | `scaffold {candidate,shortlist,audit-query,report}` |
| P4 feature descriptions | optional hash-bound `description`; renderer fallback to IDs |
| P5 golden e2e | `examples/justin/` + `tests/e2e/test_full_journey.py` (byte-exact golden) |
| P6 quality lint | `quality-lint-v1` advisories on `shortlist` / `audit score` (CLI payload) |
| #23 debug fixes | `_evidence_map` non-destructive; manual-import truncation now fails loudly |

Deliberate deferrals: report-body advisory caveats (would churn the fresh
golden), the P7 prose-synthesis pass (user-approved deferral), and a
`research_complete → research_running` re-entry edge (one research operation
per session; the batch verb covers multi-query needs — revisit only with full
invalidation-DAG analysis; see below).

## Accepted: one research operation per run (2026-07-20)

Confirmed decision, not a placeholder: the user was asked whether to add a
`RESEARCH_COMPLETE → RESEARCH_RUNNING` re-entry edge to
`ALLOWED_TRANSITIONS` (`src/patent_factory/state.py:84`) so a single run could
call more than one research adapter (e.g. `research kipris` then
`research serpapi`). **Decision: do not add it.** `state.py:84` stays exactly
as shipped.

Reasoning: re-entry is not a local edge — a second research pass after
`RESEARCH_COMPLETE` would need to invalidate every artifact derived from the
first pass's research bundle (candidate set, finalist set, audit corpus/scores)
that a driving agent may have already built on, or the pipeline's hash-binding
guarantees break silently. That requires a full invalidation-DAG analysis
(which downstream artifacts are variance-affected by new evidence, and how
`gate`/`decision` state should react if some are already committed) that is
out of scope for G009.

Accepted workaround instead: **one run per adapter.** A run performs at most
one research operation (enforced by `research.py:552`/`research.py:742`
transitioning only from `RESEARCH_READY`/`RESEARCH_RUNNING`, and by
`research serpapi`'s CLI-level preflight at `cli.py:977`, which raises
`"research is not permitted from run state ..."` once a run has left that
window). To use both KIPRIS and SerpApi evidence, run two independent runs —
run A does `research kipris`, run B does `research serpapi` — and take each
through `/ideate` onward independently. See the worked recipe in
[SETUP.md](../SETUP.md#one-research-operation-per-run). No new verb was added;
this is a documentation-only resolution of the deferral.
