# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic shadow alignment between script dialogue and ASR timelines."""

from __future__ import annotations

import json
import hashlib
import math
import re
import unicodedata
from bisect import bisect_left
from collections import Counter
from difflib import SequenceMatcher
from itertools import product

from asr_evidence import canonical_fingerprint, write_json_atomic
from asr_timeline import _validate_timeline
from script_units import validate_script_units_artifact


SCRIPT_ALIGNMENT_VERSION = "script-alignment-shadow-v1"
MAX_SCRIPT_GROUP = 3
MAX_TIMELINE_GROUP = 6
MIN_MATCH_SCORE = 0.38
SCRIPT_SKIP_COST = 0.55
TIMELINE_SKIP_COST = 0.45
MAX_VARIANT_COMBINATIONS = 4096
MAX_REGION_CELLS = 30_000
MAX_TOTAL_REGION_CELLS = 120_000
MAX_PAIR_WORK_UNITS = 1_000_000
MAX_REGION_WORK_UNITS = 50_000_000
MAX_TOTAL_WORK_UNITS = 200_000_000
AMBIGUITY_MARGIN = 0.08
EXACT_MATCH_BONUS = 0.5
MIN_EXACT_ANCHOR_LENGTH = 4
SCRIPT_TIMING_REVIEW_SHIFT_SECONDS = 0.75
NORMALIZATION_VERSION = "nfkc-katakana-to-hiragana-punctuation-v1"
_ALIGNMENT_DROP_RE = re.compile(r"[\s\u3000、。！？!?…・,.;:：\-~～「」『』（）()\[\]【】]+")


def _validate_script_units(units_artifact: dict) -> None:
    if not isinstance(units_artifact, dict):
        raise ValueError("script units must be an object")
    units = units_artifact.get("units")
    if not isinstance(units, list):
        raise ValueError("script units must be an array")
    script_text = "".join(
        unit.get("text_exact", "") if isinstance(unit, dict) else ""
        for unit in units
    )
    validate_script_units_artifact(
        units_artifact,
        script_text,
        units_artifact.get("generation_fingerprint"),
    )


