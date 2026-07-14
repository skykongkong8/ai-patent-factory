# /audit

`.claude/skills/research/SKILL.md`와 `AGENTS.md`를 따른다. 각 finalist를 위한 `audit-query-input-v1` 및 fixture용 `audit-fixture-manifest-v1`을 코어에 전달한다.

```bash
python3 -m patent_factory audit retrieve --run RUN --run-id RUN_ID --query-input AUDIT_QUERY_INPUT --fixture-manifest FIXTURE_MANIFEST
python3 -m patent_factory audit score --run RUN --run-id RUN_ID --feature-input FEATURE_MAP_SET_INPUT
```

`FEATURE_MAP_SET_INPUT`은 검토된 `feature-map-set-input-v1`이다. scorer는 `simrisk-v1.0.0`이며 래퍼가 점수, corpus, feature map 또는 label을 다시 계산하지 않는다.

stdout JSON이 `credential_required`, `coverage_insufficient`, `decision_required`, 다른 `*_required`, `stopped`, `error`이면 즉시 중단한다. 자동 승인은 충분한 coverage와 `R_hi < 75`일 때 코어만 판단한다. excessive 결과의 retain/refine/replace/research/stop 선택을 에이전트가 대신하지 않으며, 불완전한 coverage를 0으로 채우지 않는다.
