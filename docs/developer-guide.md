# ASMR Subtitle Pipeline — Developer Guide (V4.0.1)

English | [简体中文](developer-guide.zh-CN.md)

This document describes the actual implementation in the current development tree. For user-facing installation and configuration instructions, see [README.md](../README.md). The Chinese documentation entry point is [README.zh-CN.md](../README.zh-CN.md).

## Project scope

This project converts Japanese ASMR audio into bilingual Japanese–Chinese SRT subtitles. Local `large-v3` and `large-v3-turbo` models perform ASR, while an OpenAI-compatible LLM handles text fusion, Japanese review, translation, and final review. Local deterministic code handles evidence binding, timelines, caching, rule validation, and the human-review gate.

Design principles:

- ASR candidates, confidence metrics, provenance, and rescue results remain auditable. Suspicious text is quarantined before evidence is discarded.
- The LLM may judge text but must not invent timestamps. When the deterministic timeline is enabled, local code alone generates boundaries.
- Every reusable artifact must be bound to its input content and generation configuration. File existence alone does not make a cache valid.
- A script is supporting evidence, not a verbatim transcript. Action notes, psychological descriptions, and decorative symbols must be preserved but not treated as spoken dialogue.
- Human review must expose context, Japanese, romaji, Chinese, and candidate differences, and must allow direct editing of time boundaries.

## Pipeline orchestration

`run_all.py` is the unified entry point. The current execution order is:

| Stage | Entry point | Conditions and responsibilities |
| --- | --- | --- |
| STEP0 | `match_scripts.py` | Runs when `ENABLE_SCRIPT=1` and `STEP0_MATCH_SCRIPTS=1`; matches, splits, and confirms script mappings |
| STEP1 | `ensemble_transcribe.py` | Preprocessing, dual ASR, evidence/rescue, fusion, and optional long-cue timing |
| STEP2 | `review_japanese.py` | Runs without a script; in script mode, handles only mismatches and skips fully matched files |
| STEP3 | `translate.py` | Translation and per-segment review; outputs bilingual SRT files |
| STEP3.25 | `script_units_shadow_stage.py` | When formal script review filtering is enabled, classifies script structure and aligns it to the timeline |
| STEP3.5 | `human_review_gate.py` | Starts a local browser page when human review is enabled; exits with code 75 to pause if the review is not submitted |
| STEP4 | `review_final.py` | Reads the correct upstream source and performs a full-document bilingual final review |
| STEP5 | `validate_final.py` | Scans for residual content and rule violations; currently does not perform complete timeline validation |
| STEP6 | `strip_japanese.py` | Generates optional Chinese-only subtitles |

After all formal stages finish, `ENABLE_SCRIPT_UNITS_SHADOW=1` can run the non-blocking script-structure side path. A timeout in this path does not fail already completed formal subtitles.

## Complete stage reference

The following reference follows the orchestration in `run_all.py`. Except for STEP3.5, where “waiting for human submission” uses exit code 75 as a resumable pause, a nonzero exit from a formal stage stops the current pipeline run. On the next run, each stage validates its own cache before resuming.

### STEP0: Script matching and splitting

- **Execution conditions:** `ENABLE_SCRIPT=1` and `STEP0_MATCH_SCRIPTS=1`. The entire stage is skipped when script mode is disabled.
- **Inputs:** Active audio in `AUDIO_DIR`, text scripts in `SCRIPT_DIR`, and any existing mappings and confirmation markers.
- **Processing and external services:** Same-name and fuzzy filename matching run first. Local rules are preferred when splitting a merged script; the LLM is used when rules cannot split it reliably. The LLM may also match unresolved files from filenames. Audio content is not uploaded, but filenames and script text may be sent.
- **Outputs:** `script_mapping.json`, `script_mapping.verified`, and `scripts_split/*.txt` when splitting is required.
- **Caching and invalidation:** The confirmation marker is bound to the mapping and the fingerprints of active audio and script content. Changes invalidate the old confirmation and trigger matching again. `SCRIPT_FORCE_RESPLIT=1` forces split artifacts to be rebuilt.
- **Stop conditions:** Missing audio, duplicate base names, an invalid mapping, or user rejection fails the stage. A non-interactive terminal refuses to impersonate human confirmation by default; only `SCRIPT_AUTO_VERIFY=1` writes the confirmation marker automatically.

