# SPDX-License-Identifier: GPL-3.0-or-later
"""ID-only script classification contract that never lets an LLM rewrite text."""

from __future__ import annotations

import hashlib
import json
import os

from asr_evidence import canonical_fingerprint, write_json_atomic
from script_text_io import read_script_text_exact


SCRIPT_UNITS_VERSION = "script-units-v2"
SCRIPT_UNITS_PROMPT_VERSION = "2026-08-30.1"
SCRIPT_UNITS_BATCH_MAX_LINES = 250
SCRIPT_UNITS_BATCH_MAX_CHARS = 24_000
ALLOWED_TYPES = {"dialogue", "action", "psychology", "separator", "unknown"}

SCRIPT_UNITS_SYSTEM_PROMPT = """You classify publisher-provided Japanese scripts.
Return exactly one submit_script_units tool call. Cover every supplied line ID
exactly once with non-overlapping contiguous ranges. Classify each range as:
dialogue (any text intended to be audible, including narration, voice-over,
monologue, voiced inner thoughts, ad-libs, cries, and non-lexical vocalization),
action (stage direction or physical action not spoken aloud), psychology
(explicitly non-spoken inner-state notes or character-direction metadata),
separator (title, section label, decorative divider, or blank line), or unknown.
Classify by whether the text is intended to be heard, not by whether it is
written in first person or in narrative prose. A continuous first-person
narration must be dialogue; do not label it psychology merely because it
describes memories, thoughts, feelings, or motivation. Return line IDs, type,
and confidence only.
Never copy, rewrite, summarize, translate, or quote any script text."""

SCRIPT_UNITS_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_script_units",
        "description": "Classify contiguous ranges by line ID. Never return script text.",
        "parameters": {
            "type": "object",
            "properties": {
                "units": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start_line_id": {"type": "string"},
                            "end_line_id": {"type": "string"},
                            "type": {"type": "string", "enum": sorted(ALLOWED_TYPES)},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": [
                            "start_line_id", "end_line_id", "type", "confidence",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["units"],
            "additionalProperties": False,
        },
    },
}


def build_line_catalog(script_text: str) -> dict:
    if not isinstance(script_text, str):
        raise TypeError("script_text must be a string")
    physical_lines = script_text.splitlines(keepends=True)
    if script_text and not physical_lines:
        physical_lines = [script_text]
    lines = []
    offset = 0
    for number, exact in enumerate(physical_lines, start=1):
        line_id = f"L{number:06d}"
        display = exact.rstrip("\r\n")
        lines.append({
            "line_id": line_id,
            "line_number": number,
            "start_offset": offset,
            "end_offset": offset + len(exact),
            "display_text": display,
            "line_fingerprint": hashlib.sha256(exact.encode("utf-8")).hexdigest(),
        })
        offset += len(exact)
    if offset != len(script_text):
        raise ValueError("script line catalog did not preserve the full source")
    return {
        "contract_version": SCRIPT_UNITS_VERSION,
        "source_text_fingerprint": hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
        "source_length": len(script_text),
        "lines": lines,
    }


def build_classification_payload(catalog: dict) -> dict:
    return {
        "contract_version": catalog["contract_version"],
        "lines": [
            {"line_id": line["line_id"], "text": line["display_text"]}
            for line in catalog["lines"]
        ],
    }


