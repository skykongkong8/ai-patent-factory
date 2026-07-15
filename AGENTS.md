# AI Patent Factory agent contract

The agent surface of this repository is a thin wrapper that guides the
`patent_factory` JSON CLI. State transitions, gates, invalidation, idempotency, and
exports are performed only by the Python core.

## Authority and execution boundary

- Read `README.md`, `SETUP.md`, and `CLAUDE.md` first.
- Confirm the installed contract with `python3 -m patent_factory --version` and
  `python3 -m patent_factory --help`.
- Only the `help/version` probes are intentionally plain text. Every other command
  result, and any missing or invalid argument, is a single JSON object on stdout
  carrying `schema_version: cli-result-v1` and `envelope_version: cli-envelope-v1`.
- Do not read the SQLite databases or the immutable exports under `workspace/`
  directly to guess or modify state.
- Do not create state, gates, reports, reviews, validations, or share receipts with
  SQL, arbitrary scripts, or file copy/move operations.
- Every input uses a relative, non-symlink path under the configured documents root;
  every run and input contract uses one under the configured workspace root.

## Gate handling

`*_required`, `coverage_insufficient`, `decision_required`, `insufficient_evidence`,
`revision_required`, `stopped`, and `error` are not permission to auto-proceed.
Preserve the `gate_id`, `subject_revision_hash`, `actions`, and `next_state` from the
stdout JSON and stop. When needed, inspect only the current gate with this read-only
command:

```bash
python3 -m patent_factory gate inspect --run RUN --run-id RUN_ID --gate-id GATE_ID
```

Call `gate decide` only when the user has authored a `gate-decision-input-v1` file
for the exact current subject and scope. The agent does not create the approval,
decision ID, approval scope, or the user's reason on their behalf. Resume only with
the exact `decision_id` returned by the core and the same input as the original
command.

## Privacy, external transfer, and legal boundary

- Do not put the source text, profiles, or secrets in `documents/`, `workspace/`, or
  `.env` into chat or a hosted model context.
- Claude Code, Codex, and other hosted models are external processors. Without a
  current approval for the exact recipient/purpose/data class/content hash/scope and
  a minimized egress manifest, run only the local CLI and stop. This document and the
  wrapper do not authorize any external transfer.
- `share` requires an `external-report-share-v1` request and a current, exact
  sensitive-disclosure decision. Do not copy the report to bypass the gate.
- Similarity is a research aid within the retrieved corpus. Do not write conclusions
  about patentability, novelty, inventive step, validity, non-infringement/FTO, or
  legal advice.

## Review, cleanup, and release

The drafter and the reviewer use different identities and passes. Do not `validate`
unless the state is `reviewed`; on `revision_required`, return to drafting.

Clean up private runs only with the safe core command. It takes no `run-id`.

```bash
python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace
```

Preserve the partial-failure/status from the deletion JSON. Do not directly delete
DBs, exports, logs, symlinks, or a sibling run. Before a release, run at least the
offline checks below and state any failure or omission:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q src tests
python3 -m patent_factory --help
```

Do code cleanup as a separate, small change after behavior-fixing tests.
