# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic rescue-window planning for low-recall ASR regions."""

from __future__ import annotations

import math
import os
import re
import copy

from asr_evidence import (
    DEFAULT_HALLUCINATION_TEMPLATES,
    candidates_from_segments,
    canonical_fingerprint,
    load_evidence,
    model_role,
    normalize_text,
    run_key,
    upsert_model_evidence,
)


SCHEMA_VERSION = 1
_TEXT_NORMALIZE_RE = re.compile(r"[\s\u3000、。！？!?…・,.\-~～「」『』（）()\[\]]+")


def _merge_targets(targets: list[dict]) -> list[dict]:
    merged = []
    for target in sorted(targets, key=lambda item: (item["start"], item["end"])):
        if not merged or target["start"] > merged[-1]["end"]:
            merged.append({
                "start": target["start"],
                "end": target["end"],
                "reasons": set(target["reasons"]),
            })
            continue
        merged[-1]["end"] = max(merged[-1]["end"], target["end"])
        merged[-1]["reasons"].update(target["reasons"])
    return merged


def _merge_coverage(candidates: list[dict], duration: float) -> list[tuple[float, float]]:
    intervals = sorted(
        (
            max(0.0, float(item["start"])),
            min(duration, float(item["end"])),
        )
        for item in candidates
        if item["end"] > item["start"]
    )
    merged = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _tile_target(start: float, end: float, window_seconds: float, overlap_seconds: float):
    if end <= start:
        return []
    if end - start <= window_seconds:
        return [(start, end)]
    step = window_seconds - overlap_seconds
    windows = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + window_seconds, end)
        windows.append((cursor, window_end))
        if window_end >= end:
            break
        cursor += step
    return windows


_RESCUE_REASON_PRIORITY = {
    "quarantined_candidate_region": 0,
    "main_channel_gap": 1,
    "uncertain_candidate": 2,
    "model_disagreement_or_single_model": 2,
    "abnormally_long_segment": 3,
}


def _is_acoustically_suspicious(
    candidate: dict,
    *,
    no_speech_threshold: float,
    logprob_threshold: float,
) -> bool:
    no_speech = candidate.get("no_speech_prob")
    logprob = candidate.get("avg_logprob")
    return (
        isinstance(no_speech, (int, float))
        and not isinstance(no_speech, bool)
        and math.isfinite(no_speech)
        and no_speech >= no_speech_threshold
    ) or (
        isinstance(logprob, (int, float))
        and not isinstance(logprob, bool)
        and math.isfinite(logprob)
        and logprob <= logprob_threshold
    )


