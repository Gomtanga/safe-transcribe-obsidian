---
name: safe-transcribe-obsidian
description: "Transcribe one or more local audio or video recordings with any available ASR engine, while preserving originals, recording model provenance, producing resumable and reviewable artifacts, auditing timestamps and likely hallucinations, merging a no-timeline text, and safely creating or updating an Obsidian note. Use for lectures, meetings, interviews, or voice recordings that need both transcription and structured Obsidian notes across macOS, Linux, Windows, local models, or authorized transcription APIs. Do not use for simple playback or text-only summarization."
---

# Safe Transcribe to Obsidian

## 목적

특정 운영체제, 전사 라이브러리, 모델 또는 공급자에 묶이지 않는 안전한 전사 파이프라인을 수행한다. 원본, 엔진 원시 출력, 공통 형식, 검토본, 무타임라인 합본, Obsidian 노트를 서로 분리한다. 자동 정제 결과로 원본이나 원시 전사를 덮어쓰지 않는다.

## 불변 원칙

1. 사용자가 지목한 입력 파일을 먼저 정확히 식별하고 자연수 순서와 녹음 메타데이터를 확인한다.
2. 입력 파일은 읽기 전용으로 취급한다. 이름 변경, 이동, 재인코딩, 태그 수정 또는 삭제는 별도 요청 없이는 하지 않는다.
3. 입력마다 크기, SHA-256, 형식, 길이를 기록한다. 길이를 확인할 도구가 없으면 추측하지 말고 `미확인`으로 남긴다.
4. 로컬 전사를 기본값으로 삼는다. 외부 API에 음성을 업로드해야 한다면 공급자와 전송 범위를 설명하고 사용자의 명시적 허가를 먼저 받는다.
5. 엔진 원시 출력과 정규화한 원시 전사를 보존한다. 정제는 별도의 검토본에만 적용하고 모든 제외 및 교정 결정을 기록한다. 실제 오디오 청취 근거가 없는 `exclude`는 금지하며, 대체 ASR·파형·신뢰도 지표는 청취를 대신하지 않는다.
6. 화자 분리(diarization)는 실제로 수행했거나 신뢰할 수 있는 화자 메타데이터가 있을 때만 표시한다.
7. `결정 기록`, `근거 검증`, `기계 검사 통과`, `부분 오디오 대조`, `전체 오디오 대조`를 구별한다. 결정 파일이 존재한다는 이유만으로 후보가 해결됐다고 표시하지 않으며, 부분 대조를 전체 검수 완료라고 표현하지 않는다.
8. 전사 중 나온 암호, 접근 키, 토큰, 계정 식별자 등 비밀정보는 Obsidian 노트에 옮기지 않는다. 필요한 경우 마스킹하고 누락 사실만 기록한다.
9. 기존 Obsidian 노트의 frontmatter, 위키링크, 임베드, callout, 수기 메모를 보존한다. 현재 사실과 강의 당시 설명이 다르면 기존 내용을 몰래 바꾸지 말고 별도 정정 블록에 근거와 확인일을 적는다.
10. 사용자가 타임라인 없는 합본을 요청하면 TXT에 시간 표시를 새로 넣지 않는다. 시간 정보는 검수용 JSON에 계속 보존한다.
11. `draft`와 `final` 납품을 구분한다. 초안 옵션도 근거 없는 삭제나 교체를 허용하는 우회 수단으로 사용하지 않는다.

## 참조 문서 선택

- 실행할 전사 엔진을 고르거나 결과 형식을 연결할 때 [engine-adapters.md](references/engine-adapters.md)를 읽는다.
- 공통 JSON 필드나 변환 규칙이 필요할 때 [canonical-schema.md](references/canonical-schema.md)를 읽는다.
- 자동 정제 또는 전사 품질 판정을 시작하기 전에 [qa-and-cleanup.md](references/qa-and-cleanup.md)를 반드시 읽는다.
- Obsidian 노트를 만들거나 수정할 때 [obsidian-integration.md](references/obsidian-integration.md)를 반드시 읽는다.

## 실행 요구사항

