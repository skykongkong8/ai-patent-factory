# Codex Patent Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Expose the eight existing patent workflow stages as explicit Codex skills, stopping after each user-requested stage.

**Architecture:** Repository skills named `patent-<stage>` read the corresponding `.claude/commands/<stage>.md` as the shared procedure. `.codex/workflow.md` owns Codex invocation, dependency routing, stage boundaries, and output conventions. The Python JSON CLI remains the state and gate authority.

**Tech Stack:** Markdown, Codex `agents/openai.yaml`, Python standard-library unittest.

**Spec:** User-approved chat design: `$patent-setup`, `$patent-research`, `$patent-ideate`, `$patent-shortlist`, `$patent-audit`, `$patent-checkpoint`, `$patent-draft`, `$patent-review`; explicit-only invocation, no automatic next stage, shared Claude procedures, existing gate and privacy boundaries.

## Global Constraints

- Preserve existing user edits and private input files; perform no workflow state mutation during implementation.
- Keep existing `.claude` commands and Python workflow behavior authoritative.
- `review` is the final existing stage; no `preview` stage is added.
- `review` includes independent review and validation; external sharing requires a separate explicit request and its existing gate.
- Preserve the existing internal ideate scaffold/validation checkpoints and resume with another `$patent-ideate` invocation.
- All eight public skills have `policy.allow_implicit_invocation: false`.
- Use only standard-library automated tests and public instructions/fixtures for agent application checks.

## Review Focus

- A source command's next-step suggestion must not execute another stage.
- A generic local helper must not bypass explicit user invocation.
- References must resolve in a fresh clone, without ignored local migration files.
- Missing or ambiguous run/input selection must not mutate the wrong run.
- A checkpoint approval must return to the user rather than execute draft.

### Task 1: Explicit stage skills and shared execution contract

**Files:** Create `.agents/skills/patent-{setup,research,ideate,shortlist,audit,checkpoint,draft,review}/SKILL.md` and each `agents/openai.yaml`; create `.codex/workflow.md`; modify `AGENTS.md`; test `tests/unit/test_codex_workflow_skills.py`.

**Interfaces:** Consumes the eight `.claude/commands/*.md` and on-demand `.claude/skills/*/SKILL.md`; produces explicit `$patent-<stage>` entrypoints with portable repository-relative links.

- [x] Run fresh-consumer baseline scenarios against current local migration files.
- [x] Add configuration/reference tests for eight discoverable unique names, explicit-only policies, and resolvable shared dependencies; observe expected failures before implementation.
- [x] Write the shared Codex contract, routing helper references to canonical `.claude` files and translating suggested slash commands to `$patent-*`.
- [x] Create and verify each public entrypoint and its policy, using one shared procedure rather than copied stage bodies.
- [x] Add a short AGENTS routing rule applying explicit stage boundaries even when older local migration helpers are discovered.
- [x] Run `python3 -m unittest tests.unit.test_codex_workflow_skills -v`; expect all checks passing.

### Task 2: User documentation and complete validation

**Files:** Modify `.codex/README.md`, `README.md`, and the obsolete best-effort wording assertion in `tests/unit/test_g008_agent_surfaces.py`.

**Interfaces:** Consumes Task 1 entrypoint names; produces usage examples, explicit stop/resume behavior, and a retained CLI reference.

- [x] Document all eight calls, an explicit single-document setup example, input/run selection, ideate resumption, and review/share boundaries.
- [x] Preserve the existing portable CLI examples and privacy/gate contracts; replace the outdated best-effort-only UX statement.
- [x] Run the new and existing agent-surface tests.
- [x] Run fresh-consumer application scenarios for all eight stages and a final independent review; fix important findings with regression coverage.
- [x] Run `python3 -m unittest discover -s tests -p 'test_*.py'`, `python3 -m compileall -q src tests`, and `python3 -m patent_factory --help`; retain complete failure output.
- [x] Verify tracked/staged delivery includes new `.agents` files despite the local exclude, and report the tested scope without claiming live-client end-to-end execution.

## Execution record

- Work in the existing feature checkout to preserve the requested local deliverable and unrelated user edits. Scope staging to new skill files that local `.git/info/exclude` would otherwise hide; do not stage existing user changes or publish a commit/push.
- Plan and implementation were authorized together by the user's approval; continue inline without another approval gate.


## Verification record

- Baseline fresh-consumer pass found missing `patent-*` names, invalid `.Codex/skills/` references, ideate continuation without internal stops, and checkpoint continuation into another stage in ignored migration copies.
- New configuration/reference tests: observed 25 expected failing assertions before implementation; all 4 tests pass after implementation.
- Combined new/existing agent-surface checks: 16 tests pass.
- Official skill-creator `quick_validate.py`: all 8 skills pass.
- Independent fresh-consumer final review: all 8 scenarios pass; no Critical, Important, or Minor findings. This is an instruction/application review, not execution through a live Codex client.
- `python3 -m compileall -q src tests`, CLI help/version, and scoped/staged whitespace checks pass.
- Full offline suite: 571 tests, 568 passed, 2 failed, 1 skipped; complete output retained at `/tmp/codex-patent-workflow-unittest.log`.
- Both failures are in `tests.e2e.test_release_evidence.ReleaseEvidenceTests`: `test_release_manifest_is_review_blocked_without_labels_and_detects_canary` and `test_skipped_tests_are_explicit_and_cannot_pass_with_qualified_calibration`. Release privacy scanning reports `unscannable_count=31`, matching the 31 preexisting deleted tracked `openwiki/` files. Neither privacy scans nor user-owned deletions were bypassed or changed.
- Comparison: the same two release tests pass in an unchanged-HEAD temporary worktree without those deletions (2/2, log `/tmp/codex-patent-workflow-baseline-tests.log`). The temporary worktree was removed.
- Ruling: preserve user-owned OpenWiki changes rather than restoring pages or weakening release scans; the current checkout's full-suite result remains failed until that independent working-tree condition is resolved.
- Ruling: deliver locally in the existing feature checkout, staging only new deliverable files needed for tracking. No implementation commit, merge, push, or workflow run against user data was requested or performed.
- Scope limits retained from the reviewer: actual Codex composer discovery/invocation and live/private pipeline operations were not exercised; repository configuration and dependencies plus public-instruction scenarios were checked. Existing unrelated user edits remain outside the change.


## User-authorized publication follow-up

The user subsequently requested committing this implementation and pushing it
straight to `origin/main`. This supersedes the initial turn's no-publication
ruling. Include only the 23 implementation files; stage only the Codex section
of AGENTS.md and preserve unrelated user changes. Verify the resulting commit
in a clean temporary worktree before a non-force main push.
