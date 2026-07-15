# `documents/` — your private input

This folder holds the **private material you provide**: inventor background,
reference notes, interview answers, and any local source files a research/audit
stage reads. `/setup` (and the raw `profile` verbs) read from here to build your
inventor profile.

Everything under this folder **except this README is git-ignored**, so your private
text is never committed. It is safe to re-run `/setup` as you add files — the core
merges idempotently and stops at a conflict gate instead of overwriting anything.

## Supported formats

| Format | Read by `/setup`? | Notes |
| --- | --- | --- |
| `.md` | Yes | UTF-8 plain text. `field: value` lines become profile facts. |
| `.txt` | Yes | UTF-8 plain text, same `field: value` convention. |
| `.json` | Yes | A plain object of `field: value`, or a `{"facts": [...]}` array. |
| `.pdf`, `.docx`, images | No | Not parsed. Convert to UTF-8 `.md`/`.txt` first. |

Hard rules (enforced by the CLI): UTF-8 only; **symlinks are rejected** (root,
intermediate, or target); no absolute paths or `..`; each document must be a regular
file **under 2,000,000 bytes**.

## What each `/setup` path reads

- **Folder** (`profile folder documents`) — ingests every supported file in the
  folder, in filename order. Use this once your notes are organized. Items ingested
  from documents are recorded as `source_fact`.
- **Document** (`profile document documents/background.md`) — ingests a single file.
  Good for adding one new source.
- **Interview** (`profile interview`) — asks questions in the terminal; answers are
  recorded as `user_statement`. For a reproducible run, provide a scripted response
  file (see below).

### Text / Markdown example (`field: value`)

```
name: 홍길동
expertise: 분산 시스템
project_summary: 센서 데이터 처리 지연을 줄이는 방법
technical_domain: 산업용 데이터 처리
```

### Scripted interview file

Copy the redacted sample and edit the answers, then pass it to the interview path.
It is a flat JSON object of `field: answer`:

```json
{
  "name": "홍길동",
  "expertise": "분산 시스템",
  "project_summary": "센서 데이터 처리 지연을 줄이는 방법",
  "technical_domain": "산업용 데이터 처리"
}
```

```bash
cp examples/redacted/interview.json documents/interview.json
# then run /setup and choose the interview path, or:
python3 -m patent_factory profile interview --responses documents/interview.json
```

## Local source files for research / audit

Later stages also read **local source files from here** (path rules require inputs to
live under this root), for example:

- `documents/kipris.xml` — a fixture source for `/research`.
- `documents/manual-results.json` — user-supplied results for a manual research
  import.
- `documents/requests/audit-fixture-manifest-v1.json` — the fixture manifest for `/audit`.

Naming is up to you; the command asks for the exact path.

## Privacy

Placing a file here does **not** authorize sending its contents to a hosted model.
Running the local CLI is separate from putting private text into model context — the
tool keeps your material local until you explicitly approve an external share. Never
put secrets (e.g. `KIPRIS_PLUS_API_KEY`) in these files; keep them in the environment.
