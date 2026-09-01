---
description: Build or enrich the private inventor profile (folder · document · interview).
---

# /setup — build your inventor profile (step 1)

Build the authoritative inventor profile from your files in `documents/`. This is a
thin guide to the JSON CLI: the core creates all files and SQLite state — this command
does not. Read `README.md`, `SETUP.md`, `CLAUDE.md`, and `AGENTS.md` first.

## Where you provide input

Put your material in `documents/` (UTF-8 `.md`/`.txt`/`.json`; formats and the scripted
interview file are described in `documents/README.md`). Choose exactly one path: a
whole folder, a single document, or an interview (interactive, or scripted via
`--responses`).

## Steps

0. Confirm the install contract and the private roots.

```bash
python3 -m patent_factory --version
python3 -m patent_factory init
```

1. Ask which of the three paths the user wants, then run only that one.

```bash
python3 -m patent_factory profile folder documents
python3 -m patent_factory profile document documents/background.md
python3 -m patent_factory profile interview --responses documents/interview.json
```

2. Report the stdout JSON `status` verbatim. Keep inputs/responses under the documents
   root and the DB/exports under the workspace root, using relative non-symlink paths
   only. The stdout JSON is the result.
3. Confirm what was ingested and suggest the next step — **`/research`** to start a run
   and gather evidence. Re-running `/setup` as you add documents is safe.

## Stop conditions (do not bypass)

- If `status` is `conflict_resolution_required`, preserve `batch_id` and stop. Do not
  call `profile conflict-decide` or choose a value until the user provides a versioned
  decision input for the exact current conflict batch (inspect with
  `profile conflict-inspect`).
- The SQLite DB is authoritative and `profile.json` is a deterministic export — never
  edit either directly.
- Do not print private source text or load it into hosted model context. This wrapper
  does not authorize any external transfer.
