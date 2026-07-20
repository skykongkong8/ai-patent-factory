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
invalidation-DAG analysis).
