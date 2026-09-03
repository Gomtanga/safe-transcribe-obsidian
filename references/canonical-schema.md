# 공통 전사 JSON 형식

공통 형식은 엔진 원시 출력의 대체물이 아니라 감사, 검토, 합본을 위한 안정된 중간 표현이다. 원시 출력은 별도로 보존한다.

## 최상위 필드

```json
{
  "schema_version": "1.0",
  "recording_id": "day06-033",
  "stage": "raw",
  "language": "ko",
  "source": {
    "path": "/absolute/path/음성 033.m4a",
    "name": "음성 033.m4a",
    "sha256": "...",
    "size_bytes": 123,
    "duration_seconds": 1800.25
  },
  "engine": {
    "name": "engine-name",
    "model": "model-name",
    "version": null,
    "settings": {},
    "generated_at": "2026-09-02T00:00:00Z"
  },
  "lineage": {
    "native_result_path": "/path/to/raw/native.json",
    "native_result_sha256": "..."
  },
  "speaker_diarization": {
    "status": "not_run",
    "method": null
  },
  "review": {
    "status": "unreviewed",
    "reviewed_against_audio": false,
    "scope": "none",
    "audio_review": {
      "method": null,
      "coverage_confirmed": false,
      "reviewed_ranges": []
    },
    "decisions": [],
    "removed_segments": [],
    "decision_summary": {
      "recorded": 0,
      "evidence_reviewed": 0,
      "acknowledged_unverified": 0
    }
  },
  "segments": [],
  "text": ""
}
```

## 세그먼트

```json
{
  "id": 0,
  "start": 0.0,
  "end": 4.2,
  "text": "예시 문장입니다.",
  "speaker": null,
  "confidence": null,
  "metrics": {
    "avg_logprob": -0.32,
    "no_speech_prob": 0.02,
    "compression_ratio": 1.1
  },
  "words": [
    {
      "start": 0.1,
      "end": 0.8,
      "text": "예시",
      "probability": 0.91
    }
  ]
}
```

- 시간 단위는 초다.
- `start`와 `end`를 제공할 수 없으면 `null`을 쓴다. 임의 시간은 금지한다.
- `confidence`는 공급자가 명시적으로 confidence로 제공한 값만 넣는다.
- log probability, no-speech probability, compression ratio 등은 원래 이름으로 `metrics`에 둔다.
- 단어 시간과 확률이 없으면 `words`를 생략하거나 빈 배열로 둔다.
- `speaker`는 출처가 있는 경우만 채운다.

## 단계와 검토 상태

| `stage` | 의미 |
|---|---|
| `raw` | 엔진 출력을 정규화했지만 오디오 대조 전 |
| `edited-unverified` | 편집은 했지만 실제 오디오를 대조하지 않음 |
| `reviewed-partial` | 플래그 구간 또는 표본만 오디오 대조 |
| `reviewed` | 전체 범위를 오디오와 대조했다고 명시 |

`review.status`도 같은 의미를 반복해 소비자가 최상위 단계만 보더라도 실수하지 않게 한다. 기계 감사 성공은 어느 검토 단계도 자동으로 올리지 않는다.

## 결정 파일

```json
{
  "reviewer": "Codex with human-authorized audio review",
  "reviewed_against_audio": true,
  "scope": "flagged-only",
  "audio_review": {
    "method": "direct-listen",
    "coverage_confirmed": false,
    "reviewed_ranges": [
      {
        "start": 42.0,
        "end": 55.0,
        "reason": "환각 후보 세그먼트 직접 청취"
      }
    ]
  },
  "decisions": [
    {
      "segment_id": 17,
      "action": "replace_text",
      "text": "교정한 문장",
      "reason": "해당 구간 오디오를 재생해 확인",
      "evidence": {
        "type": "audio-listen",
        "source_start": 42.0,
        "source_end": 48.0
      }
    },
    {
      "segment_id": 18,
      "action": "exclude",
      "reason": "긴 무음 뒤 반복된 비음성 환각을 오디오로 확인",
      "evidence": {
        "type": "audio-listen",
        "source_start": 48.0,
        "source_end": 55.0
      }
    }
  ]
}
```

지원 행동:

- `keep`: 의심 플래그가 있지만 유지한다. 이유를 남긴다.
- `format_text`: 단어와 숫자는 그대로 두고 공백, 문장 끝 부호, 동등한 유니코드 표기만 정리한다. 그 밖의 문자 순서가 달라지면 적용 도구가 거부한다. 명령어·경로·식별자에는 사용하지 않는다.
- `replace_text`: 텍스트를 교정한다. 원문과 교정문은 검토본의 결정 기록에 모두 남는다.
- `exclude`: 활성 세그먼트에서 제외하되 `removed_segments`에 원문을 보존한다.
- `set_speaker`: 확인 가능한 근거가 있을 때만 화자 라벨을 설정한다.

`scope`는 `none`, `flagged-only`, `sampled`, `full` 중 하나다. `reviewed_against_audio=true`와 `scope=full`이 모두 충족되어야 `reviewed`가 된다.

`reviewed_against_audio=true`에는 `audio_review.method=direct-listen`과 하나 이상의 `reviewed_ranges`가 필요하다. `scope=full`이면 `coverage_confirmed=true`이고 기록한 범위가 타임스탬프가 있는 모든 canonical 세그먼트를 덮어야 한다. 파형 검사와 대체 ASR 비교는 직접 청취로 기록하지 않는다.

결정별 `evidence.type`은 `audio-listen`, `authoritative-material`, `alternate-asr`, `machine-metric`, `context-inference` 중 하나다. 적용 도구는 다음을 강제한다.

- `exclude`: `audio-listen`만 허용
- `format_text`: 별도 근거 없이 허용하되 내용 변경은 거부
- `replace_text`, `set_speaker`: `audio-listen` 또는 `authoritative-material`만 허용
- `keep`: 근거 없이 기록할 수 있지만 `acknowledged-unverified`로 남고 감사 플래그를 해결하지 않음

적용된 각 결정에는 `verification_status`가 추가된다.

- `evidence-reviewed`: 직접 청취 또는 권위 있는 자료에 근거
- `acknowledged-unverified`: 결정은 기록됐지만 증거 검수 전

`removed_segments`에는 원문, 제외 이유, `exclusion_evidence`를 보존한다. 근거 없는 파괴적 편집이 남아 있는 canonical은 `merge`가 거부하며 `validate`는 오류로 보고한다.

## 합본 JSON

합본 JSON은 녹음별 문서를 `recordings` 배열로 유지한다. 서로 다른 녹음의 시간축을 억지로 하나로 만들지 않는다. `ordered_recording_ids`와 명령 인자 순서가 일치해야 한다. 읽기용 TXT에는 시간 필드를 출력하지 않는다.

## 납품 검증 상태

`delivery-report.json`은 단일 성공 문자열만 사용하지 않고 다음 필드를 함께 제공한다.

```json
{
  "delivery_mode": "final",
  "artifact_integrity_status": "passed",
  "content_qa_status": "reviewed-partial",
  "delivery_readiness": "ready-with-limitations"
}
```

- `artifact_integrity_status`: 소스 해시, JSON·시간축, 합본, Obsidian 구조 상태
- `content_qa_status`: 결정 근거와 오디오 대조 상태
- `delivery_readiness`: 최종 사용 가능성 또는 초안·차단 상태
