---
name: patent-research
description: Use when the user explicitly invokes $patent-research to start a run and gather bounded research evidence.
---

# Patent research

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Execute only the requested **research** stage using the shared
   [research procedure](../../../.claude/commands/research.md), resolving its helper
   references through that contract.
3. Report the result and return control to the user at the contract's boundary.
