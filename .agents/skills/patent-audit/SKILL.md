---
name: patent-audit
description: Use when the user explicitly invokes $patent-audit to retrieve and score finalist similarity evidence.
---

# Patent audit

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Execute only the requested **audit** stage using the shared
   [audit procedure](../../../.claude/commands/audit.md), resolving its helper
   references through that contract.
3. Report the result and return control to the user at the contract's boundary.
