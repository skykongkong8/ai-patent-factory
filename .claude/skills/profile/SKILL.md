---
name: profile
description: Safely prepare a selected inventor's versioned profile and topic context.
---

# Profile workflow

Read `SETUP.md`, `CLAUDE.md`, and `AGENTS.md`. Follow
[the inventor-management guide](../../../docs/inventor-management.md) when selecting
an inventor/topic, ingesting input, replacing a profile, resolving conflicts, or
assigning older data. It is the shared procedure for all managed context changes.

Use only the selected folder, document, or interview response file under the
configured documents root. Pass the established inventor ID, and topic ID for
topic context. Preserve the returned candidate/version IDs; candidates publish
only after complete validation and conflict resolution. `--replace` starts an
empty candidate and leaves earlier versions and runs intact.

Stop on `conflict_resolution_required` and preserve the exact owner, candidate,
batch, and scope. Only a user-authored versioned conflict decision may select
retained facts. Use core commands for inspection and resumption; SQLite and its
deterministic exports are not hand-editable inputs.

A local CLI invocation does not approve hosted access to private text. Keep
source, profile, and topic semantics out of context without the exact current
egress approval and minimized manifest. Return metadata and paths instead.

Setup ends after the requested profile/topic work. Report the result and suggest
a separate `/research` request; do not start a run from this helper.
