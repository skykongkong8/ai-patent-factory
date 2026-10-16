---
type: operational reliability guide
title: Reliability, Recovery, and Release Evidence
description: Operational model for immutable artifact publication, crash recovery, idempotent replay, bounded research retries, safe run deletion, and redacted release evidence.
tags: [reliability, recovery, transactional-publication, idempotency, release-evidence, privacy]
verified:
  - by: openwiki/0.6.0
    at: 2026-10-16T03:36:32.329Z
sources:
  - id: openwiki-source-0c1300eec1a10e3599c56545
    resource: repo://scripts/calibration_check.py
  - id: openwiki-source-153d168925fb2652d23f2c20
    resource: repo://scripts/check_credentials.py
  - id: openwiki-source-05776284076b8cb170cec39c
    resource: repo://scripts/check_serpapi_quota.py
  - id: openwiki-source-bcf837d8439da0bf086ca5b5
    resource: repo://scripts/record_fixtures.py
  - id: openwiki-source-3354db3194de3448baf78b1d
    resource: repo://scripts/release_verify.py
  - id: openwiki-source-466115273bbe8c9ac198918b
    resource: repo://src/patent_factory/adapters/base.py
  - id: openwiki-source-134622a60158829ac24d4ae0
    resource: repo://src/patent_factory/artifacts.py
  - id: openwiki-source-c1a9676b43a2a0c748e6207b
    resource: repo://src/patent_factory/cli.py
  - id: openwiki-source-827e198ce49a8101913fa2bd
    resource: repo://src/patent_factory/database.py
  - id: openwiki-source-07c7947be6499459f1eb07ce
    resource: repo://src/patent_factory/privacy.py
  - id: openwiki-source-cb920a9252a41415078c07a9
    resource: repo://src/patent_factory/research.py
  - id: openwiki-source-ef94c4f6ce7ef5deb426b558
    resource: repo://src/patent_factory/state.py
  - id: openwiki-source-3585fc99ce981bdd7f7d1a6a
    resource: repo://tests/e2e/test_release_evidence.py
  - id: openwiki-source-5af31d130f8a427fa707eaa9
    resource: repo://tests/integration/test_g002_publish_register_integration.py
  - id: openwiki-source-d515d915b7cbf1a2f1c9ba3b
    resource: repo://tests/integration/test_g002_snapshot_and_integrity.py
  - id: openwiki-source-ebce7479cbccbe7cece2bd1d
    resource: repo://tests/integration/test_g008_cli_core.py
  - id: openwiki-source-585534f233ae497ec47292c1
    resource: repo://tests/integration/test_research_reentry_guard.py
generated: { by: "openwiki/0.6.0", at: "2026-10-16T03:36:32.329Z" }
---

The core is designed to make state authoritative without pretending that SQLite and the filesystem form one cross-filesystem transaction. A run database records state, immutable semantic revisions, dependency edges, gates, events, idempotency records, and registered byte exports. The filesystem holds private immutable copies of registered artifacts. Recovery reconciles those two layers conservatively.

This page is the operational companion to [State and Provenance Kernel](/openwiki/architecture/state-and-provenance-kernel.md), [Privacy and Authority Boundaries](/openwiki/concepts/privacy-and-authority-boundaries.md), and [CLI Surface, Private Workspace, and Scaffolded Inputs](/openwiki/operations/cli-private-workspace-and-scaffolds.md). Use the supported CLI to inspect or change a run; this page explains what to do when publication, research, cleanup, or release verification stops safely.

> **Safety rule:** diagnostics and release evidence are status-, hash-, count-, and version-oriented. Do not paste exports, database rows, report text, fixture response bodies, credentials, or other private run contents into logs, tickets, or release notes.

## Reliability model and publication boundary

An artifact has two related identities:

- the **semantic revision** hash binds canonical artifact content, schema version, and dependency revision IDs; and
- the **export byte hash** binds the exact private `.json` or `.md` bytes written for that revision.