def parse_script_unit_ranges(args: dict, catalog: dict, script_text: str) -> dict:
    if not isinstance(script_text, str):
        raise TypeError("script_text must be a string")
    if catalog != build_line_catalog(script_text):
        raise ValueError("script line catalog does not match the source text")
    if not isinstance(args, dict) or set(args) != {"units"}:
        raise ValueError("invalid script unit response fields")
    rows = args["units"]
    if not isinstance(rows, list):
        raise ValueError("script units must be an array")
    lines = catalog["lines"]
    by_id = {line["line_id"]: position for position, line in enumerate(lines)}
    covered = set()
    units = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "start_line_id", "end_line_id", "type", "confidence",
        }:
            raise ValueError("invalid script unit fields")
        start_id = row["start_line_id"]
        end_id = row["end_line_id"]
        if not isinstance(start_id, str) or not isinstance(end_id, str):
            raise ValueError("invalid script line ID")
        if start_id not in by_id or end_id not in by_id:
            raise ValueError("unknown script line ID")
        start_pos, end_pos = by_id[start_id], by_id[end_id]
        if end_pos < start_pos:
            raise ValueError("script unit range is reversed")
        positions = set(range(start_pos, end_pos + 1))
        if covered & positions:
            raise ValueError("script unit ranges overlap")
        unit_type = row["type"]
        confidence = row["confidence"]
        if unit_type not in ALLOWED_TYPES:
            raise ValueError("invalid script unit type")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("invalid script unit confidence")
        if not 0 <= float(confidence) <= 1:
            raise ValueError("invalid script unit metadata")
        unit_ranges = (
            [(position, position) for position in range(start_pos, end_pos + 1)]
            if unit_type == "dialogue"
            else [(start_pos, end_pos)]
        )
        for unit_start, unit_end in unit_ranges:
            start_offset = lines[unit_start]["start_offset"]
            end_offset = lines[unit_end]["end_offset"]
            units.append({
                "start_line_id": lines[unit_start]["line_id"],
                "end_line_id": lines[unit_end]["line_id"],
                "line_ids": [
                    lines[position]["line_id"]
                    for position in range(unit_start, unit_end + 1)
                ],
                "type": unit_type,
                "confidence": round(float(confidence), 6),
                "start_offset": start_offset,
                "end_offset": end_offset,
                "text_exact": script_text[start_offset:end_offset],
            })
        covered |= positions
    if covered != set(range(len(lines))):
        raise ValueError("script unit response does not cover every source line")
    units.sort(key=lambda unit: unit["start_offset"])
    for ordinal, unit in enumerate(units, start=1):
        unit["script_unit_id"] = f"SU{ordinal:06d}"
    artifact_body = {
        "schema_version": 1,
        "artifact_type": "script_units",
        "status": "complete",
        "contract_version": SCRIPT_UNITS_VERSION,
        "source_text_fingerprint": catalog["source_text_fingerprint"],
        "source_length": catalog["source_length"],
        "units": units,
    }
    return {
        **artifact_body,
        "artifact_fingerprint": canonical_fingerprint(artifact_body),
    }


def script_units_generation_fingerprint(llm_config: dict) -> str:
    if not isinstance(llm_config, dict):
        raise TypeError("llm_config must be an object")
    return canonical_fingerprint({
        "contract_version": SCRIPT_UNITS_VERSION,
        "prompt_version": SCRIPT_UNITS_PROMPT_VERSION,
        "system_prompt": SCRIPT_UNITS_SYSTEM_PROMPT,
        "tool_schema": SCRIPT_UNITS_TOOL,
        "batch_max_lines": SCRIPT_UNITS_BATCH_MAX_LINES,
        "batch_max_chars": SCRIPT_UNITS_BATCH_MAX_CHARS,
        "temperature": 0.25,
        "llm": llm_config,
    })


def build_script_unit_batches(catalog: dict, script_text: str) -> list[dict]:
    if catalog != build_line_catalog(script_text):
        raise ValueError("script line catalog does not match the source text")
    lines = catalog["lines"]
    batches = []
    start = 0
    while start < len(lines):
        end = start
        start_offset = lines[start]["start_offset"]
        while end < len(lines):
            next_end_offset = lines[end]["end_offset"]
            next_line_count = end - start + 1
            next_char_count = next_end_offset - start_offset
            if end > start and (
                next_line_count > SCRIPT_UNITS_BATCH_MAX_LINES
                or next_char_count > SCRIPT_UNITS_BATCH_MAX_CHARS
            ):
                break
            end += 1
            if (
                next_line_count >= SCRIPT_UNITS_BATCH_MAX_LINES
                or next_char_count >= SCRIPT_UNITS_BATCH_MAX_CHARS
            ):
                break
        end_offset = lines[end - 1]["end_offset"]
        chunk_text = script_text[start_offset:end_offset]
        batches.append({
            "global_start_position": start,
            "script_text": chunk_text,
            "catalog": build_line_catalog(chunk_text),
            "oversized": len(chunk_text) > SCRIPT_UNITS_BATCH_MAX_CHARS,
        })
        start = end
    return batches


def bind_script_units_generation(artifact: dict, generation_fingerprint: str) -> dict:
    if not isinstance(generation_fingerprint, str) or not generation_fingerprint:
        raise ValueError("generation fingerprint is required")
    body = dict(artifact)
    body.pop("artifact_fingerprint", None)
    body["generation_fingerprint"] = generation_fingerprint
    return {**body, "artifact_fingerprint": canonical_fingerprint(body)}


