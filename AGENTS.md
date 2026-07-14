# AI Patent Factory 에이전트 계약

이 저장소의 에이전트 표면은 `patent_factory` JSON CLI를 안내하는 얇은 래퍼다. 상태 전이, 게이트, 무효화, 멱등성, 내보내기는 Python 코어만 수행한다.

## 권위와 실행 경계

- 먼저 `README.md`, `SETUP.md`, `CLAUDE.md`를 읽는다.
- `python3 -m patent_factory --version`과 `python3 -m patent_factory --help`로 설치된 계약을 확인한다.
- 명령 결과는 stdout의 JSON 한 개로 판단한다. 모든 결과는 `schema_version: cli-result-v1`과 `envelope_version: cli-envelope-v1`을 가진다.
- SQLite나 `workspace/`의 불변 내보내기를 직접 읽어 상태를 추측하거나 수정하지 않는다.
- SQL, 임의 스크립트, 파일 복사/이동으로 상태·게이트·보고서·검토·검증·공유 영수증을 만들지 않는다.
- 모든 입력은 configured documents root, 모든 실행/입력 계약은 configured workspace root 아래의 상대 비-symlink 경로를 사용한다.

## 게이트 처리

`*_required`, `coverage_insufficient`, `decision_required`, `insufficient_evidence`, `revision_required`, `stopped`, `error`는 자동 진행 허가가 아니다. stdout JSON의 `gate_id`, `subject_revision_hash`, `actions`, `next_state`를 보존하고 중단한다. 필요하면 다음 읽기 전용 명령으로 현재 게이트만 확인한다.

```bash
python3 -m patent_factory gate inspect --run RUN --run-id RUN_ID --gate-id GATE_ID
```

사용자가 현재 주제·범위에 대해 작성한 `gate-decision-input-v1` 파일이 있을 때만 `gate decide`를 호출한다. 에이전트는 승인, 결정 ID, 승인 범위 또는 사용자 이유를 대신 만들지 않는다. 재개는 코어가 반환한 정확한 `decision_id`와 원래 명령의 동일한 입력으로만 한다.

## 개인정보, 외부 전송, 법적 경계

- `documents/`, `workspace/`, `.env`의 원문·프로필·비밀을 채팅이나 호스팅 모델 컨텍스트에 넣지 않는다.
- Claude Code, Codex 및 다른 호스팅 모델은 외부 처리자다. 정확한 수신자/목적/데이터 클래스/콘텐츠 해시/범위에 대한 현재 승인과 최소화된 egress manifest가 없으면 로컬 CLI만 실행하고 중단한다. 이 문서와 래퍼는 외부 전송을 승인하지 않는다.
- `share`는 `external-report-share-v1` 요청과 현재의 정확한 민감정보 공개 결정이 필요하다. 게이트를 우회해 보고서를 복사하지 않는다.
- 유사도는 검색된 코퍼스 내 연구 보조 지표다. 특허성, 신규성, 진보성, 유효성, 비침해/FTO 또는 법률 자문 결론을 작성하지 않는다.

## 검토, 정리, 릴리스

초안 작성자와 검토자는 서로 다른 identity와 pass를 사용한다. `reviewed`가 아니면 `validate`하지 않고, `revision_required`이면 초안으로 돌아간다.

비공개 실행 정리는 safe core command만 사용한다. `run-id`는 받지 않는다.

```bash
python3 -m patent_factory delete-run --run workspace/runs/RUN --workspace-root workspace
```

삭제 JSON의 partial failure/status를 보존한다. DB, export, log, symlink 또는 sibling run을 직접 삭제하지 않는다. 릴리스 전에는 최소한 아래 오프라인 검증을 실행하고 실패나 생략을 명시한다.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q src tests
python3 -m patent_factory --help
```

코드 정리는 동작 고정 테스트 후 별도 작은 변경으로 수행한다.