### STEP1: Preprocessing, dual ASR, evidence, and fusion

- **Execution conditions:** `STEP1_ENSEMBLE=1`, enabled by default. With or without scripts, both ASR models run whenever their caches are invalid.
- **Inputs:** Active audio; script mode also reads the confirmed `script_mapping.json` and its corresponding scripts.
- **Processing and external services:** Optional audio preprocessing, serial GPU transcription with large-v3 and large-v3-turbo, hallucination quarantine, targeted rescue, and timeline calculation run locally. The LLM then selects or fuses text in parallel. For a fully matched script, the pipeline uses the large-v3 timeline with script text; a mismatch falls back to ordinary dual-model fusion. When search is enabled, fusion may send queries to Tavily/Exa, but audio always remains local.
- **Outputs:** Preprocessed audio and its manifest, `*_v3.srt`, `*_turbo.srt`, ASR evidence/window/decision/rescue JSON files, `*_fusion_timeline.json`, `*_ensemble.srt`, fusion manifests, optional `*_long_cue_alignment.json`, and `script_mismatch.json` for the current run.
- **Caching and invalidation:** Preprocessing, ASR, evidence, and fusion are independently bound to the source audio, actual input audio, models/parameters, prompt, LLM configuration, search switch, and output fingerprints. Stale artifacts that are safe to rebuild are backed up first. A legacy fusion artifact with no manifest or unverifiable provenance is neither silently overwritten nor allowed downstream.
- **Stop conditions:** The stage fails when audio is missing, base names collide after extensions are removed, the script mapping is unconfirmed, or any active audio lacks a valid final `*_ensemble.srt`. Local long-cue timing is a fail-closed side path: a failure preserves the already valid fused subtitles and does not fail STEP1 by itself.

### STEP2: Japanese second-pass review

- **Execution conditions:** `STEP2_REVIEW_JP=1`. Scriptless mode handles all active fused subtitles. Script mode runs only when `script_mismatch.json` is nonempty or its status cannot be verified, and reviews only mismatched files. `run_all.py` skips it when all scripts match.
- **Inputs:** `*_ensemble.srt`; script mode also reads `script_mismatch.json`.
- **Processing and external services:** The LLM revises Japanese in batches, then checks the result with full-document context. When search is enabled, the full-document check may use Tavily/Exa; batch processing and single-item fallbacks do not search.
- **Outputs:** `*_reviewed.srt` and `*_reviewed_manifest.json`.
- **Caching and invalidation:** The manifest binds the fused input subtitles, LLM/batch configuration, search switch, and output content. When the input or generation configuration changes, the old artifact is backed up before rebuilding.
- **Stop conditions:** Missing fused subtitles cause a nonzero exit. If one LLM batch fails, the original Japanese is used as a fallback; if the full-document check fails, the batched result is kept. These API failures reduce quality but do not automatically fail the entire stage.

### STEP3: Translation and per-segment review

- **Execution conditions:** `STEP3_TRANSLATE=1`, enabled by default.
- **Inputs:** Each `*_ensemble.srt` acts as an index. A `*_reviewed.srt` is preferred only when `*_reviewed_manifest.json` proves that it matches the current fused subtitles; otherwise the stage falls back to `*_ensemble.srt`.
- **Processing and external services:** The LLM translates Japanese into Simplified Chinese in batches, then reviews the translations in batches. Local constraints safely split long sentences. Initial translation and single-item fallbacks do not search; when search is enabled, only translation review may use Tavily/Exa.
- **Outputs:** Bilingual Japanese–Chinese `*_zh.srt` and `*_zh_manifest.json`.
- **Caching and invalidation:** The manifest binds the Japanese input actually used, LLM/batch configuration, search switch, and output content. A change in the upstream source also invalidates the cache. Old artifacts are backed up before rebuilding.
- **Stop conditions:** Missing fused subtitles cause a nonzero exit. An empty translation response leaves an empty placeholder for the review fallback. A batch exception uses the original Japanese as a placeholder, while a review-batch exception keeps the initial translation. Some API failures can therefore produce degraded output that needs later inspection without automatically failing the entire stage. Input-snapshot or atomic-commit validation failures raise an exception and stop the stage.

### STEP3.25: Script structure classification and review alignment

