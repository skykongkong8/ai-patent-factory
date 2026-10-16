# Inventor, topic and pinned run contexts

Approved design: one local operator manages inventor -> tagged topic -> multiple
runs. Published profile/topic versions are immutable. Replacement starts empty;
enrichment starts from the prior version. Existing runs retain their exact context.
Legacy data stays unassigned until explicitly adopted. No hard deletion, rebase,
authentication, hosted disclosure, or private-data migration in this change.

## Tasks and verification

- [x] Catalog and versions: opaque identities, archive, snapshots, conflict drafts,
  compare-and-swap activation, recovery and scope-safe paths; unittest first.
- [x] CLI and pinned run resolver: managed setup/topic entrypoints, v2 context,
  per-run requests, cross-scope preflight before any network call.
- [x] Legacy adoption and deletion indexing; preserve existing bytes and v1 hashes.
- [x] Offline multi-inventor end-to-end tests, docs and all stage entrypoints.
- [x] Independent review; full unittest, compileall and help checks.

## Execution ledger

Pre-flight: catalog produces immutable version records consumed by CLI resolution;
run resolver produces pinned profile/context consumed by scaffold and ideation.
Catalog implementation and CLI integration own separate files. Legacy v1 remains
supported without a catalog; once a catalog exists, unassigned writes are refused.

Ruling: implement in the current feature branch, preserving the existing unrelated
`.openclaude/settings.local.json` change. The shared managed workspace is the
delivery destination; no branch switch, worktree relocation, commit or push.

Ruling: management names remain local data; list/status default to metadata only.
Explicit exact-name resolution may return matching IDs without profile contents.

Verification: catalog/legacy 38 tests passed; pinned contexts 13 tests passed;
managed CLI boundary tests passed; two independent public-fixture journeys reached
reviewed and complete; 12 unchanged documentation conformance tests passed.

Independent review: fixed root-override bypasses (including interview responses),
request outputs targeting authoritative DBs, loss of deletion receipts on index
failure, and silent resurrection of deleted run IDs. Regression tests observed
RED then GREEN. Deletion intent is durable; retry reconciles an absent directory
without inventing a prior list of removed files.

Ruling: preserve KIPRIS's existing repeatable --inventor search expansion. Owner
selection is --inventor-id on that command; other commands retain --inventor as
an owner alias. This avoids breaking existing bounded search inputs.

Baseline: the initial 571-test suite already had two release-evidence failures
and one skip. The privacy scanner rejects the pre-existing 2,513,990-byte tracked
visualization logo above its 1,000,000-byte limit; no scanner policy was weakened.
Complete failure output was retained locally during implementation; the PR records the exact failing test names and cause.

Ruling: registered workspace ownership cannot be bypassed by selecting an ancestor
or descendant root. Independent legacy fixtures use sibling roots. This preserves
explicit adoption for unassigned legacy runs instead of guessing their ownership.

Final source verification used the authoritative unittest discovery command.
Final targeted verification: 80 managed/catalog/legacy/pinned-context/batch tests
passed; 41 legacy CLI/profile/research compatibility tests passed. Corrupt legacy
databases emit a JSON error without escaping the CLI envelope.

Final full suite: 652 tests in 199.449s; 649 passed, 1 skipped, 2 pre-existing
release-evidence failures (unchanged oversized tracked logo scanner limitation).
No new failures. Compileall, CLI help and git diff --check passed. Existing
private data was not read into model context, adopted, replaced or migrated.
