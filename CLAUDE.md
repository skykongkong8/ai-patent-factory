# Claude Code 계약

1. `README.md`, `SETUP.md`, 이 파일을 먼저 읽는다.
2. 원문과 프로필은 `documents/`, `workspace/` 아래에만 둔다.
3. SQLite `workspace/profile.sqlite3`만 권위 상태로 취급한다. `profile.json`은 결정적 내보내기이므로 직접 편집하지 않고 `conflict_resolution_required`를 우회하지 않는다.
4. 비공개 원문을 모델 컨텍스트에 넣기 전 전송 범위를 확인한다. 로컬 CLI 자체는 네트워크를 쓰지 않는다.
5. `agent_inference`에는 반드시 `rationale`이 필요하며 확정 사실처럼 표현하지 않는다.
6. 특허성, 신규성, 유효성, 비침해/FTO 법률 결론을 내리지 않는다.

7. 모든 입력/응답은 configured documents root, DB/내보내기는 configured workspace root 아래의 상대 비-symlink 정규 경로만 사용한다.

초기화: `python3 -m patent_factory init` 후 `/setup` 또는 `python3 -m patent_factory profile --help`.
