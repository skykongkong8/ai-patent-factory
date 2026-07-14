# /draft

`CLAUDE.md`, `AGENTS.md`, `.claude/skills/ideation/SKILL.md`를 따른다. 현재 승인된 artifact hash에 묶인 `report-input-v1` JSON만 전달한다.

```bash
python3 -m patent_factory draft --run RUN --run-id RUN_ID --input REPORT_INPUT
```

코어가 한국어 11개 섹션과 citation/decision bindings를 렌더링한다. 래퍼가 `draft.md`나 report export를 직접 쓰거나 수정하지 않는다. unresolved evidence, stale audit/decision, `coverage_insufficient`, `decision_required`, 다른 `*_required`, `stopped`, `error`이면 중단한다. 특허성·신규성·유효성·비침해/FTO 결론을 추가하지 않는다.
