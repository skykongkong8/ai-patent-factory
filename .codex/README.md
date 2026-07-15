# Codex portable JSON CLI mapping

Codex does not run Claude slash commands. Instead it calls the same
`python3 -m patent_factory` JSON CLI directly. The Python core is the sole authority for
state, gates, and exports, so Codex must not directly modify the SQLite databases, the
JSON/JSONL/Markdown exports, or the state pointers.

## Common rules

```bash
python3 -m patent_factory --version
python3 -m patent_factory --help
```

- Read the single sorted JSON object on stdout. Every result carries
  `schema_version: cli-result-v1` and `envelope_version: cli-envelope-v1`.
- Only the `help/version` probes are intentionally plain text. Otherwise a missing or
  invalid argument is an `invalid_arguments` JSON result.
- Some safe stop states use a non-zero exit, so do not discard the JSON based on exit
  code alone.
- On `*_required`, `coverage_insufficient`, `decision_required`, `insufficient_evidence`,
  `revision_required`, `stopped`, or `error`, stop and report the gate/state/hash.
- `gate inspect` is read-only. `gate decide` needs a `gate-decision-input-v1` the user
  authored for the current subject hash and scope. Codex does not create the
  action/reason/approval/decision ID.
- Inputs use relative, non-symlink paths under the documents root; runs and versioned
  requests use ones under the workspace root.

## Setup, profile, run bootstrap

```bash
python3 -m patent_factory init
python3 -m patent_factory profile folder documents
python3 -m patent_factory profile document documents/input.md
python3 -m patent_factory profile interview --responses documents/responses.json
```

Choose exactly one of the three profile paths. On `conflict_resolution_required`,
preserve the batch and wait for the user's decision. Do not directly modify SQLite or
`profile.json`.

A new private workflow run binds the authoritative profile and starts at
`research_ready`. When using the default profile paths, the profile options may be
omitted.

```bash
python3 -m patent_factory run start --run workspace/runs/RUN --run-id RUN --profile workspace/profile.json --profile-database workspace/profile.sqlite3
```

## Research

```bash
python3 -m patent_factory research fixture documents/kipris.xml --run workspace/runs/RUN --run-id RUN --query QUERY
python3 -m patent_factory research manual documents/results.json --run workspace/runs/RUN --run-id RUN --query QUERY --allow-host HOST
```

A failure is an adapter event / coverage limitation, not evidence. If a credential or
paid-service gate is pending, no network request is permitted. Do not arbitrarily broaden
hosts or budgets.

## Ideation and shortlist

```bash
python3 -m patent_factory ideate --run workspace/runs/RUN --run-id RUN --profile workspace/profile.json --profile-database workspace/profile.sqlite3 --input workspace/requests/candidate-input-v1.json
python3 -m patent_factory shortlist --run workspace/runs/RUN --run-id RUN --input workspace/requests/shortlist-input-v1.json
```

The input contracts are `candidate-input-v1` and `shortlist-input-v1`. Do not
auto-approve `domain_pivot_required`. If three defensible finalists are unavailable,
preserve `insufficient_evidence`.

## Finalist audit

```bash
python3 -m patent_factory audit retrieve --run workspace/runs/RUN --run-id RUN --query-input workspace/requests/audit-query-input-v1.json --fixture-manifest documents/requests/audit-fixture-manifest-v1.json
python3 -m patent_factory audit score --run workspace/runs/RUN --run-id RUN --feature-input workspace/requests/feature-map-set-input-v1.json
```

Use a separate KIPRIS query group per finalist. Leave the `simrisk-v1.0.0` computation to
the core. On `coverage_insufficient` or `decision_required`, do not proceed to draft.
Whether `R_hi < 75` auto-approves is decided only by the core.

## Exact gate inspection / decision

```bash
python3 -m patent_factory gate inspect --run workspace/runs/RUN --run-id RUN --gate-id GATE_ID
python3 -m patent_factory gate decide --run workspace/runs/RUN --run-id RUN --gate-id GATE_ID --input workspace/requests/gate-decision-input-v1.json
```

After a decision, resume the suspended operation only with the exact `decision_id` the
core returned and the same original input. Changed content/hash/scope needs a new gate.

## Draft, independent review, validation

```bash
python3 -m patent_factory draft --run workspace/runs/RUN --run-id RUN --input workspace/requests/report-input-v1.json
python3 -m patent_factory review --run workspace/runs/RUN --run-id RUN --input workspace/requests/review-input-v1.json
python3 -m patent_factory validate --run workspace/runs/RUN --run-id RUN
```

The reviewer identity/pass must differ from the drafter. On `revision_required`, do not
validate. Only the `complete` of the deterministic validate after `reviewed` counts as
done. Codex does not directly edit the draft/review/validation exports.

## Guarded external share

```bash
python3 -m patent_factory share --run workspace/runs/RUN --run-id RUN --input workspace/requests/external-report-share-v1.json
```

On `sensitive_disclosure_required`, stop. Only when the user has a current decision
approving the exact recipient, purpose, destination, report hash, and sensitive fields,
add the core-returned `--decision-id DECISION_ID` to the same command. Directly copying a
file bypasses the share gate.

## Privacy and hosted egress

Local CLI execution and model-context transfer are separate. Claude Code, Codex, and
other hosted models are external processors, so do not read raw documents, source spans,
profile/proprietary facts, reports, or secrets into context. You must have the exact
recipient/model class, purpose, approved data classes, a current approval for the scope
and content hash, field minimization, canary checks, and an egress manifest. Neither this
document nor a Codex session creates such an approval. Secrets are never egressed or
persisted under any circumstances.

## Cleanup and release checks

Run deletion also uses only the Python core's safe `delete-run` JSON CLI.

```bash
python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace
```

This command takes no `run-id` and returns `cli-result-v1`/`cli-envelope-v1` and a
deletion report. Report partial failure/status as-is. Do not directly delete SQLite,
exports, logs, or a sibling run with `rm` or a symlink-following tool.

Do code cleanup as a separate small diff after behavior-fixing tests, and re-run the
offline checks below.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q src tests
python3 -m patent_factory --help
```

## UX differences from Claude Code

- Claude Code: the `.claude/commands/*` slash commands and `.claude/skills/*` are the
  canonical UX.
- Codex: a best-effort portable smoke surface that calls this document's commands
  directly. Do not assume slash-command auto argument collection or skill-discovery
  parity.
- Both surfaces use the same CLI/parser, versioned JSON inputs, SQLite state kernel, gate
  policy, and validators. A Codex-specific UX failure may be recorded as a limitation but
  is not grounds for a core bypass or a relaxation of privacy/egress rules.
