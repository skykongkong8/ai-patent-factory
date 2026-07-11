# /setup

`SETUP.md`와 `CLAUDE.md`를 읽고 `python3 -m patent_factory init`을 실행한 뒤 사용자가 고른 folder/document/interview 경로 하나만 실행한다. 입력/응답은 documents root, DB/내보내기는 workspace root 아래의 상대 경로만 쓴다. SQLite가 권위 저장소이며 `profile.json`은 내보내기다. JSON이 `conflict_resolution_required`이면 멈추고 DB나 내보내기를 직접 덮어쓰지 않는다. 비공개 원문을 출력하지 않는다.