A publication computes an `ar_...` revision ID from the semantic identity and writes it under a configured, private export directory. `export_immutable()` refuses symlinked or unsafe targets, writes a same-directory `.artifact-*.tmp` file, flushes and `fsync`s its contents, sets owner-only permissions, and uses a no-clobber hard link to publish the final name. A pre-existing final file is acceptable only when its bytes are identical; different bytes are an immutable-artifact conflict. The export directory is also synced after the link and temporary cleanup.

`StateStore.publish_transition()` and its gate-specific counterparts combine this export protocol with one SQLite `BEGIN IMMEDIATE` transaction. They validate the state and any approval, publish the immutable bytes, add the revision and dependency edges, register path/hash/size in `artifact_exports`, update current pointers, append the transition event, consume an approval where required, advance state, and record the idempotency coordinate. A gate publication additionally persists the gate envelope in that transaction.

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: a semicolon inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
  Start["Publish an artifact through the state store"] --> Writer["Acquire the SQLite writer transaction"]
  Writer --> Match{"A current matching idempotency record exists"}
  Match --> Reuse["Return the registered artifact as a replay"]
  Reuse --> Finish["No new state or export"]
  Match --> Stage["Write a same-directory temporary file and fsync it"]
  Stage --> Link["Publish with a no-clobber hard link and sync the directory"]
  Link --> Fault{"A fault occurs before the database commit"}
  Fault --> Register["No fault: register export, revision, event, state, and idempotency"]
  Register --> Commit["Commit the SQLite transaction"]
  Commit --> Published["Registered immutable export and authoritative state"]
  Fault --> Rollback["Fault: roll back database changes; an unregistered linked file can remain"]
  Rollback --> Startup["Next state-store startup acquires the writer transaction"]
  Startup --> Reconcile["Remove temporary or unregistered exports and verify registered bytes"]
  Reconcile --> Retry["Retry only with the appropriate operation or decision"]
