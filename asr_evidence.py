# SPDX-License-Identifier: GPL-3.0-or-later
"""Structured ASR evidence, deterministic alignment, and quarantine helpers.

This module deliberately has no faster-whisper or LLM dependency.  It can be
used both for fresh model output and for legacy SRT caches.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import tempfile
from difflib import SequenceMatcher


SCHEMA_VERSION = 1
MAX_ALIGNMENT_WINDOW_SECONDS = 15.0
ALLOWED_CHANNELS = {"main", "rescue"}
ALLOWED_MODEL_ROLES = {"v3", "turbo", "gal"}

MODEL_ROLES = {
    "large-v3": "v3",
    "large-v3-turbo": "turbo",
}

# These are high-precision templates observed in non-lexical/noise windows.
# They are only quarantined when a second model does not independently support
# the same phrase in the same time window.
DEFAULT_HALLUCINATION_TEMPLATES = (
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "チャンネル登録よろしくお願いします",
    "チャンネル登録をお願いします",
)

_SRT_TIMECODE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)
_NORMALIZE_RE = re.compile(r"[\s\u3000、。！？!?…・,.\-~～「」『』（）()\[\]]+")


def model_role(model_name: str) -> str:
    """Return a stable short role used in evidence IDs."""
    return MODEL_ROLES.get(model_name, re.sub(r"[^a-z0-9]+", "_", model_name.lower()).strip("_"))


def _identity_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not slug:
        raise ValueError(f"invalid empty identity component: {value!r}")
    return slug


def run_key(source_channel: str, role: str, source_view: str) -> str:
    if source_channel not in ALLOWED_CHANNELS:
        raise ValueError(f"unsupported ASR source channel: {source_channel!r}")
    if role not in ALLOWED_MODEL_ROLES:
        raise ValueError(f"unsupported ASR model role: {role!r}")
    return f"{source_channel}:{role}:{_identity_slug(source_view)}"


def _json_number(value):
    if value is None:
        return None
    try:
        number = float(value)
        return round(number, 6) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _segment_words(segment) -> list[dict]:
    words = []
    for word in getattr(segment, "words", None) or []:
        words.append({
            "start": _json_number(getattr(word, "start", None)),
            "end": _json_number(getattr(word, "end", None)),
            "text": str(getattr(word, "word", "")).strip(),
            "probability": _json_number(getattr(word, "probability", None)),
        })
    return words


def candidates_from_segments(
    segments,
    model_name: str,
    source_view: str = "raw",
    source_channel: str = "main",
) -> list[dict]:
    """Convert faster-whisper-like segment objects to JSON-safe evidence."""
    role = model_role(model_name)
    candidate_run_key = run_key(source_channel, role, source_view)
    candidates = []
    for ordinal, segment in enumerate(segments, start=1):
        text = str(getattr(segment, "text", "")).strip()
        if not text:
            continue
        candidates.append({
            "evidence_id": f"{candidate_run_key}:{ordinal:06d}",
            "run_key": candidate_run_key,
            "model": model_name,
            "model_role": role,
            "source_channel": source_channel,
            "source_view": source_view,
            "start": _json_number(getattr(segment, "start", 0.0)),
            "end": _json_number(getattr(segment, "end", 0.0)),
            "text": text,
            "avg_logprob": _json_number(getattr(segment, "avg_logprob", None)),
            "no_speech_prob": _json_number(getattr(segment, "no_speech_prob", None)),
            "compression_ratio": _json_number(getattr(segment, "compression_ratio", None)),
            "words": _segment_words(segment),
            "legacy_srt": False,
        })
    return candidates


def _timestamp_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def file_fingerprint(path: str) -> dict:
    """Return a stable file identity; intended for small SRT/JSON artifacts."""
    stat = os.stat(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "name": os.path.basename(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def candidates_from_srt(
    srt_path: str,
    model_name: str,
    source_view: str = "legacy_srt",
    source_channel: str = "main",
) -> list[dict]:
    """Create degraded evidence from an existing SRT without rerunning ASR."""
    with open(srt_path, "r", encoding="utf-8-sig") as handle:
        content = handle.read()

    role = model_role(model_name)
    candidate_run_key = run_key(source_channel, role, source_view)
    candidates = []
    blocks = re.split(r"\r?\n\s*\r?\n", content.strip()) if content.strip() else []
    for ordinal, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_index = next((i for i, line in enumerate(lines) if _SRT_TIMECODE_RE.fullmatch(line)), None)
        if time_index is None:
            continue
        match = _SRT_TIMECODE_RE.fullmatch(lines[time_index])
        text = " ".join(lines[time_index + 1:]).strip()
        if not text:
            continue
        candidates.append({
            "evidence_id": f"{candidate_run_key}:{ordinal:06d}",
            "run_key": candidate_run_key,
            "model": model_name,
            "model_role": role,
            "source_channel": source_channel,
            "source_view": source_view,
            "start": round(_timestamp_seconds(match.group(1)), 3),
            "end": round(_timestamp_seconds(match.group(2)), 3),
            "text": text,
            "avg_logprob": None,
            "no_speech_prob": None,
            "compression_ratio": None,
            "words": [],
            "legacy_srt": True,
        })
    return candidates


def new_evidence_document(audio_path: str) -> dict:
    stat = os.stat(audio_path) if os.path.exists(audio_path) else None
    content_sha256 = None
    if stat is not None:
        digest = hashlib.sha256()
        with open(audio_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        content_sha256 = digest.hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "asr_candidates",
        "status": "complete",
        "source": {
            "audio_name": os.path.basename(audio_path),
            "size_bytes": stat.st_size if stat else None,
            "mtime_ns": stat.st_mtime_ns if stat else None,
            "sha256": content_sha256,
            "path_fingerprint": hashlib.sha256(
                os.path.normcase(os.path.abspath(audio_path)).encode("utf-8")
            ).hexdigest(),
        },
        "runs": {},
        "candidates": [],
    }


def validate_evidence_document(document: dict) -> None:
    """Validate the schema boundary before evidence participates in auditing."""
    if not isinstance(document, dict):
        raise ValueError("ASR evidence must be a JSON object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported ASR evidence schema: {document.get('schema_version')!r}")
    if document.get("artifact_type") != "asr_candidates":
        raise ValueError(f"unexpected ASR artifact type: {document.get('artifact_type')!r}")
    if document.get("status") != "complete":
        raise ValueError(f"incomplete ASR evidence artifact: {document.get('status')!r}")

    source = document.get("source")
    if not isinstance(source, dict):
        raise ValueError("ASR evidence source must be an object")
    if not isinstance(source.get("audio_name"), str):
        raise ValueError("ASR evidence source.audio_name must be a string")
    for field in ("size_bytes", "mtime_ns"):
        if source.get(field) is not None and not isinstance(source[field], int):
            raise ValueError(f"ASR evidence source.{field} must be an integer or null")
    source_sha256 = source.get("sha256")
    if source_sha256 is not None and (
        not isinstance(source_sha256, str) or len(source_sha256) != 64
    ):
        raise ValueError("ASR evidence source.sha256 must be a SHA-256 string or null")
    if not isinstance(source.get("path_fingerprint"), str) or not source["path_fingerprint"]:
        raise ValueError("ASR evidence source.path_fingerprint is missing")

    candidates = document.get("candidates")
    runs = document.get("runs")
    if not isinstance(candidates, list) or not isinstance(runs, dict):
        raise ValueError("invalid ASR evidence candidates/runs container")

    seen_ids = set()
    counts_by_run = {}
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"candidate {index} must be an object")
        evidence_id = candidate.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError(f"candidate {index} has no evidence_id")
        if evidence_id in seen_ids:
            raise ValueError(f"duplicate evidence_id: {evidence_id}")
        seen_ids.add(evidence_id)
        channel = candidate.get("source_channel")
        role = candidate.get("model_role")
        view = candidate.get("source_view")
        if channel not in ALLOWED_CHANNELS or role not in ALLOWED_MODEL_ROLES:
            raise ValueError(f"candidate {evidence_id} has invalid channel/role")
        expected_run_key = run_key(channel, role, view)
        if candidate.get("run_key") != expected_run_key:
            raise ValueError(f"candidate {evidence_id} has inconsistent run_key")
        if not evidence_id.startswith(expected_run_key + ":"):
            raise ValueError(f"candidate {evidence_id} has inconsistent identity prefix")
        start, end = candidate.get("start"), candidate.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f"candidate {evidence_id} has non-numeric timestamps")
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError(f"candidate {evidence_id} has invalid timestamps")
        if not isinstance(candidate.get("text"), str) or not candidate["text"]:
            raise ValueError(f"candidate {evidence_id} has empty text")
        if not isinstance(candidate.get("legacy_srt"), bool):
            raise ValueError(f"candidate {evidence_id} has invalid legacy_srt flag")
        counts_by_run[expected_run_key] = counts_by_run.get(expected_run_key, 0) + 1

    for key, run in runs.items():
        if not isinstance(run, dict) or run.get("run_key") != key:
            raise ValueError(f"invalid ASR run record: {key!r}")
        if run.get("source_channel") not in ALLOWED_CHANNELS:
            raise ValueError(f"run {key!r} has invalid channel")
        if run.get("model_role") not in ALLOWED_MODEL_ROLES:
            raise ValueError(f"run {key!r} has invalid role")
        if not isinstance(run.get("source_view"), str) or not run["source_view"]:
            raise ValueError(f"run {key!r} has invalid view")
        if run.get("candidate_count") != counts_by_run.get(key, 0):
            raise ValueError(f"run {key!r} candidate_count mismatch")

    unknown_runs = set(counts_by_run) - set(runs)
    if unknown_runs:
        raise ValueError(f"candidates reference missing runs: {sorted(unknown_runs)!r}")


def load_evidence(path: str, audio_path: str = "") -> dict:
    if not os.path.exists(path):
        document = new_evidence_document(audio_path)
        validate_evidence_document(document)
        return document
    with open(path, "r", encoding="utf-8-sig") as handle:
        document = json.load(handle)
    validate_evidence_document(document)
    return document


def write_json_atomic(path: str, payload: dict) -> None:
    """Write a JSON artifact without exposing a half-written cache."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".asr-evidence-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def canonical_fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_audit_manifest(
    generation_id: str,
    evidence_fingerprint: str,
    artifact_files: dict[str, str],
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "asr_audit_manifest",
        "status": "complete",
        "generation_id": generation_id,
        "evidence_fingerprint": evidence_fingerprint,
        "artifacts": artifact_files,
    }


