# Manage inventors, topics, and profile versions

Use this guide when setting up an inventor, replacing a profile, choosing a topic,
resuming a managed run, or assigning older data. The local Python core owns the
catalog, input snapshots, versions, and run bindings. Pass private paths to the CLI;
reading those files into a hosted model still requires the repository's exact
egress approval.

## Set up the intended inventor

1. Look up the supplied name, then use the returned `inventor_id`. Names need not
   be unique: ask the user to select an ID when several matches exist. Create an
   inventor only when the user requests a new identity.

   ```bash
   python3 -m patent_factory inventor list --name "Example Inventor"
   python3 -m patent_factory inventor create --name "Example Inventor"
   python3 -m patent_factory inventor status --inventor INVENTOR_ID
   ```

2. Use the input route already supplied by the user, or ask for one folder,
   document, or interview response file. Run only that route. All paths below are
   repository-relative placeholders; replace IDs with core-issued values.
   With custom roots, pass the same `--documents-root` and `--workspace-root`
   on each command; there is no global current-inventor or root selection.
   Roots are bound in the catalog. Selecting an ancestor or descendant root
   cannot opt existing data out of management; use a separate sibling root for
   an independent workspace.

   ```bash
   python3 -m patent_factory profile document documents/background.md --inventor INVENTOR_ID
   python3 -m patent_factory profile folder documents/my-background --inventor INVENTOR_ID
   python3 -m patent_factory profile interview --responses documents/interview.json --inventor INVENTOR_ID
   ```

   Managed interviews require a response file under the documents root. The core
   records an owned, hash-bound snapshot of each accepted input; the original
   source remains in place. A source under another inventor's managed directory
   is rejected. An explicitly selected unassigned source can be imported.

3. Report `status`, `inventor_id`, the candidate/version IDs, and output paths from
   stdout. Successful ingestion publishes and activates a new version. Completion
   of setup returns control to the user; research needs a separate request.

Default ingestion enriches a candidate based on the active version. To replace
all current facts with the specified input, add `--replace` to the chosen route:

```bash
python3 -m patent_factory profile document documents/background.md --inventor INVENTOR_ID --replace
```

Replacement starts from an empty candidate. Facts absent from the new input are
absent from the new active version. Earlier versions, input snapshots, and runs
remain available. A failed or conflicted candidate never replaces the active
version. `--candidate-version VERSION_ID` resumes one exact candidate instead of
starting another; retain its original input and replacement mode.

## Handle a conflict or manage a topic

On `conflict_resolution_required`, preserve the exact inventor, candidate version,
batch ID, and scope from stdout and stop. Inspect only through the core:

```bash
python3 -m patent_factory profile conflict-inspect --inventor INVENTOR_ID --candidate-version VERSION_ID --batch-id BATCH_ID
python3 -m patent_factory profile conflict-decide --inventor INVENTOR_ID --candidate-version VERSION_ID --batch-id BATCH_ID --input DECISION_JSON
```

The user authors `profile-conflict-decision-v1` for that batch, including exact
`inventor_id` and `candidate_version_id` bindings. Add `--topic TOPIC_ID` for a topic
candidate. The agent never chooses retained values or authors the user's reason.
The core applies the complete retained batch after resolution, including compatible
items and their provenance; unresolved batches publish no facts.

Topics belong to one inventor and hold versioned problem definitions, goals,
constraints, and source facts. Their names may also repeat. Register or select the
intended topic and ingest its context separately from the inventor's common facts:

```bash
python3 -m patent_factory topic list --inventor INVENTOR_ID --name "센서 보정"
python3 -m patent_factory topic create --inventor INVENTOR_ID --name "센서 보정" --tag "센서" --tag "보정"
python3 -m patent_factory topic status --inventor INVENTOR_ID --topic TOPIC_ID
python3 -m patent_factory topic document documents/topic.md --inventor INVENTOR_ID --topic TOPIC_ID
```

`topic folder SOURCE` and `topic interview --responses FILE` provide the other
routes. Topic ingestion supports `--replace` and `--candidate-version` with the
same candidate and publication rules. Topic facts become `topic.<field>` claims
in a run's effective context; they do not overwrite common inventor facts. The
`topic.` namespace is reserved, so common profile fields cannot use that prefix.
Selecting or creating topic context may be part of the requested setup; it never
starts research.

