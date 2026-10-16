---
name: patent-draft
description: Use when the user explicitly invokes $patent-draft to render the invention report after checkpoint approval.
---

# Patent draft

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Execute only the requested **draft** stage using the shared
   [draft procedure](../../../.claude/commands/draft.md), resolving its helper
   references through that contract.
3. Report the result and return control to the user at the contract's boundary.