def upsert_model_evidence(
    path: str,
    audio_path: str,
    model_name: str,
    candidates: list[dict],
    transcribe_config: dict,
    actual_audio: str,
    source_channel: str = "main",
    source_view: str = "raw",
    source_relation: str = "matched",
    source_srt: dict | None = None,
    emitted_srt: dict | None = None,
) -> dict:
    document = load_evidence(path, audio_path)
    current_source = new_evidence_document(audio_path)["source"]
    if document.get("source") != current_source:
        # Never combine candidates produced from different source revisions.
        document = new_evidence_document(audio_path)
    document["status"] = "complete"
    role = model_role(model_name)
    candidate_run_key = run_key(source_channel, role, source_view)
    if any(item.get("run_key") != candidate_run_key for item in candidates):
        raise ValueError(f"candidate run_key does not match {candidate_run_key!r}")

    replaced_run_keys = {candidate_run_key}
    # A fresh main-channel run supersedes a legacy adapter for the same model.
    if source_channel == "main" and source_view != "legacy_srt":
        replaced_run_keys.update(
            key for key, run in document["runs"].items()
            if run.get("source_channel") == "main" and run.get("model_role") == role
        )
    document["candidates"] = [
        item for item in document["candidates"] if item.get("run_key") not in replaced_run_keys
    ] + candidates
    for key in replaced_run_keys:
        document["runs"].pop(key, None)
    document["candidates"].sort(key=lambda item: (item.get("start", 0.0), item.get("end", 0.0), item["evidence_id"]))
    actual_stat = os.stat(actual_audio) if os.path.exists(actual_audio) else None
    document["runs"][candidate_run_key] = {
        "run_key": candidate_run_key,
        "model": model_name,
        "model_role": role,
        "source_channel": source_channel,
        "source_view": source_view,
        "source_relation": source_relation,
        "source_srt": source_srt,
        "emitted_srt": emitted_srt,
        "actual_audio_name": os.path.basename(actual_audio),
        "actual_audio_size_bytes": actual_stat.st_size if actual_stat else None,
        "actual_audio_mtime_ns": actual_stat.st_mtime_ns if actual_stat else None,
        "config": transcribe_config,
        "config_fingerprint": hashlib.sha256(
            json.dumps(transcribe_config, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "candidate_count": len(candidates),
        "legacy_srt": source_view == "legacy_srt",
    }
    validate_evidence_document(document)
    write_json_atomic(path, document)
    return document


def ensure_legacy_model_evidence(
    path: str,
    audio_path: str,
    model_name: str,
    srt_path: str,
) -> dict:
    """Backfill one missing model role from SRT while preserving fresh evidence."""
    document = load_evidence(path, audio_path)
    role = model_role(model_name)
    current_source = new_evidence_document(audio_path)["source"]
    legacy_run_key = run_key("main", role, "legacy_srt")
    current_srt_fingerprint = file_fingerprint(srt_path)
    native_run = next((
        run for run in document["runs"].values()
        if run.get("source_channel") == "main"
        and run.get("model_role") == role
        and not run.get("legacy_srt")
        and run.get("source_relation") == "matched"
        and run.get("emitted_srt") == current_srt_fingerprint
    ), None)
    if document.get("source") == current_source and native_run:
        return document
    previous_run = document["runs"].get(legacy_run_key)
    if (
        document.get("source") == current_source
        and previous_run
        and previous_run.get("source_srt") == current_srt_fingerprint
    ):
        return document
    source_changed = document.get("source") != current_source
    candidates = candidates_from_srt(
        srt_path, model_name, source_view="legacy_srt", source_channel="main"
    )
    return upsert_model_evidence(
        path=path,
        audio_path=audio_path,
        model_name=model_name,
        candidates=candidates,
        transcribe_config={
            "source": "legacy_srt",
            "native_probabilities": False,
        },
        actual_audio=audio_path,
        source_channel="main",
        source_view="legacy_srt",
        source_relation="unknown_after_audio_change" if source_changed else "matched",
        source_srt=current_srt_fingerprint,
    )


def normalize_text(text: str) -> str:
    return _NORMALIZE_RE.sub("", text).lower()


def _time_relation(left: dict, right: dict, max_gap: float) -> bool:
    return left["start"] <= right["end"] + max_gap and right["start"] <= left["end"] + max_gap


def align_candidates(
    candidates: list[dict],
    max_gap: float = 0.8,
    max_duration: float = MAX_ALIGNMENT_WINDOW_SECONDS,
) -> list[dict]:
    """Build deterministic cross-model windows without chaining one model.

    Two adjacent segments from the same model must not merge merely because
    their timestamps are close.  They join only when linked by overlapping
    evidence from a different model.
    """
    if max_duration <= 0:
        raise ValueError("max_duration must be positive")
    ordered = sorted(candidates, key=lambda item: (item["start"], item["end"], item["evidence_id"]))
    parents = list(range(len(ordered)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_index, left in enumerate(ordered):
        for right_index in range(left_index + 1, len(ordered)):
            right = ordered[right_index]
            if right["start"] > left["end"] + max_gap:
                break
            if left["model_role"] != right["model_role"] and _time_relation(left, right, max_gap):
                union(left_index, right_index)

    grouped = {}
    for index, candidate in enumerate(ordered):
        grouped.setdefault(find(index), []).append(candidate)

    groups = sorted(
        grouped.values(),
        key=lambda items: (min(item["start"] for item in items), min(item["evidence_id"] for item in items)),
    )
    windows = []
    for items in groups:
        group_start = min(item["start"] for item in items)
        group_end = max(item["end"] for item in items)
        if group_end == group_start:
            slices = [(group_start, group_end)]
        else:
            slices = []
            cursor = group_start
            while cursor < group_end:
                hard_end = min(cursor + max_duration, group_end)
                natural_ends = [
                    item["end"] for item in items
                    if cursor < item["end"] <= hard_end
                ]
                boundary = max(natural_ends) if natural_ends else hard_end
                if boundary <= cursor:
                    boundary = hard_end
                slices.append((cursor, boundary))
                cursor = boundary

        for slice_start, slice_end in slices:
            evidence_ids = [
                item["evidence_id"] for item in items
                if (
                    item["start"] <= slice_end and item["end"] >= slice_start
                    and not (item["end"] == slice_start and item["start"] < slice_start)
                )
            ]
            windows.append({
                "window_id": f"W{len(windows) + 1:06d}",
                "start": slice_start,
                "end": slice_end,
                "evidence_ids": evidence_ids,
                "split_from_long_group": group_end - group_start > max_duration,
            })
    return windows


def _is_template(text: str, templates: tuple[str, ...]) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(template) == normalized for template in templates)


def _texts_support_each_other(left: str, right: str) -> bool:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= 2
    return SequenceMatcher(None, a, b).ratio() >= 0.62


def classify_windows(
    candidates: list[dict],
    windows: list[dict],
    templates: tuple[str, ...] = DEFAULT_HALLUCINATION_TEMPLATES,
) -> dict:
    """Classify candidates/windows as supported, uncertain, or hallucination.

    Low energy or a VAD miss is never sufficient for deletion.  Automatic
    quarantine is intentionally restricted to known templates without
    independent cross-model support.
    """
    by_id = {item["evidence_id"]: item for item in candidates}
    candidate_decisions = {}
    window_decisions = []

    supported_pairs = {
        item["evidence_id"]
        for item in candidates
        for other in candidates
        if item["model_role"] != other["model_role"]
        and _time_relation(item, other, 0.8)
        and _texts_support_each_other(item["text"], other["text"])
    }
    turbo_templates_with_v3_support = {
        item["evidence_id"]
        for item in candidates
        for other in candidates
        if item["model_role"] == "turbo"
        and _is_template(item["text"], templates)
        and other["model_role"] == "v3"
        and (
            other.get("no_speech_prob") is None
            or other["no_speech_prob"] < 0.6
        )
        and _time_relation(item, other, 0.8)
        and _texts_support_each_other(item["text"], other["text"])
    }

    for item in candidates:
        reasons = []
        is_turbo_template = item["model_role"] == "turbo" and _is_template(item["text"], templates)
        if is_turbo_template and item["evidence_id"] not in turbo_templates_with_v3_support:
            label = "hallucination"
            reasons.append("known_template_without_credible_v3_support")
        elif item["evidence_id"] in supported_pairs:
            label = "supported"
            reasons.append("cross_model_text_support")
        else:
            label = "uncertain"
            reasons.append("single_model_or_divergent_text")
            if item.get("no_speech_prob") is not None and item["no_speech_prob"] >= 0.75:
                reasons.append("high_no_speech_probability")
            if item.get("avg_logprob") is not None and item["avg_logprob"] <= -1.2:
                reasons.append("low_average_log_probability")
            if item.get("legacy_srt"):
                reasons.append("legacy_srt_without_native_probabilities")
        candidate_decisions[item["evidence_id"]] = {"label": label, "reasons": reasons}

    for window in windows:
        items = [by_id[evidence_id] for evidence_id in window["evidence_ids"]]
        roles = {item["model_role"] for item in items}
        labels = [candidate_decisions[item["evidence_id"]]["label"] for item in items]

        if "supported" in labels:
            window_label = "supported"
        elif labels and all(label == "hallucination" for label in labels):
            window_label = "hallucination"
        else:
            window_label = "uncertain"
        window_decisions.append({
            **window,
            "label": window_label,
            "model_roles": sorted(roles),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "asr_decisions",
        "status": "complete",
        "candidate_decisions": candidate_decisions,
        "windows": window_decisions,
        "quarantined_evidence_ids": sorted(
            evidence_id for evidence_id, decision in candidate_decisions.items()
            if decision["label"] == "hallucination"
        ),
    }


def build_alignment_artifact(candidates: list[dict], windows: list[dict]) -> dict:
    by_id = {item["evidence_id"]: item for item in candidates}
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "asr_alignment",
        "status": "complete",
        "strategy": "time_overlap_or_gap",
        "windows": [
            {
                **window,
                "models": sorted({by_id[item]["model_role"] for item in window["evidence_ids"]}),
            }
            for window in windows
        ],
    }


def build_windows_artifact(candidates: list[dict], windows: list[dict]) -> dict:
    by_id = {item["evidence_id"]: item for item in candidates}
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "asr_windows",
        "status": "complete",
        "windows": [
            {
                **window,
                "candidates": [
                    {
                        "evidence_id": evidence_id,
                        "model_role": by_id[evidence_id]["model_role"],
                        "text": by_id[evidence_id]["text"],
                    }
                    for evidence_id in window["evidence_ids"]
                ],
            }
            for window in windows
        ],
    }


def _format_srt_timestamp(seconds: float) -> str:
    total_millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def filtered_srt_for_role(
    document: dict,
    decisions: dict,
    model_role_name: str,
    original_srt: str,
    source_channel: str = "main",
) -> tuple[str, list[dict]]:
    """Remove quarantined evidence for one role while preserving audit records.

    If this role has no quarantined candidates, the original string is returned
    byte-for-byte so the compatibility path does not rewrite benign inputs.
    """
    validate_evidence_document(document)
    quarantined = set(decisions.get("quarantined_evidence_ids", []))
    candidates = [
        item for item in document["candidates"]
        if item["source_channel"] == source_channel
        and item["model_role"] == model_role_name
    ]
    removed = [item for item in candidates if item["evidence_id"] in quarantined]
    if not removed:
        return original_srt, []

    kept = sorted(
        (item for item in candidates if item["evidence_id"] not in quarantined),
        key=lambda item: (item["start"], item["end"], item["evidence_id"]),
    )
    blocks = []
    for index, item in enumerate(kept, start=1):
        blocks.append(
            f"{index}\n"
            f"{_format_srt_timestamp(item['start'])} --> {_format_srt_timestamp(item['end'])}\n"
            f"{item['text']}"
        )
    rendered = "\n\n".join(blocks)
    return (rendered + "\n" if rendered else ""), removed