- 전사 엔진은 이 스킬에 내장하지 않는다. 사용자가 선택했거나 현재 환경에서 안전하게 실행할 수 있는 엔진을 연결한다.
- 보조 도구 `transcript_ops.py`는 Python 3.9 이상과 표준 라이브러리만 사용한다.
- `ffprobe`는 선택 사항이다. 없으면 원본 해시와 크기는 검사하지만 길이와 스트림 정보는 `미확인`으로 남긴다.
- Python을 사용할 수 없는 환경에서는 같은 단계와 공통 JSON 계약을 해당 환경의 도구로 구현하되, 검증 항목을 생략하지 않는다.

## 작업 흐름

### 1. 범위와 순서를 고정한다

- 입력 파일의 절대 경로, 예상 순서, 대상 날짜/강의명, 출력 디렉터리, 대상 vault와 노트 경로를 확인한다.
- 파일 번호가 빠졌거나 이름과 녹음 시간이 충돌하면 임의로 이어 붙이지 않는다. 안전한 범위까지 조사한 뒤 결과가 달라질 수 있으면 사용자에게 묻는다.
- 기존 노트와 기존 전사 산출물이 있으면 먼저 읽고, 덮어쓸 파일과 재사용할 파일을 구분한다.
- 출력은 기본적으로 작업공간 안의 새 작업 디렉터리에 둔다. 소스 디렉터리 안에 임시 청크를 섞지 않는다.

권장 구조:

```text
<job>/
  manifest.json
  state.json
  raw/native/<recording-id>.*
  raw/canonical/<recording-id>.json
  audit/<recording-id>.json
  review/decisions/<recording-id>.json
  review/canonical/<recording-id>.json
  merged/<job-id>.txt
  merged/<job-id>.json
  backup/<기존-note>.md
  delivery-report.json
```

### 2. 원본 목록을 만든다

스킬 경로를 실제 설치 경로로 해석한 뒤, 공통 도구로 원본을 조사한다.

```bash
TRANSCRIBE_SKILL_DIR="<resolved-skill-directory>"
python3 "$TRANSCRIBE_SKILL_DIR/scripts/transcript_ops.py" inspect \
  --output "<job>/manifest.json" \
  "<audio-1>" "<audio-2>"
```

- `ffprobe`가 있으면 길이와 스트림 정보를 함께 기록한다. 없으면 해시와 크기는 계속 기록하고 probe 경고를 남긴다.
- 원본 파일의 해시는 납품 검증 때 다시 계산한다.

### 3. 전사 엔진을 선택하고 고정한다

- 사용자가 엔진과 모델을 지정했다면 그대로 사용하되 실행 가능성과 개인정보 경계를 확인한다.
- 지정이 없으면 현재 환경에서 사용 가능한 로컬 엔진과 가속기를 먼저 조사한다. 모델 다운로드도 네트워크·용량 영향을 알리고 관련 정책을 따른다.
- 장시간 파일은 엔진 한도와 메모리에 맞춰 재개 가능한 청크로 나눈다. 별도 한도가 없을 때 로컬 실행의 출발점은 30분이며, 경계 문맥이 중요하면 짧은 겹침을 사용한다.
- 청크 ID에 녹음 ID와 시작 오프셋을 넣는다. 완료 여부, 명령, 모델, 버전, 언어, 설정, 오류를 `state.json`에 기록해 재실행 시 완료 청크를 건너뛴다.
- 환경에서 확인할 수 있다면 작업을 지휘한 에이전트 모델, 추론 수준, 스킬 경로 또는 스킬 파일 해시도 `state.json`에 기록한다. 확인할 수 없는 값은 추측하지 않는다.
- 고정된 모델과 설정을 모든 청크에 사용한다. 중간에 모델을 바꿨다면 같은 녹음의 단일 결과인 것처럼 합치지 말고 경계를 기록한다.

### 4. 원시 결과를 보존하고 공통 형식으로 변환한다

