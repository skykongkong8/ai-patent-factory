---
description: Select an inventor and build, enrich, or replace versioned profile/topic context.
---

# /setup — prepare inventor and topic context (step 1)

Use the local JSON CLI to select the intended inventor and publish a profile
version from the user's selected files or interview responses. Read `README.md`,
`SETUP.md`, `CLAUDE.md`, and `AGENTS.md` first, then follow
[the inventor-management guide](../../docs/inventor-management.md) for the complete
selection, replacement, conflict, topic, and legacy procedures.

## Steps

1. Verify the installed contract with `--version` and `--help`; initialize private
   roots with `init` if needed. Resolve the supplied inventor name through
   `inventor list`; reuse an established ID or have the user select among matches.
   Create a new identity only when the request calls for one.
2. Use the supplied document/folder/response path directly. Otherwise ask for one
   of those three routes and its exact path. Managed interviews require
   `--responses FILE`. Never choose all of `documents/` by default.
3. Ingest only the selected input with `--inventor INVENTOR_ID`. Use `--replace`
   when the user requests full profile replacement; otherwise enrich a new
   candidate from the active version. Select/create a topic and ingest its
   separate context when requested. Setup never calls `run start`.
4. Return the stdout `status`, inventor/topic IDs, candidate and published version
   IDs, output paths, and any limitations. A successful setup can suggest
   `/research` with the selected IDs, then returns control to the user.

The portable CLI calls for install checks and the selected document route are:

```bash
python3 -m patent_factory --version
python3 -m patent_factory --help
python3 -m patent_factory init
python3 -m patent_factory profile document documents/background.md --inventor INVENTOR_ID
```

Run ingestion only after resolving the intended ID and exact input above; the
management guide provides folder, interview, topic, and replacement variants.

## Stop conditions

On `conflict_resolution_required`, preserve the exact candidate version, batch,
owner IDs, and scope. A user-authored conflict decision for those bindings is
required to resume; the agent never chooses values or creates the user's reason.
Failed or conflicted candidates leave the active version unchanged. Use the
core's JSON results instead of reading databases or immutable exports.

Passing a source path to local ingestion does not authorize reading private text
into hosted model context. Preserve the existing privacy and external-transfer
approval requirements.
