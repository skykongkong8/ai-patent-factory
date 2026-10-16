---
name: patent-setup
description: Use when the user explicitly invokes $patent-setup to initialize or enrich the inventor profile.
---

# Patent setup

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Execute only the requested **setup** stage using the shared
   [setup procedure](../../../.claude/commands/setup.md), resolving its helper
   references through that contract.
3. Report the result and return control to the user at the contract's boundary.