def validate_script_units_artifact(
    artifact: dict,
    script_text: str,
    generation_fingerprint: str,
) -> dict:
    if not isinstance(artifact, dict):
        raise ValueError("script units artifact must be an object")
    required = {
        "schema_version", "artifact_type", "status", "contract_version",
        "source_text_fingerprint", "source_length", "units",
        "generation_fingerprint", "artifact_fingerprint",
    }
    if set(artifact) != required:
        raise ValueError("invalid script units artifact fields")
    rows = []
    for unit in artifact.get("units", []):
        if not isinstance(unit, dict) or set(unit) != {
            "script_unit_id", "start_line_id", "end_line_id", "line_ids",
            "type", "confidence", "start_offset", "end_offset", "text_exact",
        }:
            raise ValueError("invalid stored script unit fields")
        rows.append({
            "start_line_id": unit["start_line_id"],
            "end_line_id": unit["end_line_id"],
            "type": unit["type"],
            "confidence": unit["confidence"],
        })
    expected = bind_script_units_generation(
        parse_script_unit_ranges(
            {"units": rows}, build_line_catalog(script_text), script_text,
        ),
        generation_fingerprint,
    )
    if artifact != expected:
        raise ValueError("script units artifact is stale or malformed")
    return artifact


def load_current_script_units(
    output_path: str,
    script_text: str,
    generation_fingerprint: str,
) -> dict | None:
    if not os.path.isfile(output_path):
        return None
    try:
        with open(output_path, "r", encoding="utf-8-sig") as handle:
            artifact = json.load(handle)
        return validate_script_units_artifact(
            artifact, script_text, generation_fingerprint,
        )
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def generate_script_units_shadow(
    client,
    call_llm,
    script_text: str,
    output_path: str,
    llm_config: dict,
    log_prefix: str = "",
    input_is_current=None,
) -> tuple[str, dict]:
    """Generate/cache a shadow artifact without changing subtitle text or timing."""
    catalog = build_line_catalog(script_text)
    generation_fingerprint = script_units_generation_fingerprint(llm_config)
    cached = load_current_script_units(
        output_path, script_text, generation_fingerprint,
    )
    if cached is not None:
        if input_is_current is not None and not input_is_current():
            raise ValueError("script source changed during shadow processing")
        return "cached", cached

    if not catalog["lines"]:
        parsed = parse_script_unit_ranges({"units": []}, catalog, script_text)
    else:
        batches = build_script_unit_batches(catalog, script_text)
        merged_rows = []
        for batch_number, batch in enumerate(batches, start=1):
            batch_catalog = batch["catalog"]
            batch_text = batch["script_text"]
            if batch["oversized"]:
                line = catalog["lines"][batch["global_start_position"]]
                merged_rows.append({
                    "start_line_id": line["line_id"],
                    "end_line_id": line["line_id"],
                    "type": "unknown",
                    "confidence": 0.0,
                })
                continue
            payload = json.dumps(
                build_classification_payload(batch_catalog), ensure_ascii=False,
                separators=(",", ":"),
            )
            batch_artifact = call_llm(
                client,
                SCRIPT_UNITS_SYSTEM_PROMPT,
                payload,
                retries=1,
                verbose=True,
                enable_search=False,
                output_tool=SCRIPT_UNITS_TOOL,
                output_tool_parser=lambda args, bc=batch_catalog, bt=batch_text: (
                    parse_script_unit_ranges(args, bc, bt)
                ),
                stream=False,
                log_prefix=(
                    f"{log_prefix}[batch {batch_number}/{len(batches)}] "
                ),
            )
            if (
                not isinstance(batch_artifact, dict)
                or batch_artifact.get("artifact_type") != "script_units"
            ):
                raise ValueError("LLM did not return a valid script units tool call")
            position_offset = batch["global_start_position"]
            for unit in batch_artifact["units"]:
                local_start = int(unit["start_line_id"][1:]) - 1
                local_end = int(unit["end_line_id"][1:]) - 1
                merged_rows.append({
                    "start_line_id": catalog["lines"][position_offset + local_start]["line_id"],
                    "end_line_id": catalog["lines"][position_offset + local_end]["line_id"],
                    "type": unit["type"],
                    "confidence": unit["confidence"],
                })
        parsed = parse_script_unit_ranges(
            {"units": merged_rows}, catalog, script_text,
        )

    artifact = bind_script_units_generation(parsed, generation_fingerprint)
    validate_script_units_artifact(artifact, script_text, generation_fingerprint)
    if input_is_current is not None and not input_is_current():
        raise ValueError("script source changed during shadow processing")
    write_json_atomic(output_path, artifact)
    return "generated", artifact
