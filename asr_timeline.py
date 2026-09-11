# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic ASR evidence timeline for later text-only LLM fusion."""

from __future__ import annotations

from difflib import SequenceMatcher
import math
from numbers import Real
import os
import tempfile
import unicodedata

from asr_evidence import canonical_fingerprint, normalize_text


SCHEMA_VERSION = 1
CONTRACT_VERSION = "evidence-v1-shadow"
FUSION_ALGORITHM_VERSION = "deterministic-fusion-v2"
_SELECTION_FIELDS = {"timeline_id", "evidence_ids", "text", "review_required", "note"}
_FORBIDDEN_TIME_FIELDS = {"timecode", "start", "end"}

EVIDENCE_FUSION_SYSTEM_PROMPT = (
    "あなたは日本語ASR証拠の校正担当です。各timeline_idを入力順に必ず1回ずつ提出し、"
    "その項目内のevidence_idだけを1件以上選んでください。時刻の生成・修正は禁止です。\n"
    "候補の文法、前後文脈、モデル間一致を比較してtextを校正してください。"
    "囁き、擬音、叫び、吃音、短い非語彙音を不自然という理由だけで削除しないでください。\n"
    "異なる話者や重なった別発話の可能性がある候補を、架空の流暢な一文に混ぜないでください。"
    "必要なら空行を挟まず複数行で保持し、判断困難ならreview_required=trueとnoteに理由を書いてください。\n"
    "submit_evidence_fusionを呼び出し、timeline_id/evidence_ids/textのみを必須提出してください。"
)

EVIDENCE_FUSION_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_evidence_fusion",
        "description": (
            "固定済みタイムラインごとに根拠IDと校正後の日本語本文だけを提出します。"
            "時刻は絶対に提出しないでください。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "selections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "timeline_id": {"type": "string"},
                            "evidence_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "text": {"type": "string", "minLength": 1},
                            "review_required": {"type": "boolean"},
                            "note": {"type": "string"},
                        },
                        "required": ["timeline_id", "evidence_ids", "text"],
                    },
                },
            },
            "required": ["selections"],
        },
    },
}


def _texts_support_each_other(left: str, right: str) -> bool:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= 2
    return SequenceMatcher(None, a, b).ratio() >= 0.62


def _candidate_priority(candidate: dict) -> tuple:
    channel = candidate.get("source_channel")
    role = candidate.get("model_role")
    native = not candidate.get("legacy_srt")
    has_words = bool(candidate.get("words"))
    if channel == "rescue" and role == "v3" and has_words:
        rank = 0
    elif channel == "main" and role == "v3" and native:
        rank = 1
    elif channel == "main" and role == "turbo" and native:
        rank = 2
    elif channel == "rescue" and role == "turbo":
        rank = 3
    elif channel == "main" and role == "v3":
        rank = 4
    elif channel == "main" and role == "turbo":
        rank = 5
    else:
        rank = 6
    logprob = candidate.get("avg_logprob")
    return (rank, -(logprob if logprob is not None else -100.0), candidate["evidence_id"])


def _candidate_payload(candidate: dict, slice_start: float, slice_end: float) -> dict | None:
    source_words = candidate.get("words", [])
    words = [
        word for word in source_words
        if word.get("start") is not None
        and word.get("end") is not None
        and slice_start <= (word["start"] + word["end"]) / 2 < slice_end
    ]
    if source_words:
        if not words:
            return None
        text = "".join(word["text"] for word in words).strip()
        evidence_start = min(word["start"] for word in words)
        evidence_end = max(word["end"] for word in words)
    else:
        midpoint = (candidate["start"] + candidate["end"]) / 2
        if not (slice_start <= midpoint < slice_end):
            return None
        text = candidate["text"]
        evidence_start = candidate["start"]
        evidence_end = candidate["end"]
    return {
        "evidence_id": candidate["evidence_id"],
        "source_channel": candidate["source_channel"],
        "source_view": candidate["source_view"],
        "model_role": candidate["model_role"],
        "text": text,
        "avg_logprob": candidate.get("avg_logprob"),
        "no_speech_prob": candidate.get("no_speech_prob"),
        "legacy_srt": bool(candidate.get("legacy_srt")),
        "fragment_from_words": bool(words),
        "evidence_start": evidence_start,
        "evidence_end": evidence_end,
    }


