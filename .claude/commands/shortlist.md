# /shortlist

`.claude/skills/ideation/SKILL.md`를 따르고 검토된 `shortlist-input-v1`만 CLI에 전달한다.

```bash
python3 -m patent_factory shortlist --run RUN --run-id RUN_ID --input SHORTLIST_INPUT
```

각 finalist의 세 독립 축, rationale, confidence, supporting/contrary evidence, gaps가 있어야 한다. stdout JSON이 `insufficient_evidence`이면 약한 후보를 만들지 말고 중단한다. `*_required`, `stopped`, `error`도 자동 진행하지 않는다. SQLite, candidate/finalist export 또는 상태 포인터를 직접 수정하지 않는다.
