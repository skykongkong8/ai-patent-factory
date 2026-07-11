# 설정

CPython 3.11 이상만 필요하며 제3자 런타임 패키지는 없습니다. `python3 -m patent_factory --help`로 확인하고 `python3 -m patent_factory init`으로 소유자 전용 `documents/`, `workspace/`를 준비합니다.

## 세 가지 프로필 경로

```bash
# UTF-8 .md/.txt/.json 파일을 이름순으로 수집
python3 -m patent_factory profile folder documents

# 문서 하나만 수집
python3 -m patent_factory profile document documents/background.md

# 실제 대화형 질문(터미널 필요)
python3 -m patent_factory profile interview

# 재현 가능한 scripted interview (응답 파일도 documents/ 아래에 둠)
cp examples/redacted/interview.json documents/interview.json
python3 -m patent_factory profile interview --responses documents/interview.json
```

텍스트 문서는 `field: value`, JSON은 일반 객체 또는 `facts` 배열 형식입니다. stdout은 정렬 키의 JSON 하나입니다. 기본 권위 저장소는 `workspace/profile.sqlite3`이고 `workspace/profile.json`은 커밋된 DB에서 만든 결정적 내보내기입니다. 문서 항목은 `source_fact`, 인터뷰 답변은 `user_statement`입니다. 동일 입력의 재실행은 배치·claim·충돌을 중복 생성하지 않습니다. 기존 값과 충돌하는 항목이 한 배치에 하나라도 있으면 그 트랜잭션은 충돌 배치와 충돌 레코드 및 상태만 기록하고, canonical fact나 호환 가능한 추가 항목은 하나도 적용하지 않은 채 종료 코드 3을 반환합니다.

기본 루트는 `documents/`와 `workspace/`이며 각각 `--documents-root`, `--workspace-root`로 저장소 안의 상대 경로를 지정할 수 있습니다. 입력과 `--responses` 파일은 documents root 아래, `--database`와 `--profile`은 workspace root 아래여야 합니다. 절대 경로, `..`, 루트/중간/대상 symlink, 비정규 파일, 읽기 전 2,000,000바이트를 넘는 문서는 거부합니다.

향후 KIPRIS Plus 자격 증명의 정확한 이름은 `KIPRIS_PLUS_API_KEY`입니다. 값은 환경에만 두고 프로필/로그/문서에 복사하지 마십시오. G001 기능은 네트워크를 사용하지 않습니다. 호스팅 Claude/Codex에 비공개 원문을 제공하는 것은 별도의 외부 전송이므로 명시적 범위 승인 전에는 로컬 CLI만 사용합니다.
