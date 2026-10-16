# Codex workflow commands

Use the repository's explicit `$patent-*` skills in Codex. They follow the shared
Claude Code stage procedures and call the same `python3 -m patent_factory` JSON
CLI. The [execution contract](workflow.md) defines invocation boundaries and
helper routing; the Python core owns state, gates, versions, and exports.

## Run one stage at a time

Open Codex in this repository. The eight entrypoints live under
`.agents/skills/patent-*/`; their `agents/openai.yaml` files set
`allow_implicit_invocation: false`. Select one using `$` in the composer or
`/skills` in Codex CLI. Restart the session if new skills do not appear.

| Claude Code | Codex | Stage result |
| --- | --- | --- |
| `/setup` | `$patent-setup` | Select an inventor and publish profile/topic versions |
| `/research` | `$patent-research` | Start a run and gather bounded evidence |
| `/ideate` | `$patent-ideate` | Complete one workbench step or publish validated candidates |
| `/shortlist` | `$patent-shortlist` | Select three supported finalists |
| `/audit` | `$patent-audit` | Retrieve similarity evidence and score it |
| `/checkpoint` | `$patent-checkpoint` | Present the dossier and apply the user's authored decision |
| `/draft` | `$patent-draft` | Render the approved invention report |
| `/review` | `$patent-review` | Independently review, then validate |

Send this as a Codex message, not a shell command:

```text
$patent-setup setup with Example Inventor documents/background.md
```

This selects the single-document route for the intended inventor. Duplicate names
need an explicit ID choice. A request to replace a profile uses a fresh empty
candidate and preserves previous versions and runs. Setup may also prepare the
selected topic, then returns control before research.

Follow [inventor management](../docs/inventor-management.md) for identity lookup,
replacement, conflict resumption, topic context, archives, and legacy adoption.
No global current-inventor setting selects work implicitly. The core returns
IDs, version IDs, and paths; retain these in the handoff to a fresh session.

Explicitly invoke `$patent-research` with the inventor/topic IDs and intended
research mode to start a new run. Later stages use that run's fixed context;
changing an active profile or topic affects future runs only. Place workspace
requests and workbenches beneath `RUN/requests/`. Use `--run RUN --run-id RUN_ID`
or a cataloged `--run-id` alone, and include run scope for report scaffolding.

Each invocation reports its result, suggests the next call, and stops. A
checkpoint approval does not start draft. A later stage never runs its
prerequisites automatically. Ideate has internal boundaries: brief scaffold →
divergence validation → entanglement validation → candidate scaffold → promotion
validation → publish. Invoke `$patent-ideate` again after each boundary.

## Results, gates, and privacy

Read stdout as the single `cli-result-v1` / `cli-envelope-v1` JSON result; only
help/version are plain text. Non-zero exits may be safe stop states. Preserve
`status`, `next_state`, and exact gate/hash/scope fields. User-authored decisions
must match the current subject; use `gate-decision-input-v2` for the always-raised
post-audit checkpoint and `v1` for the other supported gates. Never invent the
user's approval, retained conflict values, reasons, feedback, or decision ID.

The reviewer identity and pass differ from the drafter's. Validate only after
`reviewed`; `revision_required` returns control for a new draft. External sharing
is separately requested and disclosure-gated. There is no preview stage.

Passing private input paths to the local CLI does not approve hosted-model
access. Keep private documents, profile/topic facts, evidence, reports, and
secrets out of context unless the exact transfer and minimized egress manifest
are approved. Public-fixture compatibility checks remain separate from private
runs. Neither a skill nor this guide creates such an approval.
[AGENTS.md](../AGENTS.md) states the authority and privacy boundaries.

## Portable CLI reference

The managed research bootstrap selects inventor/topic versions and returns a run
in `research_ready`:

```bash
python3 -m patent_factory run start --inventor INVENTOR_ID --topic TOPIC_ID
```

A legacy installation with no catalog can still bootstrap its global profile
using the existing portable mapping. Once a catalog exists, assign older data
explicitly before a legacy write; use managed selection for new work.

```bash
python3 -m patent_factory run start --run workspace/runs/RUN --run-id RUN --profile workspace/profile.json --profile-database workspace/profile.sqlite3
```

The following mappings apply to the selected run. `RUN` is its returned path;
the fixture manifest is an explicitly selected document input.

```bash
python3 -m patent_factory audit retrieve --run RUN --run-id RUN_ID --query-input RUN/requests/audit-query-input-v1.json --fixture-manifest documents/requests/audit-fixture-manifest-v1.json
python3 -m patent_factory gate inspect --run RUN --run-id RUN_ID --gate-id GATE_ID
python3 -m patent_factory gate decide --run RUN --run-id RUN_ID --gate-id GATE_ID --input RUN/requests/gate-decision-input-v2.json
```

Use the correct gate input version described above. External share separately
requires `external-report-share-v1`. Do not directly modify SQLite or immutable
exports to imitate a transition. Do not directly delete run files; safe cleanup
takes no `run-id`, and its JSON partial-failure status must be preserved:

```bash
python3 -m patent_factory delete-run --run RUN --workspace-root workspace
```

For an existing run at the legacy location, the same safe mapping is:

```bash
python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace
```

## UX differences

Claude Code invokes `.claude/commands/` slash commands. Codex invokes explicit
`.agents/skills/patent-*` entrypoints under the same Python authority and gate
contract. Skills guide invocation; they do not authorize bypasses for direct CLI
callers.

For raw CLI examples and release checks, use [SETUP.md](../SETUP.md). Input
formats are in [documents/README.md](../documents/README.md), and versioned request
templates are in [workspace/README.md](../workspace/README.md). Local migrated
`source-command-*` copies are not maintained entrypoints.
