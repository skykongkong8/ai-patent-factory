# /ideate

`.claude/skills/ideation/SKILL.md`를 따른다. `candidate-input-v1` JSON은 최소 세 후보의 근거·반대 근거·공백·여섯 epistemic label을 보존하는 versioned CLI 입력이다. 승인 없이 비공개 원문을 모델 컨텍스트에 로드하지 않는다.

```bash
python3 -m patent_factory ideate --run RUN --run-id RUN_ID --profile PROFILE --profile-database PROFILE_DATABASE --input CANDIDATE_INPUT
```

stdout JSON이 `domain_pivot_required`, `insufficient_evidence`, 다른 `*_required`, `stopped`, `error`이면 중단한다. `gate_id`를 보존하되 에이전트가 pivot을 승인하거나 `decision_id`를 만들지 않는다. 사용자가 정확한 현재 주제의 결정을 완료한 뒤에만 동일 입력과 코어가 반환한 `--decision-id`로 재개한다. 후보 JSON이나 내보내기를 복사해 상태 전이를 흉내 내지 않는다.
