---
name: profile
description: Safely initialize or enrich the private inventor profile through one of three supported input paths.
---

# Profile workflow

Read `SETUP.md` and `CLAUDE.md`. Choose exactly one folder, document, or interview CLI path. Keep inputs/responses beneath the configured documents root and database/export outputs beneath the configured workspace root. Treat SQLite as authoritative and stdout as the command result; `profile.json` is only a deterministic export. Never bypass `conflict_resolution_required`; a conflicting batch adds no canonical or compatible facts. Do not copy private source text into chat.
