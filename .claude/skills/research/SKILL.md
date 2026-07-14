---
name: research
description: Run bounded local research and finalist-specific retrieval without fabricating evidence or broadening egress.
---

# Research workflow

Create the private workflow run only with `python3 -m patent_factory run start ...`; it binds the authoritative profile and enters `research_ready`. Use only `python3 -m patent_factory research ...` and `python3 -m patent_factory audit retrieve ...` for retrieval state changes. Pass paths and bounded query projections; never pass the whole profile to an adapter. Fixture research is the offline acceptance path. Manual imports must be user supplied, HTTPS-derived, size bounded, and restricted to explicit `--allow-host` values.

Treat each stdout JSON object as authoritative command output. Preserve query, adapter/version, retrieval time, result/failure, stable evidence IDs, and coverage limitations. A source failure is an adapter event, never evidence. Never invent fallback records, silently change query/corpus budgets, scrape an unapproved host, or edit research exports/SQLite.

Stop on `credential_required`, `research_incomplete`, `coverage_insufficient`, any other `*_required`, `stopped`, or `error`. A pending/rejected credential or paid-service gate permits zero network requests. Only a user-provided current `gate-decision-input-v1` may resolve the exact gate; this skill does not authorize credentials, paid services, hosted-model egress, or a broader data scope.