def _group_candidates(candidates: list[dict]) -> list[list[dict]]:
    groups = []
    for candidate in sorted(
        candidates, key=lambda item: (item["start"], item["end"], item["evidence_id"])
    ):
        matched = None
        for group in reversed(groups):
            group_start = min(item["start"] for item in group)
            group_end = max(item["end"] for item in group)
            if candidate["start"] > group_end + 0.8:
                break
            cross_run = candidate["run_key"] not in {item["run_key"] for item in group}
            overlaps = (
                cross_run
                and candidate["start"] < group_end
                and candidate["end"] > group_start
            )
            cross_run_support = (
                cross_run
                and candidate["start"] <= group_end + 0.8
                and any(_texts_support_each_other(candidate["text"], item["text"]) for item in group)
            )
            if overlaps or cross_run_support:
                matched = group
                break
        if matched is None:
            groups.append([candidate])
        else:
            matched.append(candidate)
    return groups


def _slice_ranges(group: list[dict], start: float, end: float, max_duration: float):
    ranges = []
    cursor = start
    word_boundaries = sorted({
        float(boundary)
        for item in group
        for word in item.get("words", [])
        for boundary in (word.get("start"), word.get("end"))
        if boundary is not None and start < boundary < end
    })
    while end - cursor > max_duration:
        limit = cursor + max_duration
        candidates = [value for value in word_boundaries if cursor + 0.05 < value <= limit]
        split_at = max(candidates) if candidates else limit
        ranges.append((cursor, split_at))
        cursor = split_at
    if end > cursor:
        ranges.append((cursor, end))
    return ranges


def _partition_overlapping_items(items: list[dict], max_duration: float) -> None:
    """Remove overlap without moving an item away from its acoustic anchor."""
    items.sort(key=lambda item: (item["start"], item["end"], item["timeline_id"]))
    cursor = 0
    while cursor < len(items):
        cluster_end = items[cursor]["end"]
        stop = cursor + 1
        while stop < len(items) and items[stop]["start"] < cluster_end:
            cluster_end = max(cluster_end, items[stop]["end"])
            stop += 1
        if stop - cursor > 1:
            cluster = sorted(
                items[cursor:stop],
                key=lambda item: (
                    (item["timing_source"]["anchor_start"] + item["timing_source"]["anchor_end"]) / 2,
                    item["timeline_id"],
                ),
            )
            midpoints = [
                (item["timing_source"]["anchor_start"] + item["timing_source"]["anchor_end"]) / 2
                for item in cluster
            ]
            boundaries = [
                (left + right) / 2 for left, right in zip(midpoints, midpoints[1:])
            ]
            adjusted = []
            for offset, item in enumerate(cluster):
                start = max(item["start"], boundaries[offset - 1]) if offset else item["start"]
                end = min(item["end"], boundaries[offset]) if offset < len(boundaries) else item["end"]
                midpoint = midpoints[offset]
                if not (start < end and start <= midpoint <= end):
                    adjusted = []
                    break
                adjusted.append((item, start, end))
            if not adjusted:
                merged = cluster[0]
                merged["start"] = min(item["start"] for item in cluster)
                merged["end"] = max(item["end"] for item in cluster)
                if merged["end"] - merged["start"] > max_duration:
                    raise ValueError("unresolved overlap cluster exceeds max_duration")
                merged["classification"] = (
                    "supported"
                    if any(item["classification"] == "supported" for item in cluster)
                    else "uncertain"
                )
                merged["evidence_ids"] = list(dict.fromkeys(
                    evidence_id for item in cluster for evidence_id in item["evidence_ids"]
                ))
                merged["candidates"] = [
                    candidate for item in cluster for candidate in item["candidates"]
                ]
                merged["review_required"] = True
                merged["timing_source"] = {
                    **merged["timing_source"],
                    "kind": "unresolved_overlap_cluster",
                }
                items[cursor:stop] = [merged]
                stop = cursor + 1
            else:
                items[cursor:stop] = [item for item, _, _ in adjusted]
                for item, start, end in adjusted:
                    item["start"] = round(start, 6)
                    item["end"] = round(end, 6)
                    item["review_required"] = True
                    item["timing_source"] = {
                        **item["timing_source"],
                        "kind": "overlap_partition",
                    }
        cursor = stop
    items.sort(key=lambda item: (item["start"], item["end"], item["timeline_id"]))
    for index, item in enumerate(items, start=1):
        item["timeline_id"] = f"TL{index:06d}"


