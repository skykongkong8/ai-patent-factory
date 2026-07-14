# /setup

`README.md`, `SETUP.md`, `CLAUDE.md`, `AGENTS.md`를 읽는다. 이 명령은 JSON CLI의 얇은 안내자이며 파일이나 SQLite 상태를 직접 만들지 않는다.

1. 설치 계약과 비공개 루트를 확인한다.

```bash
python3 -m patent_factory --version
python3 -m patent_factory init
```

2. 사용자가 고른 경로 하나만 실행한다.

```bash
python3 -m patent_factory profile folder documents
python3 -m patent_factory profile document documents/background.md
python3 -m patent_factory profile interview --responses documents/interview.json
```

입력/응답은 documents root, DB/내보내기는 workspace root 아래의 상대 비-symlink 경로만 쓴다. stdout의 JSON 한 개가 결과다. SQLite가 권위 저장소이고 `profile.json`은 결정적 내보내기이므로 둘 다 직접 수정하지 않는다.

`status`가 `conflict_resolution_required`이면 `batch_id`를 보존하고 중단한다. 사용자가 현재 충돌 배치에 대한 versioned decision input을 제공하기 전에는 `profile conflict-decide`를 호출하거나 값을 고르지 않는다. 비공개 원문을 출력하거나 호스팅 모델 컨텍스트에 로드하지 않는다. 이 래퍼는 외부 전송을 승인하지 않는다.