- 공급자 또는 로컬 엔진의 원시 결과를 `raw/native/`에 그대로 저장한다.
- 타임스탬프가 있는 JSON을 우선 요청한다. 텍스트만 받을 수 있으면 그 한계를 기록하고 없는 시간을 만들어내지 않는다.
- 다음 명령으로 일반적인 Whisper/OpenAI 호환 JSON을 공통 형식으로 변환한다. 인식하지 못한 형식은 [engine-adapters.md](references/engine-adapters.md)의 계약에 맞춘 얇은 변환기를 추가한다.

```bash
python3 "$TRANSCRIBE_SKILL_DIR/scripts/transcript_ops.py" normalize \
  --input "<job>/raw/native/<recording-id>.json" \
  --output "<job>/raw/canonical/<recording-id>.json" \
  --recording-id "<recording-id>" \
  --source-manifest "<job>/manifest.json" \
  --source-index 0 \
  --engine-name "<engine>" \
  --engine-model "<model>" \
  --language ko
```

정규화만으로 검수가 끝난 것이 아니다.

### 5. 기계 감사 후 오디오를 대조한다

```bash
python3 "$TRANSCRIBE_SKILL_DIR/scripts/transcript_ops.py" audit \
  --input "<job>/raw/canonical/<recording-id>.json" \
  --output "<job>/audit/<recording-id>.json"
```

- 오류와 검토 플래그를 먼저 처리한다. 그다음 시작, 중간, 끝, 무음 뒤 구간, 반복 문구, 낮은 신뢰도, 고유명사, 숫자, 포트, CIDR, 명령어를 실제 오디오와 대조한다.
- 대체 엔진이나 설정으로 후보 구간을 다시 디코딩하면 원본 범위, 엔진·모델·설정, 결과 경로, 기존 결과와의 충돌 여부를 `state.json` 또는 감사 기록에 남긴다. 문장 형태의 대체 결과가 나오면 직접 청취 전까지 기존 구간을 제외하지 않는다.
- 자동으로 의심 문구를 삭제하지 않는다. [review-decisions.template.json](assets/review-decisions.template.json) 구조의 결정 파일을 만들고 `keep`, `format_text`, `replace_text`, `exclude`, `set_speaker`를 명시한다.
- `reviewed_against_audio=true`이면 `audio_review.method=direct-listen`과 실제로 들은 `reviewed_ranges`를 기록한다. 파형 검사나 대체 디코딩만 수행했다면 `reviewed_against_audio=false`다.
- 모든 결정에는 이유가 필요하다. `exclude`는 해당 세그먼트 전체를 포함하는 `audio-listen` 근거가 필수다. `replace_text`와 `set_speaker`는 `audio-listen` 또는 출처를 적은 `authoritative-material` 근거가 필수다.
- 공백·문장 끝 부호·동등한 유니코드 표기만 바꿀 때는 `format_text`를 사용한다. 도구는 이를 제외한 문자 순서가 달라지면 거부하므로, 단어나 숫자가 달라지는 수정은 `replace_text`와 근거를 사용한다. 명령어·경로·식별자는 공백도 의미가 달라질 수 있으므로 자료나 오디오와 대조한다.
- 근거 없는 `keep`은 나중 검토를 위한 기록으로 남길 수 있지만 `acknowledged-unverified`이며 감사 플래그를 해결하지 않는다.
- 검토본은 다음처럼 별도 출력한다.

```bash
python3 "$TRANSCRIBE_SKILL_DIR/scripts/transcript_ops.py" review \
  --input "<job>/raw/canonical/<recording-id>.json" \
  --decisions "<job>/review/decisions/<recording-id>.json" \
  --output "<job>/review/canonical/<recording-id>.json"
```

### 6. 순서를 명시해 무타임라인 합본을 만든다

```bash
python3 "$TRANSCRIBE_SKILL_DIR/scripts/transcript_ops.py" merge \
  --inputs \
    "<job>/review/canonical/<first>.json" \
    "<job>/review/canonical/<second>.json" \
  --output-txt "<job>/merged/<job-id>.txt" \
  --output-json "<job>/merged/<job-id>.json" \
  --heading id
```