def build_fusion_timeline(
    document: dict,
    decisions: dict,
    *,
    max_duration: float = 15.0,
) -> dict:
    """Build a fixed timeline while keeping every non-quarantined hypothesis."""
    if max_duration <= 0:
        raise ValueError("max_duration must be positive")
    quarantined = set(decisions.get("quarantined_evidence_ids", []))
    review_only = {
        evidence_id
        for run in document.get("runs", {}).values()
        for evidence_id in run.get("config", {}).get("suppressed_v3_candidate_ids", [])
    }
    usable = [
        candidate for candidate in document.get("candidates", [])
        if candidate["evidence_id"] not in quarantined
        and candidate["evidence_id"] not in review_only
        and candidate["end"] > candidate["start"]
    ]
    decision_by_id = decisions.get("candidate_decisions", {})
    items = []
    for group in _group_candidates(usable):
        group_start = min(float(item["start"]) for item in group)
        group_end = max(float(item["end"]) for item in group)
        for cursor, slice_end in _slice_ranges(
            group, group_start, group_end, max_duration
        ):
            slice_candidates = [
                item for item in group
                if item["start"] < slice_end and item["end"] > cursor
            ]
            payload_pairs = [
                (item, _candidate_payload(item, cursor, slice_end))
                for item in sorted(slice_candidates, key=_candidate_priority)
            ]
            payload_pairs = [pair for pair in payload_pairs if pair[1] is not None]
            if not payload_pairs:
                continue
            active_candidates = [pair[0] for pair in payload_pairs]
            payloads = [pair[1] for pair in payload_pairs]
            anchor = min(active_candidates, key=_candidate_priority)
            anchor_payload = next(
                payload for candidate, payload in payload_pairs if candidate is anchor
            )
            labels = [
                decision_by_id.get(item["evidence_id"], {}).get("label", "uncertain")
                for item in active_candidates
            ]
            distinct_texts = {normalize_text(item["text"]) for item in payloads if item["text"]}
            unresolved_split = (
                group_end - group_start > max_duration
                and any(
                    not item.get("words") and item["end"] - item["start"] > max_duration
                    for item in active_candidates
                )
            )
            items.append({
                "timeline_id": f"TL{len(items) + 1:06d}",
                "start": round(cursor, 6),
                "end": round(slice_end, 6),
                "classification": "supported" if "supported" in labels else "uncertain",
                "review_required": (
                    "supported" not in labels or len(distinct_texts) > 1 or unresolved_split
                ),
                "timing_source": {
                    "kind": (
                        "bounded_split" if group_end - group_start > max_duration
                        else "word_candidate" if anchor.get("words")
                        else "candidate_bounds"
                    ),
                    "anchor_evidence_id": anchor["evidence_id"],
                    "anchor_start": anchor_payload["evidence_start"],
                    "anchor_end": anchor_payload["evidence_end"],
                },
                "evidence_ids": [item["evidence_id"] for item in active_candidates],
                "candidates": payloads,
            })

    _partition_overlapping_items(items, max_duration)
    provisional = {"items": items}
    _validate_timeline(provisional, max_duration=max_duration)

    evidence_fingerprint = canonical_fingerprint(document)
    decision_fingerprint = canonical_fingerprint(decisions)
    timeline_body = {
        "contract_version": CONTRACT_VERSION,
        "items": items,
        "excluded_evidence": [
            {"evidence_id": evidence_id, "reason": "quarantined_hallucination"}
            for evidence_id in sorted(quarantined)
        ] + [
            {"evidence_id": evidence_id, "reason": "review_only_rescue_suppression"}
            for evidence_id in sorted(review_only - quarantined)
        ],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "fusion_timeline",
        "status": "complete",
        "contract_version": CONTRACT_VERSION,
        "source": document.get("source"),
        "evidence_fingerprint": evidence_fingerprint,
        "decision_fingerprint": decision_fingerprint,
        "timeline_fingerprint": canonical_fingerprint(timeline_body),
        "items": items,
        "excluded_evidence": timeline_body["excluded_evidence"],
    }