- **Execution conditions:** The formal gate runs only when `ENABLE_SCRIPT=1`, `ENABLE_HUMAN_REVIEW=1`, and `ENABLE_SCRIPT_REVIEW_FILTER=1`. `ENABLE_SCRIPT_UNITS_SHADOW=1` instead runs non-blocking shadow analysis after the formal pipeline finishes. Their failure policies differ.
- **Inputs:** The current confirmed script, active audio, and `*_fusion_timeline.json`.
- **Processing and external services:** The LLM classifies the source script as dialogue/action/psychology/separator/unknown. Local code preserves original spans and deterministically aligns speakable units to ASR time windows.
- **Outputs:** `*_script_units.json` and `*_script_alignment.json`.
- **Caching and invalidation:** Artifacts are bound to the source script, classification configuration, audio source, timeline, and upstream sidecar fingerprints. They cannot be reused after any source changes.
- **Stop conditions:** The formal gate requires current, valid classification and alignment artifacts for every active audio file. Missing artifacts, failure, or timeout stops the pipeline. Failure or timeout in end-of-pipeline shadow analysis only records a warning and does not affect completed subtitles.

### STEP3.5: Local human-review gate

- **Execution conditions:** `ENABLE_HUMAN_REVIEW=1` and `STEP35_HUMAN_REVIEW=1`. Disabled by default.
- **Inputs:** Each active audio file's `*_zh.srt`, source audio, and any available ASR/timeline/script sidecars.
- **Processing and external services:** Starts a browser page bound only to `127.0.0.1` for listening to audio, viewing context and candidate differences, and editing text, start/end times, cue splits, or cue merges. This stage itself does not call an LLM or search service.
- **Outputs:** `*_human_reviewed.srt` and `*_human_review.json`.
- **Caching and invalidation:** The completion record binds the review bundle, current bilingual input, and reviewed output. Any content change invalidates the old review. Old artifacts are backed up before a new submission is committed.
- **Pause and stop conditions:** An unsubmitted page or user interruption returns exit code 75, which `run_all.py` treats as a resumable pause rather than a failure. Missing STEP3 artifacts, invalid submitted content, or a mismatched completion record fails closed.

### STEP4: Full-document final review

- **Execution conditions:** `STEP4_FINAL=1`, enabled by default.
- **Inputs:** Reads `*_zh.srt` when human review is disabled. When human review is enabled, it accepts only `*_human_reviewed.srt` and completion records matching the current input; human edits cannot be silently bypassed.
- **Processing and external services:** The LLM checks Japanese–Chinese meaning, forms of address, tone, and consistency with full-document context, then submits entries that require correction. It may use Tavily/Exa when search is enabled.
- **Outputs:** `*_final.srt` and `*_final_manifest.json`.
- **Caching and invalidation:** The manifest binds the actual upstream filename and content, LLM configuration, search switch, and final output. Source or configuration changes trigger atomic replacement while preserving the old version as a backup. When human review is disabled, a legacy final file without a manifest is retained temporarily to prevent accidental overwriting, not declared valid.
- **Stop conditions:** Missing bilingual subtitles, missing/stale human-review results, or any failed final-review task produces a nonzero exit while preserving an existing final file.

### STEP5: Rule validation

- **Execution conditions:** `STEP5_VALIDATE=1`, enabled by default.
- **Inputs:** All `*_final.srt` files.
- **Processing and external services:** Local code scans parseable subtitle blocks for API artifacts/bare sequence numbers, punctuation-only content, empty translations, missing Japanese, review comments, omitted content, and low-confidence markers. It does not call an LLM or search service. The current implementation applies content rules only to parseable subtitle blocks; it does not validate complete timeline legality and continuity.
- **Outputs:** Validation reports in the terminal and `pipeline.log`; no new subtitle file or independent cache is created.
- **Stop conditions:** Severe issues return a nonzero status and block STEP6. Warnings are reported and processing continues. Missing final subtitles also fail the stage.

### STEP6: Export Chinese-only subtitles

