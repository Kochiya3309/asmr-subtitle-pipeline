# ASMR Subtitle Pipeline — Developer Guide (Current Development Tree)

English | [简体中文](developer-guide.zh-CN.md)

This guide describes the current implementation in the development tree. It is
not a release-history document; see [CHANGELOG.md](../CHANGELOG.md) for
released and unreleased changes. For installation, user tasks, and setting
semantics, use the [Beginner's guide](getting-started.md), [Advanced
operations](advanced-operations.md), and [Configuration reference](configuration.md).

## Project scope

The pipeline produces Japanese–Simplified-Chinese subtitles from Japanese ASMR
audio. ASR, preprocessing, evidence collection, rescue transcription, and
deterministic timeline work are local. LLM calls operate on required text;
optional search can send generated queries to configured providers. Scripts are
supporting evidence, not spoken transcripts.

## Pipeline orchestration

`run_all.py` loads `.env`, snapshots active input paths, runs formal stages in
order, and passes scoped environment variables to child processes.

| Stage | Entry point | Primary input → output | Rerun / failure boundary |
| --- | --- | --- | --- |
| STEP-1 | `video_inputs.py` | `video/` → managed FLAC and source manifest | Source, selected stream, or extraction contract changes rebuild it; a missing audio stream or basename conflict stops the run. |
| STEP0 | `match_scripts.py` | Audio and scripts → mapping and optional split scripts | Audio/script hash changes invalidate confirmation; an unconfirmed or invalid mapping fails closed. |
| STEP1 | `ensemble_transcribe.py` | Active audio → ASR, evidence, fusion, and timing artifacts | Input, model, configuration, or generation fingerprint changes rebuild affected artifacts; no valid ensemble SRT stops downstream stages. |
| STEP2 | `review_japanese.py` | Ensemble SRT → reviewed SRT | Input or review configuration changes rebuild it; in script mode only mismatches are reviewed. |
| STEP3 | `translate.py` | Ensemble/reviewed SRT → bilingual `review/zh.srt` | Source or translation configuration changes rebuild it; an invalid active-input snapshot stops the stage. |
| STEP3.25 | `script_units_shadow_stage.py` | Confirmed script and timeline → units and alignment artifacts | Formal filtering fails if its required artifacts are invalid; trailing shadow analysis only warns. |
| STEP3.5 | `human_review_gate.py` | Bilingual draft and audio → reviewed SRT and completion record | Current inputs and review record must match; an unsubmitted review returns resumable exit code 75. |
| STEP4 | `review_final.py` | Current bilingual source → final SRT and manifest | Source or final-review configuration changes rebuild it; missing or invalid upstream input stops the run. |
| STEP5 | `validate_final.py` | Final SRT → rule-validation report | Severe findings make the run fail; with STEP6 enabled, export is attempted before that failure is returned. |
| STEP6 | `strip_japanese.py` | Final SRT → Chinese-only final SRT | It regenerates from the current final; extraction failure blocks transfer exports. |
| STEP7 | `export_final_srt.py` | Final SRT → `_transfer/final/` copy | It depends on STEP4; a missing final causes export failure. |
| STEP8 | `export_cn_only_srt.py` | Chinese-only final SRT → `_transfer/cn_only/` copy | It depends on STEP6; a missing Chinese-only final causes export failure. |

Formal downstream stages receive the same active-input snapshot. Video-derived
FLAC inputs join that snapshot without being copied into `audio/`; historical
output jobs are excluded from a new run.

## Stage contracts and failure boundaries

- STEP0 requires both script mode and its stage switch. Mapping confirmation is
  bound to audio and script hashes; non-interactive runs fail closed unless
  automatic verification is explicitly enabled.
- STEP1 preserves candidate evidence. Matched scripts use script text with the
  large-v3 timeline; mismatches use ordinary fusion and receive STEP2 review.
  Rescue is bounded to suspicious windows.
- STEP3 writes bilingual `review/zh.srt`. Each later final-review cue requires
  exactly four physical lines: index, timecode, one-line Japanese, and one-line
  Simplified Chinese.
- STEP3.5 returns exit code 75 for an unsubmitted human review; `run_all.py`
  treats it as a resumable pause. Current review inputs and outputs must match
  before STEP4 proceeds.
- STEP5 reports severe rule failures. When STEP6 is enabled, the pipeline still
  attempts Chinese-only output and ends with failure status after the export path.
  A STEP6 extraction failure blocks transfer exports.
- STEP7 depends on STEP4; STEP8 depends on STEP6. Neither changes canonical
  final subtitles.

## Module responsibilities

| Area | Modules |
| --- | --- |
| Orchestration and shared services | `run_all.py`, `common.py`, `pipeline_inputs.py`, `pipeline_cache.py` |
| ASR, evidence, and timelines | `ensemble_transcribe.py`, `asr_evidence.py`, `asr_rescue.py`, `asr_timeline.py`, `long_cue_alignment.py` |
| Scripts | `match_scripts.py`, `script_text_io.py`, `script_mapping_state.py`, `script_units.py`, `script_alignment.py`, `script_units_shadow_stage.py` |
| Review | `review_japanese.py`, `translate.py`, `human_review.py`, `review_app.py`, `human_review_gate.py`, `review_final.py`, `validate_final.py`, `strip_japanese.py` |
| Optional hard-subtitle delivery | `burn_subtitles.py` compatibility entry point; `burn_subtitles_cli.py` interaction and orchestration; `burn_subtitles_style.py` style model and validation; `burn_subtitles_engine.py` FFmpeg probing, benchmarking, and encoding |

## Configuration sources

