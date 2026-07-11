# Codex portable mapping

Claude command 없이 같은 JSON CLI를 직접 호출합니다.

```bash
python3 -m patent_factory init
python3 -m patent_factory profile folder documents
python3 -m patent_factory profile document documents/input.md
python3 -m patent_factory profile interview --responses responses.json
```

Slash-command UX만 다르며 프로필 정규화, 출처, 멱등성, 충돌 중단은 같습니다. 모델 컨텍스트로의 비공개 자료 전송은 CLI 실행과 별도 승인이 필요합니다.