- **Execution conditions:** `STEP6_STRIP=1`, enabled by default.
- **Inputs:** All `*_final.srt` files.
- **Processing and external services:** Local code removes the Japanese portion of each cue, preserves the timeline and Chinese text, and renumbers the cues. It does not call external services.
- **Outputs:** `*_cn_only.srt`.
- **Caching and invalidation:** There is currently no manifest. Every run regenerates and overwrites the corresponding Chinese-only subtitle from the current final file. It is a derived file; `*_final.srt` remains the authoritative final artifact.
- **Stop conditions:** The stage fails when no final file exists. If Chinese text cannot be extracted from one file, the failure is reported and the remaining files continue, but the current implementation does not return a nonzero status solely for that file-level failure.

## Module responsibilities

### Orchestration and shared services

- `run_all.py`: loads `.env`, combines stage switches, passes child-process environment variables, and handles pauses.
- `common.py`: LLM client, retries, Function Calling, search, and shared SRT/logging utilities.
- `pipeline_inputs.py`: resolves input/output directories and supported audio extensions.
- `pipeline_cache.py`: SHA-256 fingerprints, manifest validation, atomic writes, and backups of old artifacts.

### ASR, evidence, and timelines

- `ensemble_transcribe.py`: primary STEP1 orchestration, model lifecycle, caching, and fusion commit.
- `asr_evidence.py`: normalizes candidates, windows, provenance, metrics, and audit manifests.
- `asr_rescue.py`: creates bounded rescue plans and merges rescue evidence.
- `asr_timeline.py`: deterministic candidate alignment, decisions, and timeline resolution.
- `long_cue_alignment.py`: generates and validates local boundary plans for abnormally long cues.

### Scripts

- `match_scripts.py`: same-name/fuzzy matching, merged-script splitting, and human or automatic confirmation.
- `script_text_io.py`: reads UTF-8/BOM, CP932/Shift-JIS, and validated mixed-encoding text.
- `script_mapping_state.py`: binds the confirmation marker to audio and script content fingerprints.
- `script_units.py`: classifies dialogue/action/psychology/separator/unknown while preserving original spans.
- `script_alignment.py`: deterministically aligns speakable script units to ASR time windows.
- `script_units_shadow_stage.py`: entry point for formal STEP3.25 or end-of-pipeline shadow analysis.

### Review and human review

- `review_japanese.py`: batched Japanese second-pass review and full-context review.
- `translate.py`: Japanese-to-Simplified-Chinese translation, per-segment review, and safe long-sentence splitting.
- `human_review.py`: generates review items, candidate differences, romaji, and input fingerprints.
- `review_app.py`: browser UI and submission endpoint bound only to `127.0.0.1`.
- `human_review_gate.py`: validates whether review inputs and outputs are current, then decides whether to continue or pause.
- `review_final.py`: full-document final review and input-boundary checks.
- `validate_final.py`, `strip_japanese.py`: rule validation and Chinese-only export.

## Critical STEP1 path

### Dual ASR and script behavior

Whenever caches need rebuilding, STEP1 runs both models whether or not a script exists:

- `Systran/faster-whisper-large-v3`
- `mobiuslabsgmbh/faster-whisper-large-v3-turbo`

Scriptless mode fuses evidence from both models. With a fully matched script, the formal result uses the large-v3 timeline and script text while Turbo remains independent evidence. A script mismatch falls back to ordinary fusion and subsequent Japanese review. Do not reintroduce the old “skip Turbo in script mode” behavior.

### Hallucination quarantine and rescue

`ENABLE_HALLUCINATION_FILTER=1` handles only candidates that match high-precision fixed templates and lack independent support. Original candidates remain in evidence files for human auditing.

`ENABLE_ASR_RESCUE=1` scans only suspicious bounded windows. It first transcribes with large-v3 without VAD; only when V3 finds possible speech does it add a Turbo comparison. Never run a full-audio Turbo scan without VAD: quiet-speech and noisy scenes tend to produce many fixed hallucinations.

### Timeline ownership

With `ENABLE_DETERMINISTIC_TIMELINE=1`, the LLM returns text selections and evidence IDs. `asr_timeline.py` derives order, overlap, and boundaries from candidate evidence. Text without valid evidence must not enter the formal timeline directly.

`ENABLE_LONG_CUE_ALIGNMENT=1` handles abnormal cues longer than `LONG_CUE_MAX_SECONDS`:

- `LONG_CUE_ALIGNMENT_MODE=apply`: commits only boundary plans that pass local constraints.
- `LONG_CUE_ALIGNMENT_MODE=shadow`: writes an audit report without modifying the SRT.
- Normally matched script-mode output skips this path. Only scriptless or script-mismatch fallback output enters it.

