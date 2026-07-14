# /research

`SETUP.md`, `CLAUDE.md`, `AGENTS.md`와 `.claude/skills/research/SKILL.md`를 따른다. 원문을 읽거나 요약하지 말고 사용자가 지정한 경로를 로컬 JSON CLI에 전달한다.

권위 profile을 새 private run에 결합해 `research_ready`로 시작한다. 기본 profile 경로를 쓸 때 `--profile`과 `--profile-database`는 생략할 수 있다.

```bash
python3 -m patent_factory run start --run RUN --run-id RUN_ID --profile PROFILE --profile-database PROFILE_DATABASE
```

Fixture 계약:

```bash
python3 -m patent_factory research fixture SOURCE --run RUN --run-id RUN_ID --query QUERY
```

사용자 제공 수동 결과 계약:

```bash
python3 -m patent_factory research manual SOURCE --run RUN --run-id RUN_ID --query QUERY --allow-host HOST
```

stdout JSON의 `status`, `next_state`, adapter failure/coverage를 그대로 보고한다. `credential_required`, `research_incomplete`, 다른 `*_required`, `stopped`, `error`이면 중단한다. 실패를 evidence로 바꾸거나 unrestricted host를 추가하지 않는다. 호스팅 Claude 컨텍스트로 비공개 profile/source를 가져오는 것은 별도 외부 전송이며, 정확한 범위 승인과 egress manifest 없이는 금지된다.
