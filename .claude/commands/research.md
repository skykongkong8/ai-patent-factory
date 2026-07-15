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
```

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