```

This flow shows durable same-directory publication followed by transactional registration; it intentionally does **not** claim atomicity across the filesystem and SQLite. If a fault follows the hard link but precedes database commit, the database rolls back while a completed physical export may remain. That file is not authoritative. On construction with configured export directories, `StateStore` takes the same SQLite writer lock before recovery, deletes interrupted temporary entries and unregistered `ar_*` exports, and verifies every registered file's path, size, and byte hash. Missing, tampered, unsafe, or out-of-directory registrations fail closed rather than being repaired from guessed content.

### Invariants for implementers

- Publish an externally materialized artifact through `publish_transition()`, `publish_gate_transition()`, or `publish_gate_resolution()`—not through a raw state transition plus a hand-written file.
- Keep an export in a configured, symlink-free private directory and register its exact byte hash and size. New suffixes are not implicitly accepted: the publication/recovery path permits only `.json` and `.md` exports.
- Model upstream relationships as revision dependencies. Replacing a current revision invalidates its transitive descendants, drops their current pointers, and stales decisions or pending gates bound to invalidated hashes.
- A pending gate blocks artifact mutation. A decision is valid only for its recorded gate, current subject hash, exact approval scope, suspended operation, and policy action. An authorizing decision is consumed and then claimed once by the publishing transition.

These constraints are why “fixing” an export by copying, renaming, editing SQLite, or recreating an artifact file is unsafe. It can turn a recovery signal into a mismatch or conceal an invalidated decision.

## Classify a stop before acting

A retry, a replay, and a refusal are deliberately different outcomes. Read the CLI JSON envelope first, including `failure_code`, `status`, `prior_state`, `next_state`, `artifact_ids`, `event_ids`, `gate_id`, and any returned decision identifiers.

| Condition | What the system means | Safe operator response |
| --- | --- | --- |
| `run_busy` | SQLite could not acquire the immediate writer transaction within its bounded busy timeout. It is explicitly retryable; no work from that transaction was committed. | Back off and rerun the same supported operation. Do not open or modify the database to break a lock. |
| Adapter `timeout`, `network`, `rate_limit`, malformed response, or another non-auth failure | The attempted retrieval is persisted as a failed adapter event, retrieval observation, and coverage limitation; it does not invent evidence. A batch can continue across non-auth failures and becomes incomplete if it produces no successful evidence. | Preserve the returned failure/limitation. Retry only as a new, permitted attempt when appropriate; do not reinterpret failure as an empty successful corpus. |
| Credential/auth failure | A credential-requiring research adapter suspends the exact request behind a credential gate rather than silently accepting a key or fabricating a result. | Configure the credential locally, inspect the current gate, obtain the user-authored exact decision, then resume with its issued `decision_id`. |
| `replayed: true` | The operation's existing idempotency record still points to a current artifact and registered export. The core returns the prior result without republishing or re-running transport. | Treat it as a successful replay, not as a request to make another file or network call. |
| `stale_revision`, `gate_mismatch`, or a superseded/unavailable decision | An upstream change invalidated the prior artifact/approval, or the request no longer matches its exact operation, subject, scope, or one-use decision. | Do not retry the old payload or decision. Inspect current state, regenerate/scaffold the current request if needed, and resolve the current gate. |
| `partial_failure` from `delete-run` | Deletion remained contained but one or more entries could not be removed. The result lists paths and error classes without requiring recursive shell cleanup. | Retain the `failures` list, correct the local filesystem cause, and rerun `delete-run`; do not delete siblings or follow links manually. |

### Replay is not a retry

The state store looks up `(run_id, operation, idempotency_key)` before a new publication. A matching record can replay only while its artifact remains current and its export registration exists; an invalidated record raises `StaleRevisionError` instead of resurrecting an old artifact. This prevents a once-valid report, research bundle, or gate result from becoming valid again after a changed dependency.

Research has a second idempotency layer. `ResearchStore` first checks the stored execution key; a match returns the stored success **or failure** without calling the adapter. Thus, repeating the same key is useful for deterministic replay and post-failure inspection, but it is not a way to retry a transient service failure. The SerpApi CLI chooses a fresh `-rN` attempt key for an unpinned prior failure; an explicit `--idempotency-key` intentionally pins the coordinate and therefore replays its stored result.

A `rate_limit` result is also not proof of quota exhaustion. The SerpApi path records it as an incomplete attempt and converts it to `quota_exhausted` only when the account preflight confirms the configured remaining-search threshold. A confirmed quota stop writes a private, manual-import template and does not fabricate research evidence. If the account check is unavailable, the result remains a rate-limit/retry situation rather than a made-up quota state.

## Research, credentials, and re-entry are egress boundaries

Before live SerpApi work, the CLI validates the contained run, its authoritative state, any supplied credential decision, and replay conditions before it calls the account endpoint or sends the credential. KIPRIS and generic credentialed research use the same principle inside the research runner: a missing credential or a forced second-pass approval raises a credential gate before adapter transport.

A credential gate is not a generic authorization token. Its approval scope binds the adapter and version, allowed host, capability, credential name, request fingerprint, and suspended `research.execute:...` operation. Batch scopes additionally carry the approved request/page/result ceilings. Resume validation rejects a stale decision, changed request revision, changed operation, increased page count or result budget, and a decision already used by a different operation.

### Re-research has stronger safeguards

A live second pass after a post-audit `re_research` decision has a special re-entry anchor. The runner derives it from the latest persisted re-research resolution only while no later `research_complete` event exists. It then applies these protections before live egress:

1. A stale anchor, absent/unbounded plan, or plan whose digest no longer matches its recorded `plan_hash` is refused. The recovery is to resolve current state or use a legitimate offline route—not to reuse a prior live approval.
2. The active re-entry decision ID salts the base idempotency key. This keeps a same-term second pass from colliding with or silently replaying the first pass's research/credential coordinate.
3. Even if a credential is already present in the environment, a second live pass with no fresh credential decision force-raises a gate. Its scope exposes the bounded plan, plan hash, re-research decision ID, and literal terms that will leave the machine.
4. A completed attempt can be replayed by the very decision that produced it with zero transport. Conversely, a different fresh attempt cannot reuse a spent finish coordinate: the normal fresh retry advances to a new `-rN` coordinate, while a pinned spent coordinate is refused before egress rather than silently returning another attempt's bundle.

These rules distinguish a supported replay from a new network attempt and prevent a newly fetched response from being discarded behind an older idempotency result. A successful offline/manual research publication can establish `research_complete` and end the live re-entry anchor; it is a bounded recovery path, not permission to bypass the evidence and state checks.

## Safe private-run deletion

Remove a run only with the core command; it deliberately accepts a path but no `--run-id`:

```bash
python3 -m patent_factory delete-run \
  --run workspace/runs/RUN --workspace-root workspace
