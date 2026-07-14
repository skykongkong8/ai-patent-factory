# Codex portable JSON CLI mapping

Codex는 Claude slash command를 실행하지 않는다. 대신 같은 `python3 -m patent_factory` JSON CLI를 직접 호출한다. Python 코어가 유일한 상태·게이트·내보내기 권위이므로 Codex가 SQLite, JSON/JSONL/Markdown export, 상태 포인터를 직접 수정하지 않는다.

## 공통 규칙

```bash
python3 -m patent_factory --version
python3 -m patent_factory --help
```

- stdout의 정렬된 JSON 한 개를 읽는다. 모든 결과는 `schema_version: cli-result-v1`과 `envelope_version: cli-envelope-v1`을 가진다.
- `help/version` probe만 의도적으로 plain text다. 그 밖의 누락되거나 잘못된 인자는 `invalid_arguments` JSON 결과다.
- 일부 안전한 중단 상태는 nonzero exit를 사용하므로 exit code만으로 JSON을 버리지 않는다.
- `*_required`, `coverage_insufficient`, `decision_required`, `insufficient_evidence`, `revision_required`, `stopped`, `error`이면 중단하고 gate/state/hash를 보고한다.
- `gate inspect`는 읽기 전용이다. `gate decide`에는 사용자가 현재 subject hash와 scope에 대해 작성한 `gate-decision-input-v1`이 필요하다. Codex는 action/reason/approval/decision ID를 만들지 않는다.
- 입력은 documents root, 실행과 versioned request는 workspace root 아래의 상대 비-symlink 경로만 사용한다.

## Setup, profile, run bootstrap

```bash
python3 -m patent_factory init
python3 -m patent_factory profile folder documents
python3 -m patent_factory profile document documents/input.md
python3 -m patent_factory profile interview --responses documents/responses.json
```

세 profile 경로 중 하나만 선택한다. `conflict_resolution_required`이면 batch를 보존하고 사용자 결정을 기다린다. SQLite와 `profile.json`을 직접 수정하지 않는다.

새 private workflow run은 권위 profile을 결합해 `research_ready`로 시작한다. 기본 profile 경로를 쓸 때 profile 옵션은 생략할 수 있다.

```bash
python3 -m patent_factory run start --run workspace/runs/RUN --run-id RUN --profile workspace/profile.json --profile-database workspace/profile.sqlite3
```

## Research

```bash
python3 -m patent_factory research fixture documents/kipris.xml --run workspace/runs/RUN --run-id RUN --query QUERY
python3 -m patent_factory research manual documents/results.json --run workspace/runs/RUN --run-id RUN --query QUERY --allow-host HOST
```

실패는 adapter event/coverage limitation이며 evidence가 아니다. credential/paid-service gate가 pending이면 네트워크 요청은 허용되지 않는다. 호스트나 budget을 임의로 넓히지 않는다.

## Ideation과 shortlist

```bash
python3 -m patent_factory ideate --run workspace/runs/RUN --run-id RUN --profile workspace/profile.json --profile-database workspace/profile.sqlite3 --input workspace/requests/candidate-input-v1.json
python3 -m patent_factory shortlist --run workspace/runs/RUN --run-id RUN --input workspace/requests/shortlist-input-v1.json
```

입력 계약은 `candidate-input-v1`, `shortlist-input-v1`이다. `domain_pivot_required`는 자동 승인하지 않는다. 세 개의 방어 가능한 finalist가 없으면 `insufficient_evidence`를 유지한다.

## Finalist audit

```bash
python3 -m patent_factory audit retrieve --run workspace/runs/RUN --run-id RUN --query-input workspace/requests/audit-query-input-v1.json --fixture-manifest documents/requests/audit-fixture-manifest-v1.json
python3 -m patent_factory audit score --run workspace/runs/RUN --run-id RUN --feature-input workspace/requests/feature-map-set-input-v1.json
```

각 finalist의 별도 KIPRIS query group을 사용한다. `simrisk-v1.0.0` 계산은 코어에 맡긴다. `coverage_insufficient` 또는 `decision_required`이면 draft로 진행하지 않는다. `R_hi < 75` 자동 승인 여부도 코어만 결정한다.

## Exact gate inspection/decision

```bash
python3 -m patent_factory gate inspect --run workspace/runs/RUN --run-id RUN --gate-id GATE_ID
python3 -m patent_factory gate decide --run workspace/runs/RUN --run-id RUN --gate-id GATE_ID --input workspace/requests/gate-decision-input-v1.json
```

결정 후에는 코어가 반환한 정확한 `decision_id`와 원래의 동일 입력으로만 suspended operation을 재개한다. changed content/hash/scope에는 새 게이트가 필요하다.

## Draft, independent review, validation

```bash
python3 -m patent_factory draft --run workspace/runs/RUN --run-id RUN --input workspace/requests/report-input-v1.json
python3 -m patent_factory review --run workspace/runs/RUN --run-id RUN --input workspace/requests/review-input-v1.json
python3 -m patent_factory validate --run workspace/runs/RUN --run-id RUN
```

reviewer identity/pass는 drafter와 달라야 한다. `revision_required`이면 validate하지 않는다. `reviewed` 후 deterministic validate의 `complete`만 완료다. Codex는 draft/review/validation export를 직접 고치지 않는다.

## Guarded external share

```bash
python3 -m patent_factory share --run workspace/runs/RUN --run-id RUN --input workspace/requests/external-report-share-v1.json
```

`sensitive_disclosure_required`이면 중단한다. 사용자가 exact recipient, purpose, destination, report hash, sensitive fields를 승인한 현재 결정이 있을 때만 동일 명령에 코어가 반환한 `--decision-id DECISION_ID`를 추가한다. 직접 파일을 복사하는 것은 share gate 우회다.

## Privacy와 hosted egress

로컬 CLI 실행과 모델 컨텍스트 전송은 별개다. Claude Code, Codex 및 다른 호스팅 모델은 외부 처리자이므로 raw documents, source spans, profile/proprietary facts, reports, secrets를 컨텍스트에 읽지 않는다. 정확한 recipient/model class, purpose, approved data classes, scope와 content hash에 대한 현재 승인, 필드 최소화, canary 검사, egress manifest가 모두 있어야 한다. 이 문서나 Codex session은 그러한 승인을 생성하지 않는다. Secret은 어떤 경우에도 egress/persistence하지 않는다.

## Cleanup과 release checks

실행 삭제도 Python 코어의 safe `delete-run` JSON CLI만 사용한다.

```bash
python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace
```

이 명령은 `run-id`를 받지 않으며 `cli-result-v1`/`cli-envelope-v1`과 deletion report를 반환한다. partial failure/status를 그대로 보고한다. SQLite, exports, logs 또는 sibling run을 `rm`이나 symlink-following 도구로 직접 삭제하지 않는다.

코드 cleanup은 동작 고정 테스트 후 별도 작은 diff로 수행하고, 다음 오프라인 확인을 다시 실행한다.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q src tests
python3 -m patent_factory --help
```

## Claude Code와의 UX 차이

- Claude Code: `.claude/commands/*` slash command와 `.claude/skills/*`가 정규 UX다.
- Codex: 이 문서의 명령을 직접 호출하는 best-effort portable smoke surface다. slash command 자동 인자 수집이나 skill discovery parity를 가정하지 않는다.
- 두 표면 모두 같은 CLI/parser, versioned JSON inputs, SQLite state kernel, gate policy와 validators를 사용한다. Codex 고유 UX 실패는 제한으로 기록할 수 있지만 core bypass나 privacy/egress 완화 사유가 되지 않는다.
