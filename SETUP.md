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

#### Exit codes

A non-zero exit is usually a *gate*, not a crash. Any driving agent must branch
on these rather than treating non-zero as failure:

| Code | Meaning | What to do |
| ---: | --- | --- |
| `0` | The operation completed. | Continue to the next stage. |
| `2` | Invalid input, unsafe path, or a validation error. `status` is `error` and `failure_code` names the class. | Fix the input and re-run. Nothing was written. |
| `3` | `conflict_resolution_required` — a profile batch conflicts with existing facts. | Resolve with `profile conflict-inspect` / `conflict-decide`. Never bypass. |
| `4` | The stage ran but did not reach its complete state (e.g. research `incomplete`). | Read `status`, `next_state`, and `incomplete_reason`. This is recoverable, not a dead end — see below. |
| `5` | A gate is open: `credential_required`, or `insufficient_evidence` from shortlist. | `gate inspect` → author a decision → `gate decide` → re-run with `--decision-id`. |
| `7` | `coverage_insufficient` — `audit score` found at least one finalist's corpus coverage too thin to decide, with no finalist requiring the checkpoint. | `gate inspect` → author a `coverage` decision (`expand`/`retry`/`stop`) → `gate decide`. |
| `8` | `decision_required` — a `post_audit_checkpoint` gate is pending. **Every** `audit score` now stops here, clean or breaching (accepted breaking change: a clean audit no longer exits `0`). | `gate inspect` → compose the dossier → `scaffold gate-decision` → author the `gate-decision-input-v2` → `gate decide` (`approve`/`re_ideate`/`re_research`/`stop`). See `/checkpoint`. |
| `9` | `sensitive_disclosure_required` — `share` found a sensitive field in the requested scope. | `gate inspect` → author a decision (`approve`/`redact`/`stop`) → `gate decide` → resume `share` with `--decision-id`. |
| `10` | `revision_required` — the independent review found a defect. | Return to `/draft` with a corrected report; only a new report revision may receive a new review. |

Every one of these still prints a complete `cli-result-v1` object on stdout. If
you ever get unparseable stdout, that is a bug worth reporting — the envelope is
the contract.

**On `incomplete_reason`:** research reports `incomplete` when the adapter
succeeded but contributed no *new* evidence — every record deduplicated against
what the run already had. `evidence_count` will still be non-zero, which reads
as a dead end without the reason field. The fix is to supply at least one
reference the run has not already retrieved, or to proceed with existing
evidence.
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

# Live KIPRIS — credentialed multi-keyword batch (Korean/English expansions)
python3 -m patent_factory research kipris \
  --run workspace/runs/example --run-id example \
  --query 센서 --korean-synonym 감지기 --english-synonym sensor
