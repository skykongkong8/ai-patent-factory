---
name: patent-draft
description: Use when the user explicitly invokes $patent-draft to render the invention report after checkpoint approval.
---

# Patent draft

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Resolve the inventor/topic or fixed run scope through
   [inventor management](../../../docs/inventor-management.md); preserve its IDs
   and keep workspace requests under the selected run.
3. Execute only the requested **draft** stage using the shared
   [draft procedure](../../../.claude/commands/draft.md), resolving its helper
   references through that contract.
4. Report the selected IDs, versions, paths, and result, then return control to
   the user at the contract's boundary.