- 입력 순서는 명령에 적힌 순서다. 파일시스템의 임의 정렬에 맡기지 않는다.
- 기본값은 검토 완료 또는 부분 검토 결과만 합친다. 검토하지 않은 결과를 꼭 합쳐야 할 때만 `--allow-unreviewed`를 사용하고 산출물과 최종 보고에 그 사실을 표시한다.
- `--allow-unreviewed`는 안전한 비파괴 초안만 허용한다. 근거 없는 삭제·교체·화자 지정이 들어 있는 기존 산출물은 합치지 말고 검토 결정을 복구한다.
- TXT는 읽기용 합본이며 타임라인을 추가하지 않는다. JSON은 출처, 검토 상태, 시간축을 유지하는 감사용 합본이다.

### 7. 대분류를 먼저 만든 뒤 Obsidian에 통합한다

- 전체 검토본을 한 번 훑어 주제 후보를 모은다.
- `대분류 → 핵심 개념 → 세부 내용 → 실습 및 명령 → 질문과 답변 → 주의 및 정정` 순서로 분류 지도를 먼저 만든다.
- 같은 설명이 여러 녹음에 반복되면 한 항목으로 통합하되, 상충하는 설명은 병합하지 말고 차이를 남긴다.
- [obsidian-note-template.md](assets/obsidian-note-template.md)를 출발점으로 사용하되 기존 노트가 있으면 템플릿으로 통째로 교체하지 않는다.
- 대상 노트가 이미 존재하면 작업 디렉터리의 `backup/`에 원본 복사본을 남긴 뒤 최소 범위로 수정한다.
- 강의에서 직접 나온 내용, 전사상 불확실한 내용, 별도로 확인한 최신 정보, 편집자의 해설을 문장 수준에서 구분한다.

### 8. 납품본을 검증한다

```bash
python3 "$TRANSCRIBE_SKILL_DIR/scripts/transcript_ops.py" validate \
  --manifest "<job>/manifest.json" \
  --canonical "<job>/review/canonical/<first>.json" \
  --canonical "<job>/review/canonical/<second>.json" \
  --merged-txt "<job>/merged/<job-id>.txt" \
  --note "<vault>/<note>.md" \
  --vault-root "<vault>" \
  --delivery-mode final \
  --report "<job>/delivery-report.json"
```

오디오 검수 전 안전한 원시 전사와 노트 초안만 점검할 때는 `--delivery-mode draft`를 사용한다. `final`은 기본값이며 `reviewed` 또는 `reviewed-partial` canonical만 허용한다. 어느 모드도 근거 없는 파괴적 편집은 허용하지 않는다.

검증 항목:

- 원본 존재 여부와 SHA-256 일치
- 공통 JSON 구조, 시간축 단조성, 소스 길이 초과 여부
- 합본의 비어 있지 않음과 자동 타임라인 표식 부재
- Obsidian frontmatter와 코드 펜스 균형, `대분류` 존재, 내부 링크 해석 가능성
- 비밀 키와 토큰 패턴, 노트에 남은 민감 식별자 검토
- 각 입력의 산출물 존재와 검토 상태
- 삭제·교체·화자 지정의 근거와 실제 청취 범위
- `artifact_integrity_status`, `content_qa_status`, `delivery_readiness`의 분리

검증 경고를 지우려고 원문을 임의로 바꾸지 않는다. 원인과 처리 결정을 기록한다.

## 완료 보고

최종 응답에는 다음을 짧고 명확하게 적는다.

- 처리한 원본 파일과 순서
- 사용한 엔진, 모델, 언어, 주요 설정
- 원본 보존 및 해시 검증 결과
- 전사, 감사, 오디오 대조, 합본, Obsidian 반영 각각의 상태
- 검토하지 못한 구간, 화자 분리 한계, 고유명사와 숫자 등 남은 불확실성
- 생성하거나 수정한 산출물의 절대 경로

`audit`의 `machine-checks-passed`는 검토 후보가 없다는 뜻일 뿐 오디오 QA 완료가 아니다. `validate`가 구조 검증을 통과했더라도 `content_qa_status`와 `delivery_readiness`를 함께 보고한다. 실제 오디오 전체를 대조하지 않았다면 “독립적인 최종 전사 QA는 부분적”이라고 명시하고, `delivery_readiness=draft-only` 또는 `blocked`인 결과를 “완료”라고 단독 표현하지 않는다.
