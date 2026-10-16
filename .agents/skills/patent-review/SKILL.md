---
name: patent-review
description: Use when the user explicitly invokes $patent-review to independently review and validate the invention report.
---

# Patent review

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Execute only the requested **review** stage using the shared
   [review procedure](../../../.claude/commands/review.md), resolving its helper
   references through that contract.
3. Report the result and return control to the user at the contract's boundary.
