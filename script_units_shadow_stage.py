# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional post-pipeline entrypoint for publisher-script structure analysis."""

from output_layout import output_file, prepare_output
import json
import hashlib
import os

from ensemble_transcribe import (
    OUTPUT_DIR,
    check_script_mapping,
    collect_audio_files,
    run_script_units_shadow,
    script_unit_source_is_current,
)
from asr_evidence import new_evidence_document
from pipeline_cache import file_sha256
from script_alignment import write_script_alignment_shadow


def _read_json_snapshot(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    return json.loads(raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest()


def run_script_alignment_shadow(
    audio_files,
    script_unit_results,
    output_dir=OUTPUT_DIR,
    script_source_is_current=None,
    fail_fast=False,
):
    results = {}
    for audio_path in audio_files:
        base = os.path.splitext(os.path.basename(audio_path))[0]
        if base not in script_unit_results:
            continue
        units_path = output_file(output_dir, f"{base}_script_units.json")
        timeline_path = output_file(output_dir, f"{base}_fusion_timeline.json")
        output_path = output_file(output_dir, f"{base}_script_alignment.json")
        if not os.path.isfile(timeline_path):
            if fail_fast:
                raise FileNotFoundError(
                    f"Script alignment {base}: fusion timeline missing"
                )
            print(f"Script alignment {base}: fusion timeline missing; skipped")
            continue
        try:
            units, units_sha = _read_json_snapshot(units_path)
            timeline, timeline_sha = _read_json_snapshot(timeline_path)
            if units != script_unit_results[base]:
                raise ValueError("script units result differs from the committed sidecar")
            expected_audio_source = new_evidence_document(audio_path)["source"]
            if timeline.get("source") != expected_audio_source:
                raise ValueError("fusion timeline is stale for the current audio")

            def inputs_are_current():
                return (
                    file_sha256(units_path) == units_sha
                    and file_sha256(timeline_path) == timeline_sha
                    and new_evidence_document(audio_path)["source"] == expected_audio_source
                    and (
                        script_source_is_current is None
                        or script_source_is_current(base)
                    )
                )

            status, artifact = write_script_alignment_shadow(
                output_path,
                units,
                timeline,
                input_is_current=inputs_are_current,
            )
            results[base] = artifact
            print(f"Script alignment {base}: {status}")
        except Exception as exc:
            if fail_fast:
                raise RuntimeError(f"Script alignment {base} failed") from exc
            print(f"Script alignment {base} failed; continuing: {exc}")
    return results


def main():
    prepare_output(OUTPUT_DIR)
    review_filter_required = os.environ.get(
        "ENABLE_SCRIPT_REVIEW_FILTER", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    audio_files = collect_audio_files()
    if not audio_files:
        if review_filter_required:
            raise FileNotFoundError("Script review filter requires active audio")
        print("Script-units shadow: no audio files; skipped")
        return
    check_script_mapping(force_script_units=review_filter_required)
    results = run_script_units_shadow(
        audio_files, force=review_filter_required,
    )
    print(f"Script-units shadow: {len(results)} artifact(s) current")
    if not results:
        print("Script-units shadow: no non-empty scripts; skipped")
        return
    if review_filter_required and len(results) != len(audio_files):
        raise RuntimeError("Script review filter requires units for every active audio")
    alignments = run_script_alignment_shadow(
        audio_files, results,
        script_source_is_current=script_unit_source_is_current,
        fail_fast=review_filter_required,
    )
    print(f"Script alignment shadow: {len(alignments)} artifact(s) current")
    if review_filter_required and len(alignments) != len(audio_files):
        raise RuntimeError("Script review filter requires alignment for every active audio")


if __name__ == "__main__":
    main()
