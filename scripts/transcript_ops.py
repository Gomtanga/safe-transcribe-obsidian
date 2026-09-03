#!/usr/bin/env python3
"""Portable, source-preserving helpers for transcription workflows.

This tool deliberately does not run an ASR model. It inspects source media,
normalizes common ASR JSON, audits it, applies explicit review decisions,
merges reviewed transcripts, and validates delivery artifacts.

Only Python's standard library is required. ffprobe is optional for media
duration and stream metadata.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SCHEMA_VERSION = "1.0"
VALID_REVIEW_SCOPES = {"none", "flagged-only", "sampled", "full"}
VALID_REVIEW_ACTIONS = {
    "keep",
    "format_text",
    "replace_text",
    "exclude",
    "set_speaker",
}
REVIEWED_STAGES = {"reviewed", "reviewed-partial"}
VALID_EVIDENCE_TYPES = {
    "audio-listen",
    "authoritative-material",
    "alternate-asr",
    "machine-metric",
    "context-inference",
}
EVIDENCE_REVIEWED_TYPES = {"audio-listen", "authoritative-material"}
DESTRUCTIVE_REVIEW_ACTIONS = {"replace_text", "exclude", "set_speaker"}
VALID_DELIVERY_MODES = {"draft", "final"}

SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:password|passwd|passphrase|secret|token|authorization|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)"
)

SUSPICIOUS_PHRASES = (
    "시청해 주셔서 감사합니다",
    "시청해주셔서 감사합니다",
    "구독과 좋아요",
    "자막 제공",
    "자막제공",
    "thank you for watching",
    "please subscribe",
)

TIMELINE_LINE_RE = re.compile(
    r"(?m)^\s*(?:"
    r"\[\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]"
    r"|\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\s*(?:-->|→|-)"
    r")"
)

SECRET_PATTERNS: Sequence[Tuple[str, re.Pattern[str]]] = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "authorization-bearer",
        re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "labeled-secret",
        re.compile(
            r"(?i)\b(?:api[_ -]?key|access[_ -]?key|secret[_ -]?key|client[_ -]?secret|password|token)\b"
            r"\s*[:=]\s*[\"']?(?!<REDACTED>|REDACTED|\$\{|\{\{)[A-Za-z0-9_./+~=-]{8,}"
        ),
    ),
)

IPV4_OR_CIDR_RE = re.compile(
    r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?:/(?:[12]?\d|3[0-2]))?(?![\d.])"
)
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


class UserError(Exception):
    """An expected, actionable input or validation error."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise UserError(f"JSON file does not exist: {path}") from exc
    except UnicodeDecodeError as exc:
        raise UserError(f"JSON is not valid UTF-8: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UserError(f"Invalid JSON: {path}: {exc}") from exc


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def write_json_atomic(path: Path, value: Any) -> None:
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def ensure_distinct_paths(input_path: Path, output_path: Path) -> None:
    if input_path.resolve(strict=False) == output_path.resolve(strict=False):
        raise UserError(f"Refusing to overwrite an input artifact: {input_path}")


def to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def redact_settings(value: Any, parent_key: str = "") -> Any:
    if parent_key and SENSITIVE_KEY_RE.search(parent_key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(key): redact_settings(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_settings(item) for item in value]
    if isinstance(value, str) and re.search(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", value):
        return "[REDACTED]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def inspect_media_file(path: Path, ffprobe: Optional[str]) -> Dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise UserError(f"Source file does not exist: {resolved}")
    if not resolved.is_file():
        raise UserError(f"Source is not a regular file: {resolved}")

    stat = resolved.stat()
    item: Dict[str, Any] = {
        "path": str(resolved),
        "name": resolved.name,
        "suffix": resolved.suffix.lower(),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(resolved),
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "duration_seconds": None,
        "format_name": None,
        "audio_streams": [],
        "probe_status": "unavailable" if not ffprobe else "pending",
        "warnings": [],
    }

    if not ffprobe:
        item["warnings"].append("ffprobe not found; duration and stream metadata were not verified")
        return item

    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(resolved),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        item["probe_status"] = "failed"
        item["warnings"].append(f"ffprobe could not run: {type(exc).__name__}")
        return item

    if completed.returncode != 0:
        item["probe_status"] = "failed"
        item["warnings"].append(f"ffprobe exited with code {completed.returncode}")
        return item

    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError:
        item["probe_status"] = "failed"
        item["warnings"].append("ffprobe returned invalid JSON")
        return item

    format_info = probe.get("format") if isinstance(probe, dict) else None
    if not isinstance(format_info, dict):
        format_info = {}
    item["format_name"] = format_info.get("format_name")
    item["duration_seconds"] = to_float(format_info.get("duration"))

    streams = probe.get("streams", []) if isinstance(probe, dict) else []
    stream_durations: List[float] = []
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
                continue
            stream_duration = to_float(stream.get("duration"))
            if stream_duration is not None:
                stream_durations.append(stream_duration)
            item["audio_streams"].append(
                {
                    "index": to_int(stream.get("index")),
                    "codec_name": stream.get("codec_name"),
                    "sample_rate": to_int(stream.get("sample_rate")),
                    "channels": to_int(stream.get("channels")),
                    "channel_layout": stream.get("channel_layout"),
                    "bit_rate": to_int(stream.get("bit_rate")),
                    "duration_seconds": stream_duration,
                }
            )

    if item["duration_seconds"] is None and stream_durations:
        item["duration_seconds"] = max(stream_durations)
    if not item["audio_streams"]:
        item["warnings"].append("ffprobe found no audio stream")
    item["probe_status"] = "ok"
    return item


def create_manifest(
    paths: Sequence[Path], ffprobe: Optional[str], require_probe: bool
) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    for order, path in enumerate(paths, start=1):
        item = inspect_media_file(path, ffprobe)
        item["order"] = order
        if require_probe and item["probe_status"] != "ok":
            raise UserError(f"Required ffprobe metadata is unavailable for {path}")
        files.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "source-media-manifest",
        "created_at": utc_now(),
        "ffprobe": ffprobe,
        "files": files,
    }


def _first(mapping: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _time_value(mapping: Dict[str, Any], base: str) -> Optional[float]:
    millisecond_keys = (f"{base}_ms", f"{base}Ms", f"{base}_milliseconds")
    value = _first(mapping, millisecond_keys)
    if value is not None:
        parsed = to_float(value)
        return parsed / 1000.0 if parsed is not None else None
    aliases = {
        "start": ("start", "start_time", "startTime"),
        "end": ("end", "end_time", "endTime"),
    }
    return to_float(_first(mapping, aliases[base]))


def _extract_native_payload(raw: Any) -> Tuple[List[Any], str, Optional[str]]:
    if isinstance(raw, list):
        return raw, "", None
    if isinstance(raw, str):
        return [], raw, None
    if not isinstance(raw, dict):
        raise UserError("Native ASR JSON must be an object, a segment list, or a JSON string")

    language = _first(raw, ("language", "language_code", "detected_language"))
    text_value = _first(raw, ("text", "transcript", "transcription"))
    text = text_value if isinstance(text_value, str) else ""

    for key in ("segments", "utterances"):
        candidate = raw.get(key)
        if isinstance(candidate, list):
            return candidate, text, str(language) if language is not None else None

    results = raw.get("results")
    if isinstance(results, dict):
        channels = results.get("channels")
        if isinstance(channels, list) and channels:
            channel = channels[0]
            if isinstance(channel, dict):
                alternatives = channel.get("alternatives")
                if isinstance(alternatives, list) and alternatives:
                    alternative = alternatives[0]
                    if isinstance(alternative, dict):
                        deepgram_text = alternative.get("transcript")
                        words = alternative.get("words")
                        if isinstance(deepgram_text, str):
                            text = deepgram_text
                        if isinstance(words, list) and words:
                            segment: Dict[str, Any] = {
                                "text": text,
                                "words": words,
                                "start": _time_value(words[0], "start")
                                if isinstance(words[0], dict)
                                else None,
                                "end": _time_value(words[-1], "end")
                                if isinstance(words[-1], dict)
                                else None,
                            }
                            return [segment], text, str(language) if language is not None else None

    words = raw.get("words")
    if isinstance(words, list) and words:
        segment = {
            "text": text,
            "words": words,
            "start": _time_value(words[0], "start") if isinstance(words[0], dict) else None,
            "end": _time_value(words[-1], "end") if isinstance(words[-1], dict) else None,
        }
        return [segment], text, str(language) if language is not None else None

    return [], text, str(language) if language is not None else None


def _normalize_word(word: Any, offset: float) -> Optional[Dict[str, Any]]:
    if not isinstance(word, dict):
        return None
    text_value = _first(word, ("word", "text", "punctuated_word", "token"))
    text = str(text_value).strip() if text_value is not None else ""
    start = _time_value(word, "start")
    end = _time_value(word, "end")
    if start is not None:
        start += offset
    if end is not None:
        end += offset
    result: Dict[str, Any] = {"start": start, "end": end, "text": text}
    probability = to_float(word.get("probability"))
    confidence = to_float(word.get("confidence"))
    if probability is not None:
        result["probability"] = probability
    if confidence is not None:
        result["confidence"] = confidence
    speaker = _first(word, ("speaker", "speaker_label", "speaker_id"))
    if speaker is not None:
        result["speaker"] = str(speaker)
    return result


def _normalize_segment(segment: Any, index: int, offset: float) -> Dict[str, Any]:
    if isinstance(segment, str):
        segment = {"text": segment}
    if not isinstance(segment, dict):
        raise UserError(f"Segment {index} is not an object or string")

    timestamp = segment.get("timestamp")
    start = _time_value(segment, "start")
    end = _time_value(segment, "end")
    if isinstance(timestamp, (list, tuple)) and len(timestamp) >= 2:
        if start is None:
            start = to_float(timestamp[0])
        if end is None:
            end = to_float(timestamp[1])
    if start is not None:
        start += offset
    if end is not None:
        end += offset

    text_value = _first(segment, ("text", "transcript", "utterance"))
    text = str(text_value).strip() if text_value is not None else ""
    speaker_value = _first(segment, ("speaker", "speaker_label", "speaker_id"))
    confidence = to_float(segment.get("confidence"))

    result: Dict[str, Any] = {
        "id": index,
        "start": start,
        "end": end,
        "text": text,
        "speaker": str(speaker_value) if speaker_value is not None else None,
        "confidence": confidence,
        "metrics": {},
    }
    if "id" in segment and segment["id"] != index:
        result["native_id"] = segment["id"]

    raw_metrics = segment.get("metrics")
    if isinstance(raw_metrics, dict):
        for key, value in raw_metrics.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                result["metrics"][str(key)] = value
    for key in (
        "avg_logprob",
        "no_speech_prob",
        "compression_ratio",
        "temperature",
        "seek",
    ):
        if key in segment and segment[key] is not None:
            result["metrics"][key] = segment[key]

    words = segment.get("words")
    if isinstance(words, list):
        normalized_words = [
            normalized
            for normalized in (_normalize_word(word, offset) for word in words)
            if normalized is not None
        ]
        result["words"] = normalized_words
    return result


def _source_from_manifest(path: Path, index: int) -> Dict[str, Any]:
    manifest = load_json(path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise UserError(f"Invalid source manifest: {path}")
    files = manifest["files"]
    if index < 0 or index >= len(files):
        raise UserError(f"source-index {index} is out of range for {path}")
    item = files[index]
    if not isinstance(item, dict):
        raise UserError(f"Manifest file entry {index} is invalid")
    return {
        "path": item.get("path"),
        "name": item.get("name"),
        "sha256": item.get("sha256"),
        "size_bytes": item.get("size_bytes"),
        "duration_seconds": item.get("duration_seconds"),
    }


def _source_from_path(path: Path) -> Dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise UserError(f"Source file does not exist: {resolved}")
    return {
        "path": str(resolved),
        "name": resolved.name,
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
        "duration_seconds": None,
    }


def join_segment_text(segments: Sequence[Dict[str, Any]]) -> str:
    paragraphs: List[str] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def normalize_document(
    raw: Any,
    native_path: Path,
    recording_id: str,
    source: Dict[str, Any],
    engine_name: str,
    engine_model: str,
    engine_version: Optional[str],
    settings: Dict[str, Any],
    language_override: Optional[str],
    time_offset: float,
) -> Dict[str, Any]:
    native_segments, native_text, native_language = _extract_native_payload(raw)
    segments = [
        _normalize_segment(segment, index, time_offset)
        for index, segment in enumerate(native_segments)
    ]
    if not segments and native_text.strip():
        segments = [
            {
                "id": 0,
                "start": None,
                "end": None,
                "text": native_text.strip(),
                "speaker": None,
                "confidence": None,
                "metrics": {},
            }
        ]

    has_speaker = any(segment.get("speaker") is not None for segment in segments)
    document: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "recording_id": recording_id,
        "stage": "raw",
        "language": language_override or native_language,
        "source": source,
        "engine": {
            "name": engine_name,
            "model": engine_model,
            "version": engine_version,
            "settings": redact_settings(settings),
            "generated_at": utc_now(),
        },
        "lineage": {
            "native_result_path": str(native_path.expanduser().resolve()),
            "native_result_sha256": sha256_file(native_path.expanduser().resolve()),
            "time_offset_seconds": time_offset,
        },
        "speaker_diarization": {
            "status": "provided_by_engine" if has_speaker else "not_run",
            "method": engine_name if has_speaker else None,
        },
        "review": {
            "status": "unreviewed",
            "reviewed_against_audio": False,
            "scope": "none",
            "audio_review": {
                "method": None,
                "coverage_confirmed": False,
                "reviewed_ranges": [],
            },
            "decisions": [],
            "removed_segments": [],
            "decision_summary": {
                "recorded": 0,
                "evidence_reviewed": 0,
                "acknowledged_unverified": 0,
            },
        },
        "segments": segments,
        "text": join_segment_text(segments) or native_text.strip(),
    }
    return document


def _normalized_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _format_text_skeleton(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    without_whitespace = re.sub(r"\s+", "", normalized, flags=re.UNICODE)
    return without_whitespace.rstrip(".!?…。！？")


def _decision_is_evidence_reviewed(
    decision: Dict[str, Any], review: Dict[str, Any], stage: Any
) -> bool:
    status = decision.get("verification_status")
    if status == "evidence-reviewed":
        return True
    if status == "acknowledged-unverified":
        return False

    evidence = decision.get("evidence")
    if isinstance(evidence, dict) and evidence.get("type") in EVIDENCE_REVIEWED_TYPES:
        return True

    # Backward compatibility for review artifacts created before per-decision
    # evidence was introduced. Never grant this fallback to an unreviewed stage.
    return bool(review.get("reviewed_against_audio")) and stage in REVIEWED_STAGES


def _review_decision_segment_sets(
    document: Any,
) -> Tuple[set[str], set[str]]:
    if not isinstance(document, dict):
        return set(), set()
    review = document.get("review")
    if not isinstance(review, dict):
        return set(), set()
    decisions = review.get("decisions")
    if not isinstance(decisions, list):
        return set(), set()

    recorded: set[str] = set()
    evidence_reviewed: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict) or "segment_id" not in decision:
            continue
        identifier = str(decision["segment_id"])
        recorded.add(identifier)
        if _decision_is_evidence_reviewed(decision, review, document.get("stage")):
            evidence_reviewed.add(identifier)
    return recorded, evidence_reviewed


def audit_document(document: Any) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    recorded_decision_ids, evidence_reviewed_ids = _review_decision_segment_sets(document)

    def add_issue(
        severity: str,
        code: str,
        message: str,
        segment_id: Any = None,
        addressed: bool = False,
    ) -> None:
        issue: Dict[str, Any] = {
            "severity": severity,
            "code": code,
            "message": message,
        }
        if segment_id is not None:
            issue["segment_id"] = segment_id
            issue["addressed_by_review"] = addressed
        issues.append(issue)

    if not isinstance(document, dict):
        add_issue("error", "document-not-object", "Canonical transcript must be a JSON object")
        return _finish_audit(None, issues, recorded_decision_ids, evidence_reviewed_ids)

    recording_id = document.get("recording_id")
    if document.get("schema_version") != SCHEMA_VERSION:
        add_issue(
            "error",
            "schema-version",
            f"Expected schema_version {SCHEMA_VERSION}",
        )
    if not isinstance(recording_id, str) or not recording_id.strip():
        add_issue("error", "missing-recording-id", "recording_id must be a non-empty string")

    segments = document.get("segments")
    if not isinstance(segments, list):
        add_issue("error", "segments-not-list", "segments must be a list")
        return _finish_audit(
            recording_id, issues, recorded_decision_ids, evidence_reviewed_ids
        )
    if not segments:
        add_issue("error", "segments-empty", "segments is empty")

    reviewed_ids = evidence_reviewed_ids
    previous_start: Optional[float] = None
    previous_end: Optional[float] = None
    normalized_texts: List[str] = []
    missing_timestamp_count = 0
    duration = None
    source = document.get("source")
    if isinstance(source, dict):
        duration = to_float(source.get("duration_seconds"))

    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            add_issue("error", "segment-not-object", f"Segment {index} is not an object")
            normalized_texts.append("")
            continue
        segment_id = segment.get("id", index)
        addressed = str(segment_id) in reviewed_ids
        text = str(segment.get("text") or "")
        normalized_texts.append(_normalized_text(text))
        if not text.strip():
            add_issue("review", "empty-segment-text", "Segment text is empty", segment_id, addressed)
        if "\ufffd" in text:
            add_issue(
                "review",
                "replacement-character",
                "Segment contains U+FFFD; inspect the response and decoding path before diagnosing corruption",
                segment_id,
                addressed,
            )

        start = to_float(segment.get("start"))
        end = to_float(segment.get("end"))
        if start is None and end is None:
            missing_timestamp_count += 1
        elif start is None or end is None:
            add_issue(
                "error",
                "partial-timestamp",
                "Segment must have both start and end, or neither",
                segment_id,
                addressed,
            )
        else:
            if start < 0:
                add_issue("error", "negative-start", "Segment start is negative", segment_id, addressed)
            if end < start:
                add_issue("error", "end-before-start", "Segment end precedes start", segment_id, addressed)
            if previous_start is not None and start + 0.001 < previous_start:
                add_issue(
                    "error",
                    "timeline-regression",
                    "Segment starts before the previous segment start",
                    segment_id,
                    addressed,
                )
            if previous_end is not None:
                if start < previous_end - 1.0:
                    add_issue(
                        "review",
                        "large-overlap",
                        "Segment overlaps the previous segment by more than one second",
                        segment_id,
                        addressed,
                    )
                if start - previous_end > 30.0:
                    add_issue(
                        "review",
                        "long-gap",
                        "More than 30 seconds separate this segment from the previous one",
                        segment_id,
                        addressed,
                    )
            if duration is not None and end > duration + 2.0:
                add_issue(
                    "error",
                    "source-duration-exceeded",
                    "Segment end exceeds source duration by more than two seconds",
                    segment_id,
                    addressed,
                )
            if end - start > 90.0 and len(text.strip()) < 10:
                add_issue(
                    "review",
                    "sparse-long-segment",
                    "A long segment contains very little text",
                    segment_id,
                    addressed,
                )
            previous_start = start
            previous_end = end

        metrics = segment.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
        confidence = to_float(segment.get("confidence"))
        avg_logprob = to_float(metrics.get("avg_logprob"))
        no_speech_prob = to_float(metrics.get("no_speech_prob"))
        compression_ratio = to_float(metrics.get("compression_ratio"))
        if confidence is not None and confidence < 0.35:
            add_issue(
                "review",
                "low-confidence",
                "Provider confidence is below 0.35",
                segment_id,
                addressed,
            )
        if avg_logprob is not None and avg_logprob < -1.2:
            add_issue(
                "review",
                "low-average-logprob",
                "Average log probability is below -1.2",
                segment_id,
                addressed,
            )
        if compression_ratio is not None and compression_ratio > 2.4:
            add_issue(
                "review",
                "high-compression-ratio",
                "Compression ratio is above 2.4",
                segment_id,
                addressed,
            )
        if (
            no_speech_prob is not None
            and avg_logprob is not None
            and no_speech_prob >= 0.6
            and avg_logprob <= -1.0
        ):
            add_issue(
                "review",
                "likely-non-speech",
                "High no-speech probability and low average log probability require audio review",
                segment_id,
                addressed,
            )

        folded = unicodedata.normalize("NFKC", text).casefold()
        if any(phrase in folded for phrase in SUSPICIOUS_PHRASES):
            add_issue(
                "review",
                "boilerplate-like-phrase",
                "A subtitle-like boilerplate phrase requires audio review; do not auto-delete it",
                segment_id,
                addressed,
            )

    if missing_timestamp_count:
        add_issue(
            "review",
            "timestamps-missing",
            f"{missing_timestamp_count} segment(s) have no timestamps; timeline QA is limited",
        )

    for index in range(2, len(normalized_texts)):
        current = normalized_texts[index]
        if not current or len(current) < 4:
            continue
        if current == normalized_texts[index - 1] == normalized_texts[index - 2]:
            if index >= 3 and normalized_texts[index - 3] == current:
                continue
            segment = segments[index] if isinstance(segments[index], dict) else {}
            segment_id = segment.get("id", index)
            add_issue(
                "review",
                "repeated-segment-run",
                "The same normalized text appears in at least three consecutive segments",
                segment_id,
                str(segment_id) in reviewed_ids,
            )

    text = document.get("text")
    if not isinstance(text, str) or not text.strip():
        add_issue("error", "document-text-empty", "Top-level text is empty")

    diarization = document.get("speaker_diarization")
    if not isinstance(diarization, dict) or "status" not in diarization:
        add_issue(
            "review",
            "diarization-status-missing",
            "speaker_diarization.status is missing; do not claim speaker identification",
        )

    return _finish_audit(
        recording_id, issues, recorded_decision_ids, evidence_reviewed_ids
    )


def _finish_audit(
    recording_id: Any,
    issues: List[Dict[str, Any]],
    recorded_decision_ids: Optional[set[str]] = None,
    evidence_reviewed_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    recorded_decision_ids = recorded_decision_ids or set()
    evidence_reviewed_ids = evidence_reviewed_ids or set()
    errors = [issue for issue in issues if issue["severity"] == "error"]
    review_flags = [issue for issue in issues if issue["severity"] == "review"]
    unaddressed = [
        issue
        for issue in review_flags
        if not bool(issue.get("addressed_by_review", False))
    ]
    if errors:
        status = "invalid"
    elif unaddressed:
        status = "needs-review"
    elif review_flags:
        status = "review-flags-evidence-resolved"
    else:
        status = "machine-checks-passed"

    flagged_segment_ids = {
        str(issue["segment_id"])
        for issue in review_flags
        if "segment_id" in issue
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "transcript-audit",
        "created_at": utc_now(),
        "recording_id": recording_id,
        "status": status,
        "summary": {
            "errors": len(errors),
            "review_flags": len(review_flags),
            "unaddressed_review_flags": len(unaddressed),
            "review_flag_segments_with_decisions": len(
                flagged_segment_ids & recorded_decision_ids
            ),
            "review_flag_segments_evidence_resolved": len(
                flagged_segment_ids & evidence_reviewed_ids
            ),
            "decision_segments_recorded": len(recorded_decision_ids),
            "decision_segments_evidence_reviewed": len(evidence_reviewed_ids),
            "decision_segments_acknowledged_unverified": len(
                recorded_decision_ids - evidence_reviewed_ids
            ),
        },
        "full_audio_review_required_for_final_qa": True,
        "issues": issues,
    }


def _normalize_audio_review(
    decisions_document: Dict[str, Any],
    reviewed_against_audio: bool,
    scope: str,
    source_duration: Optional[float],
) -> Dict[str, Any]:
    audio_review = decisions_document.get("audio_review")

    if not reviewed_against_audio:
        if scope != "none":
            raise UserError(
                "scope must be 'none' when reviewed_against_audio is false"
            )
        if isinstance(audio_review, dict) and audio_review.get("reviewed_ranges"):
            raise UserError(
                "audio_review.reviewed_ranges cannot be declared when reviewed_against_audio is false"
            )
        return {
            "method": None,
            "coverage_confirmed": False,
            "reviewed_ranges": [],
        }

    if scope == "none":
        raise UserError(
            "reviewed_against_audio=true requires flagged-only, sampled, or full scope"
        )
    if not isinstance(audio_review, dict):
        raise UserError(
            "reviewed_against_audio=true requires an audio_review object"
        )
    if audio_review.get("method") != "direct-listen":
        raise UserError(
            "audio_review.method must be 'direct-listen'; waveform or alternate ASR checks are not listening"
        )
    coverage_confirmed = audio_review.get("coverage_confirmed", False)
    if not isinstance(coverage_confirmed, bool):
        raise UserError("audio_review.coverage_confirmed must be true or false")
    if scope == "full" and not coverage_confirmed:
        raise UserError(
            "scope='full' requires audio_review.coverage_confirmed=true"
        )

    raw_ranges = audio_review.get("reviewed_ranges")
    if not isinstance(raw_ranges, list) or not raw_ranges:
        raise UserError(
            "reviewed_against_audio=true requires at least one audio_review.reviewed_ranges entry"
        )

    ranges: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_ranges):
        if not isinstance(item, dict):
            raise UserError(f"Audio review range {index} is not an object")
        start = to_float(item.get("start"))
        end = to_float(item.get("end"))
        if start is None or end is None or start < 0 or end <= start:
            raise UserError(
                f"Audio review range {index} requires finite start >= 0 and end > start"
            )
        if source_duration is not None and end > source_duration + 2.0:
            raise UserError(
                f"Audio review range {index} exceeds source duration by more than two seconds"
            )
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise UserError(f"Audio review range {index} requires a non-empty reason")
        ranges.append(
            {
                "start": start,
                "end": end,
                "reason": reason.strip(),
            }
        )

    ranges.sort(key=lambda item: (item["start"], item["end"]))
    return {
        "method": "direct-listen",
        "coverage_confirmed": coverage_confirmed,
        "reviewed_ranges": ranges,
    }


def _range_contains(
    outer_start: float, outer_end: float, inner_start: float, inner_end: float
) -> bool:
    tolerance = 0.5
    return outer_start <= inner_start + tolerance and outer_end >= inner_end - tolerance


def _ranges_cover_interval(
    ranges: Sequence[Dict[str, Any]], inner_start: float, inner_end: float
) -> bool:
    tolerance = 0.5
    cursor = inner_start
    for item in sorted(ranges, key=lambda value: (value["start"], value["end"])):
        start = float(item["start"])
        end = float(item["end"])
        if end < cursor - tolerance:
            continue
        if start > cursor + tolerance:
            return False
        cursor = max(cursor, end)
        if cursor >= inner_end - tolerance:
            return True
    return cursor >= inner_end - tolerance


def _normalize_decision_evidence(
    decision: Dict[str, Any],
    decision_index: int,
    segment: Dict[str, Any],
    action: str,
    reviewed_against_audio: bool,
    audio_review: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], str]:
    raw_evidence = decision.get("evidence")
    if raw_evidence is None:
        if action in DESTRUCTIVE_REVIEW_ACTIONS:
            raise UserError(
                f"Decision {decision_index} action {action!r} requires explicit evidence"
            )
        return None, "acknowledged-unverified"
    if not isinstance(raw_evidence, dict):
        raise UserError(f"Decision {decision_index} evidence must be an object")

    evidence_type = raw_evidence.get("type")
    if evidence_type not in VALID_EVIDENCE_TYPES:
        raise UserError(
            f"Decision {decision_index} has invalid evidence type: {evidence_type}"
        )
    evidence: Dict[str, Any] = {"type": evidence_type}

    note = raw_evidence.get("note")
    if note is not None:
        if not isinstance(note, str) or not note.strip():
            raise UserError(f"Decision {decision_index} evidence.note must be non-empty")
        evidence["note"] = note.strip()

    if evidence_type == "audio-listen":
        if not reviewed_against_audio:
            raise UserError(
                f"Decision {decision_index} declares audio-listen evidence while reviewed_against_audio is false"
            )
        source_start = to_float(raw_evidence.get("source_start"))
        source_end = to_float(raw_evidence.get("source_end"))
        if (
            source_start is None
            or source_end is None
            or source_start < 0
            or source_end <= source_start
        ):
            raise UserError(
                f"Decision {decision_index} audio-listen evidence requires source_start >= 0 and source_end > source_start"
            )
        evidence["source_start"] = source_start
        evidence["source_end"] = source_end

        segment_start = to_float(segment.get("start"))
        segment_end = to_float(segment.get("end"))
        if (
            segment_start is not None
            and segment_end is not None
            and not _range_contains(
                source_start, source_end, segment_start, segment_end
            )
        ):
            raise UserError(
                f"Decision {decision_index} audio evidence does not cover segment {segment.get('id')}"
            )

        declared_ranges = audio_review.get("reviewed_ranges", [])
        if not _ranges_cover_interval(
            [item for item in declared_ranges if isinstance(item, dict)],
            source_start,
            source_end,
        ):
            raise UserError(
                f"Decision {decision_index} audio evidence is outside audio_review.reviewed_ranges"
            )

    elif evidence_type in {"authoritative-material", "alternate-asr"}:
        reference = raw_evidence.get("reference")
        if not isinstance(reference, str) or not reference.strip():
            raise UserError(
                f"Decision {decision_index} {evidence_type} evidence requires a non-empty reference"
            )
        evidence["reference"] = reference.strip()

    if action == "exclude" and evidence_type != "audio-listen":
        raise UserError(
            f"Decision {decision_index} exclude requires direct audio-listen evidence"
        )
    if (
        action in {"replace_text", "set_speaker"}
        and evidence_type not in EVIDENCE_REVIEWED_TYPES
    ):
        raise UserError(
            f"Decision {decision_index} action {action!r} requires audio-listen or authoritative-material evidence"
        )

    verification_status = (
        "evidence-reviewed"
        if evidence_type in EVIDENCE_REVIEWED_TYPES
        else "acknowledged-unverified"
    )
    return evidence, verification_status


def _decision_has_safe_destructive_evidence(
    decision: Dict[str, Any], review: Dict[str, Any], stage: Any
) -> bool:
    action = decision.get("action")
    if action not in DESTRUCTIVE_REVIEW_ACTIONS:
        return True
    evidence = decision.get("evidence")
    if isinstance(evidence, dict):
        evidence_type = evidence.get("type")
        if action == "exclude":
            return (
                evidence_type == "audio-listen"
                and bool(review.get("reviewed_against_audio"))
                and decision.get("verification_status") == "evidence-reviewed"
            )
        return (
            evidence_type in EVIDENCE_REVIEWED_TYPES
            and decision.get("verification_status") == "evidence-reviewed"
        )

    # Legacy reviewed artifacts predate explicit evidence. Preserve them only
    # when the artifact itself already claims partial or full audio review.
    return (
        decision.get("verification_status") is None
        and bool(review.get("reviewed_against_audio"))
        and stage in REVIEWED_STAGES
    )


def _unverified_destructive_edits(document: Any) -> List[Dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    review = document.get("review")
    if not isinstance(review, dict):
        return []
    decisions = review.get("decisions")
    if not isinstance(decisions, list):
        decisions = []

    unsafe: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    verified_exclusions: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        action = decision.get("action")
        identifier = str(decision.get("segment_id"))
        if action not in DESTRUCTIVE_REVIEW_ACTIONS:
            continue
        if _decision_has_safe_destructive_evidence(
            decision, review, document.get("stage")
        ):
            if action == "exclude":
                verified_exclusions.add(identifier)
            continue
        key = (identifier, str(action))
        if key not in seen:
            unsafe.append({"segment_id": decision.get("segment_id"), "action": action})
            seen.add(key)

    removed = review.get("removed_segments")
    if isinstance(removed, list):
        for segment in removed:
            if not isinstance(segment, dict):
                continue
            identifier = str(segment.get("id"))
            if identifier in verified_exclusions:
                continue
            key = (identifier, "exclude")
            if key not in seen:
                unsafe.append(
                    {"segment_id": segment.get("id"), "action": "exclude"}
                )
                seen.add(key)
    return unsafe


def apply_review_decisions(
    document: Any, decisions_document: Any, input_path: Path
) -> Dict[str, Any]:
    if not isinstance(document, dict) or not isinstance(document.get("segments"), list):
        raise UserError("Review input is not a canonical transcript")
    if not isinstance(decisions_document, dict):
        raise UserError("Review decisions must be a JSON object")

    reviewer = decisions_document.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise UserError("Review decisions require a non-empty reviewer")
    scope = decisions_document.get("scope", "none")
    if scope not in VALID_REVIEW_SCOPES:
        raise UserError(f"Invalid review scope: {scope}")
    reviewed_against_audio = decisions_document.get("reviewed_against_audio")
    if not isinstance(reviewed_against_audio, bool):
        raise UserError("reviewed_against_audio must be true or false")
    decisions = decisions_document.get("decisions", [])
    if not isinstance(decisions, list):
        raise UserError("decisions must be a list")

    source = document.get("source")
    source_duration = (
        to_float(source.get("duration_seconds")) if isinstance(source, dict) else None
    )
    audio_review = _normalize_audio_review(
        decisions_document,
        reviewed_against_audio,
        scope,
        source_duration,
    )

    result = copy.deepcopy(document)
    segments = result["segments"]
    by_id: Dict[str, Dict[str, Any]] = {}
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise UserError(f"Canonical segment {index} is invalid")
        identifier = str(segment.get("id", index))
        if identifier in by_id:
            raise UserError(f"Duplicate segment id: {identifier}")
        by_id[identifier] = segment

    if reviewed_against_audio and scope == "full":
        declared_ranges = audio_review["reviewed_ranges"]
        for index, segment in enumerate(segments):
            segment_start = to_float(segment.get("start"))
            segment_end = to_float(segment.get("end"))
            if segment_start is None or segment_end is None:
                raise UserError(
                    "scope='full' cannot be substantiated when canonical segments lack timestamps"
                )
            if not _ranges_cover_interval(
                declared_ranges, segment_start, segment_end
            ):
                raise UserError(
                    f"scope='full' audio ranges do not cover canonical segment {segment.get('id', index)}"
                )

    excluded: set[str] = set()
    removed_segments: List[Dict[str, Any]] = []
    applied: List[Dict[str, Any]] = []
    speaker_changed = False
    decided_ids: set[str] = set()

    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise UserError(f"Decision {index} is not an object")
        if "segment_id" not in decision:
            raise UserError(f"Decision {index} has no segment_id")
        identifier = str(decision["segment_id"])
        if identifier not in by_id:
            raise UserError(f"Decision {index} refers to unknown segment {identifier}")
        if identifier in decided_ids:
            raise UserError(f"Decision {index} duplicates segment {identifier}")
        decided_ids.add(identifier)
        action = decision.get("action")
        if action not in VALID_REVIEW_ACTIONS:
            raise UserError(f"Decision {index} has invalid action: {action}")
        reason = decision.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise UserError(f"Decision {index} requires a non-empty reason")
        if identifier in excluded:
            raise UserError(f"Decision {index} targets already excluded segment {identifier}")

        segment = by_id[identifier]
        evidence, verification_status = _normalize_decision_evidence(
            decision,
            index,
            segment,
            str(action),
            reviewed_against_audio,
            audio_review,
        )
        audit_entry: Dict[str, Any] = {
            "segment_id": segment.get("id"),
            "action": action,
            "reason": reason.strip(),
            "reviewer": reviewer.strip(),
            "applied_at": utc_now(),
            "verification_status": verification_status,
        }
        if evidence is not None:
            audit_entry["evidence"] = evidence
        if action == "keep":
            audit_entry["text"] = segment.get("text")
        elif action == "format_text":
            replacement = decision.get("text")
            if not isinstance(replacement, str) or not replacement.strip():
                raise UserError(f"Decision {index} format_text requires non-empty text")
            before_text = str(segment.get("text") or "")
            if _format_text_skeleton(before_text) != _format_text_skeleton(
                replacement
            ):
                raise UserError(
                    f"Decision {index} format_text may change only whitespace, trailing sentence punctuation, or equivalent Unicode presentation; use replace_text with evidence for content changes"
                )
            audit_entry["before_text"] = before_text
            segment["text"] = replacement.strip()
            audit_entry["after_text"] = segment["text"]
        elif action == "replace_text":
            replacement = decision.get("text")
            if not isinstance(replacement, str) or not replacement.strip():
                raise UserError(f"Decision {index} replace_text requires non-empty text")
            audit_entry["before_text"] = segment.get("text")
            segment["text"] = replacement.strip()
            audit_entry["after_text"] = segment["text"]
        elif action == "exclude":
            excluded.add(identifier)
            removed = copy.deepcopy(segment)
            removed["exclusion_reason"] = reason.strip()
            removed["exclusion_evidence"] = copy.deepcopy(evidence)
            removed_segments.append(removed)
            audit_entry["before_text"] = segment.get("text")
        elif action == "set_speaker":
            speaker = decision.get("speaker")
            if not isinstance(speaker, str) or not speaker.strip():
                raise UserError(f"Decision {index} set_speaker requires a speaker")
            audit_entry["before_speaker"] = segment.get("speaker")
            segment["speaker"] = speaker.strip()
            audit_entry["after_speaker"] = segment["speaker"]
            speaker_changed = True
        applied.append(audit_entry)

    result["segments"] = [
        segment
        for segment in segments
        if str(segment.get("id")) not in excluded
    ]
    result["text"] = join_segment_text(result["segments"])

    if reviewed_against_audio and scope == "full":
        stage = "reviewed"
    elif reviewed_against_audio and scope in {"flagged-only", "sampled"}:
        stage = "reviewed-partial"
    else:
        stage = "edited-unverified"
    result["stage"] = stage

    existing_review = result.get("review")
    if not isinstance(existing_review, dict):
        existing_review = {}
    previous_decisions = existing_review.get("decisions")
    if not isinstance(previous_decisions, list):
        previous_decisions = []
    previous_removed = existing_review.get("removed_segments")
    if not isinstance(previous_removed, list):
        previous_removed = []
    combined_decisions = previous_decisions + applied
    result["review"] = {
        "status": stage,
        "reviewed_against_audio": reviewed_against_audio,
        "scope": scope,
        "reviewer": reviewer.strip(),
        "audio_review": audio_review,
        "decisions": combined_decisions,
        "removed_segments": previous_removed + removed_segments,
        "decision_summary": {
            "recorded": len(combined_decisions),
            "evidence_reviewed": sum(
                1
                for item in combined_decisions
                if item.get("verification_status") == "evidence-reviewed"
            ),
            "acknowledged_unverified": sum(
                1
                for item in combined_decisions
                if item.get("verification_status")
                == "acknowledged-unverified"
            ),
        },
    }
    if speaker_changed:
        result["speaker_diarization"] = {
            "status": "manually-reviewed-labels",
            "method": "review-decision-log",
        }

    lineage = result.get("lineage")
    if not isinstance(lineage, dict):
        lineage = {}
    lineage["review_parent_path"] = str(input_path.expanduser().resolve())
    lineage["review_parent_sha256"] = sha256_file(input_path.expanduser().resolve())
    result["lineage"] = lineage
    return result


def merge_documents(
    documents: Sequence[Dict[str, Any]], heading: str, allow_unreviewed: bool
) -> Tuple[str, Dict[str, Any]]:
    if not documents:
        raise UserError("At least one canonical transcript is required")

    blocks: List[str] = []
    identifiers: List[str] = []
    seen: set[str] = set()
    contains_unreviewed = False

    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise UserError(f"Merge input {index} is not an object")
        recording_id = document.get("recording_id")
        if not isinstance(recording_id, str) or not recording_id.strip():
            raise UserError(f"Merge input {index} has no recording_id")
        if recording_id in seen:
            raise UserError(f"Duplicate recording_id in merge inputs: {recording_id}")
        seen.add(recording_id)
        identifiers.append(recording_id)
        unsafe_edits = _unverified_destructive_edits(document)
        if unsafe_edits:
            raise UserError(
                f"{recording_id} contains {len(unsafe_edits)} destructive review edit(s) without permitted evidence; repair the review artifact before merging"
            )
        stage = document.get("stage")
        if stage not in REVIEWED_STAGES:
            contains_unreviewed = True
            if not allow_unreviewed:
                raise UserError(
                    f"{recording_id} has stage {stage!r}; use --allow-unreviewed only when this limitation is intentional"
                )
        text = document.get("text")
        if not isinstance(text, str) or not text.strip():
            raise UserError(f"{recording_id} has empty text")

        label: Optional[str] = None
        if heading == "id":
            label = recording_id
        elif heading == "source-name":
            source = document.get("source")
            if isinstance(source, dict) and source.get("name"):
                label = str(source["name"])
            else:
                label = recording_id
        block = text.strip()
        if label:
            block = f"===== {label} =====\n\n{block}"
        blocks.append(block)

    merged_text = "\n\n".join(blocks).rstrip() + "\n"
    merged_json = {
        "schema_version": SCHEMA_VERSION,
        "kind": "ordered-transcript-collection",
        "created_at": utc_now(),
        "ordered_recording_ids": identifiers,
        "contains_unreviewed": contains_unreviewed,
        "delivery_readiness": "draft-only" if contains_unreviewed else "reviewed-inputs",
        "text_has_added_timeline": False,
        "recordings": documents,
    }
    return merged_text, merged_json


def _line_numbers(text: str, pattern: re.Pattern[str]) -> List[int]:
    starts = [0]
    for match in re.finditer("\n", text):
        starts.append(match.end())
    result: List[int] = []
    for match in pattern.finditer(text):
        position = match.start()
        low, high = 0, len(starts)
        while low < high:
            middle = (low + high) // 2
            if starts[middle] <= position:
                low = middle + 1
            else:
                high = middle
        result.append(low)
    return sorted(set(result))


def _strip_fenced_code(text: str) -> str:
    lines = text.splitlines(keepends=True)
    output: List[str] = []
    in_fence = False
    fence_char = ""
    fence_length = 0
    for line in lines:
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                in_fence = False
            output.append("\n")
        elif in_fence:
            output.append("\n" if line.endswith("\n") else "")
        else:
            output.append(line)
    return "".join(output)


def _heading_names(text: str) -> set[str]:
    return {
        match.group(1).strip().casefold()
        for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", _strip_fenced_code(text))
    }


def _resolve_wikilinks(note: Path, vault_root: Path, text: str) -> List[str]:
    unresolved: List[str] = []
    clean_text = _strip_fenced_code(text)
    headings = _heading_names(text)
    vault = vault_root.expanduser().resolve()
    note_resolved = note.expanduser().resolve()
    if not vault.is_dir():
        return ["<vault-root-does-not-exist>"]

    markdown_files = list(vault.rglob("*.md"))
    by_stem: Dict[str, List[Path]] = {}
    for candidate in markdown_files:
        by_stem.setdefault(candidate.stem.casefold(), []).append(candidate)

    for match in re.finditer(r"!?\[\[([^\]]+)\]\]", clean_text):
        raw_target = match.group(1)
        target = raw_target.split("|", 1)[0].strip()
        path_part, separator, anchor = target.partition("#")
        if not path_part:
            if separator and anchor.strip().casefold() not in headings:
                unresolved.append(raw_target)
            continue

        normalized = path_part[:-3] if path_part.casefold().endswith(".md") else path_part
        exact = (vault / f"{normalized}.md").resolve(strict=False)
        exists = exact.is_file()
        if not exists and "/" not in normalized and "\\" not in normalized:
            matches = by_stem.get(Path(normalized).name.casefold(), [])
            exists = len(matches) == 1
            if len(matches) > 1:
                unresolved.append(f"{raw_target} (ambiguous)")
                continue
        if not exists:
            unresolved.append(raw_target)
            continue
        if anchor and exact == note_resolved:
            linked_text = exact.read_text(encoding="utf-8-sig")
            if anchor.strip().casefold() not in _heading_names(linked_text):
                unresolved.append(raw_target)
    return unresolved


def validate_delivery(
    manifest_path: Optional[Path],
    canonical_paths: Sequence[Path],
    merged_path: Optional[Path],
    note_path: Optional[Path],
    vault_root: Optional[Path],
    delivery_mode: str = "final",
) -> Dict[str, Any]:
    if delivery_mode not in VALID_DELIVERY_MODES:
        raise UserError(f"Invalid delivery mode: {delivery_mode}")
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    checks: List[Dict[str, Any]] = []
    canonical_stages: List[Any] = []
    has_unresolved_review_flags = False

    def error(
        code: str,
        message: str,
        path: Optional[Path] = None,
        category: str = "artifact-integrity",
    ) -> None:
        item: Dict[str, Any] = {
            "code": code,
            "message": message,
            "category": category,
        }
        if path:
            item["path"] = str(path.expanduser().resolve(strict=False))
        errors.append(item)

    def warning(
        code: str,
        message: str,
        path: Optional[Path] = None,
        category: str = "artifact-integrity",
    ) -> None:
        item: Dict[str, Any] = {
            "code": code,
            "message": message,
            "category": category,
        }
        if path:
            item["path"] = str(path.expanduser().resolve(strict=False))
        warnings.append(item)

    manifest: Optional[Dict[str, Any]] = None
    if manifest_path:
        try:
            loaded = load_json(manifest_path)
            if not isinstance(loaded, dict) or not isinstance(loaded.get("files"), list):
                error("invalid-manifest", "Manifest has no files list", manifest_path)
            else:
                manifest = loaded
                for index, item in enumerate(loaded["files"]):
                    if not isinstance(item, dict) or not item.get("path"):
                        error("invalid-manifest-entry", f"Manifest entry {index} is invalid", manifest_path)
                        continue
                    source_path = Path(str(item["path"]))
                    if not source_path.is_file():
                        error("source-missing", f"Manifest source {index} is missing", source_path)
                        continue
                    current_hash = sha256_file(source_path)
                    expected_hash = item.get("sha256")
                    if not expected_hash or current_hash != expected_hash:
                        error("source-hash-mismatch", f"Manifest source {index} changed", source_path)
                checks.append({"check": "source-hashes", "count": len(loaded["files"])})
        except UserError as exc:
            error("manifest-read", str(exc), manifest_path)

    manifest_hashes: Dict[str, str] = {}
    if manifest:
        for item in manifest["files"]:
            if isinstance(item, dict) and item.get("path") and item.get("sha256"):
                manifest_hashes[str(Path(str(item["path"])).resolve(strict=False))] = str(item["sha256"])

    for canonical_path in canonical_paths:
        try:
            document = load_json(canonical_path)
        except UserError as exc:
            error("canonical-read", str(exc), canonical_path)
            continue
        report = audit_document(document)
        if report["summary"]["errors"]:
            error(
                "canonical-invalid",
                f"Canonical transcript has {report['summary']['errors']} structural/timeline error(s)",
                canonical_path,
            )
        if report["summary"]["unaddressed_review_flags"]:
            has_unresolved_review_flags = True
            warning(
                "canonical-needs-review",
                f"Canonical transcript has {report['summary']['unaddressed_review_flags']} unaddressed review flag(s)",
                canonical_path,
                "content-qa",
            )
        if isinstance(document, dict):
            stage = document.get("stage")
            canonical_stages.append(stage)
            unsafe_edits = _unverified_destructive_edits(document)
            if unsafe_edits:
                error(
                    "unverified-destructive-edit",
                    f"Canonical transcript contains {len(unsafe_edits)} destructive review edit(s) without permitted evidence",
                    canonical_path,
                    "content-qa",
                )
            review = document.get("review")
            if not isinstance(review, dict):
                review = {}
            review_claims_audio = bool(review.get("reviewed_against_audio"))
            review_scope = review.get("scope")
            if stage in REVIEWED_STAGES and not review_claims_audio:
                error(
                    "review-state-inconsistent",
                    f"Canonical stage {stage!r} requires reviewed_against_audio=true",
                    canonical_path,
                    "content-qa",
                )
            if review_claims_audio and review_scope == "none":
                error(
                    "review-scope-inconsistent",
                    "reviewed_against_audio=true cannot use scope='none'",
                    canonical_path,
                    "content-qa",
                )
            if stage in REVIEWED_STAGES:
                audio_review = review.get("audio_review")
                if not (
                    isinstance(audio_review, dict)
                    and audio_review.get("method") == "direct-listen"
                    and isinstance(audio_review.get("reviewed_ranges"), list)
                    and audio_review.get("reviewed_ranges")
                ):
                    warning(
                        "legacy-audio-review-evidence-missing",
                        "Reviewed artifact lacks explicit direct-listen ranges; preserve it as legacy evidence and re-document ranges when feasible",
                        canonical_path,
                        "content-qa",
                    )
            if stage not in REVIEWED_STAGES:
                if delivery_mode == "final":
                    error(
                        "canonical-unreviewed",
                        f"Final delivery requires reviewed or reviewed-partial input; canonical stage is {stage!r}",
                        canonical_path,
                        "content-qa",
                    )
                else:
                    warning(
                        "canonical-unreviewed",
                        f"Draft delivery includes canonical stage {stage!r}",
                        canonical_path,
                        "content-qa",
                    )
            elif stage == "reviewed-partial":
                warning(
                    "canonical-partial-review",
                    "Canonical transcript has only partial audio review; disclose the unreviewed scope",
                    canonical_path,
                    "content-qa",
                )
            source = document.get("source")
            if isinstance(source, dict) and source.get("path") and source.get("sha256"):
                resolved_source = str(Path(str(source["path"])).resolve(strict=False))
                manifest_hash = manifest_hashes.get(resolved_source)
                if manifest_hash and manifest_hash != source.get("sha256"):
                    error(
                        "canonical-source-hash-mismatch",
                        "Canonical source hash differs from manifest",
                        canonical_path,
                    )
        checks.append(
            {
                "check": "canonical-audit",
                "path": str(canonical_path.expanduser().resolve(strict=False)),
                "status": report["status"],
            }
        )

    if merged_path:
        try:
            merged_text = merged_path.read_text(encoding="utf-8-sig")
            if not merged_text.strip():
                error("merged-empty", "Merged text is empty", merged_path)
            if "\ufffd" in merged_text:
                warning(
                    "merged-replacement-character",
                    "Merged text contains U+FFFD; inspect the decoding path",
                    merged_path,
                )
            timeline_lines = _line_numbers(merged_text, TIMELINE_LINE_RE)
            if timeline_lines:
                error(
                    "merged-timeline-markers",
                    f"Merged text has timeline-like markers on {len(timeline_lines)} line(s)",
                    merged_path,
                )
            checks.append({"check": "merged-text", "timeline_marker_lines": timeline_lines})
        except FileNotFoundError:
            error("merged-missing", "Merged text does not exist", merged_path)
        except UnicodeDecodeError:
            error("merged-encoding", "Merged text is not valid UTF-8", merged_path)

    if note_path:
        try:
            note_text = note_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            error("note-missing", "Obsidian note does not exist", note_path)
            note_text = ""
        except UnicodeDecodeError:
            error("note-encoding", "Obsidian note is not valid UTF-8", note_path)
            note_text = ""

        if note_text:
            if note_text.startswith("---\n"):
                if not re.search(r"(?m)^---\s*$", note_text[4:]):
                    error("frontmatter-unclosed", "Frontmatter has no closing delimiter", note_path)
            fence_markers = re.findall(r"(?m)^\s*(?:`{3,}|~{3,})", note_text)
            if len(fence_markers) % 2:
                error("code-fence-unbalanced", "Code fences are unbalanced", note_path)
            if not re.search(r"(?m)^##\s+대분류\s*$", note_text):
                warning("category-heading-missing", "The note has no level-2 대분류 heading", note_path)

            for name, pattern in SECRET_PATTERNS:
                lines = _line_numbers(note_text, pattern)
                if lines:
                    error(
                        f"secret-pattern-{name}",
                        f"Potential {name} pattern appears on {len(lines)} line(s); values are intentionally omitted from this report",
                        note_path,
                    )
            ip_lines = _line_numbers(note_text, IPV4_OR_CIDR_RE)
            if ip_lines:
                warning(
                    "network-identifiers-present",
                    f"IP/CIDR-like values appear on {len(ip_lines)} line(s); verify that they are safe examples",
                    note_path,
                )
            email_lines = _line_numbers(note_text, EMAIL_RE)
            if email_lines:
                warning(
                    "email-identifiers-present",
                    f"Email-like values appear on {len(email_lines)} line(s); verify that they are necessary",
                    note_path,
                )
            if vault_root:
                unresolved = _resolve_wikilinks(note_path, vault_root, note_text)
                if unresolved:
                    warning(
                        "wikilinks-unresolved",
                        f"{len(unresolved)} wikilink(s) are unresolved or ambiguous",
                        note_path,
                    )
            elif re.search(r"!?\[\[[^\]]+\]\]", _strip_fenced_code(note_text)):
                warning(
                    "wikilinks-not-checked",
                    "Wikilinks exist but --vault-root was not provided",
                    note_path,
                )
            checks.append(
                {
                    "check": "obsidian-note",
                    "path": str(note_path.expanduser().resolve(strict=False)),
                    "fence_marker_count": len(fence_markers),
                }
            )

    if errors:
        status = "failed"
    elif warnings:
        status = "passed-with-warnings"
    else:
        status = "passed"
    artifact_errors = [
        item for item in errors if item.get("category") == "artifact-integrity"
    ]
    artifact_warnings = [
        item for item in warnings if item.get("category") == "artifact-integrity"
    ]
    content_errors = [item for item in errors if item.get("category") == "content-qa"]

    if artifact_errors:
        artifact_integrity_status = "failed"
    elif artifact_warnings:
        artifact_integrity_status = "passed-with-warnings"
    else:
        artifact_integrity_status = "passed"

    if content_errors:
        content_qa_status = "failed"
    elif not canonical_paths:
        content_qa_status = "not-assessed"
    elif any(stage not in REVIEWED_STAGES for stage in canonical_stages):
        content_qa_status = "unverified"
    elif has_unresolved_review_flags:
        content_qa_status = "needs-review"
    elif any(stage == "reviewed-partial" for stage in canonical_stages):
        content_qa_status = "reviewed-partial"
    else:
        content_qa_status = "reviewed"

    if errors:
        delivery_readiness = "blocked"
    elif content_qa_status == "reviewed":
        delivery_readiness = "final-ready"
    elif content_qa_status in {"reviewed-partial", "needs-review"}:
        delivery_readiness = "ready-with-limitations"
    elif delivery_mode == "draft":
        delivery_readiness = "draft-only"
    else:
        delivery_readiness = "not-final"

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "transcription-delivery-validation",
        "created_at": utc_now(),
        "delivery_mode": delivery_mode,
        "status": status,
        "summary": {"errors": len(errors), "warnings": len(warnings)},
        "artifact_integrity_status": artifact_integrity_status,
        "content_qa_status": content_qa_status,
        "delivery_readiness": delivery_readiness,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "full_audio_review_claimed_by_artifacts": bool(canonical_stages)
        and all(stage == "reviewed" for stage in canonical_stages),
        "full_audio_review_verified_by_this_tool": False,
    }


def command_inspect(args: argparse.Namespace) -> int:
    ffprobe = args.ffprobe or shutil.which("ffprobe")
    manifest = create_manifest([Path(item) for item in args.files], ffprobe, args.require_probe)
    write_json_atomic(Path(args.output), manifest)
    print(f"WROTE {Path(args.output).resolve()}")
    return 0


def command_normalize(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.output)
    ensure_distinct_paths(input_path, output_path)
    raw = load_json(input_path)
    if args.source_manifest:
        source = _source_from_manifest(Path(args.source_manifest), args.source_index)
    elif args.source:
        source = _source_from_path(Path(args.source))
    elif isinstance(raw, dict) and isinstance(raw.get("source"), dict):
        source = copy.deepcopy(raw["source"])
    else:
        source = {
            "path": None,
            "name": None,
            "sha256": None,
            "size_bytes": None,
            "duration_seconds": None,
        }
    try:
        settings = json.loads(args.settings_json)
    except json.JSONDecodeError as exc:
        raise UserError(f"--settings-json is invalid JSON: {exc}") from exc
    if not isinstance(settings, dict):
        raise UserError("--settings-json must decode to an object")

    document = normalize_document(
        raw=raw,
        native_path=input_path,
        recording_id=args.recording_id,
        source=source,
        engine_name=args.engine_name,
        engine_model=args.engine_model,
        engine_version=args.engine_version,
        settings=settings,
        language_override=args.language,
        time_offset=args.time_offset,
    )
    write_json_atomic(output_path, document)
    print(f"WROTE {output_path.resolve()}")
    return 0


def command_audit(args: argparse.Namespace) -> int:
    report = audit_document(load_json(Path(args.input)))
    write_json_atomic(Path(args.output), report)
    print(
        f"{report['status'].upper()} errors={report['summary']['errors']} "
        f"review_flags={report['summary']['review_flags']} "
        f"unaddressed={report['summary']['unaddressed_review_flags']}"
    )
    return 1 if report["summary"]["errors"] else 0


def command_review(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    decisions_path = Path(args.decisions)
    output_path = Path(args.output)
    ensure_distinct_paths(input_path, output_path)
    ensure_distinct_paths(decisions_path, output_path)
    result = apply_review_decisions(
        load_json(input_path), load_json(decisions_path), input_path
    )
    write_json_atomic(output_path, result)
    print(f"WROTE {output_path.resolve()} stage={result['stage']}")
    return 0


def command_merge(args: argparse.Namespace) -> int:
    input_paths = [Path(item) for item in args.inputs]
    output_txt = Path(args.output_txt)
    output_json = Path(args.output_json)
    for input_path in input_paths:
        ensure_distinct_paths(input_path, output_txt)
        ensure_distinct_paths(input_path, output_json)
    ensure_distinct_paths(output_txt, output_json)
    documents = [load_json(path) for path in input_paths]
    text, merged = merge_documents(documents, args.heading, args.allow_unreviewed)
    write_text_atomic(output_txt, text)
    write_json_atomic(output_json, merged)
    print(
        f"WROTE {output_txt.resolve()} and {output_json.resolve()} "
        f"recordings={len(documents)} unreviewed={merged['contains_unreviewed']}"
    )
    return 0


def command_validate(args: argparse.Namespace) -> int:
    canonical_paths = [Path(item) for item in (args.canonical or [])]
    report = validate_delivery(
        manifest_path=Path(args.manifest) if args.manifest else None,
        canonical_paths=canonical_paths,
        merged_path=Path(args.merged_txt) if args.merged_txt else None,
        note_path=Path(args.note) if args.note else None,
        vault_root=Path(args.vault_root) if args.vault_root else None,
        delivery_mode=args.delivery_mode,
    )
    write_json_atomic(Path(args.report), report)
    print(
        f"{report['status'].upper()} errors={report['summary']['errors']} "
        f"warnings={report['summary']['warnings']} report={Path(args.report).resolve()}"
    )
    return 1 if report["summary"]["errors"] else 0


def command_self_test(_: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="safe-transcribe-obsidian-") as directory:
        root = Path(directory)
        source = root / "fixture.wav"
        with wave.open(str(source), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 4)

        manifest = create_manifest([source], shutil.which("ffprobe"), False)
        manifest_path = root / "manifest.json"
        write_json_atomic(manifest_path, manifest)

        native = {
            "language": "ko",
            "text": "반복 반복 반복 시청해 주셔서 감사합니다",
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "반복"},
                {"start": 1.0, "end": 2.0, "text": "반복"},
                {"start": 2.0, "end": 3.0, "text": "반복"},
                {
                    "start": 3.0,
                    "end": 4.0,
                    "text": "시청해 주셔서 감사합니다",
                    "avg_logprob": -1.3,
                    "no_speech_prob": 0.8,
                },
            ],
        }
        native_path = root / "native.json"
        write_json_atomic(native_path, native)
        canonical = normalize_document(
            raw=native,
            native_path=native_path,
            recording_id="fixture-1",
            source=_source_from_manifest(manifest_path, 0),
            engine_name="fixture-engine",
            engine_model="fixture-model",
            engine_version="1",
            settings={"api_key": "must-not-survive", "temperature": 0},
            language_override="ko",
            time_offset=0.0,
        )
        if canonical["engine"]["settings"]["api_key"] != "[REDACTED]":
            raise AssertionError("settings redaction failed")
        raw_audit = audit_document(canonical)
        if raw_audit["status"] != "needs-review":
            raise AssertionError("audit did not flag the controlled suspicious fixture")

        # Simulate a provider that nests a transcript and word timestamps.
        deepgram_native = {
            "results": {
                "channels": [
                    {
                        "alternatives": [
                            {
                                "transcript": "다른 공급자 형식",
                                "words": [
                                    {
                                        "start": 0.1,
                                        "end": 0.4,
                                        "punctuated_word": "다른",
                                        "confidence": 0.92,
                                    },
                                    {
                                        "start": 0.4,
                                        "end": 0.9,
                                        "punctuated_word": "공급자 형식",
                                        "confidence": 0.90,
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }
        }
        deepgram_path = root / "deepgram-native.json"
        write_json_atomic(deepgram_path, deepgram_native)
        deepgram_canonical = normalize_document(
            raw=deepgram_native,
            native_path=deepgram_path,
            recording_id="provider-fixture",
            source=_source_from_manifest(manifest_path, 0),
            engine_name="provider-adapter",
            engine_model="provider-model",
            engine_version=None,
            settings={"diarize": False},
            language_override="ko",
            time_offset=10.0,
        )
        if (
            deepgram_canonical["segments"][0]["start"] != 10.1
            or deepgram_canonical["segments"][0]["words"][0]["text"] != "다른"
        ):
            raise AssertionError("nested provider normalization or offset failed")

        # Simulate a runtime that reports milliseconds rather than seconds.
        milliseconds_native = {
            "segments": [
                {"start_ms": 500, "end_ms": 1250, "transcript": "밀리초 형식"}
            ]
        }
        milliseconds_path = root / "milliseconds-native.json"
        write_json_atomic(milliseconds_path, milliseconds_native)
        milliseconds_canonical = normalize_document(
            raw=milliseconds_native,
            native_path=milliseconds_path,
            recording_id="milliseconds-fixture",
            source=_source_from_manifest(manifest_path, 0),
            engine_name="local-runtime",
            engine_model="quantized-model",
            engine_version="test",
            settings={},
            language_override="ko",
            time_offset=5.0,
        )
        if (
            milliseconds_canonical["segments"][0]["start"] != 5.5
            or milliseconds_canonical["segments"][0]["end"] != 6.25
        ):
            raise AssertionError("millisecond timestamp normalization failed")

        # Text-only engines remain usable but must expose the missing-timestamp limit.
        text_only_path = root / "text-only.json"
        write_json_atomic(text_only_path, "텍스트 전용 결과")
        text_only = normalize_document(
            raw="텍스트 전용 결과",
            native_path=text_only_path,
            recording_id="text-only-fixture",
            source=_source_from_manifest(manifest_path, 0),
            engine_name="text-only-engine",
            engine_model="unknown",
            engine_version=None,
            settings={},
            language_override="ko",
            time_offset=0.0,
        )
        text_only_audit = audit_document(text_only)
        if not any(
            issue["code"] == "timestamps-missing"
            for issue in text_only_audit["issues"]
        ):
            raise AssertionError("text-only limitation was not surfaced")

        canonical_path = root / "canonical.json"
        write_json_atomic(canonical_path, canonical)

        unsafe_exclude = {
            "reviewer": "self-test",
            "reviewed_against_audio": False,
            "scope": "none",
            "decisions": [
                {
                    "segment_id": 3,
                    "action": "exclude",
                    "reason": "Machine output looks repetitive",
                }
            ],
        }
        try:
            apply_review_decisions(canonical, unsafe_exclude, canonical_path)
        except UserError as exc:
            if "requires explicit evidence" not in str(exc):
                raise
        else:
            raise AssertionError("review accepted an exclusion without evidence")

        alternate_asr_exclude = {
            "reviewer": "self-test",
            "reviewed_against_audio": False,
            "scope": "none",
            "decisions": [
                {
                    "segment_id": 3,
                    "action": "exclude",
                    "reason": "Alternate decoder also looked suspicious",
                    "evidence": {
                        "type": "alternate-asr",
                        "reference": "alternate-result.json",
                    },
                }
            ],
        }
        try:
            apply_review_decisions(
                canonical, alternate_asr_exclude, canonical_path
            )
        except UserError as exc:
            if "requires direct audio-listen evidence" not in str(exc):
                raise
        else:
            raise AssertionError("review treated alternate ASR as exclusion evidence")

        incomplete_full_review = {
            "reviewer": "self-test",
            "reviewed_against_audio": True,
            "scope": "full",
            "audio_review": {
                "method": "direct-listen",
                "coverage_confirmed": True,
                "reviewed_ranges": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "reason": "Deliberately incomplete test range",
                    }
                ],
            },
            "decisions": [],
        }
        try:
            apply_review_decisions(
                canonical, incomplete_full_review, canonical_path
            )
        except UserError as exc:
            if "do not cover canonical segment" not in str(exc):
                raise
        else:
            raise AssertionError("full review accepted incomplete audio ranges")

        unverified_keep = {
            "reviewer": "self-test",
            "reviewed_against_audio": False,
            "scope": "none",
            "decisions": [
                {
                    "segment_id": 3,
                    "action": "keep",
                    "reason": "Recorded for later audio review",
                }
            ],
        }
        acknowledged = apply_review_decisions(
            canonical, unverified_keep, canonical_path
        )
        acknowledged_audit = audit_document(acknowledged)
        if (
            acknowledged["stage"] != "edited-unverified"
            or acknowledged_audit["status"] != "needs-review"
            or acknowledged_audit["summary"][
                "decision_segments_acknowledged_unverified"
            ]
            != 1
        ):
            raise AssertionError(
                "unverified keep decision was incorrectly treated as resolved"
            )

        safe_format = {
            "reviewer": "self-test",
            "reviewed_against_audio": False,
            "scope": "none",
            "decisions": [
                {
                    "segment_id": 0,
                    "action": "format_text",
                    "text": "반복.",
                    "reason": "Add punctuation without changing content",
                }
            ],
        }
        formatted = apply_review_decisions(canonical, safe_format, canonical_path)
        if formatted["segments"][0]["text"] != "반복.":
            raise AssertionError("format_text did not apply a safe punctuation edit")
        if _format_text_skeleton("AWS 1.2") == _format_text_skeleton("aws 12"):
            raise AssertionError("format_text skeleton erased meaningful case or number punctuation")

        unsafe_format = copy.deepcopy(safe_format)
        unsafe_format["decisions"][0]["text"] = "다른 내용"
        try:
            apply_review_decisions(canonical, unsafe_format, canonical_path)
        except UserError as exc:
            if "may change only whitespace" not in str(exc):
                raise
        else:
            raise AssertionError("format_text accepted a semantic content change")

        decisions = {
            "reviewer": "self-test",
            "reviewed_against_audio": True,
            "scope": "full",
            "audio_review": {
                "method": "direct-listen",
                "coverage_confirmed": True,
                "reviewed_ranges": [
                    {
                        "start": 0.0,
                        "end": 4.0,
                        "reason": "Controlled full-fixture listening",
                    }
                ],
            },
            "decisions": [
                {
                    "segment_id": 2,
                    "action": "keep",
                    "reason": "Controlled fixture decision",
                    "evidence": {
                        "type": "audio-listen",
                        "source_start": 2.0,
                        "source_end": 3.0,
                    },
                },
                {
                    "segment_id": 3,
                    "action": "replace_text",
                    "text": "마무리",
                    "reason": "Controlled fixture correction",
                    "evidence": {
                        "type": "audio-listen",
                        "source_start": 3.0,
                        "source_end": 4.0,
                    },
                },
            ],
        }
        reviewed = apply_review_decisions(canonical, decisions, canonical_path)
        if reviewed["stage"] != "reviewed" or "시청해" in reviewed["text"]:
            raise AssertionError("review decision application failed")
        reviewed_path = root / "reviewed.json"
        write_json_atomic(reviewed_path, reviewed)

        reviewed_audit = audit_document(reviewed)
        if (
            reviewed_audit["summary"]["unaddressed_review_flags"]
            or reviewed_audit["summary"]["decision_segments_evidence_reviewed"]
            != 2
        ):
            raise AssertionError("evidence-backed decisions did not resolve audit flags")

        legacy_unsafe = copy.deepcopy(canonical)
        removed_segment = copy.deepcopy(legacy_unsafe["segments"].pop())
        legacy_unsafe["text"] = join_segment_text(legacy_unsafe["segments"])
        legacy_unsafe["stage"] = "edited-unverified"
        legacy_unsafe["review"] = {
            "status": "edited-unverified",
            "reviewed_against_audio": False,
            "scope": "none",
            "decisions": [
                {
                    "segment_id": removed_segment["id"],
                    "action": "exclude",
                    "reason": "Legacy machine-only exclusion",
                }
            ],
            "removed_segments": [removed_segment],
        }
        legacy_unsafe_path = root / "legacy-unsafe.json"
        write_json_atomic(legacy_unsafe_path, legacy_unsafe)
        unsafe_transcript_delivery = validate_delivery(
            manifest_path=manifest_path,
            canonical_paths=[legacy_unsafe_path],
            merged_path=None,
            note_path=None,
            vault_root=None,
            delivery_mode="draft",
        )
        if not any(
            item["code"] == "unverified-destructive-edit"
            for item in unsafe_transcript_delivery["errors"]
        ):
            raise AssertionError(
                "delivery validation missed a legacy unverified exclusion"
            )
        try:
            merge_documents([legacy_unsafe], "id", True)
        except UserError:
            pass
        else:
            raise AssertionError(
                "merge accepted a destructive unverified artifact with opt-in"
            )

        final_unreviewed = validate_delivery(
            manifest_path=manifest_path,
            canonical_paths=[canonical_path],
            merged_path=None,
            note_path=None,
            vault_root=None,
            delivery_mode="final",
        )
        if not any(
            item["code"] == "canonical-unreviewed"
            for item in final_unreviewed["errors"]
        ):
            raise AssertionError("final delivery accepted an unreviewed canonical")
        draft_unreviewed = validate_delivery(
            manifest_path=manifest_path,
            canonical_paths=[canonical_path],
            merged_path=None,
            note_path=None,
            vault_root=None,
            delivery_mode="draft",
        )
        if (
            draft_unreviewed["summary"]["errors"]
            or draft_unreviewed["delivery_readiness"] != "draft-only"
        ):
            raise AssertionError("draft delivery did not preserve safe unreviewed output")

        try:
            merge_documents([canonical], "id", False)
        except UserError:
            pass
        else:
            raise AssertionError("merge accepted an unreviewed transcript without opt-in")

        second = copy.deepcopy(reviewed)
        second["recording_id"] = "fixture-2"
        second["text"] = "두 번째 녹음"
        second["segments"] = [
            {
                "id": 0,
                "start": 0.0,
                "end": 1.0,
                "text": "두 번째 녹음",
                "speaker": None,
                "confidence": None,
                "metrics": {},
            }
        ]
        merged_text, merged_json = merge_documents([reviewed, second], "id", False)
        if TIMELINE_LINE_RE.search(merged_text) or merged_json["contains_unreviewed"]:
            raise AssertionError("safe merge invariant failed")
        merged_path = root / "merged.txt"
        write_text_atomic(merged_path, merged_text)

        note = root / "note.md"
        write_text_atomic(
            note,
            "---\ntranscription_status: reviewed\n---\n\n"
            "# Fixture\n\n## 대분류\n\n- [[#핵심 개념]]\n\n## 핵심 개념\n",
        )
        delivery = validate_delivery(
            manifest_path=manifest_path,
            canonical_paths=[reviewed_path],
            merged_path=merged_path,
            note_path=note,
            vault_root=root,
        )
        if delivery["summary"]["errors"]:
            raise AssertionError(json.dumps(delivery, ensure_ascii=False, indent=2))
        if (
            delivery["artifact_integrity_status"] != "passed"
            or delivery["content_qa_status"] != "reviewed"
            or delivery["delivery_readiness"] != "final-ready"
        ):
            raise AssertionError("safe final delivery statuses are inconsistent")

        unsafe_note = root / "unsafe-note.md"
        synthetic_bearer = "abcdefghijkl" + "mnop"
        write_text_atomic(
            unsafe_note,
            "# Fixture\n\n## 대분류\n\nAuthorization: Bearer "
            + synthetic_bearer
            + "\n",
        )
        unsafe_delivery = validate_delivery(
            manifest_path=None,
            canonical_paths=[],
            merged_path=None,
            note_path=unsafe_note,
            vault_root=root,
        )
        if not any(
            item["code"] == "secret-pattern-authorization-bearer"
            for item in unsafe_delivery["errors"]
        ):
            raise AssertionError("secret-pattern validation failed")

    print("SELF_TEST_OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Source-preserving utilities for portable ASR-to-Obsidian workflows"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="hash source media and optionally collect ffprobe metadata"
    )
    inspect_parser.add_argument("files", nargs="+", help="source audio/video files in explicit order")
    inspect_parser.add_argument("--output", required=True, help="manifest JSON path")
    inspect_parser.add_argument("--ffprobe", help="explicit ffprobe executable path")
    inspect_parser.add_argument(
        "--require-probe", action="store_true", help="fail if ffprobe metadata is unavailable"
    )
    inspect_parser.set_defaults(func=command_inspect)

    normalize_parser = subparsers.add_parser(
        "normalize", help="convert common ASR JSON to the canonical schema"
    )
    normalize_parser.add_argument("--input", required=True, help="native ASR JSON path")
    normalize_parser.add_argument("--output", required=True, help="canonical JSON path")
    normalize_parser.add_argument("--recording-id", required=True)
    source_group = normalize_parser.add_mutually_exclusive_group()
    source_group.add_argument("--source-manifest", help="source manifest JSON path")
    source_group.add_argument("--source", help="source media path")
    normalize_parser.add_argument(
        "--source-index", type=int, default=0, help="zero-based entry in --source-manifest"
    )
    normalize_parser.add_argument("--engine-name", required=True)
    normalize_parser.add_argument("--engine-model", required=True)
    normalize_parser.add_argument("--engine-version")
    normalize_parser.add_argument("--language")
    normalize_parser.add_argument(
        "--settings-json",
        default="{}",
        help="non-secret engine settings as a JSON object; sensitive keys are redacted",
    )
    normalize_parser.add_argument(
        "--time-offset",
        type=float,
        default=0.0,
        help="seconds to add to segment and word timestamps",
    )
    normalize_parser.set_defaults(func=command_normalize)

    audit_parser = subparsers.add_parser(
        "audit", help="run non-destructive structural and hallucination-candidate checks"
    )
    audit_parser.add_argument("--input", required=True, help="canonical JSON path")
    audit_parser.add_argument("--output", required=True, help="audit report JSON path")
    audit_parser.set_defaults(func=command_audit)

    review_parser = subparsers.add_parser(
        "review", help="apply explicit decisions to a new canonical review artifact"
    )
    review_parser.add_argument("--input", required=True, help="canonical input JSON")
    review_parser.add_argument("--decisions", required=True, help="review decision JSON")
    review_parser.add_argument("--output", required=True, help="reviewed canonical JSON")
    review_parser.set_defaults(func=command_review)

    merge_parser = subparsers.add_parser(
        "merge", help="merge canonical transcripts in explicit order without adding timelines"
    )
    merge_parser.add_argument("--inputs", nargs="+", required=True)
    merge_parser.add_argument("--output-txt", required=True)
    merge_parser.add_argument("--output-json", required=True)
    merge_parser.add_argument(
        "--heading", choices=("none", "id", "source-name"), default="id"
    )
    merge_parser.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help="permit raw or edited-unverified inputs and mark the merged JSON",
    )
    merge_parser.set_defaults(func=command_merge)

    validate_parser = subparsers.add_parser(
        "validate", help="validate source hashes and final transcript/Obsidian artifacts"
    )
    validate_parser.add_argument("--manifest")
    validate_parser.add_argument("--canonical", action="append")
    validate_parser.add_argument("--merged-txt")
    validate_parser.add_argument("--note")
    validate_parser.add_argument("--vault-root")
    validate_parser.add_argument(
        "--delivery-mode",
        choices=("draft", "final"),
        default="final",
        help="draft permits unreviewed non-destructive inputs; final requires reviewed or reviewed-partial inputs",
    )
    validate_parser.add_argument("--report", required=True)
    validate_parser.set_defaults(func=command_validate)

    self_test_parser = subparsers.add_parser(
        "self-test", help="run deterministic cross-engine fixture tests in a temporary directory"
    )
    self_test_parser.set_defaults(func=command_self_test)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except UserError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except AssertionError as exc:
        print(f"SELF_TEST_FAILED: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
