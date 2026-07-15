---
name: profile
description: Safely initialize or enrich the private inventor profile through one of three supported input paths.
---

# Profile workflow

Read `SETUP.md`, `CLAUDE.md`, and `AGENTS.md`. This workflow initializes or enriches the
private inventor profile that every later stage builds on.

## Inputs & where they live

Private material goes in `documents/` (UTF-8 `.md`/`.txt`/`.json`; see
`documents/README.md`). Choose exactly one folder, document, or interview CLI path. Keep
inputs/responses beneath the configured documents root and database/export outputs
beneath the configured workspace root. Treat SQLite as authoritative and stdout JSON as
the command result; `profile.json` is only a deterministic export.

## Rules

Never bypass `conflict_resolution_required`; a conflicting batch adds no canonical or
compatible facts. Inspect or decide conflicts only through `profile conflict-inspect` and
a user-authored versioned conflict decision input. Do not choose a source/value for the
user, directly edit SQLite/exports, or copy private source text into chat.

Claude Code is a hosted external processor unless separately verified local. Running the
local CLI by path does not authorize reading the file into model context. Require current
exact approval, data minimization, canary checks, and an egress manifest before any
private profile field enters hosted context; otherwise stop at local CLI results.

## Next

Once the profile is built and any conflict is resolved, continue with `/research` to
start a run and gather evidence.