## Start and resume work with fixed context

Only an explicit research request starts a run. Select the inventor and topic by
ID, ensuring both have an active version, then use the returned run path and ID:

```bash
python3 -m patent_factory run start --inventor INVENTOR_ID --topic TOPIC_ID
python3 -m patent_factory run status --run RUN --run-id RUN_ID
```

`run start` can accept a caller-selected `--run-id` and its canonical `--run` path.
The core chooses the active versions once, binds their IDs and hashes into
`profile-context-v2`, and retains common profile and topic provenance. All later
research, scaffold, ideation, audit, draft, review, validation, and share commands
use that fixed context. Updating a profile or topic affects future runs only.
To use newer context, start a new run on a fresh research request.

For later commands, use `--run RUN --run-id RUN_ID`, or a cataloged `--run-id`
alone. A supplied owner ID or path must match the catalog. Do not pass the latest
global profile or another inventor's database to a managed run. Place every
workspace request, workbench, and explicit scaffold `--out` beneath
`RUN/requests/`; `scaffold report` also needs this run scope. Inputs and generated
manual-import templates under the documents root stay associated with that run's
owner. Use the output paths the core returns.

`--inventor-id INVENTOR_ID` is an owner-ID alias on leaf commands. For
`research kipris`, use this spelling for an owner assertion: its existing
repeatable `--inventor NAME` option (also `--inventor-term NAME`) remains a patent
query expansion. Run-scoped `research normalize-web` output belongs under
`documents/inventors/INVENTOR_ID/topics/TOPIC_ID/runs/RUN_ID/`; pass that same run
scope and use its returned path for the following manual import.

Each stage still needs its own explicit user request. Gates stop immediately;
decisions remain user-authored and bound to their exact subject. Review uses a
different identity and pass from drafting, and validation follows `reviewed`.

## Storage and retention

The catalog at `workspace/catalog.sqlite3` records inventor, topic, version, and
run ownership. Managed state is organized beneath
`workspace/inventors/INVENTOR_ID/`: profile versions under `profiles/`, topic
versions under `topics/TOPIC_ID/contexts/`, and runs under
`topics/TOPIC_ID/runs/`. Owned input snapshots live beneath
`documents/inventors/INVENTOR_ID/`. The core returns their exact paths.

Published versions and snapshots remain fixed. A later source edit requires new
ingestion; it cannot change a historical run. The core manages owner-only
directories (`0700`) and files (`0600`), publication recovery, and concurrent
activation checks. Retry the original failed operation using its returned
candidate identity; do not repair databases or move generated files manually.

```bash
python3 -m patent_factory topic archive --inventor INVENTOR_ID --topic TOPIC_ID
python3 -m patent_factory inventor archive --inventor INVENTOR_ID
```

Archiving excludes a record from new work but preserves existing run access and
resumption. This is local organization for one operator, not account access
control. There is no inventor/profile hard deletion or existing-run context
replacement. Delete a run only with the existing safe `delete-run` command and
preserve any partial-failure result.

## Assign older data explicitly

`inventor create` initializes the catalog; `legacy inspect` does not. Creating the
catalog does not silently assign the former global profile or runs.
Inspect their metadata, then assign only the user-selected objects:

```bash
python3 -m patent_factory legacy inspect
python3 -m patent_factory legacy inspect --run LEGACY_RUN --run-id LEGACY_RUN_ID
python3 -m patent_factory legacy adopt --inventor INVENTOR_ID --profile
python3 -m patent_factory legacy adopt --inventor INVENTOR_ID --topic TOPIC_ID --run LEGACY_RUN --run-id LEGACY_RUN_ID
```

Once the catalog exists, unassigned legacy writes stop until explicit adoption;
read-only inspection remains available. Profile and run adoption are separate.
Run adoption records ownership while preserving its location, historical
`profile-context-v1`, hashes, artifacts, gates, and approvals. It does not apply
the selected topic's current context to that run. To replace an inventor's
profile from a new document, ingest that document with `--replace`; adopting old
data is unnecessary unless the user also wants that data assigned.