def build_fusion_payload(timeline: dict) -> dict:
    """Return the read-only evidence payload shown to the text adjudicator."""
    _validate_timeline(timeline)
    return {
        "contract_version": timeline["contract_version"],
        "timeline_fingerprint": timeline["timeline_fingerprint"],
        "items": [
            {
                "timeline_id": item["timeline_id"],
                "start": item["start"],
                "end": item["end"],
                "classification": item["classification"],
                "review_required": item["review_required"],
                "candidates": [
                    {
                        key: candidate[key]
                        for key in (
                            "evidence_id", "source_channel", "source_view", "model_role",
                            "text", "avg_logprob", "no_speech_prob", "legacy_srt",
                        )
                    }
                    for candidate in item["candidates"]
                ],
            }
            for item in timeline["items"]
        ],
    }


def _validate_timeline(timeline: dict, max_duration: float = 15.0) -> None:
    if not isinstance(timeline, dict) or not isinstance(timeline.get("items"), list):
        raise ValueError("timeline items must be an array")
    previous_end = None
    seen_ids = set()
    for item in timeline.get("items", []):
        if not isinstance(item, dict):
            raise ValueError("timeline item must be an object")
        timeline_id = item.get("timeline_id")
        if not timeline_id or timeline_id in seen_ids:
            raise ValueError("timeline_id must be present and unique")
        seen_ids.add(timeline_id)
        start, end = item.get("start"), item.get("end")
        if (
            not isinstance(start, Real) or isinstance(start, bool)
            or not isinstance(end, Real) or isinstance(end, bool)
            or not math.isfinite(start) or not math.isfinite(end)
        ):
            raise ValueError(f"invalid timeline bounds for {timeline_id}")
        if start < 0 or start >= end or end - start > max_duration:
            raise ValueError(f"unsafe timeline bounds for {timeline_id}")
        if previous_end is not None and start < previous_end:
            raise ValueError(f"overlapping timeline item {timeline_id}")
        previous_end = end
        if item.get("classification") not in {"supported", "uncertain"}:
            raise ValueError(f"invalid classification for {timeline_id}")
        if not isinstance(item.get("review_required"), bool):
            raise ValueError(f"invalid review flag for {timeline_id}")
        evidence_ids = item.get("evidence_ids")
        candidates = item.get("candidates")
        if (
            not isinstance(evidence_ids, list) or not evidence_ids
            or any(not isinstance(value, str) or not value for value in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
            or not isinstance(candidates, list) or not candidates
            or any(not isinstance(candidate, dict) for candidate in candidates)
        ):
            raise ValueError(f"invalid evidence structure for {timeline_id}")
        candidate_ids = [candidate.get("evidence_id") for candidate in candidates]
        if (
            any(not isinstance(value, str) or not value for value in candidate_ids)
            or len(candidate_ids) != len(set(candidate_ids))
            or set(candidate_ids) != set(evidence_ids)
        ):
            raise ValueError(f"candidate/evidence mismatch for {timeline_id}")
        timing_source = item.get("timing_source")
        if not isinstance(timing_source, dict):
            raise ValueError(f"invalid timing source for {timeline_id}")
        anchor_id = timing_source.get("anchor_evidence_id")
        anchor_start = timing_source.get("anchor_start")
        anchor_end = timing_source.get("anchor_end")
        if anchor_id not in evidence_ids:
            raise ValueError(f"anchor evidence mismatch for {timeline_id}")
        if (
            not isinstance(anchor_start, Real) or isinstance(anchor_start, bool)
            or not isinstance(anchor_end, Real) or isinstance(anchor_end, bool)
            or not math.isfinite(anchor_start) or not math.isfinite(anchor_end)
            or anchor_start >= anchor_end
        ):
            raise ValueError(f"invalid anchor bounds for {timeline_id}")
        midpoint = (anchor_start + anchor_end) / 2
        if not (start <= midpoint <= end):
            raise ValueError(f"timeline does not contain anchor midpoint for {timeline_id}")


def _normalize_selection_text(value, timeline_id: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"empty text for {timeline_id}")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or "-->" in text:
        raise ValueError(f"invalid text for {timeline_id}")
    if any(unicodedata.category(char) == "Cc" and char != "\n" for char in text):
        raise ValueError(f"control character in text for {timeline_id}")
    if any(not line.strip() for line in text.split("\n")):
        raise ValueError(f"empty subtitle line for {timeline_id}")
    return text


def parse_fusion_selections(arguments: dict, timeline: dict) -> list[dict]:
    """Validate an LLM text decision without accepting any timing authority."""
    _validate_timeline(timeline)
    if not isinstance(arguments, dict) or set(arguments) != {"selections"}:
        raise ValueError("fusion response must contain only selections")
    selections = arguments["selections"]
    if not isinstance(selections, list):
        raise ValueError("selections must be an array")
    by_timeline = {item["timeline_id"]: item for item in timeline["items"]}
    parsed = {}
    for selection in selections:
        if not isinstance(selection, dict):
            raise ValueError("each selection must be an object")
        fields = set(selection)
        if fields & _FORBIDDEN_TIME_FIELDS:
            raise ValueError("LLM timing fields are forbidden")
        if not fields <= _SELECTION_FIELDS:
            raise ValueError("unknown selection field")
        timeline_id = selection.get("timeline_id")
        if timeline_id not in by_timeline:
            raise ValueError(f"unknown timeline_id: {timeline_id}")
        if timeline_id in parsed:
            raise ValueError(f"duplicate timeline_id: {timeline_id}")
        evidence_ids = selection.get("evidence_ids")
        if (
            not isinstance(evidence_ids, list)
            or not evidence_ids
            or any(not isinstance(value, str) or not value for value in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            raise ValueError(f"invalid evidence_ids for {timeline_id}")
        allowed_ids = set(by_timeline[timeline_id]["evidence_ids"])
        if not set(evidence_ids) <= allowed_ids:
            raise ValueError(f"cross-window or unknown evidence_id for {timeline_id}")
        text = _normalize_selection_text(selection.get("text"), timeline_id)
        review_required = selection.get("review_required", False)
        note = selection.get("note", "")
        if not isinstance(review_required, bool) or not isinstance(note, str):
            raise ValueError(f"invalid review metadata for {timeline_id}")
        parsed[timeline_id] = {
            "timeline_id": timeline_id,
            "evidence_ids": evidence_ids,
            "text": text,
            "review_required": bool(
                by_timeline[timeline_id].get("review_required") or review_required
            ),
            "note": note.strip(),
        }
    missing = [timeline_id for timeline_id in by_timeline if timeline_id not in parsed]
    if missing:
        raise ValueError(f"missing timeline selections: {', '.join(missing)}")
    return [parsed[item["timeline_id"]] for item in timeline["items"]]


def _format_srt_timestamp(seconds: float) -> str:
    total_millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def render_timeline_srt(timeline: dict, selections: list[dict]) -> str:
    """Render SRT using only locally fixed timeline bounds."""
    validated = parse_fusion_selections({"selections": selections}, timeline)
    by_id = {selection["timeline_id"]: selection for selection in validated}
    blocks = []
    for index, item in enumerate(timeline["items"], start=1):
        text = by_id[item["timeline_id"]]["text"]
        blocks.append(
            f"{index}\n"
            f"{_format_srt_timestamp(item['start'])} --> "
            f"{_format_srt_timestamp(item['end'])}\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_text_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".timeline-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def validate_fusion_manifest(manifest: dict, *, require_complete: bool = True) -> None:
    required = {
        "schema_version", "artifact_type", "status", "contract_version",
        "timeline_fingerprint", "fusion_generation_fingerprint", "output_srt",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError("invalid fusion manifest fields")
    if manifest["schema_version"] != 1 or manifest["artifact_type"] != "fusion_manifest":
        raise ValueError("invalid fusion manifest identity")
    if manifest["status"] not in {"pending", "complete"}:
        raise ValueError("invalid fusion manifest status")
    if require_complete and manifest["status"] != "complete":
        raise ValueError("fusion manifest is not complete")
    for key in ("contract_version", "timeline_fingerprint", "fusion_generation_fingerprint"):
        if not isinstance(manifest[key], str) or not manifest[key]:
            raise ValueError(f"invalid fusion manifest {key}")
    output = manifest["output_srt"]
    if manifest["status"] == "pending":
        if output is not None:
            raise ValueError("pending fusion manifest cannot have output fingerprint")
        return
    if (
        not isinstance(output, dict)
        or set(output) != {"name", "size_bytes", "mtime_ns", "sha256"}
        or not isinstance(output["name"], str)
        or not isinstance(output["size_bytes"], int)
        or not isinstance(output["mtime_ns"], int)
        or not isinstance(output["sha256"], str)
    ):
        raise ValueError("invalid fusion output fingerprint")
