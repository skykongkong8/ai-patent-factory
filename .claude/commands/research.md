---
description: Start a run and gather bounded fixture or manual research evidence.
---

# /research — gather bounded evidence (step 2)

Bind your profile into a fresh run and collect bounded evidence. Follow `SETUP.md`,
`CLAUDE.md`, `AGENTS.md`, and `.claude/skills/research/SKILL.md`. Do not read or
summarize the source yourself — pass the user's paths to the local CLI.

## Where you provide input

Local source files live under `documents/` (e.g. `documents/kipris.xml` for a fixture,
or `documents/manual-results.json` for a manual import). See `documents/README.md`.

## Steps

0. Start the run once — it binds your profile and enters `research_ready`. Omit
   `--profile`/`--profile-database` to use the defaults.

```bash
python3 -m patent_factory run start --run RUN --run-id RUN_ID --profile PROFILE --profile-database PROFILE_DATABASE
```

1. Run one bounded operation the user chose.

```bash
# Fixture — offline acceptance path
python3 -m patent_factory research fixture SOURCE --run RUN --run-id RUN_ID --query QUERY

# Manual — user-supplied, HTTPS-derived; requires an explicit host
python3 -m patent_factory research manual SOURCE --run RUN --run-id RUN_ID --query QUERY --allow-host HOST

# Live KIPRIS — credentialed KO/EN keyword batch (check the credential first)
python3 scripts/check_credentials.py --check-name KIPRIS_PLUS_API_KEY
python3 -m patent_factory research kipris --run RUN --run-id RUN_ID --query QUERY \
  --korean-synonym KO_TERM --english-synonym EN_TERM

# Web evidence — agent searches out-of-band, then normalizes + imports offline
python3 -m patent_factory research normalize-web documents/web-rows.json \
  --out documents/normalized.json --allow-host HOST --source-type arxiv
python3 -m patent_factory research manual documents/normalized.json \
  --run RUN --run-id RUN_ID --query QUERY --allow-host HOST
```

For web evidence, follow the deep-research procedure in
`.claude/skills/research/SKILL.md`: KO+EN keyword combinations per source
(Google Patents, Naver, arXiv, Papers with Code, GitHub), public metadata only,
one bounded import per source.

For the live path, run the credential check first and report its status. If the
CLI returns `status: credential_required` (exit 5), preserve `gate_id` and
`subject_revision_hash` and stop — resume only after the user decides the gate,
re-running the same command with `--decision-id`.

2. Report the stdout JSON `status`, `next_state`, and any adapter failure / coverage
   limitation verbatim. A source failure is an adapter event, not evidence.
3. Confirm the run reached `research_complete` and suggest the next step — **`/ideate`**
   to propose candidates.

## Stop conditions (do not bypass)

- Stop on `credential_required`, `research_incomplete`, any other `*_required`,
  `stopped`, or `error`. Do not turn a failure into evidence, and do not add an
  unrestricted `--allow-host`.
- Importing a private profile/source into hosted Claude context is a separate external
  transfer, forbidden without exact scope approval and an egress manifest.