```

The deletion routine first proves that both workspace and run are real directories, that the run is strictly contained below the workspace, and that no component of that path is a symlink. It recurses using `lstat`: a symlink inside the run is unlinked as the entry itself, never traversed. It records each removed workspace-relative entry. An `OSError` on one entry is accumulated as a redacted `{code, path}` failure while deletion continues where possible.

The command returns `status: "deleted"` and exit `0` only when that report has no failures. It returns `status: "partial_failure"`, the complete failure list, and exit `11` otherwise. Preserve that output exactly enough to remediate the local issue; do not replace it with `rm -rf`, direct SQLite deletion, export cleanup, or a best-effort sibling sweep.

## Redacted validation, calibration, and release evidence

The scripts below assemble evidence about checks, identities, hashes, and policy versions. They are not export/dump tools. Keep any resulting manifest within the private workspace unless a separately approved sharing workflow authorizes its disclosure.

### Local credential and quota probes

```bash
python3 scripts/check_credentials.py
python3 scripts/check_serpapi_quota.py
python3 scripts/check_serpapi_quota.py --live
```

`check_credentials.py` performs no network call and reports only whether `KIPRIS_PLUS_API_KEY` is configured, missing, simulated-invalid, or fixture-usable. It never prints the value. `check_serpapi_quota.py` is likewise offline by default and reports only redacted credential presence. Its explicit `--live` mode calls the SerpApi account endpoint to report remaining/plan/monthly counts; it does not print the API key and should be treated as an intentional credential-bearing network operation, not routine diagnostics.

### Recording trustworthy adapter fixtures

```bash
PYTHONPATH=src python3 scripts/record_fixtures.py --confirm-live --target kipris
PYTHONPATH=src python3 scripts/record_fixtures.py --confirm-live --target serpapi
```

Fixture recording is intentionally opt-in: `--confirm-live` and the relevant environment credential are both required. The recorder targets happy-path, empty/error, and pagination-shaped responses through `recording_transport`, at the transport boundary where raw bytes exist. That wrapper replaces all configured credential canaries, fails if a canary survives, pins selected volatile fields for byte-stable replay, writes the fixture owner-only, and returns the original response unchanged to the adapter. Do not use the recorder to publish raw service output in a diagnostic channel.

### Validate a run and evaluate calibration

```bash
python3 scripts/validate_run.py \
  --run workspace/runs/RUN --run-id RUN_ID --workspace-root workspace

python3 scripts/calibration_check.py \
  --manifest tests/fixtures/similarity/calibration/manifest.json
