# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic data contract for local human subtitle review.

The browser UI is deliberately kept separate from this module.  This layer
builds a review bundle from bilingual SRT and ASR evidence, validates untrusted
browser submissions, and renders a new bilingual SRT without overwriting the
translation input.
"""

from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from output_layout import artifact_name

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Callable
import unicodedata

from asr_evidence import (
    canonical_fingerprint,
    file_fingerprint,
    normalize_text,
    write_json_atomic,
)
from asr_timeline import write_text_atomic
from script_alignment import validate_script_alignment_artifact


BUNDLE_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 2
MAX_TEXT_LENGTH = 20_000
TIMING_BOUNDARY_REVIEW_THRESHOLD_MS = 1_500
TIMING_OVERLAP_GAP_REVIEW_MAX_MS = 1_000
TIMING_OVERLAP_PARTITION_SHIFT_REVIEW_MIN_MS = 500
_TIMECODE_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)
_SAFE_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_LOW_CONFIDENCE_MARKERS = ("〔認識不良〕", "（低信頼度）", "(低信頼度)")
_TIMING_REVIEW_REASONS = {
    "timeline_requires_review",
    "shared_timeline_window",
    "group_level_timing_only",
}


def _load_json(path: os.PathLike | str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _file_fingerprint(path: os.PathLike | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_clock(groups: tuple[str, ...]) -> int:
    hours, minutes, seconds, millis = (int(value) for value in groups)
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid SRT time component")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _parse_timecode(value: str) -> tuple[int, int]:
    match = _TIMECODE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"invalid SRT timecode: {value!r}")
    start_ms = _parse_clock(match.groups()[:4])
    end_ms = _parse_clock(match.groups()[4:])
    if end_ms <= start_ms:
        raise ValueError(f"non-positive SRT duration: {value!r}")
    return start_ms, end_ms


def _normalize_and_validate_srt_text(
    value: str,
    field: str,
    *,
    allow_empty: bool,
    multiline: bool,
) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > MAX_TEXT_LENGTH:
        raise ValueError(f"{field} is too long")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    if "-->" in value:
        raise ValueError(f"{field} contains a forbidden SRT time arrow")
    if any(
        unicodedata.category(char) == "Cc" and char not in "\n\t"
        for char in value
    ):
        raise ValueError(f"{field} contains control characters")
    if not multiline and "\n" in value:
        raise ValueError(f"{field} must be a single line")
    if multiline and value and any(not line.strip() for line in value.split("\n")):
        raise ValueError(f"{field} contains an empty subtitle line")
    return value


def _read_srt(path: os.PathLike | str, *, bilingual: bool) -> list[dict]:
    content = Path(path).read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\s*\r?\n", content.strip()) if content.strip() else []
    items = []
    seen = set()
    for position, block in enumerate(blocks, start=1):
        lines = block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        minimum = 4 if bilingual else 3
        if len(lines) < minimum:
            raise ValueError(f"invalid SRT block {position} in {path}")
        try:
            index = int(lines[0])
        except ValueError as exc:
            raise ValueError(f"invalid SRT index in block {position}: {lines[0]!r}") from exc
        if index <= 0 or index in seen:
            raise ValueError(f"duplicate or invalid SRT index: {index}")
        seen.add(index)
        start_ms, end_ms = _parse_timecode(lines[1])
        if bilingual:
            text_ja = _normalize_and_validate_srt_text(
                lines[2], f"SRT block {index} Japanese", allow_empty=False, multiline=False,
            )
            text_zh = _normalize_and_validate_srt_text(
                "\n".join(lines[3:]), f"SRT block {index} Chinese",
                allow_empty=False, multiline=True,
            )
        else:
            text_ja = _normalize_and_validate_srt_text(
                " ".join(line.strip() for line in lines[2:] if line.strip()),
                f"SRT block {index} Japanese", allow_empty=False, multiline=False,
            )
            text_zh = ""
        items.append({
            "index": index,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text_ja": text_ja,
            "text_zh": text_zh,
        })
    return items


def _by_index(path: os.PathLike | str | None) -> dict[int, dict]:
    if not path or not Path(path).exists():
        return {}
    return {item["index"]: item for item in _read_srt(path, bilingual=False)}


def _validate_evidence_document(document: dict) -> None:
    if document.get("schema_version") != 1:
        raise ValueError("unsupported ASR evidence schema")
    if document.get("artifact_type") != "asr_candidates" or document.get("status") != "complete":
        raise ValueError("invalid ASR evidence artifact")
    candidates = document.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("ASR candidates must be an array")
    ids = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("evidence_id"), str):
            raise ValueError("invalid ASR candidate")
        ids.append(candidate["evidence_id"])
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate ASR evidence_id")


def _validate_timeline(document: dict) -> None:
    if document.get("schema_version") != 1:
        raise ValueError("unsupported fusion timeline schema")
    if document.get("artifact_type") != "fusion_timeline" or document.get("status") != "complete":
        raise ValueError("invalid fusion timeline artifact")
    if not isinstance(document.get("items"), list):
        raise ValueError("fusion timeline items must be an array")
    if not isinstance(document.get("excluded_evidence", []), list):
        raise ValueError("excluded_evidence must be an array")
    timeline_body = {
        "contract_version": document.get("contract_version"),
        "items": document["items"],
        "excluded_evidence": document.get("excluded_evidence", []),
    }
    if document.get("timeline_fingerprint") != canonical_fingerprint(timeline_body):
        raise ValueError("fusion timeline fingerprint mismatch")


def _validate_artifact_sources(
    source_audio: Path,
    evidence_document: dict | None,
    timeline: dict | None,
) -> None:
    if not evidence_document and not timeline:
        return
    if timeline and not evidence_document:
        raise ValueError("fusion timeline requires its ASR evidence artifact")
    stat = source_audio.stat()
    evidence_source = evidence_document.get("source", {})
    if not isinstance(evidence_source, dict):
        raise ValueError("ASR evidence source must be an object")
    expected = {
        "audio_name": source_audio.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": file_fingerprint(str(source_audio))["sha256"],
    }
    for field, value in expected.items():
        recorded = evidence_source.get(field)
        if recorded is not None and recorded != value:
            raise ValueError(f"ASR evidence source {field} mismatch")
    if timeline:
        timeline_source = timeline.get("source")
        if not isinstance(timeline_source, dict):
            raise ValueError("fusion timeline source must be an object")
        for field in (
            "audio_name", "size_bytes", "mtime_ns", "sha256", "path_fingerprint",
        ):
            if timeline_source.get(field) != evidence_source.get(field):
                raise ValueError(f"fusion timeline source {field} mismatch")
        if timeline.get("evidence_fingerprint") != canonical_fingerprint(evidence_document):
            raise ValueError("fusion timeline evidence fingerprint mismatch")
        known_ids = {candidate["evidence_id"] for candidate in evidence_document["candidates"]}
        referenced_ids = {
            evidence_id
            for item in timeline["items"]
            for evidence_id in item.get("evidence_ids", [])
        } | {
            item.get("evidence_id") for item in timeline.get("excluded_evidence", [])
        }
        referenced_ids.discard(None)
        unknown = sorted(referenced_ids - known_ids)
        if unknown:
            raise ValueError(f"fusion timeline references unknown evidence: {unknown[0]}")


def _candidate_public(candidate: dict, romanizer: Callable[[str], str] | None) -> dict:
    text = str(candidate.get("text", "")).strip()
    return {
        "evidence_id": candidate["evidence_id"],
        "model": str(candidate.get("model", "")),
        "model_role": str(candidate.get("model_role", "")),
        "channel": str(candidate.get("source_channel", "")),
        "view": str(candidate.get("source_view", "")),
        "start_ms": int(round(float(candidate.get("start", 0.0)) * 1000)),
        "end_ms": int(round(float(candidate.get("end", 0.0)) * 1000)),
        "text_ja": text,
        "romaji": romanizer(text) if romanizer and text else None,
        "confidence": {
            "avg_logprob": candidate.get("avg_logprob"),
            "no_speech_prob": candidate.get("no_speech_prob"),
            "compression_ratio": candidate.get("compression_ratio"),
        },
        "legacy_srt": bool(candidate.get("legacy_srt")),
    }


def _matching_timeline_item(cue: dict, timeline_items: list[dict]) -> dict | None:
    exact = [
        item for item in timeline_items
        if abs(int(round(float(item["start"]) * 1000)) - cue["start_ms"]) <= 2
        and abs(int(round(float(item["end"]) * 1000)) - cue["end_ms"]) <= 2
    ]
    if len(exact) == 1:
        return exact[0]
    overlaps = []
    for item in timeline_items:
        start_ms = int(round(float(item["start"]) * 1000))
        end_ms = int(round(float(item["end"]) * 1000))
        overlap = max(0, min(cue["end_ms"], end_ms) - max(cue["start_ms"], start_ms))
        if overlap:
            union = max(cue["end_ms"], end_ms) - min(cue["start_ms"], start_ms)
            overlaps.append((overlap / union, item))
    overlaps.sort(key=lambda pair: (-pair[0], pair[1].get("timeline_id", "")))
    if overlaps and overlaps[0][0] >= 0.8:
        if len(overlaps) == 1 or overlaps[0][0] > overlaps[1][0]:
            return overlaps[0][1]
    return None


def _match_timeline_items_for_cues(
    cues: list[dict], timeline_items: list[dict],
) -> list[dict | None]:
    matched = [_matching_timeline_item(cue, timeline_items) for cue in cues]
    if len(cues) != len(timeline_items):
        return matched
    if [cue.get("index") for cue in cues] != list(range(1, len(cues) + 1)):
        return matched
    expected_ids = [f"TL{index:06d}" for index in range(1, len(cues) + 1)]
    if [item.get("timeline_id") for item in timeline_items] != expected_ids:
        return matched
    if any(
        item is not None and item.get("timeline_id") != expected_ids[position]
        for position, item in enumerate(matched)
    ):
        return matched
    return [item or timeline_items[position] for position, item in enumerate(matched)]


def _timeline_item_for_long_cue(
    long_cue_item: dict,
    timeline_items: list[dict],
    source_cue_count: int,
) -> dict | None:
    original = long_cue_item["original"]
    matched = _matching_timeline_item(
        {
            "start_ms": int(original["start_ms"]),
            "end_ms": int(original["end_ms"]),
        },
        timeline_items,
    )
    if matched is not None:
        return matched
    try:
        source_index = int(long_cue_item["source_index"])
    except (KeyError, TypeError, ValueError):
        return None
    if source_cue_count != len(timeline_items):
        return None
    if not 1 <= source_index <= source_cue_count:
        return None
    expected_ids = [
        f"TL{index:06d}" for index in range(1, len(timeline_items) + 1)
    ]
    if [item.get("timeline_id") for item in timeline_items] != expected_ids:
        return None
    return timeline_items[source_index - 1]


def _script_review_reasons_by_timeline_id(alignment: dict) -> dict[str, list[str]]:
    reasons_by_id: dict[str, set[str]] = {}
    for match in alignment.get("matches", []):
        if not match.get("review_required"):
            continue
        reasons = {str(value) for value in match.get("reasons", []) if value}
        reasons.add("script_alignment_review")
        for timeline_id in match.get("timeline_ids", []):
            reasons_by_id.setdefault(str(timeline_id), set()).update(reasons)
    for unmatched in alignment.get("unmatched_timeline_items", []):
        timeline_id = str(unmatched.get("timeline_id", ""))
        if not timeline_id:
            continue
        reasons = {str(value) for value in unmatched.get("reasons", []) if value}
        reasons.add("script_alignment_unmatched")
        reasons_by_id.setdefault(timeline_id, set()).update(reasons)
    return {
        timeline_id: sorted(reasons)
        for timeline_id, reasons in reasons_by_id.items()
    }


def _load_script_review_reasons(
    script_units_path: os.PathLike | str | None,
    script_alignment_path: os.PathLike | str | None,
    timeline: dict | None,
) -> tuple[dict[str, list[str]] | None, str | None]:
    provided = (script_units_path is not None, script_alignment_path is not None)
    if provided == (False, False):
        return None, None
    if provided != (True, True):
        raise ValueError("script units and script alignment must be provided together")
    if timeline is None:
        raise ValueError("script alignment review requires a fusion timeline")
    units_path = Path(script_units_path)
    alignment_path = Path(script_alignment_path)
    if not units_path.is_file() or not alignment_path.is_file():
        raise FileNotFoundError(
            units_path if not units_path.is_file() else alignment_path
        )
    units = _load_json(units_path)
    alignment = _load_json(alignment_path)
    validate_script_alignment_artifact(alignment, units, timeline)
    return (
        _script_review_reasons_by_timeline_id(alignment),
        alignment["artifact_fingerprint"],
    )


def _load_long_cue_alignment(
    path: os.PathLike | str | None,
    source_audio: Path,
    ensemble_srt_path: os.PathLike | str | None,
) -> dict | None:
    if path is None or not Path(path).is_file():
        return None
    if ensemble_srt_path is None or not Path(ensemble_srt_path).is_file():
        raise ValueError("long-cue alignment requires the ensemble SRT")
    document = _load_json(path)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported long-cue alignment schema")
    if (
        document.get("artifact_type") != "long_cue_alignment"
        or document.get("status") != "complete"
    ):
        raise ValueError("invalid long-cue alignment artifact")
    if document.get("mode") not in {"shadow", "apply"}:
        raise ValueError("invalid long-cue alignment mode")
    if not isinstance(document.get("items"), list):
        raise ValueError("long-cue alignment items must be an array")
    if (
        isinstance(document.get("source_cue_count"), bool)
        or not isinstance(document.get("source_cue_count"), int)
        or document["source_cue_count"] < 0
    ):
        raise ValueError("invalid long-cue alignment source cue count")
    if document.get("source_audio") != file_fingerprint(source_audio):
        raise ValueError("long-cue alignment audio fingerprint mismatch")
    if document.get("output_srt") != file_fingerprint(ensemble_srt_path):
        raise ValueError("long-cue alignment ensemble fingerprint mismatch")
    return document


def _long_cue_alignment_by_bounds(document: dict | None) -> dict[tuple[int, int], dict]:
    if document is None:
        return {}
    indexed = {}
    for item in document["items"]:
        if not isinstance(item, dict):
            raise ValueError("invalid long-cue alignment item")
        status = item.get("status")
        if status not in {"aligned", "split", "unresolved"}:
            raise ValueError("invalid long-cue alignment item status")
        original = item.get("original")
        if not isinstance(original, dict):
            raise ValueError("long-cue alignment item lacks original bounds")
        bounds_source = (
            item.get("cues", [])
            if document["mode"] == "apply" and status in {"aligned", "split"}
            else [original]
        )
        if not isinstance(bounds_source, list) or not bounds_source:
            raise ValueError("long-cue alignment item lacks usable bounds")
        for bounds in bounds_source:
            try:
                key = (int(bounds["start_ms"]), int(bounds["end_ms"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid long-cue alignment bounds") from exc
            if key[0] < 0 or key[1] <= key[0]:
                raise ValueError("non-positive long-cue alignment bounds")
            if key in indexed:
                raise ValueError("duplicate long-cue alignment bounds")
            indexed[key] = item
    return indexed


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _texts_support_each_other(left: str, right: str) -> bool:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= 2
    return SequenceMatcher(None, a, b).ratio() >= 0.62


def _timing_candidate_supports_text(candidate_text: str, current_text: str) -> bool:
    candidate = normalize_text(candidate_text)
    current = normalize_text(current_text)
    if not candidate or not current:
        return False
    coverage = min(len(candidate), len(current)) / max(len(candidate), len(current))
    if coverage < 0.7:
        return False
    return (
        candidate in current
        or current in candidate
        or SequenceMatcher(None, candidate, current).ratio() >= 0.7
    )


def _has_boundary_disagreement(
    candidate_rows: list[dict], current_ja: str, start_ms: int, end_ms: int,
) -> bool:
    compatible = [
        row for row in candidate_rows
        if _timing_candidate_supports_text(row.get("text_ja", ""), current_ja)
    ]
    if not compatible:
        return False
    starts = [start_ms] + [row["start_ms"] for row in compatible]
    ends = [end_ms] + [row["end_ms"] for row in compatible]
    return (
        max(starts) - min(starts) > TIMING_BOUNDARY_REVIEW_THRESHOLD_MS
        or max(ends) - min(ends) > TIMING_BOUNDARY_REVIEW_THRESHOLD_MS
    )


def _overlap_requires_review(
    previous_gap_ms: int | None,
    next_gap_ms: int | None,
    partition_shift_ms: int,
) -> bool:
    has_small_positive_gap = any(
        gap is not None and 0 < gap <= TIMING_OVERLAP_GAP_REVIEW_MAX_MS
        for gap in (previous_gap_ms, next_gap_ms)
    )
    return (
        has_small_positive_gap
        and partition_shift_ms >= TIMING_OVERLAP_PARTITION_SHIFT_REVIEW_MIN_MS
    )


def _item_review_flags(
    item: dict | None,
    candidate_rows: list[dict],
    ja: str,
    zh: str,
    start_ms: int,
    end_ms: int,
    *,
    script_review_reasons: list[str] | None = None,
    script_filter_active: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    text_flags = []
    timing_flags = []
    informational_flags = []
    current_supported = any(
        _texts_support_each_other(ja, row.get("text_ja", ""))
        for row in candidate_rows
    )
    if item:
        if item.get("classification") != "supported" and not current_supported:
            text_flags.extend(["uncertain", "uncertain_asr_evidence"])
        if script_review_reasons:
            for reason in script_review_reasons:
                if reason == "script_alignment_review":
                    continue
                if reason in _TIMING_REVIEW_REASONS:
                    timing_flags.append(reason)
                else:
                    text_flags.append(reason)
        elif (
            not script_filter_active
            and item.get("review_required")
            and not current_supported
        ):
            text_flags.append("uncertain")
        timing_kind = str(item.get("timing_source", {}).get("kind", ""))
        if "overlap" in timing_kind:
            if script_filter_active and timing_flags:
                timing_flags.append("overlap")
            elif not script_filter_active:
                informational_flags.append("overlap_partitioned")
        if _has_boundary_disagreement(candidate_rows, ja, start_ms, end_ms):
            timing_flags.append("boundary_disagreement")
    unique_texts = {normalize_text(row.get("text_ja", "")) for row in candidate_rows}
    unique_texts.discard("")
    if len(unique_texts) > 1:
        if current_supported and not script_filter_active:
            informational_flags.append("model_disagreement_resolved")
        elif not script_filter_active or bool(text_flags):
            text_flags.append("model_disagreement")
    if any(marker in ja or marker in zh for marker in _LOW_CONFIDENCE_MARKERS):
        text_flags.append("low_confidence")
    return (
        _unique(text_flags),
        _unique(timing_flags),
        _unique(informational_flags),
    )


def _set_context(items: list[dict]) -> None:
    previous = []
    last_subtitle = None
    for item in items:
        previous.append(last_subtitle)
        if item["kind"] == "subtitle":
            last_subtitle = item["id"]
    following = [None] * len(items)
    next_subtitle = None
    for position in range(len(items) - 1, -1, -1):
        following[position] = next_subtitle
        if items[position]["kind"] == "subtitle":
            next_subtitle = items[position]["id"]
    for position, item in enumerate(items):
        item["context"] = {
            "prev_id": previous[position],
            "next_id": following[position],
        }


def build_review_bundle(
    bilingual_srt_path: os.PathLike | str,
    audio_path: os.PathLike | str,
    *,
    ensemble_srt_path: os.PathLike | str | None = None,
    reviewed_srt_path: os.PathLike | str | None = None,
    candidates_path: os.PathLike | str | None = None,
    timeline_path: os.PathLike | str | None = None,
    script_units_path: os.PathLike | str | None = None,
    script_alignment_path: os.PathLike | str | None = None,
    long_cue_alignment_path: os.PathLike | str | None = None,
    romanizer: Callable[[str], str] | None = None,
) -> dict:
    """Build a self-contained, path-safe review bundle.

    Absolute local paths are intentionally omitted.  A loopback UI server may
    map the opaque ``audio_id`` back to ``audio_path`` without exposing it to
    browser-controlled data.
    """
    bilingual_path = Path(bilingual_srt_path)
    source_audio = Path(audio_path)
    if not bilingual_path.is_file():
        raise FileNotFoundError(bilingual_path)
    if not source_audio.is_file():
        raise FileNotFoundError(source_audio)
    cues = _read_srt(bilingual_path, bilingual=True)
    ensemble_by_index = _by_index(ensemble_srt_path)
    reviewed_by_index = _by_index(reviewed_srt_path)

    evidence_document = _load_json(candidates_path) if candidates_path and Path(candidates_path).exists() else None
    if evidence_document:
        _validate_evidence_document(evidence_document)
    timeline = _load_json(timeline_path) if timeline_path and Path(timeline_path).exists() else None
    if timeline:
        _validate_timeline(timeline)
    _validate_artifact_sources(source_audio, evidence_document, timeline)
    script_review_reasons, script_alignment_fingerprint = _load_script_review_reasons(
        script_units_path, script_alignment_path, timeline,
    )
    long_cue_alignment = _load_long_cue_alignment(
        long_cue_alignment_path, source_audio, ensemble_srt_path,
    )
    long_cue_by_bounds = _long_cue_alignment_by_bounds(long_cue_alignment)

    evidence_candidates = evidence_document.get("candidates", []) if evidence_document else []
    evidence_by_id = {candidate["evidence_id"]: candidate for candidate in evidence_candidates}
    timeline_items = timeline.get("items", []) if timeline else []
    matched_timeline_items = (
        _match_timeline_items_for_cues(cues, timeline_items) if timeline else [None] * len(cues)
    )
    if timeline and long_cue_by_bounds:
        for position, cue in enumerate(cues):
            if matched_timeline_items[position] is not None:
                continue
            long_cue_item = long_cue_by_bounds.get(
                (cue["start_ms"], cue["end_ms"])
            )
            if long_cue_item is None:
                continue
            matched_timeline_items[position] = _timeline_item_for_long_cue(
                long_cue_item,
                timeline_items,
                long_cue_alignment["source_cue_count"],
            )
    if script_review_reasons is not None:
        mapped_timeline_ids = {
            item.get("timeline_id") for item in matched_timeline_items if item
        }
        missing_review_ids = set(script_review_reasons) - mapped_timeline_ids
        if missing_review_ids:
            raise ValueError(
                "script review timeline items are not represented in the bilingual SRT: "
                + ", ".join(sorted(missing_review_ids))
            )
    public_evidence = {}
    items = []

    for cue, timeline_item in zip(cues, matched_timeline_items):
        evidence_ids = list(timeline_item.get("evidence_ids", [])) if timeline_item else []
        candidate_rows = []
        for evidence_id in evidence_ids:
            candidate = evidence_by_id.get(evidence_id)
            if candidate:
                public_evidence[evidence_id] = _candidate_public(candidate, romanizer)
                candidate_rows.append(public_evidence[evidence_id])
        current_ja = cue["text_ja"]
        item_id = f"SRT{cue['index']:06d}"
        text_flags, timing_flags, informational_flags = _item_review_flags(
            timeline_item,
            candidate_rows,
            current_ja,
            cue["text_zh"],
            cue["start_ms"],
            cue["end_ms"],
            script_review_reasons=(
                script_review_reasons.get(timeline_item.get("timeline_id"), [])
                if script_review_reasons is not None and timeline_item else None
            ),
            script_filter_active=script_review_reasons is not None,
        )
        long_cue_item = long_cue_by_bounds.get((cue["start_ms"], cue["end_ms"]))
        if long_cue_item is not None:
            informational_flags = _unique(
                informational_flags + ["long_cue_alignment"]
            )
            if (
                long_cue_alignment["mode"] == "shadow"
                or long_cue_item["status"] == "unresolved"
                or bool(long_cue_item.get("review_required"))
            ):
                timing_flags = _unique(
                    timing_flags + ["long_cue_alignment_review"]
                )
        items.append({
            "id": item_id,
            "kind": "subtitle",
            "source_index": cue["index"],
            "timeline_id": timeline_item.get("timeline_id") if timeline_item else None,
            "start_ms": cue["start_ms"],
            "end_ms": cue["end_ms"],
            "texts": {
                "ensemble_ja": ensemble_by_index.get(cue["index"], {}).get("text_ja"),
                "reviewed_ja": reviewed_by_index.get(cue["index"], {}).get("text_ja", current_ja),
                "ja": current_ja,
                "zh": cue["text_zh"],
                "romaji": romanizer(current_ja) if romanizer else None,
            },
            "evidence_ids": evidence_ids,
            "text_flags": text_flags,
            "timing_flags": timing_flags,
            "informational_flags": informational_flags,
            "review_dimensions": {
                "text_required": bool(text_flags),
                "timing_required": bool(timing_flags),
            },
            "flags": text_flags + timing_flags,
        })

    subtitle_items = list(items)
    for position, item in enumerate(subtitle_items):
        if "overlap_partitioned" not in item["informational_flags"]:
            continue
        timeline_item = matched_timeline_items[position]
        if timeline_item is None:
            continue
        previous = subtitle_items[position - 1] if position else None
        following = (
            subtitle_items[position + 1]
            if position + 1 < len(subtitle_items) else None
        )
        previous_gap = (
            item["start_ms"] - previous["end_ms"] if previous else None
        )
        next_gap = (
            following["start_ms"] - item["end_ms"] if following else None
        )
        timeline_start_ms = int(round(float(timeline_item["start"]) * 1000))
        timeline_end_ms = int(round(float(timeline_item["end"]) * 1000))
        partition_shift_ms = max(
            abs(item["start_ms"] - timeline_start_ms),
            abs(item["end_ms"] - timeline_end_ms),
        )
        if _overlap_requires_review(
            previous_gap, next_gap, partition_shift_ms,
        ):
            item["timing_flags"] = _unique(item["timing_flags"] + ["overlap"])
            item["review_dimensions"]["timing_required"] = True
            item["flags"] = item["text_flags"] + item["timing_flags"]

    if timeline:
        for position, excluded in enumerate(timeline.get("excluded_evidence", []), start=1):
            evidence_id = excluded.get("evidence_id")
            candidate = evidence_by_id.get(evidence_id)
            if not candidate:
                continue
            public = _candidate_public(candidate, romanizer)
            public_evidence[evidence_id] = public
            reason = str(excluded.get("reason", "excluded_evidence"))
            flags = [reason]
            if "hallucination" in reason or "template" in reason:
                flags.append("hallucination")
            optional_suppressed = reason == "review_only_rescue_suppression"
            items.append({
                "id": f"EX{position:06d}",
                "kind": "excluded_evidence",
                "source_index": None,
                "timeline_id": None,
                "start_ms": public["start_ms"],
                "end_ms": public["end_ms"],
                "texts": {
                    "ensemble_ja": None,
                    "reviewed_ja": None,
                    "ja": "",
                    "zh": "",
                    "romaji": public["romaji"],
                },
                "evidence_ids": [evidence_id],
                "text_flags": flags,
                "timing_flags": [],
                "informational_flags": [],
                "review_dimensions": {
                    "text_required": not optional_suppressed,
                    "timing_required": False,
                },
                "review_policy": (
                    "optional_suppressed" if optional_suppressed else "required"
                ),
                "default_action": "reject" if optional_suppressed else "",
                "flags": flags,
            })

    items.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["id"]))
    _set_context(items)
    stat = source_audio.stat()
    source_identity = {
        "audio_name": source_audio.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "audio_fingerprint": _file_fingerprint(source_audio),
        "input_srt_name": artifact_name(bilingual_path),
        "input_srt_fingerprint": _file_fingerprint(bilingual_path),
    }
    identity_payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source": source_identity,
        "timeline_fingerprint": timeline.get("timeline_fingerprint") if timeline else None,
        "script_alignment_fingerprint": script_alignment_fingerprint,
        "long_cue_alignment_fingerprint": (
            canonical_fingerprint(long_cue_alignment) if long_cue_alignment else None
        ),
    }
    bundle_id = canonical_fingerprint(identity_payload)
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "artifact_type": "human_review_bundle",
        "status": "ready",
        "bundle_id": bundle_id,
        "source": source_identity,
        "audio": {"audio_id": f"audio-{bundle_id[:16]}", "name": source_audio.name},
        "input_stage": "zh",
        "timeline_fingerprint": timeline.get("timeline_fingerprint") if timeline else None,
        "script_alignment_fingerprint": script_alignment_fingerprint,
        "long_cue_alignment_fingerprint": (
            canonical_fingerprint(long_cue_alignment) if long_cue_alignment else None
        ),
        "romaji_available": romanizer is not None,
        "items": items,
        "evidence": public_evidence,
        "bundle_fingerprint": canonical_fingerprint({
            "identity": identity_payload,
            "items": items,
            "evidence": public_evidence,
        }),
    }


def _safe_review_text(value, field: str, *, allow_empty: bool = True, multiline: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return _normalize_and_validate_srt_text(
        value, field, allow_empty=allow_empty, multiline=multiline,
    )


def _review_dimensions(item: dict) -> dict:
    value = item.get("review_dimensions")
    if isinstance(value, dict):
        return {
            "text_required": bool(value.get("text_required")),
            "timing_required": bool(value.get("timing_required")),
        }
    return {
        "text_required": bool(item.get("flags")),
        "timing_required": False,
    }


def migrate_legacy_review_submission(bundle: dict, submission: dict) -> dict[str, dict]:
    """Convert a prior v1/v2 result into a safe browser draft.

    Text decisions survive a protocol upgrade.  Timing decisions are left open
    for v1 results when the current bundle requires timing review.
    """
    if not isinstance(submission, dict) or submission.get("schema_version") not in {1, 2}:
        raise ValueError("unsupported prior human review schema")
    if submission.get("bundle_id") != bundle.get("bundle_id"):
        raise ValueError("prior human review does not match bundle_id")
    submitted_items = submission.get("items")
    if not isinstance(submitted_items, dict):
        raise ValueError("prior human review items must be an object")
    migrated = {}
    for item in bundle.get("items", []):
        raw = submitted_items.get(item["id"])
        if not isinstance(raw, dict):
            continue
        allowed = {"keep", "edit", "reject"} if item["kind"] == "subtitle" else {"accept", "edit", "reject"}
        action = raw.get("action") if raw.get("action") in allowed else ""
        selected = raw.get("selected_evidence_ids", [])
        if not isinstance(selected, list):
            selected = []
        selected = [
            value for value in selected
            if isinstance(value, str) and value in item.get("evidence_ids", [])
        ]
        timing_action = raw.get("timing_action", "") if submission["schema_version"] == 2 else ""
        if timing_action not in {"keep", "edit"}:
            timing_action = ""
        if not _review_dimensions(item)["timing_required"] and not timing_action:
            timing_action = "keep"
        migrated[item["id"]] = {
            "action": action,
            "selected_evidence_ids": list(dict.fromkeys(selected)),
            "edited_ja": raw.get("edited_ja", "") if isinstance(raw.get("edited_ja", ""), str) else "",
            "edited_zh": raw.get("edited_zh", "") if isinstance(raw.get("edited_zh", ""), str) else "",
            "note": raw.get("note", "") if isinstance(raw.get("note", ""), str) else "",
            "timing_action": timing_action,
            "edited_start_ms": raw.get("edited_start_ms") if submission["schema_version"] == 2 else None,
            "edited_end_ms": raw.get("edited_end_ms") if submission["schema_version"] == 2 else None,
            "structure_action": (
                raw.get("structure_action", "keep")
                if raw.get("structure_action", "keep") in {"keep", "split", "merge_next"}
                else "keep"
            ),
            "split_at_ms": raw.get("split_at_ms"),
            "split_ja_before": raw.get("split_ja_before", ""),
            "split_ja_after": raw.get("split_ja_after", ""),
            "split_zh_before": raw.get("split_zh_before", ""),
            "split_zh_after": raw.get("split_zh_after", ""),
        }
    return migrated


def _optional_millisecond(value, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer millisecond value")
    return value


def validate_review_submission(
    bundle: dict,
    submission: dict,
    *,
    require_complete: bool = False,
) -> dict:
    if not isinstance(submission, dict) or submission.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise ValueError("unsupported human review submission schema")
    if submission.get("bundle_id") != bundle.get("bundle_id"):
        raise ValueError("review submission does not match bundle_id")
    submitted_items = submission.get("items")
    if not isinstance(submitted_items, dict):
        raise ValueError("review submission items must be an object")
    bundle_items = {item["id"]: item for item in bundle.get("items", [])}
    unknown = sorted(set(submitted_items) - set(bundle_items))
    if unknown:
        raise ValueError(f"unknown review item: {unknown[0]}")
    if require_complete:
        missing = sorted(set(bundle_items) - set(submitted_items))
        if missing:
            raise ValueError(f"missing review item: {missing[0]}")

    normalized_items = {}
    for item_id, review in submitted_items.items():
        if not _SAFE_ITEM_ID_RE.fullmatch(item_id) or not isinstance(review, dict):
            raise ValueError(f"invalid review item: {item_id!r}")
        item = bundle_items[item_id]
        allowed_actions = {"keep", "edit", "reject"} if item["kind"] == "subtitle" else {"accept", "edit", "reject"}
        action = review.get("action")
        if action not in allowed_actions:
            raise ValueError(f"invalid action for {item_id}: {action!r}")
        selected = review.get("selected_evidence_ids", [])
        if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
            raise ValueError(f"selected_evidence_ids must be a string array for {item_id}")
        if len(selected) != len(set(selected)):
            raise ValueError(f"duplicate selected evidence for {item_id}")
        unknown_evidence = set(selected) - set(item.get("evidence_ids", []))
        if unknown_evidence:
            raise ValueError(f"unknown evidence for {item_id}: {sorted(unknown_evidence)[0]}")
        edited_ja = _safe_review_text(review.get("edited_ja"), f"{item_id}.edited_ja", multiline=False)
        edited_zh = _safe_review_text(review.get("edited_zh"), f"{item_id}.edited_zh")
        if action in {"edit", "accept"} and (not edited_ja or not edited_zh):
            raise ValueError(f"edited bilingual text is required for {item_id}")
        dimensions = _review_dimensions(item)
        timing_action = review.get("timing_action", "")
        if action == "reject":
            timing_action = "keep"
        elif timing_action not in {"keep", "edit"}:
            if require_complete and dimensions["timing_required"]:
                raise ValueError(f"timing review is required for {item_id}")
            timing_action = "keep"
        edited_start_ms = _optional_millisecond(
            review.get("edited_start_ms"), f"{item_id}.edited_start_ms",
        )
        edited_end_ms = _optional_millisecond(
            review.get("edited_end_ms"), f"{item_id}.edited_end_ms",
        )
        if timing_action == "edit":
            if edited_start_ms is None or edited_end_ms is None:
                raise ValueError(f"edited start and end are required for {item_id}")
            if edited_start_ms < 0:
                raise ValueError(f"timing start must be non-negative for {item_id}")
            if edited_start_ms >= edited_end_ms:
                raise ValueError(f"timing start must be before end for {item_id}")
            duration_ms = bundle.get("audio", {}).get("duration_ms")
            if duration_ms is not None and edited_end_ms > duration_ms:
                raise ValueError(f"timing end exceeds audio duration for {item_id}")
        else:
            edited_start_ms = None
            edited_end_ms = None
        structure_action = review.get("structure_action", "keep")
        if action == "reject":
            structure_action = "keep"
        if item["kind"] != "subtitle" and structure_action != "keep":
            raise ValueError(f"structural editing is only allowed for subtitles: {item_id}")
        if structure_action not in {"keep", "split", "merge_next"}:
            raise ValueError(f"invalid structure action for {item_id}")
        split_at_ms = _optional_millisecond(
            review.get("split_at_ms"), f"{item_id}.split_at_ms",
        )
        split_fields = {
            field: _safe_review_text(
                review.get(field), f"{item_id}.{field}",
                multiline=not field.startswith("split_ja_"),
            )
            for field in (
                "split_ja_before", "split_ja_after",
                "split_zh_before", "split_zh_after",
            )
        }
        if structure_action == "split":
            effective_start = edited_start_ms if timing_action == "edit" else item["start_ms"]
            effective_end = edited_end_ms if timing_action == "edit" else item["end_ms"]
            if split_at_ms is None or not effective_start < split_at_ms < effective_end:
                raise ValueError(f"split point must be inside subtitle bounds for {item_id}")
            if not all(split_fields.values()):
                raise ValueError(f"complete bilingual split text is required for {item_id}")
        else:
            split_at_ms = None
            split_fields = {field: "" for field in split_fields}
        normalized_items[item_id] = {
            "action": action,
            "selected_evidence_ids": selected,
            "edited_ja": edited_ja,
            "edited_zh": edited_zh,
            "note": _safe_review_text(review.get("note"), f"{item_id}.note"),
            "timing_action": timing_action,
            "edited_start_ms": edited_start_ms,
            "edited_end_ms": edited_end_ms,
            "structure_action": structure_action,
            "split_at_ms": split_at_ms,
            **split_fields,
        }
    ordered_subtitles = [
        item for item in bundle.get("items", []) if item.get("kind") == "subtitle"
    ]
    merged_targets = set()
    for position, item in enumerate(ordered_subtitles):
        review = normalized_items.get(item["id"])
        if not review or review["structure_action"] != "merge_next":
            continue
        if position + 1 >= len(ordered_subtitles):
            raise ValueError(f"cannot merge the last subtitle: {item['id']}")
        next_item = ordered_subtitles[position + 1]
        next_review = normalized_items.get(next_item["id"])
        if next_review is None:
            raise ValueError(f"merge target is missing from submission: {next_item['id']}")
        if next_review["action"] == "reject":
            raise ValueError(f"merge target is rejected: {next_item['id']}")
        if next_review["structure_action"] != "keep":
            raise ValueError(f"merge target also changes structure: {next_item['id']}")
        if item["id"] in merged_targets:
            raise ValueError(f"subtitle is already consumed by a merge: {item['id']}")
        current_start = (
            review["edited_start_ms"]
            if review["timing_action"] == "edit" else item["start_ms"]
        )
        next_end = (
            next_review["edited_end_ms"]
            if next_review["timing_action"] == "edit" else next_item["end_ms"]
        )
        if next_end <= current_start:
            raise ValueError(f"merged subtitle has non-positive duration: {item['id']}")
        merged_targets.add(next_item["id"])
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "artifact_type": "human_review_result",
        "status": "complete" if require_complete else "draft",
        "bundle_id": bundle["bundle_id"],
        "bundle_fingerprint": bundle.get("bundle_fingerprint"),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "items": normalized_items,
        "warnings": [],
    }


def _format_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, int(milliseconds)), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def apply_review_submission(bundle: dict, submission: dict) -> tuple[str, dict]:
    """Validate a complete submission and render a new bilingual SRT."""
    normalized = validate_review_submission(bundle, submission, require_complete=True)
    reviews = normalized["items"]
    cue_by_id = {}
    subtitle_ids = [
        item["id"] for item in bundle["items"] if item["kind"] == "subtitle"
    ]
    for item in bundle["items"]:
        review = reviews[item["id"]]
        if review["action"] == "reject":
            continue
        if review["action"] in {"edit", "accept"}:
            text_ja = review["edited_ja"]
            text_zh = review["edited_zh"]
        else:
            text_ja = item["texts"]["ja"]
            text_zh = item["texts"]["zh"]
        if not text_ja or not text_zh:
            raise ValueError(f"review item has no bilingual output: {item['id']}")
        start_ms = (
            review["edited_start_ms"]
            if review["timing_action"] == "edit" else item["start_ms"]
        )
        end_ms = (
            review["edited_end_ms"]
            if review["timing_action"] == "edit" else item["end_ms"]
        )
        cue_by_id[item["id"]] = {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "id": item["id"],
            "text_ja": text_ja,
            "text_zh": text_zh,
        }
    cues = []
    consumed = set()
    for item in bundle["items"]:
        item_id = item["id"]
        if item_id in consumed or item_id not in cue_by_id:
            continue
        cue = cue_by_id[item_id]
        review = reviews[item_id]
        if review["structure_action"] == "split":
            split_at_ms = review["split_at_ms"]
            cues.extend([
                {
                    **cue,
                    "end_ms": split_at_ms,
                    "id": f"{item_id}-A",
                    "text_ja": review["split_ja_before"],
                    "text_zh": review["split_zh_before"],
                },
                {
                    **cue,
                    "start_ms": split_at_ms,
                    "id": f"{item_id}-B",
                    "text_ja": review["split_ja_after"],
                    "text_zh": review["split_zh_after"],
                },
            ])
            continue
        if review["structure_action"] == "merge_next":
            position = subtitle_ids.index(item_id)
            next_id = subtitle_ids[position + 1]
            next_cue = cue_by_id[next_id]
            cues.append({
                **cue,
                "end_ms": next_cue["end_ms"],
                "id": f"{item_id}+{next_id}",
                "text_ja": cue["text_ja"] + next_cue["text_ja"],
                "text_zh": cue["text_zh"] + next_cue["text_zh"],
            })
            consumed.add(next_id)
            continue
        cues.append(cue)
    cues.sort(key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["id"]))
    normalized["warnings"] = [
        f"{left['id']} overlaps {right['id']} by {left['end_ms'] - right['start_ms']} ms"
        for left, right in zip(cues, cues[1:])
        if left["end_ms"] > right["start_ms"]
    ]
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n"
            f"{_format_timestamp(cue['start_ms'])} --> {_format_timestamp(cue['end_ms'])}\n"
            f"{cue['text_ja']}\n{cue['text_zh']}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else ""), normalized


def write_review_outputs(
    bundle: dict,
    submission: dict,
    *,
    output_srt: os.PathLike | str,
    result_json: os.PathLike | str,
) -> dict:
    """Atomically write SRT first and its JSON completion marker last."""
    output_path = Path(output_srt)
    result_path = Path(result_json)
    srt_text, normalized = apply_review_submission(bundle, submission)
    normalized = {
        **normalized,
        "source_input_srt_name": bundle["source"]["input_srt_name"],
        "source_input_srt_fingerprint": bundle["source"]["input_srt_fingerprint"],
        "timeline_fingerprint": bundle.get("timeline_fingerprint"),
        "output_srt_name": artifact_name(output_path),
        "output_srt_fingerprint": hashlib.sha256(srt_text.encode("utf-8")).hexdigest(),
    }
    existing = [path for path in (output_path, result_path) if path.exists()]
    if existing:
        backup_dir = output_path.parent / "_review_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_suffix = str(time.time_ns())
        for path in existing:
            shutil.copy2(path, backup_dir / f"{path.name}.{backup_suffix}.bak")
    write_text_atomic(str(output_path), srt_text)
    write_json_atomic(str(result_path), normalized)
    return normalized


def validate_completed_review_outputs(
    output_srt: os.PathLike | str,
    result_json: os.PathLike | str,
    *,
    expected_bundle_id: str | None = None,
    expected_bundle_fingerprint: str | None = None,
    current_input_srt: os.PathLike | str | None = None,
) -> dict:
    """Accept a human-reviewed SRT only when its completion marker matches."""
    output_path = Path(output_srt)
    result_path = Path(result_json)
    if not output_path.is_file() or not result_path.is_file():
        raise FileNotFoundError("human review output pair is incomplete")
    result = _load_json(result_path)
    required = {
        "schema_version", "artifact_type", "status", "bundle_id",
        "bundle_fingerprint", "reviewed_at", "items", "output_srt_name",
        "output_srt_fingerprint", "source_input_srt_name",
        "source_input_srt_fingerprint", "timeline_fingerprint", "warnings",
    }
    if set(result) != required:
        raise ValueError("invalid completed human review fields")
    if (
        result["schema_version"] != REVIEW_SCHEMA_VERSION
        or result["artifact_type"] != "human_review_result"
        or result["status"] != "complete"
    ):
        raise ValueError("invalid completed human review identity")
    if expected_bundle_id is not None and result["bundle_id"] != expected_bundle_id:
        raise ValueError("completed human review bundle_id mismatch")
    if (
        expected_bundle_fingerprint is not None
        and result["bundle_fingerprint"] != expected_bundle_fingerprint
    ):
        raise ValueError("completed human review bundle fingerprint mismatch")
    if result["output_srt_name"] != artifact_name(output_path):
        raise ValueError("completed human review output filename mismatch")
    if current_input_srt is not None:
        input_path = Path(current_input_srt)
        if result["source_input_srt_name"] != artifact_name(input_path):
            raise ValueError("completed human review input filename mismatch")
        if result["source_input_srt_fingerprint"] != _file_fingerprint(input_path):
            raise ValueError("completed human review input SRT fingerprint mismatch")
    actual_fingerprint = _file_fingerprint(output_path)
    if actual_fingerprint != result["output_srt_fingerprint"]:
        raise ValueError("completed human review SRT fingerprint mismatch")
    if not isinstance(result["items"], dict):
        raise ValueError("completed human review items must be an object")
    if not isinstance(result["warnings"], list) or any(
        not isinstance(value, str) for value in result["warnings"]
    ):
        raise ValueError("completed human review warnings must be a string array")
    return result
