# Setup & CLI reference

Slash commands (see [README.md](README.md)) are the recommended way to drive this
project. This document is the **escape hatch**: the full `python3 -m patent_factory`
CLI, for scripting, non-slash runtimes, or debugging. Every slash command runs one
of these verbs for you.

## Install

Requires **CPython 3.11+** and no third-party runtime packages. Verify the
installed contract and create the two owner-only roots:

```bash
python3 -m patent_factory --version
python3 -m patent_factory --help
python3 -m patent_factory init
```

`init` creates `documents/` (private input) and `workspace/` (generated state and
exports). Override locations with `init --documents DIR --workspace DIR` if needed.

### How to read CLI output

- Every command prints **one sorted JSON object** on stdout, carrying
  `schema_version: cli-result-v1` and `envelope_version: cli-envelope-v1`. Only
  `--help` / `--version` are plain text; missing or invalid arguments are still a
  JSON `invalid_arguments` result.
- Some safe stop states exit non-zero, so **do not discard the JSON based on exit
  code alone** — read `status` / `next_state` first.
- Path rules everywhere: inputs and `--responses` files live under the documents
  root; databases, exports, and versioned request files live under the workspace
  root. Absolute paths, `..`, symlinks (root, intermediate, or target),
  non-regular files, and documents larger than 2,000,000 bytes are rejected.

## Profile — three input paths

Choose exactly one path. All three write the authoritative
`workspace/profile.sqlite3` and regenerate the deterministic `workspace/profile.json`
export.

```bash
# 1. Ingest a whole folder (UTF-8 .md/.txt/.json, in name order)
python3 -m patent_factory profile folder documents

# 2. Ingest a single document
python3 -m patent_factory profile document documents/background.md

# 3. Interview — interactive in a terminal…
python3 -m patent_factory profile interview

#    …or reproducible from a scripted response file (kept under documents/)
cp examples/redacted/interview.json documents/interview.json
python3 -m patent_factory profile interview --responses documents/interview.json
```

Input formats: text documents use `field: value` lines; JSON is a plain object or
a `{"facts": [...]}` array. Document-derived items are `source_fact`; interview
answers are `user_statement`. Re-running the same input does **not** duplicate
batches, claims, or conflicts.

Common options: `--documents-root`, `--workspace-root` (repository-relative roots),
`--profile` (default `WORKSPACE_ROOT/profile.json`), `--database` (default
`WORKSPACE_ROOT/profile.sqlite3`).

### Conflicts

If any single item in a batch conflicts with an existing value, the whole
transaction records only the conflict batch, conflict records, and state, applies
**no** canonical or compatible facts, and exits with code 3
(`conflict_resolution_required`). Resolve it explicitly — never bypass it:

```bash
python3 -m patent_factory profile conflict-inspect --batch-id BATCH_ID
python3 -m patent_factory profile conflict-decide  --batch-id BATCH_ID --input DECISION_JSON
```

## Credentials (KIPRIS Plus)

The KIPRIS Plus credential is named exactly `KIPRIS_PLUS_API_KEY`. Keep it in the
environment only — never copy it into a profile, log, or document. The check makes
no network request and prints exactly one of `missing`, `present`,
`simulated_invalid`, or `fixture_usable`:

```bash
python3 scripts/check_credentials.py --check-name KIPRIS_PLUS_API_KEY
python3 scripts/check_credentials.py --check-name KIPRIS_PLUS_API_KEY --fixture-usable
```

## Research

Research operates only on a run directory already bootstrapped to `research_ready`.
`run start` binds an authoritative profile into a fresh run (omit `--profile` /
`--profile-database` to use the defaults):

```bash
python3 -m patent_factory run start \
  --run workspace/runs/example --run-id example \
  --profile workspace/profile.json --profile-database workspace/profile.sqlite3
```

Then run one bounded operation. `fixture` is the offline acceptance path; `manual`
imports user-supplied, HTTPS-derived results and requires an explicit
`--allow-host`:

