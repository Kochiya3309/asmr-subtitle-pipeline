# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed acoustic realignment helpers for unusually long SRT cues."""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
import re
import unicodedata


DEFAULT_MAX_DURATION_MS = 15_000
DEFAULT_MIN_SIMILARITY = 0.65
DEFAULT_MIN_COVERAGE = 0.70
DEFAULT_MIN_WORD_PROBABILITY = 0.35
START_PAD_MS = 150
END_PAD_MS = 250
WINDOW_PAD_OPTIONS_MS = (1_000, 5_000)

_TIMECODE_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)
_BOUNDARY_RE = re.compile(r"[。！？!?]+")
_FINE_BOUNDARY_RE = re.compile(r"[。！？!?、，,；;]+")


def _timestamp_ms(parts: tuple[str, ...]) -> int:
    hours, minutes, seconds, millis = map(int, parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _format_timestamp(value_ms: int) -> str:
    seconds, millis = divmod(int(value_ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def parse_srt(content: str) -> list[dict]:
    cues = []
    blocks = re.split(r"\r?\n\s*\r?\n", content.strip()) if content.strip() else []
    for block in blocks:
        lines = block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(lines) < 3:
            raise ValueError("invalid SRT block")
        match = _TIMECODE_RE.fullmatch(lines[1].strip())
        if not match:
            raise ValueError(f"invalid SRT timecode: {lines[1]!r}")
        start_ms = _timestamp_ms(match.groups()[:4])
        end_ms = _timestamp_ms(match.groups()[4:])
        text = "\n".join(lines[2:]).strip()
        if start_ms >= end_ms or not text:
            raise ValueError("invalid empty or non-positive SRT cue")
        cues.append({
            "index": int(lines[0].strip()),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
        })
    return cues


def render_srt(cues: list[dict]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n{_format_timestamp(cue['start_ms'])} --> "
            f"{_format_timestamp(cue['end_ms'])}\n{cue['text']}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def normalize_alignment_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value))
    return "".join(character for character in normalized if character.isalnum())


@lru_cache(maxsize=4096)
def normalize_phonetic_text(value: str) -> str:
    try:
        from pykakasi import kakasi

        converted = "".join(item["hira"] for item in kakasi().convert(str(value)))
    except (ImportError, KeyError, TypeError, ValueError):
        converted = str(value)
    return normalize_alignment_text(converted)


def _split_preserving_text(text: str, boundary_re: re.Pattern) -> list[str]:
    units = []
    cursor = 0
    for match in boundary_re.finditer(text):
        stop = match.end()
        if stop > cursor:
            units.append(text[cursor:stop])
        cursor = stop
    if cursor < len(text):
        units.append(text[cursor:])
    return [unit for unit in units if unit]


def _usable_words(words: list[dict]) -> list[dict]:
    usable = []
    for word in words:
        text = str(word.get("text", ""))
        normalized = normalize_alignment_text(text)
        try:
            start_ms = int(round(float(word["start_ms"])))
            end_ms = int(round(float(word["end_ms"])))
        except (KeyError, TypeError, ValueError):
            continue
        probability = word.get("probability")
        try:
            probability = float(probability) if probability is not None else None
        except (TypeError, ValueError):
            probability = None
        if normalized and 0 <= start_ms < end_ms:
            usable.append({
                "text": text,
                "normalized": normalized,
                "phonetic": normalize_phonetic_text(text),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "probability": probability,
            })
    return sorted(usable, key=lambda item: (item["start_ms"], item["end_ms"]))


def _best_word_span(
    target_text: str,
    words: list[dict],
    *,
    first_word: int = 0,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    min_probability: float = DEFAULT_MIN_WORD_PROBABILITY,
) -> dict | None:
    target = normalize_alignment_text(target_text)
    target_phonetic = normalize_phonetic_text(target_text)
    if not target:
        return None
    best = None
    for start in range(first_word, len(words)):
        candidate = ""
        candidate_phonetic = ""
        probabilities = []
        for stop in range(start, len(words)):
            candidate += words[stop]["normalized"]
            candidate_phonetic += words[stop]["phonetic"]
            probability = words[stop]["probability"]
            if probability is not None:
                probabilities.append(probability)
            if len(candidate) > max(len(target) * 2 + 4, len(target) + 12):
                break
            comparisons = []
            for basis, expected, observed in (
                ("orthographic", target, candidate),
                ("phonetic", target_phonetic, candidate_phonetic),
            ):
                if not expected or not observed:
                    continue
                matcher = SequenceMatcher(None, expected, observed)
                matched = sum(block.size for block in matcher.get_matching_blocks())
                comparisons.append((matcher.ratio(), matched / len(expected), basis))
            similarity, coverage, basis = max(
                comparisons,
                key=lambda item: item[0] * 0.65 + item[1] * 0.35,
            )
            average_probability = (
                sum(probabilities) / len(probabilities) if probabilities else 1.0
            )
            score = similarity * 0.65 + coverage * 0.30 + min(average_probability, 1.0) * 0.05
            result = {
                "first_word": start,
                "last_word": stop,
                "similarity": round(similarity, 6),
                "coverage": round(coverage, 6),
                "average_probability": round(average_probability, 6),
                "alignment_basis": basis,
                "score": round(score, 6),
            }
            if best is None or (
                result["score"], -(stop - start)
            ) > (
                best["score"], -(best["last_word"] - best["first_word"])
            ):
                best = result
    if best is None:
        return None
    stricter_similarity = 0.80 if len(target) <= 3 else min_similarity
    probability_supported = (
        best["average_probability"] >= min_probability
        or (
            best["similarity"] >= 0.85
            and best["coverage"] >= 0.85
            and best["average_probability"] >= 0.25
        )
    )
    if (
        best["similarity"] < stricter_similarity
        or best["coverage"] < min_coverage
        or not probability_supported
    ):
        return None
    return best


def _cue_from_span(cue: dict, text: str, words: list[dict], span: dict) -> dict:
    start_ms = max(
        cue["start_ms"], words[span["first_word"]]["start_ms"] - START_PAD_MS,
    )
    end_ms = min(
        cue["end_ms"], words[span["last_word"]]["end_ms"] + END_PAD_MS,
    )
    return {
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "text": text,
        "similarity": span["similarity"],
        "coverage": span["coverage"],
        "average_probability": span["average_probability"],
        "alignment_basis": span["alignment_basis"],
    }


def _align_units(cue: dict, units: list[str], words: list[dict]) -> list[dict] | None:
    aligned = []
    first_word = 0
    for unit in units:
        span = _best_word_span(unit, words, first_word=first_word)
        if span is None:
            return None
        aligned.append(_cue_from_span(cue, unit, words, span))
        first_word = span["last_word"] + 1
    for left, right in zip(aligned, aligned[1:]):
        if left["end_ms"] > right["start_ms"]:
            boundary = (left["end_ms"] + right["start_ms"]) // 2
            left["end_ms"] = boundary
            right["start_ms"] = boundary
    if any(item["start_ms"] >= item["end_ms"] for item in aligned):
        return None
    return aligned


def propose_long_cue(
    cue: dict,
    raw_words: list[dict],
    *,
    max_duration_ms: int = DEFAULT_MAX_DURATION_MS,
) -> dict:
    words = _usable_words(raw_words)
    base = {
        "source_index": cue["index"],
        "original": {
            "start_ms": cue["start_ms"],
            "end_ms": cue["end_ms"],
            "text": cue["text"],
        },
    }
    if cue["end_ms"] - cue["start_ms"] <= max_duration_ms:
        return {**base, "status": "not_long", "reason": "within_limit", "cues": []}
    if not words:
        return {**base, "status": "unresolved", "reason": "no_word_timestamps", "cues": []}

    whole_span = _best_word_span(cue["text"], words)
    if whole_span is not None:
        whole = _cue_from_span(cue, cue["text"], words, whole_span)
        if whole["end_ms"] - whole["start_ms"] <= max_duration_ms:
            return {**base, "status": "aligned", "reason": "whole_text_aligned", "cues": [whole]}

    for boundary_re, reason in (
        (_BOUNDARY_RE, "sentence_boundaries"),
        (_FINE_BOUNDARY_RE, "punctuation_boundaries"),
    ):
        units = _split_preserving_text(cue["text"], boundary_re)
        if len(units) < 2 or "".join(units) != cue["text"]:
            continue
        aligned = _align_units(cue, units, words)
        if aligned and all(
            item["end_ms"] - item["start_ms"] <= max_duration_ms
            for item in aligned
        ):
            return {**base, "status": "split", "reason": reason, "cues": aligned}

    return {
        **base,
        "status": "unresolved",
        "reason": "insufficient_text_or_timing_alignment",
        "cues": [],
    }


def rewrite_srt_with_proposals(content: str, proposals: dict[int, dict]) -> str:
    output = []
    for cue in parse_srt(content):
        proposal = proposals.get(cue["index"])
        if not proposal or proposal.get("status") not in {"aligned", "split"}:
            output.append(cue)
            continue
        replacements = proposal.get("cues", [])
        if not replacements or "".join(item["text"] for item in replacements) != cue["text"]:
            raise ValueError(f"alignment changes subtitle text for cue {cue['index']}")
        previous_end = cue["start_ms"]
        for replacement in replacements:
            if not (
                cue["start_ms"] <= replacement["start_ms"] < replacement["end_ms"] <= cue["end_ms"]
                and replacement["start_ms"] >= previous_end
            ):
                raise ValueError(f"unsafe aligned bounds for cue {cue['index']}")
            output.append({
                "index": 0,
                "start_ms": replacement["start_ms"],
                "end_ms": replacement["end_ms"],
                "text": replacement["text"],
            })
            previous_end = replacement["end_ms"]
    return render_srt(output)


def collect_window_words(
    model,
    audio_samples,
    sampling_rate: int,
    cue: dict,
    *,
    window_pad_ms: int = WINDOW_PAD_OPTIONS_MS[0],
) -> tuple[list[dict], str]:
    if sampling_rate <= 0:
        raise ValueError("sampling_rate must be positive")
    audio_duration_ms = round(len(audio_samples) * 1000 / sampling_rate)
    if window_pad_ms < 0:
        raise ValueError("window_pad_ms must not be negative")
    window_start_ms = max(0, cue["start_ms"] - window_pad_ms)
    window_end_ms = min(audio_duration_ms, cue["end_ms"] + window_pad_ms)
    start_sample = round(window_start_ms * sampling_rate / 1000)
    end_sample = round(window_end_ms * sampling_rate / 1000)
    if start_sample >= end_sample:
        return [], ""
    segments, _ = model.transcribe(
        audio_samples[start_sample:end_sample],
        language="ja",
        beam_size=5,
        word_timestamps=True,
        vad_filter=False,
        condition_on_previous_text=False,
        no_repeat_ngram_size=5,
        repetition_penalty=1.5,
        temperature=0.0,
        compression_ratio_threshold=2.0,
        log_prob_threshold=-1.0,
    )
    words = []
    transcript = []
    for segment in segments:
        segment_text = str(getattr(segment, "text", "")).strip()
        if segment_text:
            transcript.append(segment_text)
        for word in getattr(segment, "words", None) or []:
            start = getattr(word, "start", None)
            end = getattr(word, "end", None)
            if start is None or end is None:
                continue
            words.append({
                "start_ms": window_start_ms + round(float(start) * 1000),
                "end_ms": window_start_ms + round(float(end) * 1000),
                "text": str(getattr(word, "word", "")).strip(),
                "probability": getattr(word, "probability", None),
            })
    return words, " ".join(transcript)


def build_alignment_report(
    srt_content: str,
    model,
    audio_samples,
    *,
    sampling_rate: int = 16_000,
    max_duration_ms: int = DEFAULT_MAX_DURATION_MS,
) -> dict:
    source_cues = parse_srt(srt_content)
    items = []
    for cue in source_cues:
        if cue["end_ms"] - cue["start_ms"] <= max_duration_ms:
            continue
        attempts = []
        for window_pad_ms in WINDOW_PAD_OPTIONS_MS:
            words, transcript = collect_window_words(
                model,
                audio_samples,
                sampling_rate,
                cue,
                window_pad_ms=window_pad_ms,
            )
            proposal = propose_long_cue(
                cue, words, max_duration_ms=max_duration_ms,
            )
            attempts.append({
                "window_pad_ms": window_pad_ms,
                "alignment_transcript": transcript,
                "words": words,
                "proposal": proposal,
            })
        resolved = [
            attempt for attempt in attempts
            if attempt["proposal"]["status"] in {"aligned", "split"}
        ]
        if resolved:
            def attempt_score(attempt):
                cues = attempt["proposal"]["cues"]
                return min(
                    cue_result["similarity"] * 0.55
                    + cue_result["coverage"] * 0.35
                    + min(cue_result["average_probability"], 1.0) * 0.10
                    for cue_result in cues
                )

            chosen = max(resolved, key=attempt_score)
        else:
            chosen = max(attempts, key=lambda attempt: len(attempt["words"]))
        proposal = chosen["proposal"]
        consistency_shift_ms = None
        if len(resolved) > 1:
            first_cues = resolved[0]["proposal"]["cues"]
            second_cues = resolved[1]["proposal"]["cues"]
            if len(first_cues) == len(second_cues):
                consistency_shift_ms = max(
                    abs(left[boundary] - right[boundary])
                    for left, right in zip(first_cues, second_cues)
                    for boundary in ("start_ms", "end_ms")
                )
                consensus_cues = []
                for selected, left, right in zip(
                    proposal["cues"], first_cues, second_cues,
                ):
                    if left["text"] != right["text"]:
                        consensus_cues = []
                        break
                    consensus = {
                        **selected,
                        "start_ms": max(left["start_ms"], right["start_ms"]),
                        "end_ms": min(left["end_ms"], right["end_ms"]),
                    }
                    if consensus["start_ms"] >= consensus["end_ms"]:
                        consensus_cues = []
                        break
                    consensus_cues.append(consensus)
                if consensus_cues:
                    proposal = {
                        **proposal,
                        "reason": proposal["reason"] + "_consensus",
                        "cues": consensus_cues,
                    }
            else:
                consistency_shift_ms = max_duration_ms + 1
        items.append({
            **proposal,
            "alignment_transcript": chosen["alignment_transcript"],
            "words": chosen["words"],
            "selected_window_pad_ms": chosen["window_pad_ms"],
            "consistency_shift_ms": consistency_shift_ms,
            "review_required": (
                proposal["status"] == "split"
                or len(resolved) != len(WINDOW_PAD_OPTIONS_MS)
                or consistency_shift_ms is not None and consistency_shift_ms > 1_500
            ),
            "attempts": attempts,
        })
    return {
        "schema_version": 1,
        "artifact_type": "long_cue_alignment",
        "status": "complete",
        "max_duration_ms": max_duration_ms,
        "source_cue_count": len(source_cues),
        "items": items,
        "summary": {
            "detected": len(items),
            "aligned": sum(item["status"] == "aligned" for item in items),
            "split": sum(item["status"] == "split" for item in items),
            "unresolved": sum(item["status"] == "unresolved" for item in items),
        },
    }
