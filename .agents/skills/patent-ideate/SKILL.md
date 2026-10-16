---
name: patent-ideate
description: Use when the user explicitly invokes $patent-ideate to resume the next bounded ideation workbench step.
---

# Patent ideate

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Resolve the inventor/topic or fixed run scope through
   [inventor management](../../../docs/inventor-management.md); preserve its IDs
   and keep workspace requests under the selected run.
3. Execute only the requested **ideate** stage using the shared
   [ideate procedure](../../../.claude/commands/ideate.md), resolving its helper
   references through that contract.
4. Report the selected IDs, versions, paths, and result, then return control to
   the user at the contract's boundary.