## Human-review contract

Human review is disabled by default. When enabled, the page must:

- display bilingual Japanese–Chinese context for the previous, current, and next cues;
- display and highlight differences among the current Japanese, candidate Japanese, and their romaji;
- provide manual play/pause without automatically looping abnormal segments;
- support editing start/end times, splitting cues, and merging with the next cue;
- collapse known fixed-hallucination candidates by default to reduce review noise.

Submission creates `*_human_reviewed.srt` and `*_human_review.json`. The completion marker binds the current input and output; an old review cannot be reused after content changes. When human review is enabled, STEP4 must not bypass missing, stale, or mismatched review results.

## Script-processing contract

Script mode is not auto-detected. The pipeline reads `scripts/` only when `ENABLE_SCRIPT=1`. A future `SCRIPT_MODE=auto|off|required` interface may be considered, but it does not exist in the current code.

STEP0 supports one publisher-supplied merged script for multiple audio files. Splitting and subsequent structuring must preserve all original content, including dialogue, character actions, psychological descriptions, chapter headings, and decorative separators. The LLM handles classification and textual judgment only; deterministic code maintains segmentation boundaries, original spans, and timelines.

The confirmation state in `script_mapping.json` is bound to audio and script SHA-256 hashes. Interactive terminals can request human confirmation. Non-interactive environments fail closed by default; only an explicit `SCRIPT_AUTO_VERIFY=1` can write the confirmation marker automatically.

## Configuration sources

`run_all.py` loads `.env`; existing process environment variables take precedence. Boolean values accept `0/1`, `false/true`, `no/yes`, and `off/on`.

Primary switches:

```dotenv
ENABLE_SEARCH=0
ENABLE_SCRIPT=0
STEP0_MATCH_SCRIPTS=0
ENABLE_AUDIO_PREPROCESS=1
ENABLE_ASR_EVIDENCE=1
ENABLE_HALLUCINATION_FILTER=1
ENABLE_ASR_RESCUE=1
ENABLE_DETERMINISTIC_TIMELINE=0
ENABLE_LONG_CUE_ALIGNMENT=1
LONG_CUE_ALIGNMENT_MODE=apply
LONG_CUE_MAX_SECONDS=15
ENABLE_HUMAN_REVIEW=0
HUMAN_REVIEW_PORT=8765
STEP1_ENSEMBLE=1
STEP2_REVIEW_JP=1
STEP3_TRANSLATE=1
STEP35_HUMAN_REVIEW=1
STEP4_FINAL=1
STEP5_VALIDATE=1
STEP6_STRIP=1
```

`RESCUE_WINDOW_SECONDS`, `RESCUE_OVERLAP_SECONDS`, `INITIAL_PROMPT`, `HOTWORDS`, and `VAD_*` are currently hard-coded in `run_all.py` and injected into child processes. Setting variables with those names only in `.env` does not override them. Exposing them requires changes to the loading logic in `run_all.py`, updates to the cache generation fingerprint, and tests.

`STEP35_HUMAN_REVIEW` is only a subordinate switch; it runs only when `ENABLE_HUMAN_REVIEW=1` is also set. Similarly, STEP0 is gated by `ENABLE_SCRIPT`, while STEP3.25 is gated by script mode, human review, and `ENABLE_SCRIPT_REVIEW_FILTER` together.

## Artifacts and caching

Primary formal artifacts:

| Category | Typical files |
| --- | --- |
| Raw ASR | `*_v3.srt`, `*_turbo.srt`, and their `*_manifest.json` files |
| ASR audit | `*_asr_candidates.json`, `*_asr_alignment.json`, `*_asr_windows.json`, `*_asr_decisions.json`, `*_asr_rescue_windows.json`, `*_asr_manifest.json` |
| Fusion | `*_fusion_timeline.json`, `*_ensemble.srt`, `*_fusion_manifest.json`, `*_script_fusion_manifest.json` |
| Long cues | `*_long_cue_alignment.json` |
| Downstream subtitles | `*_reviewed.srt`, `*_zh.srt`, `*_human_reviewed.srt`, `*_final.srt`, `*_cn_only.srt` |
| Human review | `*_human_review.json` |
| Script structure | `*_script_units.json`, `*_script_alignment.json` |
| Logs | `pipeline.log` |

