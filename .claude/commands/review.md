# /review

`.claude/skills/patent-review/SKILL.md`를 따른 별도 reviewer pass에서 실행한다. 작성자와 다른 identity/pass를 가진 `review-input-v1`만 전달한다.

```bash
python3 -m patent_factory review --run RUN --run-id RUN_ID --input REVIEW_INPUT
python3 -m patent_factory validate --run RUN --run-id RUN_ID
```

`review` JSON이 `revision_required`이면 `validate`하지 않고 중단한다. `reviewed`일 때만 deterministic `validate`를 실행한다. `validate`의 `complete`만 완료로 보고하며, `*_required`, `stopped`, `error`를 우회하지 않는다.

외부 공유는 별도 `external-report-share-v1` 요청으로만 시도한다.

```bash
python3 -m patent_factory share --run RUN --run-id RUN_ID --input SHARE_INPUT
```

`sensitive_disclosure_required`이면 `gate_id`, `subject_revision_hash`, exact scope를 보존하고 중단한다. 에이전트가 approve/redact/stop을 선택하거나 approval을 만들지 않는다. 사용자가 현재 `gate-decision-input-v1`로 결정한 뒤 코어가 반환한 정확한 `decision_id`가 있을 때만 동일 share 입력에 `--decision-id`를 추가해 재개한다. 파일 복사는 공유가 아니다.
