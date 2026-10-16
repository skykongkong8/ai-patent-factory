# Patkong visualization implementation record

Approved specification: the Agent-Quest integration plan in this conversation.

Goal: optional local observation of this project's Codex and Claude sessions,
with a Patkong factory theme and the upstream detail panels and map editor.

Constraints: no Python API changes; no pipeline execution or gate decisions;
no private workspace/document access; server-side project filtering; loopback
only; no AI-review exports or session-content persistence. Preserve existing edits.

## Tasks

- [x] Import Agent-Quest at 736eeea65607b122ede92c502f0a6e56483c473f with licenses.
- [x] Test and implement project-scoped session ingestion and local-only serving.
- [x] Add factory terrain, buildings, Patkong sprites, and Korean primary labels.
- [x] Document setup and verify synthetic providers, browser behavior, and Python checks.
- [x] Review the complete change and fix material findings.

Implementation rulings and verification results are recorded below.

Ruling: implement in the supplied checkout without changing branches or creating
a worktree, because the approved plan preserves the user's existing changes and
the optional UI is a new, isolated directory. No commits or pushes are requested.

Ruling: the project observer does not install lifecycle hooks into global Claude
settings. Log polling provides the required observation without changing CLI setup.


Verification record:
- Imported upstream with the original MIT license and bundled CC0 credits.
- Bun: 404 tests passed, including project-boundary HTTP/WebSocket/reconnect,
  first-discovery and incremental partial-line tests for both log providers.
- Server and client TypeScript checks passed; frontend build passed. Vite reports
  the inherited Phaser bundle exceeding its 500 kB advisory chunk threshold.
- All 38 active-theme asset paths verified.
- Browser: synthetic Codex, Claude and child sprites observed moving; detail
  selection and map editor loading/saving verified. Real private logs not read.
- Python: 571 tests, 2 failures, 1 skip, matching the baseline. Both failures are
  in test_release_evidence: test_release_manifest_is_review_blocked_without_labels_and_detects_canary
  and test_skipped_tests_are_explicit_and_cannot_pass_with_qualified_calibration.
  Their release privacy checks report 31 unscannable tracked paths, matching
  pre-existing deleted OpenWiki files. No Python runtime code was changed.
- compileall and CLI --help/--version passed.
- Independent review found two first-discovery line-boundary bugs, reproduced
  with failing synthetic tests and fixed: Codex incomplete tails were lost;
  Claude could parse a tail twice by rereading past the consumed prefix.

Final browser check: latest prompt is visible in the detail panel; map save returned
Saved. Synthetic demo was stopped and its temporary logs removed after QA.
Responsive CSS is included; a mobile viewport override did not change the
connected Chrome viewport, so mobile rendering is not claimed as verified.