Cache validity depends on input/output SHA-256 hashes, configuration fingerprints, generation fingerprints, artifact type, schema version, and completion state. Changes to audio, models, VAD, prompts, upstream SRT files, LLM configuration, or evidence must invalidate dependent artifacts.

Write rules:

- Most text and JSON files are committed through temporary files and atomic replacement.
- Downstream SRT stages commit the manifest first, then atomically commit canonical output after confirming that inputs have not changed.
- Legacy artifacts without a manifest are not assumed valid and are not silently overwritten.
- Stale artifacts that can be rebuilt safely are first moved to `output/_review_backups/`; downstream processing stops when provenance cannot be verified or overwriting is high risk.
- `_cache/` holds tests, diagnostics, and temporary artifacts only and is ignored by Git.

## Privacy and external services

- ASR and audio preprocessing run locally. Audio is not uploaded to the LLM or search providers.
- Japanese and Chinese subtitle text is sent to the configured OpenAI-compatible LLM for fusion, review, or translation.
- Tavily/Exa is not called when `ENABLE_SEARCH=0`. When enabled, only review calls that actually trigger search send generated queries to the search provider.
- The browser review server listens only on `127.0.0.1`.
- `.env` contains secrets and is ignored by Git. Logs, documentation, and test fixtures must not contain real keys.

## Development and validation

The seven direct dependencies are faster-whisper, openai, librosa, noisereduce, scipy, soundfile, and pykakasi. The project does not depend on PyTorch; CTranslate2 determines GPU availability.

Before committing, run at least:

```powershell
$files = Get-ChildItem -File *.py | ForEach-Object { $_.FullName }
venv\Scripts\python.exe -m py_compile $files
git diff --check
```

When internal regression tests are present in the working tree, run:

```powershell
venv\Scripts\python.exe -m unittest discover -s _cache\tests -p 'test_*.py' -q
```

Under project rules, these internal tests and temporary validation files remain in `_cache/` and are not published through Git. The mandatory checks for the public repository remain syntax compilation and `git diff --check`.

Runtime validation must point `AUDIO_DIR` and `OUTPUT_DIR` to isolated directories. Put all mocks, diagnostic scripts, and intermediate artifacts in `_cache/`. Search-related changes require a real API smoke test with test credentials, but credentials must not appear in command output, logs, or commits.

The current local development environment has been validated with Windows venv Python 3.14.5, imports for all seven direct dependencies, a successful `pip check`, available FFmpeg/ffprobe, and one CUDA GPU detected by CTranslate2. README continues to recommend Python 3.10/3.11; 3.14.5 is only the version tested in the current environment.

## Pre-release checklist

- The English and Chinese READMEs have matching heading hierarchies, facts, commands, and configuration blocks.
- `.env.example` contains only an empty key and configuration entries that actually work.
- Dependencies are consistent among `requirements.txt`, README, and the developer guide.
- New source files are in the repository root; tests and debugging artifacts are in `_cache/`.
- `py_compile` and `git diff --check` pass. If the commit contains formal tests, the corresponding test suite also passes.
- `git status --short` contains no `.env`, audio, model files, subtitles, logs, or review backups.
- Do not create commits or push without explicit authorization.

## Changelog

### V4.0.1

- Added English beginner and developer guides with bidirectional language navigation.
- Updated README documentation links and added a tag-based version badge that links to GitHub Releases.

### V4.0.0

- Added structured dual-ASR evidence, fixed-hallucination quarantine, targeted rescue, and deterministic timelines.
- Added browser-based bilingual human review with romaji hints and timeline editing.
- Added local timing for abnormally long cues with a safe apply mode.
- Reworked lossless script reading, mapping validation, structure classification, and timeline alignment.
- Added content-addressed manifests and atomic commit boundaries for ASR, fusion, and downstream subtitles.

See [CHANGELOG.md](../CHANGELOG.md) for the complete version history.

## AI disclosure

The code and documentation were developed through iterative human–AI collaboration. Human authors confirmed the architecture, parameters, listening-based findings, acceptance criteria, and release decisions. AI assisted with implementation, review, testing, and documentation.

## License

GNU General Public License v3.0 or later (`GPL-3.0-or-later`). See [LICENSE](../LICENSE). Copyright (c) 2025–2026 Kochiya3309.