`run_all.py` loads `.env`; an existing process environment variable takes
precedence. The user-facing meanings, defaults, dependencies, and privacy
effects are owned by the [Configuration reference](configuration.md).

`RESCUE_WINDOW_SECONDS`, `RESCUE_OVERLAP_SECONDS`, `INITIAL_PROMPT`,
`HOTWORDS`, and `VAD_*` are currently set by `run_all.py` and injected into
child processes. Adding them only to `.env` does not override active values.

## Critical implementation contracts

### Dual ASR, evidence, and timing

Whenever caches need rebuilding, STEP1 runs `Systran/faster-whisper-large-v3`
and `mobiuslabsgmbh/faster-whisper-large-v3-turbo`. Scriptless output fuses both
models. A fully matched script preserves script text while using the large-v3
timeline; Turbo remains independent evidence. The old behavior of skipping Turbo
because a script exists must not return.

Hallucination quarantine removes only high-precision fixed templates from
automatic fusion input; original candidates remain in evidence artifacts for
audit. Rescue first runs large-v3 without VAD in suspicious windows and adds a
Turbo comparison only when V3 detects possible speech. Do not turn this into a
full-audio Turbo scan without VAD.

With `ENABLE_DETERMINISTIC_TIMELINE=1`, the LLM returns text selections and
evidence IDs while `asr_timeline.py` owns order, overlap, and boundaries. Text
without valid evidence cannot enter the formal timeline. Long-cue `apply` mode
commits only locally valid boundary plans; `shadow` mode writes an audit report.

### Human review and script processing

The review page is bound to `127.0.0.1`, exposes context and candidate
differences, and allows edits to text and cue timing. Submission writes
`review/human_reviewed.srt` and `review/human_review.json`; their completion
record is bound to current input and output.

Script splitting preserves dialogue, action, psychology, headings, and
decorative separators. LLM work is limited to classification and textual
judgment; deterministic code keeps original spans, segmentation boundaries, and
timelines. Formal script review filtering requires script mode, human review,
and `ENABLE_SCRIPT_REVIEW_FILTER`; end-of-pipeline shadow analysis is
non-blocking.

## Output, caches, and migration

```text
output/
  <audio>/
    input/     # video-derived FLAC and source manifest when applicable
    asr/       # preprocessed audio, v3/turbo SRT, manifests
    evidence/  # candidates, alignment, windows, decisions, rescue, manifest
    review/    # fusion, review, translation, human-review, timing artifacts
    final/     # <audio>_final.srt, final_manifest.json, optional Chinese-only SRT
    script/    # optional units and alignment artifacts
  _shared/     # log, script mappings, split scripts, migration backups
  _transfer/   # derived batch-transfer copies
```

`output_layout.py` owns path resolution. Cache validity uses content hashes,
configuration and generation fingerprints, artifact type, schema version, and
completion state. Stale rebuildable artifacts are backed up before replacement;
unverifiable provenance stops downstream processing. Migration preserves known
artifact content and timestamps, retains unknown user files, and refuses
conflicting targets. User-facing migration and export procedures belong in
[Advanced operations](advanced-operations.md).

### Artifact identities

| Category | Typical artifacts |
| --- | --- |
| Raw ASR | `asr/v3.srt`, `asr/turbo.srt`, and manifests |
| Evidence | `evidence/candidates.json`, `alignment.json`, `windows.json`, `decisions.json`, `rescue_windows.json` |
| Fusion | `review/fusion_timeline.json`, `review/ensemble.srt`, fusion manifests |
| Downstream subtitles | `review/reviewed.srt`, `review/zh.srt`, `review/human_reviewed.srt`, final SRT files |
| Human review | `review/human_review.json` |
| Logs | `_shared/pipeline.log` |

## Privacy and external services

Audio is not uploaded to LLM or search providers. Japanese and Chinese subtitle
text can be sent to the configured OpenAI-compatible LLM. With `ENABLE_SEARCH=0`,
Tavily and Exa are not called. The review server binds only to `127.0.0.1`.

## Troubleshooting index

| Symptom | First check | Then decide |
| --- | --- | --- |
| A run stops or exits nonzero | `output/_shared/pipeline.log`; identify the first failed stage and active input | Correct that stage's reported cause, then rerun; do not delete caches first. |
| Final review rejects a bilingual SRT | `review/ensemble.srt`, `review/reviewed.srt`, and `review/zh.srt` for the same cue | Confirm the four-physical-line contract before changing downstream files. |
| A cache unexpectedly rebuilds | The relevant manifest, source content, and configuration or generation fingerprint | Determine which recorded dependency changed; cache existence alone is not validity. |
| Script mapping or human review blocks progress | `_shared/script_mapping.json`, its confirmation state, or `review/human_review.json` | Confirm current inputs match the record; an unsubmitted review is the resumable exit-75 pause. |
| Video input stops before ASR | The video's first audio stream and `input/source_manifest.json` for that job | Resolve a missing stream or basename collision before rerunning. |
| Transfer copies are missing | `final/` canonical SRTs and STEP4/STEP6 plus STEP7/STEP8 switches | Restore the missing upstream final, then use the export helpers if standalone export is needed. |

## Development and validation

Use the project virtual environment for Python checks. Before a commit, compile
relevant Python files and run `git diff --check`; run focused internal tests when
they exist. Runtime validation must use isolated audio and output directories.
Keep temporary tests and diagnostics under `_cache/`; do not expose keys in logs,
fixtures, or documentation.

The pre-release consistency check includes paired language navigation, matching
commands and configuration values, `.env.example` without real keys, dependency
agreement, clean secret/media exclusions, and relevant syntax or focused tests.

## License

GNU General Public License v3.0 or later (`GPL-3.0-or-later`). See
[LICENSE](../LICENSE).