def _spread_sample(items: list[dict], count: int) -> list[dict]:
    """Select windows across the whole time span instead of only its prefix."""
    if count >= len(items):
        return list(items)
    if count <= 0:
        return []
    if count == 1:
        indexes = {len(items) // 2}
    else:
        indexes = {
            round(position * (len(items) - 1) / (count - 1))
            for position in range(count)
        }
    if len(indexes) < count:
        indexes.update(index for index in range(len(items)) if index not in indexes)
    return [items[index] for index in sorted(indexes)[:count]]


def _apply_rescue_budget(
    windows: list[dict],
    audio_duration: float,
    *,
    window_seconds: float,
    max_windows: int,
    max_total_seconds: float,
    max_audio_ratio: float,
) -> tuple[list[dict], dict]:
    allowed_duration = min(
        max_total_seconds,
        max(min(audio_duration, window_seconds), audio_duration * max_audio_ratio),
    )
    requested_duration = sum(item["end"] - item["start"] for item in windows)
    selected = []
    remaining_duration = allowed_duration
    remaining_count = max_windows
    priorities = sorted({
        min(_RESCUE_REASON_PRIORITY.get(reason, 99) for reason in item["reasons"])
        for item in windows
    })
    for priority in priorities:
        group = sorted((
            item for item in windows
            if min(_RESCUE_REASON_PRIORITY.get(reason, 99) for reason in item["reasons"])
            == priority
        ), key=lambda item: (item["start"], item["end"]))
        if not group or remaining_count <= 0 or remaining_duration <= 0:
            continue
        count = min(len(group), remaining_count)
        chosen = _spread_sample(group, count)
        while chosen and sum(item["end"] - item["start"] for item in chosen) > remaining_duration:
            count -= 1
            chosen = _spread_sample(group, count)
        selected.extend(chosen)
        remaining_count -= len(chosen)
        remaining_duration -= sum(item["end"] - item["start"] for item in chosen)

    selected.sort(key=lambda item: (item["start"], item["end"]))
    for index, item in enumerate(selected, start=1):
        item["window_id"] = f"R{index:06d}"
    selected_duration = sum(item["end"] - item["start"] for item in selected)
    return selected, {
        "triggered": len(selected) < len(windows),
        "requested_window_count": len(windows),
        "selected_window_count": len(selected),
        "dropped_window_count": len(windows) - len(selected),
        "requested_inference_seconds": round(requested_duration, 3),
        "selected_inference_seconds": round(selected_duration, 3),
        "allowed_inference_seconds": round(allowed_duration, 3),
        "max_windows": max_windows,
        "max_total_seconds": max_total_seconds,
        "max_audio_ratio": max_audio_ratio,
    }


def finalize_rescue_candidates(
    batches: list[tuple[dict, list[dict]]],
    *,
    run_prefix: str,
) -> list[dict]:
    """Offset window-local candidates, deduplicate overlap, and assign IDs."""
    expanded = []
    for window, candidates in batches:
        offset = float(window["start"])
        for candidate in candidates:
            item = copy.deepcopy(candidate)
            item["start"] = round(item["start"] + offset, 6)
            item["end"] = round(item["end"] + offset, 6)
            item["rescue_window_ids"] = [window["window_id"]]
            item["run_key"] = run_prefix
            for word in item.get("words", []):
                if word.get("start") is not None:
                    word["start"] = round(word["start"] + offset, 6)
                if word.get("end") is not None:
                    word["end"] = round(word["end"] + offset, 6)
            expanded.append(item)

    deduplicated = []
    for item in sorted(expanded, key=lambda value: (value["start"], value["end"], value["text"])):
        normalized = _TEXT_NORMALIZE_RE.sub("", item["text"]).lower()
        duplicate = next((
            existing for existing in reversed(deduplicated)
            if _TEXT_NORMALIZE_RE.sub("", existing["text"]).lower() == normalized
            and item["start"] <= existing["end"] + 0.4
            and existing["start"] <= item["end"] + 0.4
        ), None)
        if duplicate is None:
            deduplicated.append(item)
            continue
        duplicate["start"] = min(duplicate["start"], item["start"])
        duplicate["end"] = max(duplicate["end"], item["end"])
        duplicate["rescue_window_ids"] = sorted(set(
            duplicate["rescue_window_ids"] + item["rescue_window_ids"]
        ))
        current_score = duplicate.get("avg_logprob")
        new_score = item.get("avg_logprob")
        if new_score is not None and (current_score is None or new_score > current_score):
            preserved_start = duplicate["start"]
            preserved_end = duplicate["end"]
            preserved_windows = duplicate["rescue_window_ids"]
            duplicate.update(item)
            duplicate["start"] = preserved_start
            duplicate["end"] = preserved_end
            duplicate["rescue_window_ids"] = preserved_windows

    for index, item in enumerate(deduplicated, start=1):
        item["evidence_id"] = f"{run_prefix}:{index:06d}"
    return deduplicated


def rescue_transcribe_kwargs() -> dict:
    return {
        "language": "ja",
        "beam_size": 5,
        "vad_filter": False,
        "condition_on_previous_text": False,
        "no_repeat_ngram_size": 5,
        "repetition_penalty": 1.5,
        "temperature": 0.0,
        "compression_ratio_threshold": 2.0,
        "log_prob_threshold": -1.0,
        "word_timestamps": True,
    }


def select_turbo_target_window_ids(v3_candidates: list[dict]) -> tuple[set[str], list[str]]:
    """Select credible V3 hits for Turbo adjudication without deleting evidence."""
    by_text = {}
    for item in v3_candidates:
        normalized = normalize_text(item["text"])
        if normalized:
            by_text.setdefault(normalized, []).append(item)

    distant_high_no_speech_texts = set()
    for normalized, items in by_text.items():
        starts = [item["start"] for item in items]
        probabilities = [item.get("no_speech_prob") for item in items]
        if (
            len(items) >= 2
            and max(starts) - min(starts) >= 20.0
            and all(value is not None and value >= 0.6 for value in probabilities)
        ):
            distant_high_no_speech_texts.add(normalized)

    template_texts = {normalize_text(value) for value in DEFAULT_HALLUCINATION_TEMPLATES}
    selected_window_ids = set()
    suppressed_ids = []
    for item in v3_candidates:
        normalized = normalize_text(item["text"])
        no_speech_prob = item.get("no_speech_prob")
        high_no_speech = no_speech_prob is not None and no_speech_prob >= 0.6
        suppress = high_no_speech and (
            normalized in template_texts or normalized in distant_high_no_speech_texts
        )
        if suppress:
            suppressed_ids.append(item["evidence_id"])
            continue
        selected_window_ids.update(item.get("rescue_window_ids", []))
    return selected_window_ids, suppressed_ids


def build_turbo_target_windows(
    v3_candidates: list[dict],
    audio_duration: float,
    *,
    padding_seconds: float = 0.75,
    max_window_seconds: float = 8.0,
    max_total_seconds: float = 120.0,
    max_audio_ratio: float = 0.35,
    max_windows: int = 24,
) -> tuple[list[dict], list[str], dict]:
    """Build narrow Turbo adjudication windows with a hard coverage guard."""
    selected_window_ids, suppressed_ids = select_turbo_target_window_ids(v3_candidates)
    selected = [
        item for item in v3_candidates
        if any(value in selected_window_ids for value in item.get("rescue_window_ids", []))
        and item["evidence_id"] not in suppressed_ids
    ]
    targets = []
    for item in selected:
        start = max(0.0, float(item["start"]) - padding_seconds)
        end = min(float(audio_duration), float(item["end"]) + padding_seconds)
        if end <= start:
            continue
        targets.append({
            "start": start,
            "end": end,
            "reasons": {"credible_v3_candidate"},
        })

    merged_targets = _merge_targets(targets)
    requested = []
    overlap = min(0.5, max_window_seconds / 4)
    for target in merged_targets:
        for start, end in _tile_target(
            target["start"], target["end"], max_window_seconds, overlap
        ):
            evidence_ids = sorted(
                item["evidence_id"] for item in selected
                if item["start"] <= end and item["end"] >= start
            )
            parent_ids = sorted({
                parent_id
                for item in selected
                if item["evidence_id"] in evidence_ids
                for parent_id in item.get("rescue_window_ids", [])
            })
            requested.append({
                "window_id": f"T{len(requested) + 1:06d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "reasons": ["credible_v3_candidate"],
                "source_evidence_ids": evidence_ids,
                "parent_rescue_window_ids": parent_ids,
            })

    unique_coverage_duration = sum(
        target["end"] - target["start"] for target in merged_targets
    )
    inference_duration = sum(
        window["end"] - window["start"] for window in requested
    )
    allowed_duration = min(max_total_seconds, float(audio_duration) * max_audio_ratio)
    guard_reasons = []
    if len(requested) > max_windows:
        guard_reasons.append("window_count_limit")
    if inference_duration > allowed_duration:
        guard_reasons.append("coverage_duration_limit")
    if unique_coverage_duration / float(audio_duration) >= 0.9:
        guard_reasons.append("near_full_audio_coverage")
    guard = {
        "triggered": bool(guard_reasons),
        "reasons": guard_reasons,
        "requested_window_count": len(requested),
        "requested_inference_seconds": round(inference_duration, 3),
        "unique_coverage_seconds": round(unique_coverage_duration, 3),
        "allowed_duration_seconds": round(allowed_duration, 3),
        "max_windows": max_windows,
        "max_audio_ratio": max_audio_ratio,
        "max_total_seconds": max_total_seconds,
    }
    return ([] if guard_reasons else requested), suppressed_ids, guard


def _transcribe_windows(model_name, model, samples, sample_rate, windows, source_view, log):
    kwargs = rescue_transcribe_kwargs()
    batches = []
    for index, window in enumerate(windows, start=1):
        start_sample = max(0, int(round(window["start"] * sample_rate)))
        end_sample = min(len(samples), int(round(window["end"] * sample_rate)))
        if end_sample <= start_sample:
            continue
        log(
            f"    [scan] {model_name} 救援 {index}/{len(windows)} "
            f"[{window['start']:.2f}s-{window['end']:.2f}s]"
        )
        segments, _ = model.transcribe(samples[start_sample:end_sample], **kwargs)
        local_candidates = candidates_from_segments(
            list(segments),
            model_name,
            source_view=source_view,
            source_channel="rescue",
        )
        batches.append((window, local_candidates))
    prefix = run_key("rescue", model_role(model_name), source_view)
    return finalize_rescue_candidates(batches, run_prefix=prefix)


def run_rescue_channel(
    *,
    audio_path: str,
    actual_audio: str,
    evidence_path: str,
    rescue_artifact: dict,
    get_model,
    log=print,
) -> bool:
    """Run V3 no-VAD scan, then Turbo only where V3 found candidates."""
    if not rescue_artifact or not rescue_artifact.get("windows"):
        return False

    planner_fingerprint = canonical_fingerprint({
        "audio_duration": rescue_artifact["audio_duration"],
        "config": rescue_artifact["config"],
        "windows": rescue_artifact["windows"],
    })
    common_config = {
        **rescue_transcribe_kwargs(),
        "planner_fingerprint": planner_fingerprint,
        "window_count": len(rescue_artifact["windows"]),
        "initial_prompt": None,
        "hotwords": None,
    }
    document = load_evidence(evidence_path, audio_path)
    actual_stat = os.stat(actual_audio)
    actual_audio_identity = canonical_fingerprint({
        "resolved_path": os.path.normcase(os.path.realpath(actual_audio)),
        "name": os.path.basename(actual_audio),
        "size_bytes": actual_stat.st_size,
        "mtime_ns": actual_stat.st_mtime_ns,
    })
    common_config["actual_audio_identity"] = actual_audio_identity

    def cache_matches(run, config):
        return (
            run
            and run.get("config") == config
            and run.get("actual_audio_size_bytes") == actual_stat.st_size
            and run.get("actual_audio_mtime_ns") == actual_stat.st_mtime_ns
            and run.get("config", {}).get("actual_audio_identity") == actual_audio_identity
        )

    samples = None
    sample_rate = None

    def ensure_samples():
        nonlocal samples, sample_rate
        if samples is None:
            import librosa
            samples, sample_rate = librosa.load(actual_audio, sr=16000, mono=True)
        return samples, sample_rate

    v3_key = run_key("rescue", "v3", "no_vad_scan")
    v3_run = document["runs"].get(v3_key)
    if cache_matches(v3_run, common_config):
        log("    [cache] 复用 large-v3 救援证据")
        v3_candidates = [
            item for item in document["candidates"] if item.get("run_key") == v3_key
        ]
    else:
        samples, sample_rate = ensure_samples()
        v3_candidates = _transcribe_windows(
            "large-v3",
            get_model("large-v3"),
            samples,
            sample_rate,
            rescue_artifact["windows"],
            "no_vad_scan",
            log,
        )
        document = upsert_model_evidence(
            path=evidence_path,
            audio_path=audio_path,
            model_name="large-v3",
            candidates=v3_candidates,
            transcribe_config=common_config,
            actual_audio=actual_audio,
            source_channel="rescue",
            source_view="no_vad_scan",
        )

    turbo_windows, suppressed_v3_ids, coverage_guard = build_turbo_target_windows(
        v3_candidates, rescue_artifact["audio_duration"]
    )
    turbo_config = {
        **common_config,
        "policy": "narrow_windows_around_credible_v3_candidates",
        "target_windows": turbo_windows,
        "suppressed_v3_candidate_ids": suppressed_v3_ids,
        "coverage_guard": coverage_guard,
    }
    turbo_key = run_key("rescue", "turbo", "no_vad_targeted")
    turbo_run = document["runs"].get(turbo_key)
    if cache_matches(turbo_run, turbo_config):
        log("    [cache] 复用 Turbo 定向救援证据")
    else:
        if turbo_windows:
            samples, sample_rate = ensure_samples()
            turbo_candidates = _transcribe_windows(
                "large-v3-turbo",
                get_model("large-v3-turbo"),
                samples,
                sample_rate,
                turbo_windows,
                "no_vad_targeted",
                log,
            )
        else:
            turbo_candidates = []
        upsert_model_evidence(
            path=evidence_path,
            audio_path=audio_path,
            model_name="large-v3-turbo",
            candidates=turbo_candidates,
            transcribe_config=turbo_config,
            actual_audio=actual_audio,
            source_channel="rescue",
            source_view="no_vad_targeted",
        )
    if coverage_guard["triggered"]:
        log(
            "    [review] Turbo 覆盖保护已触发，跳过自动对照并保留 V3 候选供人工复核"
        )
    log(
        f"    [ok] 救援通道：V3 {len(v3_candidates)} 条候选，"
        f"Turbo 定向窗口 {len(turbo_windows)} 个"
    )
    return True


def generate_rescue_windows(
    candidates: list[dict],
    decisions: dict,
    audio_duration: float,
    *,
    window_seconds: float = 12.0,
    overlap_seconds: float = 2.0,
    minimum_gap_seconds: float = 1.5,
    context_padding_seconds: float = 0.5,
    long_segment_seconds: float | None = None,
    uncertain_no_speech_threshold: float = 0.5,
    uncertain_logprob_threshold: float = -0.8,
    max_windows: int = 24,
    max_total_seconds: float = 180.0,
    max_audio_ratio: float = 1.0,
) -> dict:
    """Plan bounded no-VAD scan windows from holes and uncertain evidence."""
    if not isinstance(audio_duration, (int, float)) or not math.isfinite(audio_duration):
        raise ValueError("audio_duration must be a finite number")
    if audio_duration <= 0:
        raise ValueError("audio_duration must be positive")
    if window_seconds <= 0 or overlap_seconds < 0 or overlap_seconds >= window_seconds:
        raise ValueError("invalid rescue window/overlap configuration")
    if long_segment_seconds is not None and long_segment_seconds <= 0:
        raise ValueError("long_segment_seconds must be positive or None")
    if not 0 <= uncertain_no_speech_threshold <= 1:
        raise ValueError("uncertain_no_speech_threshold must be between 0 and 1")
    if uncertain_logprob_threshold >= 0:
        raise ValueError("uncertain_logprob_threshold must be negative")
    if max_windows <= 0 or max_total_seconds <= 0 or not 0 < max_audio_ratio <= 1:
        raise ValueError("invalid rescue coverage budget")

    main_candidates = [
        item for item in candidates
        if item.get("source_channel") == "main"
    ]
    targets = []

    candidate_decisions = decisions.get("candidate_decisions", {})
    coverage_candidates = [
        item for item in main_candidates
        if candidate_decisions.get(item["evidence_id"], {}).get("label") != "hallucination"
    ]
    coverage = _merge_coverage(coverage_candidates, audio_duration)
    cursor = 0.0
    for start, end in coverage:
        if start - cursor >= minimum_gap_seconds:
            targets.append({
                "start": max(0.0, cursor - context_padding_seconds),
                "end": min(audio_duration, start + context_padding_seconds),
                "reasons": {"main_channel_gap"},
            })
        cursor = max(cursor, end)
    if audio_duration - cursor >= minimum_gap_seconds:
        targets.append({
            "start": max(0.0, cursor - context_padding_seconds),
            "end": audio_duration,
            "reasons": {"main_channel_gap"},
        })

    by_id = {item["evidence_id"]: item for item in main_candidates}
    for evidence_id, decision in candidate_decisions.items():
        if decision.get("label") != "hallucination" or evidence_id not in by_id:
            continue
        item = by_id[evidence_id]
        targets.append({
            "start": max(0.0, item["start"] - context_padding_seconds),
            "end": min(audio_duration, item["end"] + context_padding_seconds),
            "reasons": {"quarantined_candidate_region"},
        })
    for evidence_id, decision in decisions.get("candidate_decisions", {}).items():
        if decision.get("label") != "uncertain" or evidence_id not in by_id:
            continue
        item = by_id[evidence_id]
        if not _is_acoustically_suspicious(
            item,
            no_speech_threshold=uncertain_no_speech_threshold,
            logprob_threshold=uncertain_logprob_threshold,
        ):
            continue
        targets.append({
            "start": max(0.0, item["start"] - context_padding_seconds),
            "end": min(audio_duration, item["end"] + context_padding_seconds),
            "reasons": {"uncertain_candidate"},
        })

    for window in decisions.get("windows", []):
        if window.get("label") != "uncertain":
            continue
        if not any(evidence_id in by_id for evidence_id in window.get("evidence_ids", [])):
            continue
        if not any(
            _is_acoustically_suspicious(
                by_id[evidence_id],
                no_speech_threshold=uncertain_no_speech_threshold,
                logprob_threshold=uncertain_logprob_threshold,
            )
            for evidence_id in window.get("evidence_ids", [])
            if evidence_id in by_id
        ):
            continue
        targets.append({
            "start": max(0.0, window["start"] - context_padding_seconds),
            "end": min(audio_duration, window["end"] + context_padding_seconds),
            "reasons": {"model_disagreement_or_single_model"},
        })

    for item in main_candidates:
        if (
            long_segment_seconds is not None
            and item["end"] - item["start"] > long_segment_seconds
        ):
            targets.append({
                "start": max(0.0, item["start"] - context_padding_seconds),
                "end": min(audio_duration, item["end"] + context_padding_seconds),
                "reasons": {"abnormally_long_segment"},
            })

    merged_targets = _merge_targets(targets)
    requested_windows = []
    for target in merged_targets:
        for start, end in _tile_target(
            target["start"], target["end"], window_seconds, overlap_seconds
        ):
            requested_windows.append({
                "window_id": f"R{len(requested_windows) + 1:06d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "reasons": sorted(target["reasons"]),
                "scan_model": "large-v3",
                "scan_vad": False,
                "turbo_policy": "only_if_v3_returns_candidate",
            })

    windows, budget = _apply_rescue_budget(
        requested_windows,
        audio_duration,
        window_seconds=window_seconds,
        max_windows=max_windows,
        max_total_seconds=max_total_seconds,
        max_audio_ratio=max_audio_ratio,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "asr_rescue_windows",
        "status": "complete",
        "audio_duration": round(float(audio_duration), 3),
        "config": {
            "window_seconds": window_seconds,
            "overlap_seconds": overlap_seconds,
            "minimum_gap_seconds": minimum_gap_seconds,
            "context_padding_seconds": context_padding_seconds,
            "long_segment_seconds": long_segment_seconds,
            "uncertain_no_speech_threshold": uncertain_no_speech_threshold,
            "uncertain_logprob_threshold": uncertain_logprob_threshold,
            "max_windows": max_windows,
            "max_total_seconds": max_total_seconds,
            "max_audio_ratio": max_audio_ratio,
        },
        "budget": budget,
        "windows": windows,
    }