```bash
python3 -m patent_factory research fixture documents/kipris.xml \
  --run workspace/runs/example --run-id example --query 센서

python3 -m patent_factory research manual documents/manual-results.json \
  --run workspace/runs/example --run-id example --query 센서 --allow-host example.com
```

Research performs only `research_ready → research_running →
research_complete | research_incomplete`. Results and failures are written to the
per-run SQLite, and a deterministic bundle manifest is exported as an immutable file
under the run's owner-only `research-exports/`. A source failure is an adapter event,
never evidence. Current/legacy API distinctions, errors, limits, and open questions
are in [docs/kipris-contract-spike.md](docs/kipris-contract-spike.md).

## Full pipeline verbs

Each stage below takes a versioned `*-input-v1.json` you author under
`workspace/requests/` (templates and field notes in
[`workspace/README.md`](workspace/README.md); JSON Schemas in `schemas/`). The core
validates the input, binds hashes to prior stages, and records state — it never
trusts a hand-copied export.

```bash
# Candidates and finalists
python3 -m patent_factory ideate    --run RUN --run-id RUN_ID \
  --profile workspace/profile.json --profile-database workspace/profile.sqlite3 \
  --input workspace/requests/candidate-input-v1.json
python3 -m patent_factory shortlist --run RUN --run-id RUN_ID \
  --input workspace/requests/shortlist-input-v1.json

# Finalist similarity audit (per-finalist KIPRIS groups; scorer simrisk-v1.0.0)
python3 -m patent_factory audit retrieve --run RUN --run-id RUN_ID \
  --query-input workspace/requests/audit-query-input-v1.json \
  --fixture-manifest documents/requests/audit-fixture-manifest-v1.json
python3 -m patent_factory audit score    --run RUN --run-id RUN_ID \
  --feature-input workspace/requests/feature-map-set-input-v1.json

# One exact gate (read-only inspect; decide needs a user-authored decision input)
python3 -m patent_factory gate inspect --run RUN --run-id RUN_ID --gate-id GATE_ID
python3 -m patent_factory gate decide  --run RUN --run-id RUN_ID --gate-id GATE_ID \
  --input workspace/requests/gate-decision-input-v1.json

# Report → independent review → deterministic validate
python3 -m patent_factory draft    --run RUN --run-id RUN_ID \
  --input workspace/requests/report-input-v1.json
python3 -m patent_factory review   --run RUN --run-id RUN_ID \
  --input workspace/requests/review-input-v1.json
python3 -m patent_factory validate --run RUN --run-id RUN_ID

# Guarded external share (add --decision-id only after the core issues one)
python3 -m patent_factory share --run RUN --run-id RUN_ID \
  --input workspace/requests/external-report-share-v1.json

# Safe run deletion (takes no --run-id; preserves partial-failure status)
python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace
```

Gate rule for every stage: `*_required`, `coverage_insufficient`,
`decision_required`, `insufficient_evidence`, `revision_required`, `stopped`, and
`error` are hard stops. Preserve `gate_id`, `subject_revision_hash`, `actions`, and
`next_state`; resume only with the core-issued `decision_id` and the same input.

## Privacy & hosted egress

Running the local CLI by path does **not** authorize reading your private text into
a hosted model context. Claude Code, Codex, and other hosted models are external
processors: without a current exact approval (recipient/model class, purpose,
approved data classes, content hash, and a minimized egress manifest), keep private
documents, source spans, profile facts, reports, and secrets out of context. Secrets
are never egressed or persisted. Providing private text to a hosted model, or the
`share` verb, is a separate external transfer — use the local CLI only until scope is
approved.

## Release checks

Before a release, run at least the offline checks and state any failure or omission:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q src tests
python3 -m patent_factory --help
```

The agent behavior contract is in [CLAUDE.md](CLAUDE.md) and [AGENTS.md](AGENTS.md).