def _validate_fusion_timeline(timeline: dict) -> None:
    if not isinstance(timeline, dict):
        raise ValueError("fusion timeline must be an object")
    if (
        timeline.get("schema_version") != 1
        or timeline.get("artifact_type") != "fusion_timeline"
        or timeline.get("status") != "complete"
        or not isinstance(timeline.get("contract_version"), str)
        or not isinstance(timeline.get("excluded_evidence"), list)
    ):
        raise ValueError("invalid fusion timeline identity")
    source = timeline.get("source")
    legacy_source_fields = {
        "audio_name", "size_bytes", "mtime_ns", "path_fingerprint",
    }
    if not isinstance(source, dict) or frozenset(source) not in {
        frozenset(legacy_source_fields),
        frozenset(legacy_source_fields | {"sha256"}),
    }:
        raise ValueError("invalid fusion timeline source")
    if (
        not isinstance(source["audio_name"], str) or not source["audio_name"]
        or not isinstance(source["size_bytes"], int) or source["size_bytes"] < 0
        or not isinstance(source["mtime_ns"], int) or source["mtime_ns"] < 0
        or not isinstance(source["path_fingerprint"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", source["path_fingerprint"])
        or (
            "sha256" in source
            and (
                not isinstance(source["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
            )
        )
    ):
        raise ValueError("invalid fusion timeline source identity")
    for field in ("evidence_fingerprint", "decision_fingerprint"):
        if (
            not isinstance(timeline.get(field), str)
            or not re.fullmatch(r"[0-9a-f]{64}", timeline[field])
        ):
            raise ValueError(f"invalid {field}")
    _validate_timeline(timeline)
    for item in timeline["items"]:
        for candidate in item["candidates"]:
            if (
                not isinstance(candidate.get("text"), str)
                or not candidate["text"].strip()
                or not isinstance(candidate.get("source_channel"), str)
                or not candidate["source_channel"]
                or not isinstance(candidate.get("model_role"), str)
                or not candidate["model_role"]
                or not isinstance(candidate.get("source_view"), str)
                or not candidate["source_view"]
            ):
                raise ValueError("invalid fusion timeline candidate provenance")
    body = {
        "contract_version": timeline["contract_version"],
        "items": timeline["items"],
        "excluded_evidence": timeline["excluded_evidence"],
    }
    if timeline.get("timeline_fingerprint") != canonical_fingerprint(body):
        raise ValueError("fusion timeline fingerprint mismatch")


def _normalize_alignment_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = "".join(
        chr(ord(char) - 0x60) if "ァ" <= char <= "ヶ" else char
        for char in normalized
    )
    return _ALIGNMENT_DROP_RE.sub("", normalized)


def _bigram_dice(left: str, right: str) -> float:
    if len(left) < 2 or len(right) < 2:
        return 1.0 if left == right else 0.0
    left_pairs = Counter(
        left[index:index + 2] for index in range(len(left) - 1)
    )
    right_pairs = Counter(
        right[index:index + 2] for index in range(len(right) - 1)
    )
    overlap = sum((left_pairs & right_pairs).values())
    return 2 * overlap / (sum(left_pairs.values()) + sum(right_pairs.values()))


def _text_score(left: str, right: str) -> dict:
    left_norm = _normalize_alignment_text(left)
    right_norm = _normalize_alignment_text(right)
    if not left_norm or not right_norm:
        return {"total": 0.0, "edit_similarity": 0.0, "ngram_similarity": 0.0,
                "length_score": 0.0}
    if left_norm == right_norm:
        return {"total": 1.0, "edit_similarity": 1.0, "ngram_similarity": 1.0,
                "length_score": 1.0}
    edit = SequenceMatcher(None, left_norm, right_norm, autojunk=False).ratio()
    ngram = _bigram_dice(left_norm, right_norm)
    length_score = min(len(left_norm), len(right_norm)) / max(
        len(left_norm), len(right_norm)
    )
    total = 0.55 * edit + 0.30 * ngram + 0.15 * length_score
    if min(len(left_norm), len(right_norm)) <= 2 and left_norm != right_norm:
        total = 0.0
    return {
        "total": total,
        "edit_similarity": edit,
        "ngram_similarity": ngram,
        "length_score": length_score,
    }


def _exact_match_bonus(script_text: str, score: dict) -> float:
    if (
        score["total"] >= 1.0 - 1e-12
        and len(_normalize_alignment_text(script_text)) >= MIN_EXACT_ANCHOR_LENGTH
    ):
        return EXACT_MATCH_BONUS
    return 0.0


def _candidate_options(item: dict) -> list[dict]:
    unique = {}
    for candidate in item["candidates"]:
        text = candidate.get("text")
        evidence_id = candidate.get("evidence_id")
        if not isinstance(text, str) or not _normalize_alignment_text(text):
            continue
        if not isinstance(evidence_id, str) or not evidence_id:
            continue
        provenance = (
            candidate.get("source_channel"),
            candidate.get("model_role"),
            candidate.get("source_view"),
        )
        option = {
            "text": text,
            "evidence_ids": [evidence_id],
            "provenance": provenance,
        }
        key = (provenance, _normalize_alignment_text(text))
        previous = unique.get(key)
        if previous is None or evidence_id < previous["evidence_ids"][0]:
            unique[key] = option
    return sorted(
        unique.values(),
        key=lambda option: (_normalize_alignment_text(option["text"]), option["evidence_ids"]),
    )


def _best_timeline_variant(script_text: str, timeline_items: list[dict]) -> dict | None:
    option_sets = [_candidate_options(item) for item in timeline_items]
    if any(not options for options in option_sets):
        return None
    common_provenance = set(option["provenance"] for option in option_sets[0])
    for options in option_sets[1:]:
        common_provenance &= {option["provenance"] for option in options}
    variants = []
    for provenance in sorted(common_provenance, key=repr):
        option_lists = [
            sorted(
                [option for option in options if option["provenance"] == provenance],
                key=lambda option: (
                    option["evidence_ids"], _normalize_alignment_text(option["text"]),
                ),
            )
            for options in option_sets
        ]
        combinations = 1
        for options in option_lists:
            combinations *= len(options)
        if combinations > MAX_VARIANT_COMBINATIONS:
            return None
        for selected in product(*option_lists):
            text_parts = []
            evidence_ids = []
            for option in selected:
                evidence_id = option["evidence_ids"][0]
                text_parts.append(option["text"])
                evidence_ids.append(evidence_id)
            variants.append({"text": "".join(text_parts), "evidence_ids": evidence_ids})
    variants.sort(key=lambda option: (
        -_text_score(script_text, option["text"])["total"],
        abs(len(_normalize_alignment_text(script_text)) - len(_normalize_alignment_text(option["text"]))),
        option["evidence_ids"],
        _normalize_alignment_text(option["text"]),
    ))
    if not variants:
        return None
    best = variants[0]
    return {**best, "score": _text_score(script_text, best["text"])}


def _push_node(dp, target, parent, source, operation, score_delta, matched_chars=0):
    def extend(signature, token):
        return hashlib.sha256(f"{signature}|{token!r}".encode("utf-8")).hexdigest()

    script_signature = parent["script_signature"]
    timeline_signature = parent["timeline_signature"]
    evidence_signature = parent["evidence_signature"]
    script_count = operation.get("script_count", 0)
    timeline_count = operation.get("timeline_count", 0)
    if operation["kind"] == "skip_script":
        for _ in range(script_count):
            script_signature = extend(script_signature, None)
    elif operation["kind"] == "skip_timeline":
        for _ in range(timeline_count):
            timeline_signature = extend(timeline_signature, None)
    else:
        timeline_range = tuple(range(source[1], target[1]))
        script_range = tuple(range(source[0], target[0]))
        for _ in range(script_count):
            script_signature = extend(script_signature, timeline_range)
        for _ in range(timeline_count):
            timeline_signature = extend(timeline_signature, script_range)
        evidence_signature = extend(
            evidence_signature,
            tuple(operation.get("variant", {}).get("evidence_ids", [])),
        )
    signature = hashlib.sha256(
        f"{script_signature}|{timeline_signature}|{evidence_signature}".encode("utf-8")
    ).hexdigest()
    node = {
        # Quantize accumulated scores so mathematically equal paths are decided
        # by the explicit coverage/skip tie-breakers, not binary float noise.
        "score": round(parent["score"] + score_delta, 12),
        "matched_chars": parent["matched_chars"] + matched_chars,
        "skips": parent["skips"] + (operation["kind"].startswith("skip")),
        "span_penalty": parent["span_penalty"] + max(
            0, operation.get("script_count", 0) + operation.get("timeline_count", 0) - 2
        ),
        "signature": signature,
        "script_signature": script_signature,
        "timeline_signature": timeline_signature,
        "evidence_signature": evidence_signature,
        "parent": parent,
        "step": (source, target, operation),
    }
    nodes = dp.setdefault(target, [])
    if any(existing["signature"] == signature for existing in nodes):
        return
    nodes.append(node)
    nodes.sort(key=lambda value: (
        -value["score"], -value["matched_chars"], value["skips"],
        value["span_penalty"], value["signature"],
    ))
    del nodes[2:]


def _trace_node(node):
    operations = []
    while node.get("step") is not None:
        operations.append(node["step"])
        node = node["parent"]
    operations.reverse()
    return operations


def _variant_combination_count(timeline_items: list[dict]) -> int:
    option_sets = [_candidate_options(item) for item in timeline_items]
    if any(not options for options in option_sets):
        return 0
    common = set(option["provenance"] for option in option_sets[0])
    for options in option_sets[1:]:
        common &= {option["provenance"] for option in options}
    total = 0
    for provenance in common:
        combinations = 1
        for options in option_sets:
            combinations *= sum(
                1 for option in options if option["provenance"] == provenance
            )
        total += combinations
        if total > MAX_VARIANT_COMBINATIONS:
            return total
    return total


def _estimate_region_work(dialogue_units, timeline_items, stop_after):
    work = 0
    for i in range(len(dialogue_units)):
        for j in range(len(timeline_items)):
            for script_size in range(1, min(MAX_SCRIPT_GROUP, len(dialogue_units) - i) + 1):
                script_length = len(_normalize_alignment_text("".join(
                    unit["text_exact"] for unit in dialogue_units[i:i + script_size]
                )))
                for timeline_size in range(
                    1, min(MAX_TIMELINE_GROUP, len(timeline_items) - j) + 1
                ):
                    group = timeline_items[j:j + timeline_size]
                    combinations = _variant_combination_count(group)
                    if combinations > MAX_VARIANT_COMBINATIONS:
                        return stop_after + 1
                    candidate_length = sum(
                        max(
                            (len(_normalize_alignment_text(candidate["text"]))
                             for candidate in item["candidates"]),
                            default=0,
                        )
                        for item in group
                    )
                    pair_work = max(1, script_length) * max(1, candidate_length)
                    if pair_work > MAX_PAIR_WORK_UNITS:
                        return stop_after + 1
                    # SequenceMatcher(autojunk=False) can approach O(L*R).
                    # Use a conservative length product so expensive comparisons
                    # fail closed before the matcher runs.
                    work += combinations * pair_work
                    if work > stop_after:
                        return work
    return work


def _align_region(dialogue_units: list[dict], timeline_items: list[dict], work_budget):
    script_count = len(dialogue_units)
    timeline_count = len(timeline_items)
    if script_count * timeline_count > MAX_REGION_CELLS:
        operations = []
        for i in range(script_count):
            operations.append(((i, 0), (i + 1, 0), {
                "kind": "skip_script", "script_count": 1,
                "reason": "alignment_region_too_large",
            }))
        for j in range(timeline_count):
            operations.append(((script_count, j), (script_count, j + 1), {
                "kind": "skip_timeline", "timeline_count": 1,
                "reason": "alignment_region_too_large",
            }))
        return operations, None, None, 0
    work_limit = min(MAX_REGION_WORK_UNITS, work_budget)
    estimated_work = _estimate_region_work(
        dialogue_units, timeline_items, work_limit,
    )
    if estimated_work > work_limit:
        operations = []
        for i in range(script_count):
            operations.append(((i, 0), (i + 1, 0), {
                "kind": "skip_script", "script_count": 1,
                "reason": "alignment_work_budget_exceeded",
            }))
        for j in range(timeline_count):
            operations.append(((script_count, j), (script_count, j + 1), {
                "kind": "skip_timeline", "timeline_count": 1,
                "reason": "alignment_work_budget_exceeded",
            }))
        return operations, None, None, 0
    root = {
        "score": 0.0, "matched_chars": 0, "skips": 0,
        "span_penalty": 0, "signature": "root",
        "script_signature": "script-root", "timeline_signature": "timeline-root",
        "evidence_signature": "evidence-root", "parent": None, "step": None,
    }
    dp = {(0, 0): [root]}
    match_cache = {}

    for i in range(script_count + 1):
        for j in range(timeline_count + 1):
            if (i, j) not in dp:
                continue
            for parent in list(dp[(i, j)]):
                if i < script_count:
                    _push_node(
                        dp, (i + 1, j), parent, (i, j),
                        {"kind": "skip_script", "script_count": 1},
                        -SCRIPT_SKIP_COST,
                    )
                if j < timeline_count:
                    _push_node(
                        dp, (i, j + 1), parent, (i, j),
                        {"kind": "skip_timeline", "timeline_count": 1},
                        -TIMELINE_SKIP_COST,
                    )
                for script_group_size in range(
                    1, min(MAX_SCRIPT_GROUP, script_count - i) + 1
                ):
                    script_group = dialogue_units[i:i + script_group_size]
                    script_text = "".join(unit["text_exact"] for unit in script_group)
                    for timeline_group_size in range(
                        1, min(MAX_TIMELINE_GROUP, timeline_count - j) + 1
                    ):
                        target = (i + script_group_size, j + timeline_group_size)
                        cache_key = (i, j, script_group_size, timeline_group_size)
                        variant = match_cache.get(cache_key)
                        if variant is None:
                            variant = _best_timeline_variant(
                                script_text,
                                timeline_items[j:j + timeline_group_size],
                            )
                            match_cache[cache_key] = variant or False
                        elif variant is False:
                            variant = None
                        if variant is None or variant["score"]["total"] < MIN_MATCH_SCORE:
                            continue
                        replaced_skips = (
                            script_group_size * SCRIPT_SKIP_COST
                            + timeline_group_size * TIMELINE_SKIP_COST
                        )
                        # ASR segmentation is arbitrary, so one script line may
                        # legitimately span several adjacent timeline items.
                        # Only penalize collapsing multiple script lines into a
                        # shared timing group, which loses timing granularity.
                        group_penalty = 0.06 * (script_group_size - 1)
                        transition = (
                            -replaced_skips
                            + 2.2 * (variant["score"]["total"] - MIN_MATCH_SCORE)
                            - group_penalty
                            + _exact_match_bonus(script_text, variant["score"])
                        )
                        _push_node(
                            dp, target, parent, (i, j), {
                                "kind": "match",
                                "script_count": script_group_size,
                                "timeline_count": timeline_group_size,
                                "variant": variant,
                            },
                            transition,
                            matched_chars=len(_normalize_alignment_text(script_text)),
                        )

    end = (script_count, timeline_count)
    if end not in dp:
        raise ValueError("alignment could not reach the terminal state")
    nodes = dp[end]
    best = _trace_node(nodes[0])
    if len(nodes) == 1:
        return best, None, None, estimated_work
    alternative = _trace_node(nodes[1])
    return best, alternative, nodes[0]["score"] - nodes[1]["score"], estimated_work


def _unique_exact_anchors(dialogue_units: list[dict], timeline_items: list[dict]):
    script_positions = {}
    for index, unit in enumerate(dialogue_units):
        normalized = _normalize_alignment_text(unit["text_exact"])
        if len(normalized) >= MIN_EXACT_ANCHOR_LENGTH:
            script_positions.setdefault(normalized, []).append(index)
    timeline_positions = {}
    for index, item in enumerate(timeline_items):
        if item["classification"] != "supported" or item["review_required"]:
            continue
        normalized_candidates = {
            _normalize_alignment_text(candidate.get("text", ""))
            for candidate in item["candidates"]
        } - {""}
        if len(normalized_candidates) != 1:
            continue
        normalized = next(iter(normalized_candidates))
        timeline_positions.setdefault(normalized, []).append(index)
    pairs = sorted(
        (positions[0], timeline_positions[text][0])
        for text, positions in script_positions.items()
        if len(positions) == 1
        and len(timeline_positions.get(text, [])) == 1
    )
    if not pairs:
        return []
    tails = []
    tail_pair_indices = []
    parents = [-1] * len(pairs)
    for pair_index, (_script_index, timeline_index) in enumerate(pairs):
        position = bisect_left(tails, timeline_index)
        if position == len(tails):
            tails.append(timeline_index)
            tail_pair_indices.append(pair_index)
        else:
            tails[position] = timeline_index
            tail_pair_indices[position] = pair_index
        if position > 0:
            parents[pair_index] = tail_pair_indices[position - 1]
    selected = []
    cursor = tail_pair_indices[-1]
    while cursor >= 0:
        selected.append(pairs[cursor])
        cursor = parents[cursor]
    selected.reverse()
    if len(selected) != len(pairs):
        # Conflicting exact anchors represent alternative global orders; let the
        # region top-2 DP expose the ambiguity instead of silently fixing one LIS.
        return []
    return selected


def _offset_operations(operations, script_offset, timeline_offset):
    return [
        (
            (previous[0] + script_offset, previous[1] + timeline_offset),
            (current[0] + script_offset, current[1] + timeline_offset),
            operation,
        )
        for previous, current, operation in operations
    ]


def _operation_assignments(operations):
    script_map = {}
    timeline_map = {}
    for previous, current, operation in operations:
        script_positions = tuple(range(previous[0], current[0]))
        timeline_positions = tuple(range(previous[1], current[1]))
        if operation["kind"] == "match":
            for position in script_positions:
                script_map[position] = timeline_positions
            for position in timeline_positions:
                timeline_map[position] = script_positions
        elif operation["kind"] == "skip_script":
            for position in script_positions:
                script_map[position] = None
        elif operation["kind"] == "skip_timeline":
            for position in timeline_positions:
                timeline_map[position] = None
    return script_map, timeline_map


def _align_dialogue(dialogue_units: list[dict], timeline_items: list[dict]):
    anchors = _unique_exact_anchors(dialogue_units, timeline_items)
    operations = []
    ambiguous_script = {}
    ambiguous_timeline = {}
    script_cursor = timeline_cursor = 0
    remaining_budget = MAX_TOTAL_REGION_CELLS
    remaining_work_budget = MAX_TOTAL_WORK_UNITS
    for script_anchor, timeline_anchor in anchors + [
        (len(dialogue_units), len(timeline_items))
    ]:
        script_region = dialogue_units[script_cursor:script_anchor]
        timeline_region = timeline_items[timeline_cursor:timeline_anchor]
        region_cells = len(script_region) * len(timeline_region)
        if region_cells > remaining_budget:
            region_operations = []
            for index in range(len(script_region)):
                region_operations.append(((index, 0), (index + 1, 0), {
                    "kind": "skip_script", "script_count": 1,
                    "reason": "alignment_total_budget_exceeded",
                }))
            for index in range(len(timeline_region)):
                region_operations.append(
                    ((len(script_region), index), (len(script_region), index + 1), {
                        "kind": "skip_timeline", "timeline_count": 1,
                        "reason": "alignment_total_budget_exceeded",
                    })
                )
            alternative_operations = None
            alternative_margin = None
        else:
            (
                region_operations,
                alternative_operations,
                alternative_margin,
                work_used,
            ) = _align_region(
                script_region, timeline_region, remaining_work_budget,
            )
            remaining_budget -= region_cells
            remaining_work_budget -= work_used
        offset_region = _offset_operations(
            region_operations, script_cursor, timeline_cursor,
        )
        operations.extend(offset_region)
        if (
            alternative_operations is not None
            and alternative_margin is not None
            and alternative_margin <= AMBIGUITY_MARGIN
        ):
            offset_alternative = _offset_operations(
                alternative_operations, script_cursor, timeline_cursor,
            )
            best_script, best_timeline = _operation_assignments(offset_region)
            alt_script, alt_timeline = _operation_assignments(offset_alternative)
            for position in set(best_script) | set(alt_script):
                if best_script.get(position) != alt_script.get(position):
                    ambiguous_script[position] = round(alternative_margin, 6)
            for position in set(best_timeline) | set(alt_timeline):
                if best_timeline.get(position) != alt_timeline.get(position):
                    ambiguous_timeline[position] = round(alternative_margin, 6)
        if script_anchor == len(dialogue_units):
            break
        script_text = dialogue_units[script_anchor]["text_exact"]
        variant = _best_timeline_variant(
            script_text, [timeline_items[timeline_anchor]],
        )
        if variant is None or variant["score"]["total"] < 1.0 - 1e-12:
            raise ValueError("exact anchor lost its exact candidate")
        operations.append((
            (script_anchor, timeline_anchor),
            (script_anchor + 1, timeline_anchor + 1),
            {"kind": "match", "script_count": 1, "timeline_count": 1,
             "variant": variant, "fixed_exact_anchor": True},
        ))
        script_cursor = script_anchor + 1
        timeline_cursor = timeline_anchor + 1
    return operations, ambiguous_script, ambiguous_timeline


def _algorithm_fingerprint() -> str:
    return canonical_fingerprint({
        "contract_version": SCRIPT_ALIGNMENT_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "max_script_group": MAX_SCRIPT_GROUP,
        "max_timeline_group": MAX_TIMELINE_GROUP,
        "minimum_match_score": MIN_MATCH_SCORE,
        "script_skip_cost": SCRIPT_SKIP_COST,
        "timeline_skip_cost": TIMELINE_SKIP_COST,
        "max_variant_combinations": MAX_VARIANT_COMBINATIONS,
        "max_region_cells": MAX_REGION_CELLS,
        "max_total_region_cells": MAX_TOTAL_REGION_CELLS,
        "max_pair_work_units": MAX_PAIR_WORK_UNITS,
        "max_region_work_units": MAX_REGION_WORK_UNITS,
        "max_total_work_units": MAX_TOTAL_WORK_UNITS,
        "ambiguity_margin": AMBIGUITY_MARGIN,
        "exact_match_bonus": EXACT_MATCH_BONUS,
        "minimum_exact_anchor_length": MIN_EXACT_ANCHOR_LENGTH,
        "script_timing_review_shift_seconds": SCRIPT_TIMING_REVIEW_SHIFT_SECONDS,
        "anchor_strategy": "unique-consensus-supported-exact-min4-monotonic-lis-v3",
        "short_text_rule": "length-le-2-requires-exact-v1",
        "score_weights": {"edit": 0.55, "ngram": 0.30, "length": 0.15},
        "script_group_penalty": 0.06,
        "timeline_group_penalty": 0.0,
        "match_gain": 2.2,
    })


def _relation(script_count: int, timeline_count: int) -> str:
    if script_count == timeline_count == 1:
        return "one_to_one"
    if script_count == 1:
        return "one_to_many"
    if timeline_count == 1:
        return "many_to_one"
    return "many_to_many"


def _timeline_needs_script_timing_review(item: dict) -> bool:
    """Ignore text-only ASR disagreement once an official script is aligned."""
    kind = item["timing_source"].get("kind")
    if kind in {"unresolved_overlap_cluster", "bounded_split"}:
        return True
    if kind == "overlap_partition":
        source = item["timing_source"]
        shift = max(
            abs(item["start"] - source["anchor_start"]),
            abs(item["end"] - source["anchor_end"]),
        )
        return shift > SCRIPT_TIMING_REVIEW_SHIFT_SECONDS
    if kind in {"candidate_bounds", "word_candidate"}:
        return False
    return item["review_required"]


def build_script_alignment(script_units: dict, fusion_timeline: dict) -> dict:
    _validate_script_units(script_units)
    _validate_fusion_timeline(fusion_timeline)
    dialogue_units = [
        unit for unit in script_units["units"] if unit["type"] == "dialogue"
    ]
    excluded_units = [
        {
            "script_unit_id": unit["script_unit_id"],
            "type": unit["type"],
            "review_required": unit["type"] == "unknown" or unit["confidence"] < 0.7,
            "reasons": (
                ["unknown_script_unit"] if unit["type"] == "unknown" else []
            ) + (
                ["low_script_classification_confidence"]
                if unit["confidence"] < 0.7 else []
            ),
        }
        for unit in script_units["units"] if unit["type"] != "dialogue"
    ]
    timeline_items = fusion_timeline["items"]
    operations, ambiguous_script, ambiguous_timeline = _align_dialogue(
        dialogue_units, timeline_items
    )

    matches = []
    unmatched_script = []
    unmatched_timeline = []
    for previous, current, operation in operations:
        i, j = previous
        if operation["kind"] == "skip_script":
            reasons = [operation.get("reason", "asr_omission_candidate")]
            margin = ambiguous_script.get(i)
            if margin is not None:
                reasons = ["alternative_path_ambiguous"]
            unmatched_script.append({
                "script_unit_id": dialogue_units[i]["script_unit_id"],
                "review_required": True,
                "alternative_margin": margin,
                "reasons": reasons,
            })
            continue
        if operation["kind"] == "skip_timeline":
            reasons = [operation.get("reason", "script_missing_or_asr_extra")]
            margin = ambiguous_timeline.get(j)
            if margin is not None:
                reasons = ["alternative_path_ambiguous"]
            unmatched_timeline.append({
                "timeline_id": timeline_items[j]["timeline_id"],
                "review_required": True,
                "alternative_margin": margin,
                "reasons": reasons,
            })
            continue
        script_group = dialogue_units[i:current[0]]
        timeline_group = timeline_items[j:current[1]]
        score = operation["variant"]["score"]
        total_score = score["total"]
        confidence = (
            "high" if total_score >= 0.78
            else "medium" if total_score >= 0.58 else "low"
        )
        shared_timing = len(script_group) > 1
        reasons = []
        if confidence != "high":
            reasons.append("low_text_similarity")
        if shared_timing:
            reasons.append("shared_timeline_window")
        if len(script_group) > 1 and len(timeline_group) > 1:
            reasons.append("group_level_timing_only")
        if any(unit["confidence"] < 0.7 for unit in script_group):
            reasons.append("low_script_classification_confidence")
        if any(
            _timeline_needs_script_timing_review(item)
            for item in timeline_group
        ):
            reasons.append("timeline_requires_review")
        if any(item["classification"] != "supported" for item in timeline_group):
            reasons.append("uncertain_asr_evidence")
        if any(
            len(_normalize_alignment_text(unit["text_exact"])) <= 2
            for unit in script_group
        ):
            reasons.append("short_text_match")
        ambiguity_margins = [
            ambiguous_script[position]
            for position in range(i, current[0]) if position in ambiguous_script
        ] + [
            ambiguous_timeline[position]
            for position in range(j, current[1]) if position in ambiguous_timeline
        ]
        alternative_margin = min(ambiguity_margins) if ambiguity_margins else None
        if alternative_margin is not None:
            reasons.append("alternative_path_ambiguous")
        evidence_refs = [
            {"timeline_id": item["timeline_id"], "evidence_id": evidence_id}
            for item, evidence_id in zip(
                timeline_group, operation["variant"]["evidence_ids"], strict=True,
            )
        ]
        matches.append({
            "alignment_id": f"SA{len(matches) + 1:06d}",
            "script_unit_ids": [unit["script_unit_id"] for unit in script_group],
            "timeline_ids": [item["timeline_id"] for item in timeline_group],
            "relation": _relation(len(script_group), len(timeline_group)),
            "start": timeline_group[0]["start"],
            "end": timeline_group[-1]["end"],
            "timing_kind": (
                "shared_timeline_window" if shared_timing
                else "timeline_span" if len(timeline_group) > 1
                else "timeline_window"
            ),
            "score": {
                key: round(value, 6) for key, value in score.items()
            },
            "confidence": confidence,
            "alternative_margin": alternative_margin,
            "review_required": bool(reasons),
            "reasons": reasons,
            "selected_evidence_refs": evidence_refs,
        })

    repeated = {}
    for unit in dialogue_units:
        key = _normalize_alignment_text(unit["text_exact"])
        if key:
            repeated.setdefault(key, []).append(unit["script_unit_id"])
    unmatched_ids = {item["script_unit_id"] for item in unmatched_script}
    ambiguous_ids = {
        unit_id
        for ids in repeated.values() if len(ids) > 1 and unmatched_ids.intersection(ids)
        for unit_id in ids
    }
    if ambiguous_ids:
        for match in matches:
            if ambiguous_ids.intersection(match["script_unit_ids"]):
                match["review_required"] = True
                if "repeated_text_ambiguous" not in match["reasons"]:
                    match["reasons"].append("repeated_text_ambiguous")
        for item in unmatched_script:
            if item["script_unit_id"] in ambiguous_ids:
                item["reasons"] = ["ambiguous_repeated_script_text"]

    body = {
        "schema_version": 1,
        "artifact_type": "script_alignment",
        "status": "complete",
        "contract_version": SCRIPT_ALIGNMENT_VERSION,
        "algorithm_fingerprint": _algorithm_fingerprint(),
        "inputs": {
            "script_units_input_fingerprint": canonical_fingerprint(script_units),
            "timeline_input_fingerprint": canonical_fingerprint(fusion_timeline),
            "script_source_fingerprint": script_units["source_text_fingerprint"],
            "timeline_fingerprint": fusion_timeline["timeline_fingerprint"],
            "evidence_fingerprint": fusion_timeline.get("evidence_fingerprint"),
            "decision_fingerprint": fusion_timeline.get("decision_fingerprint"),
        },
        "matches": matches,
        "unmatched_script_units": unmatched_script,
        "unmatched_timeline_items": unmatched_timeline,
        "excluded_script_units": excluded_units,
        "summary": {
            "dialogue_units": len(dialogue_units),
            "timeline_items": len(timeline_items),
            "matches": len(matches),
            "unmatched_script_units": len(unmatched_script),
            "unmatched_timeline_items": len(unmatched_timeline),
            "excluded_script_units": len(excluded_units),
            "review_required_matches": sum(
                1 for match in matches if match["review_required"]
            ),
        },
    }
    return {**body, "artifact_fingerprint": canonical_fingerprint(body)}


def validate_script_alignment_artifact(
    artifact: dict,
    script_units: dict,
    fusion_timeline: dict,
) -> dict:
    _validate_script_units(script_units)
    _validate_fusion_timeline(fusion_timeline)
    required = {
        "schema_version", "artifact_type", "status", "contract_version",
        "algorithm_fingerprint", "inputs", "matches", "unmatched_script_units",
        "unmatched_timeline_items", "excluded_script_units", "summary",
        "artifact_fingerprint",
    }
    if not isinstance(artifact, dict) or set(artifact) != required:
        raise ValueError("invalid script alignment artifact fields")
    expected_inputs = {
        "script_units_input_fingerprint": canonical_fingerprint(script_units),
        "timeline_input_fingerprint": canonical_fingerprint(fusion_timeline),
        "script_source_fingerprint": script_units["source_text_fingerprint"],
        "timeline_fingerprint": fusion_timeline["timeline_fingerprint"],
        "evidence_fingerprint": fusion_timeline["evidence_fingerprint"],
        "decision_fingerprint": fusion_timeline["decision_fingerprint"],
    }
    if (
        artifact["schema_version"] != 1
        or artifact["artifact_type"] != "script_alignment"
        or artifact["status"] != "complete"
        or artifact["contract_version"] != SCRIPT_ALIGNMENT_VERSION
        or artifact["algorithm_fingerprint"] != _algorithm_fingerprint()
        or artifact["inputs"] != expected_inputs
    ):
        raise ValueError("stale script alignment identity")
    for field in (
        "matches", "unmatched_script_units", "unmatched_timeline_items",
        "excluded_script_units",
    ):
        if not isinstance(artifact[field], list):
            raise ValueError(f"invalid script alignment {field}")
    if not isinstance(artifact["summary"], dict):
        raise ValueError("invalid script alignment summary")
    body = dict(artifact)
    fingerprint = body.pop("artifact_fingerprint")
    if fingerprint != canonical_fingerprint(body):
        raise ValueError("script alignment fingerprint mismatch")

    dialogue = [unit for unit in script_units["units"] if unit["type"] == "dialogue"]
    non_dialogue = [unit for unit in script_units["units"] if unit["type"] != "dialogue"]
    dialogue_by_id = {unit["script_unit_id"]: (index, unit) for index, unit in enumerate(dialogue)}
    timeline_by_id = {
        item["timeline_id"]: (index, item)
        for index, item in enumerate(fusion_timeline["items"])
    }
    seen_script = set()
    seen_timeline = set()
    previous_script_end = previous_timeline_end = -1
    for ordinal, match in enumerate(artifact["matches"], start=1):
        fields = {
            "alignment_id", "script_unit_ids", "timeline_ids", "relation",
            "start", "end", "timing_kind", "score", "confidence",
            "alternative_margin", "review_required", "reasons",
            "selected_evidence_refs",
        }
        if not isinstance(match, dict) or set(match) != fields:
            raise ValueError("invalid script alignment match fields")
        if match["alignment_id"] != f"SA{ordinal:06d}":
            raise ValueError("invalid script alignment ID")
        script_ids = match["script_unit_ids"]
        timeline_ids = match["timeline_ids"]
        if (
            not isinstance(script_ids, list) or not script_ids
            or not isinstance(timeline_ids, list) or not timeline_ids
            or any(value not in dialogue_by_id for value in script_ids)
            or any(value not in timeline_by_id for value in timeline_ids)
        ):
            raise ValueError("unknown script alignment reference")
        script_positions = [dialogue_by_id[value][0] for value in script_ids]
        timeline_positions = [timeline_by_id[value][0] for value in timeline_ids]
        if (
            script_positions != list(range(script_positions[0], script_positions[-1] + 1))
            or timeline_positions != list(range(timeline_positions[0], timeline_positions[-1] + 1))
            or script_positions[0] <= previous_script_end
            or timeline_positions[0] <= previous_timeline_end
            or seen_script.intersection(script_ids)
            or seen_timeline.intersection(timeline_ids)
        ):
            raise ValueError("non-monotonic or overlapping script alignment")
        previous_script_end = script_positions[-1]
        previous_timeline_end = timeline_positions[-1]
        seen_script.update(script_ids)
        seen_timeline.update(timeline_ids)
        timeline_group = [timeline_by_id[value][1] for value in timeline_ids]
        if (
            match["relation"] != _relation(len(script_ids), len(timeline_ids))
            or match["start"] != timeline_group[0]["start"]
            or match["end"] != timeline_group[-1]["end"]
        ):
            raise ValueError("derived script alignment timing mismatch")
        expected_timing = (
            "shared_timeline_window" if len(script_ids) > 1
            else "timeline_span" if len(timeline_ids) > 1
            else "timeline_window"
        )
        if match["timing_kind"] != expected_timing:
            raise ValueError("derived script alignment timing kind mismatch")
        refs = match["selected_evidence_refs"]
        if (
            not isinstance(refs, list) or len(refs) != len(timeline_group)
            or [ref.get("timeline_id") for ref in refs] != timeline_ids
        ):
            raise ValueError("invalid selected evidence references")
        selected = []
        for ref, item in zip(refs, timeline_group, strict=True):
            candidates = {
                candidate["evidence_id"]: candidate for candidate in item["candidates"]
            }
            evidence_id = ref.get("evidence_id")
            if set(ref) != {"timeline_id", "evidence_id"} or evidence_id not in candidates:
                raise ValueError("selected evidence is outside its timeline item")
            selected.append(candidates[evidence_id])
        if len({
            (item["source_channel"], item["model_role"], item["source_view"])
            for item in selected
        }) != 1:
            raise ValueError("mixed provenance evidence path")
        text_parts = []
        for candidate in selected:
            text_parts.append(candidate["text"])
        script_text = "".join(dialogue_by_id[value][1]["text_exact"] for value in script_ids)
        expected_score = {
            key: round(value, 6)
            for key, value in _text_score(script_text, "".join(text_parts)).items()
        }
        if match["score"] != expected_score:
            raise ValueError("script alignment score mismatch")
        expected_confidence = (
            "high" if expected_score["total"] >= 0.78
            else "medium" if expected_score["total"] >= 0.58 else "low"
        )
        if match["confidence"] != expected_confidence:
            raise ValueError("script alignment confidence mismatch")
        margin = match["alternative_margin"]
        if (
            margin is not None
            and (
                isinstance(margin, bool) or not isinstance(margin, (int, float))
                or not math.isfinite(margin) or margin < 0 or margin > AMBIGUITY_MARGIN
            )
        ):
            raise ValueError("invalid script alignment alternative margin")
        if (
            not isinstance(match["review_required"], bool)
            or not isinstance(match["reasons"], list)
            or any(not isinstance(reason, str) or not reason for reason in match["reasons"])
            or match["review_required"] != bool(match["reasons"])
        ):
            raise ValueError("invalid script alignment review metadata")

    unmatched_script_ids = []
    for item in artifact["unmatched_script_units"]:
        if not isinstance(item, dict) or set(item) != {
            "script_unit_id", "review_required", "alternative_margin", "reasons",
        }:
            raise ValueError("invalid unmatched script unit")
        unit_id = item["script_unit_id"]
        if unit_id not in dialogue_by_id or unit_id in seen_script:
            raise ValueError("duplicate unmatched script unit")
        if (
            item["review_required"] is not True
            or not isinstance(item["reasons"], list) or not item["reasons"]
            or any(not isinstance(reason, str) or not reason for reason in item["reasons"])
            or (
                item["alternative_margin"] is not None
                and (
                    isinstance(item["alternative_margin"], bool)
                    or not isinstance(item["alternative_margin"], (int, float))
                    or not math.isfinite(item["alternative_margin"])
                    or item["alternative_margin"] < 0
                    or item["alternative_margin"] > AMBIGUITY_MARGIN
                )
            )
        ):
            raise ValueError("invalid unmatched script metadata")
        seen_script.add(unit_id)
        unmatched_script_ids.append(unit_id)
    unmatched_timeline_ids = []
    for item in artifact["unmatched_timeline_items"]:
        if not isinstance(item, dict) or set(item) != {
            "timeline_id", "review_required", "alternative_margin", "reasons",
        }:
            raise ValueError("invalid unmatched timeline item")
        timeline_id = item["timeline_id"]
        if timeline_id not in timeline_by_id or timeline_id in seen_timeline:
            raise ValueError("duplicate unmatched timeline item")
        if (
            item["review_required"] is not True
            or not isinstance(item["reasons"], list) or not item["reasons"]
            or any(not isinstance(reason, str) or not reason for reason in item["reasons"])
            or (
                item["alternative_margin"] is not None
                and (
                    isinstance(item["alternative_margin"], bool)
                    or not isinstance(item["alternative_margin"], (int, float))
                    or not math.isfinite(item["alternative_margin"])
                    or item["alternative_margin"] < 0
                    or item["alternative_margin"] > AMBIGUITY_MARGIN
                )
            )
        ):
            raise ValueError("invalid unmatched timeline metadata")
        seen_timeline.add(timeline_id)
        unmatched_timeline_ids.append(timeline_id)
    if seen_script != set(dialogue_by_id) or seen_timeline != set(timeline_by_id):
        raise ValueError("script alignment does not cover every input")

    expected_excluded = [
        {
            "script_unit_id": unit["script_unit_id"],
            "type": unit["type"],
            "review_required": unit["type"] == "unknown" or unit["confidence"] < 0.7,
            "reasons": (["unknown_script_unit"] if unit["type"] == "unknown" else [])
            + (["low_script_classification_confidence"] if unit["confidence"] < 0.7 else []),
        }
        for unit in non_dialogue
    ]
    if artifact["excluded_script_units"] != expected_excluded:
        raise ValueError("excluded script units mismatch")
    expected_summary = {
        "dialogue_units": len(dialogue),
        "timeline_items": len(fusion_timeline["items"]),
        "matches": len(artifact["matches"]),
        "unmatched_script_units": len(unmatched_script_ids),
        "unmatched_timeline_items": len(unmatched_timeline_ids),
        "excluded_script_units": len(non_dialogue),
        "review_required_matches": sum(
            1 for match in artifact["matches"] if match["review_required"]
        ),
    }
    if artifact["summary"] != expected_summary:
        raise ValueError("script alignment summary mismatch")
    return artifact


def write_script_alignment_shadow(
    output_path: str,
    script_units: dict,
    fusion_timeline: dict,
    input_is_current=None,
) -> tuple[str, dict]:
    cached = None
    try:
        with open(output_path, "r", encoding="utf-8-sig") as handle:
            cached = json.load(handle)
        validate_script_alignment_artifact(cached, script_units, fusion_timeline)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        cached = None
    if cached is not None:
        if input_is_current is not None and not input_is_current():
            raise ValueError("alignment inputs changed during processing")
        return "cached", cached
    expected = build_script_alignment(script_units, fusion_timeline)
    validate_script_alignment_artifact(expected, script_units, fusion_timeline)
    if input_is_current is not None and not input_is_current():
        raise ValueError("alignment inputs changed during processing")
    write_json_atomic(output_path, expected)
    return "generated", expected