```

`research kipris` fans one origin query plus repeatable `--korean-synonym` /
`--english-synonym` / `--discovered-term` / `--classification` / `--applicant` /
`--inventor` expansions through the live KIPRIS Plus adapter in a single
research session. Every expansion kind is sent as the same free-text `word=`
term — these are **query expansions, not fielded searches**; KIPRIS
`getWordSearch` accepts no per-field parameter, so `--classification G06N
3/04` is a text match, not an IPC lookup.

By default each planned term fetches exactly one page (`--result-budget`,
default 30, is a single KIPRIS page: `min(30, --result-budget)` rows), so the
number of live requests equals the number of planned terms, bounded by
`--max-calls` (default 12). `--paging` follows the service's own cursor past
that first page, stopping at `--result-budget` or when no further page is
reported; raising it multiplies credentialed live requests, so it is opt-in,
and it does nothing at `--result-budget <= 30` (rejected loudly rather than
shipped as a silent no-op — raise `--result-budget` above 30 to actually get a
second page). With `--paging`, the true request ceiling is `--max-calls *
5` (5 pages per term when paging is on), and `research_budget.validate()`
enforces `max_calls * effective_pages <= 100`. `--byte-budget` bounds each
individual request/page, not the whole paged sequence — a term that runs
several pages can receive up to that many times `--byte-budget` in total. It
requires `KIPRIS_PLUS_API_KEY` in the environment: a missing or rejected key
suspends the exact batch behind a credential gate (`status:
credential_required`, exit 5) — resolve it with `gate decide` and resume with
the same command plus `--decision-id`; the approval scope shown at the gate
includes `effective_pages`, `result_budget`, and the derived `max_requests`
ceiling, so what is approved is what the wire can actually send, pages
included. A non-auth source failure is recorded as a coverage limitation and
the batch continues. One-time live verification: `python3
scripts/live_kipris_smoke.py --confirm-live` (offline-skips without the key;
redacted output only).

Research performs only `research_ready → research_running →
research_complete | research_incomplete`. Results and failures are written to the
per-run SQLite, and a deterministic bundle manifest is exported as an immutable file
under the run's owner-only `research-exports/`. A source failure is an adapter event,
never evidence. Current/legacy API distinctions, errors, limits, and open questions
are in [docs/kipris-contract-spike.md](docs/kipris-contract-spike.md).

### One research operation per run

There is no direct `RESEARCH_COMPLETE → RESEARCH_RUNNING` re-entry edge in the
state machine, so calling a research verb (`research fixture`, `research
manual`, `research kipris`, or `research serpapi`) again right after a run
reaches `research_complete` is refused. Enforcement sits in more than one
place, all deriving from the same transition table: `run_research` and
`run_research_batch` (the shared executors behind every research verb) only
auto-start `RESEARCH_RUNNING` from `RESEARCH_READY`/`RESEARCH_RUNNING`, and
`research serpapi` additionally checks the run's current state before any
network egress and raises `"research is not permitted from run state ..."` if
the state can no longer reach `RESEARCH_RUNNING`. Either way, no second
research call reaches an adapter from a completed run outside the two
gate-mediated routes below.
This is a user-accepted decision — see
[docs/g009-scope-addendum.md](docs/g009-scope-addendum.md) — because a general
re-entry edge needs a full invalidation-DAG analysis of what a second research
pass would invalidate downstream (candidates, finalists, audit).

**Two gate-mediated routes back do exist**, both reached from the audit stage.
The first: if the final similarity audit
raises a COVERAGE gate — evidence coverage on one or more finalists came in
below threshold — resolving it with `gate decide --action expand` plus a
bounded expansion plan genuinely returns the run to `research_running`, and a
second research operation executes and publishes a second research bundle.
This is not a general "run research again" escape hatch: it is reached only
through the audit pipeline's own coverage check, for the finalist set the
audit already scored, not as a way to combine independent adapter runs. The
second pass's manifest is scoped to the research stage — it excludes any row
the audit itself already wrote against the same run (the audit tags its own
queries so they can be told apart) — so the republished bundle, and the
report's "Research Scope and Method" section rendered from it, describe only
what research retrieved, never what audit separately pulled into its own
similarity corpus.

The second: the post-audit `/checkpoint` gate's `re_research` branch, which
re-enters `RESEARCH_RUNNING` for exactly one bounded second pass, and only
through `research fixture` / `research normalize-web` + `research manual` —
never `research kipris` or `research serpapi`. Live credentialed research on
that second pass is deferred to
[issue #48](https://github.com/skykongkong8/ai-patent-factory/issues/48); no
networked verb, credential gate, or egress policy changed to add this path.

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
# …or live retrieval with the credentialed adapter (no fixture manifest):
python3 -m patent_factory audit retrieve --run RUN --run-id RUN_ID \
  --query-input workspace/requests/audit-query-input-v1.json --live
python3 -m patent_factory audit score    --run RUN --run-id RUN_ID \
  --feature-input workspace/requests/feature-map-set-input-v1.json

# Post-audit checkpoint — ALWAYS raised, clean or breaching (exit 8). Scaffold a
# pre-filled gate-decision-input-v2 draft (judgment fields left TODO(agent));
# the user completes action/reason/per-finalist feedback (and, on re_research,
# a bounded plan) before gate decide.
python3 -m patent_factory scaffold gate-decision --run RUN --run-id RUN_ID \
  --gate-id GATE_ID --out workspace/requests/gate-decision-input-v2.json

# One exact gate (read-only inspect; decide needs a user-authored decision input).
# Legacy gate kinds (credential, coverage, domain_pivot, sensitive_disclosure,
# excessive_similarity — the last only on gates raised before this feature)
# still take gate-decision-input-v1; a post_audit_checkpoint gate requires
# gate-decision-input-v2 and rejects v1 outright, and vice versa.
python3 -m patent_factory gate inspect --run RUN --run-id RUN_ID --gate-id GATE_ID
python3 -m patent_factory gate decide  --run RUN --run-id RUN_ID --gate-id GATE_ID \
  --input workspace/requests/gate-decision-input-v1.json
python3 -m patent_factory gate decide  --run RUN --run-id RUN_ID --gate-id GATE_ID \
  --input workspace/requests/gate-decision-input-v2.json

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
`decision_required` is the normal exit of every `audit score` call now (clean or
breaching), not only a breach — see exit code `8` above and `/checkpoint`.

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

This is the **authoritative** test command. The suite is pure standard-library
`unittest` and runs fully offline with no installation step — `pyproject.toml`
declares `dependencies = []`, and neither `pytest` nor `ruff` is a project
dependency. Do not add them, and do not report results from a `pytest` run: it
ignores the `load_tests` protocol this suite uses, so its counts do not match.
`PYTHONPATH=src` is optional (the root `patent_factory/` shim already appends
`src/patent_factory` to the package path); both forms exercise the same code.

Live, credential-gated paths (`research kipris`, `research serpapi`,
`audit retrieve --live`) are excluded from this run by design. Nothing in the
codebase reads `.env`, so those verbs need the credentials exported first:

```bash
set -a && . ./.env && set +a
```

Without that step every live verb stops at the credential gate (exit 5).

The agent behavior contract is in [CLAUDE.md](CLAUDE.md) and [AGENTS.md](AGENTS.md).