```

`validate_run.py` is a narrow path-bound wrapper around `python3 -m patent_factory validate`; it passes through the standard JSON result rather than guessing a run identifier. Validation itself is idempotent: a completed, unchanged validation returns its existing artifact as a replay rather than creating another revision.

Calibration is deliberately harder than a structurally plausible JSON file. `calibration_check.py` accepts only an `independent_redacted` manifest tied to the current scorer version/configuration hash, with at least two independent qualified reviewers, reviewer-complete labels, hash-only rationales and inputs, and explicit disagreement handling. A structurally qualified manifest is still `review_blocked` until its manifest hash appears in the repository's fixed calibration trust file. An absent or invalid manifest is `deferred_provisional`/`review_blocked`; invalid diagnostics disclose the error class, not labels or source text.

### Assemble the release manifest

Run the full verifier without `--skip-tests` for release evidence:

```bash
python3 scripts/release_verify.py \
  --run workspace/runs/RUN --run-id RUN_ID \
  --workspace-root workspace \
  --calibration-manifest tests/fixtures/similarity/calibration/manifest.json \
  --output workspace/release/manifest.json
```

The verifier first requires a contained, non-symlink run. It runs the ordinary test suite, the required named end-to-end evidence tests, `compileall`, path-bound validation, and the offline credential diagnostic. It then records command exit status and hashes of stdout/stderr rather than embedding command streams. Required evidence fails if a named test cannot load, does not run exactly once, skips, fails, errors, or has an unexpected/expected failure.

For the selected authoritative run, the manifest records only current artifact kind, revision ID, semantic content hash, and schema version; validation/scorer/schema/policy version hashes; platform/Python data; calibration status; test accounting; and privacy-scan counts/statuses. It does not include artifact bodies, report contents, credentials, raw test output, or private source material. The privacy scan checks captured command streams, run files, and tracked repository files for supplied canaries and a configured KIPRIS key; it also rejects unsafe tracked private roots, ignored-root policy failures, symlinks, unreadable files, and oversized/unscannable files.

Release status is intentionally three-valued:

| `release_status` | Meaning |
| --- | --- |
| `passed` | Core checks and both test gates passed, and calibration is trusted/eligible. |
| `review_blocked` | No core failure, but tests were skipped or incomplete, or calibration is absent, invalid, structurally untrusted, or otherwise not eligible. This is not a release pass. |
| `failed` | A core command, required test, run validation, containment/privacy scan, or equivalent hard check failed. |

When `--output` is supplied, the manifest writer creates a private contained directory as needed, writes owner-only canonical JSON through a temporary file and no-clobber hard link, and accepts an existing output only if its bytes are identical. A differently populated destination is an error, not an overwrite. `--skip-tests` is useful only for a deliberately incomplete local check: it is recorded as an explicit skip and cannot produce `passed`.

## Focused regression evidence

The reliability contract is backed by fault and concurrency tests rather than only successful-path tests.

| Test area | What it guards |
| --- | --- |
| `tests/integration/test_g002_publish_register_integration.py` | Fault injection before/after export and at database boundaries leaves authoritative database state rolled back; a post-export orphan is recoverable; concurrent publishers sharing one idempotency key yield one registered winner and one replay; invalidated artifacts cannot replay. |
| `tests/integration/test_g002_snapshot_and_integrity.py` | Read snapshots do not mix concurrent commits; corruption is refused without replacement; startup removes interruption/orphan residue and rejects missing or tampered registered bytes. |
| `tests/integration/test_research_reentry_guard.py` | Credential suspension occurs before unapproved egress, force-gated re-research scopes contain plan/term bindings, legal replays still work, and spent coordinates do not silently replay another attempt. |
| `tests/integration/test_g008_cli_core.py` | `delete-run` remains path-scoped, unlinks rather than follows embedded symlinks, preserves siblings/outside targets, and surfaces partial deletion failure as exit `11`. |
| `tests/e2e/test_release_evidence.py` | Path-only validation replays without changing state, calibration/test skips block a release, canaries in run files or captured output fail privacy checks without being echoed, and release output is private/no-clobber. |

When changing a publish path, adapter, state transition, or release script, extend the corresponding fault/replay/privacy test before treating a green happy path as sufficient. In particular, test an interrupted post-link/pre-commit publish, a current replay, an invalidated replay refusal, a concurrent writer, and the redaction behavior of any new diagnostic field.
