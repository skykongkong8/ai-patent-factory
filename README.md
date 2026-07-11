# AI Patent Factory

로컬에서 발명가 프로필을 결정적으로 구성하는 한국어 우선 CLI입니다. CPython 3.11 이상에서 다음 명령으로 개인정보 보호용 루트를 준비합니다.

```bash
python3 -m patent_factory init
```

이 명령은 비공개 원문용 `documents/`와 생성 프로필·작업 산출물용 `workspace/`를 소유자 전용 권한으로 만듭니다. 원문과 프로필은 이 두 루트 밖에 두지 마십시오.

## 프로필 빠른 시작

```bash
# 폴더의 지원 문서를 재귀 수집
python3 -m patent_factory profile folder documents

# 문서 하나 수집
python3 -m patent_factory profile document documents/background.md

# 터미널 대화형 인터뷰
python3 -m patent_factory profile interview
```

SQLite `workspace/profile.sqlite3`가 사실, claim/provenance, 수집 배치, 충돌, 현재 상태의 유일한 권위 저장소입니다. `workspace/profile.json`은 커밋된 SQLite 상태에서 매번 결정적으로 다시 만든 비공개 내보내기일 뿐이며 직접 편집하지 않습니다. 설치·입력 형식과 scripted interview는 [SETUP.md](SETUP.md), 에이전트의 개인정보·추론 규칙은 [CLAUDE.md](CLAUDE.md)를 참조하세요.

## 법적 경계

이 도구의 결과는 발명 정리 보조 자료이며 법률 자문이 아닙니다. 특허성, 신규성, 유효성 또는 비침해/FTO에 관한 법적 결론을 제공하지 않으며, 필요한 판단은 자격 있는 변리사·변호사와 확인해야 합니다.
