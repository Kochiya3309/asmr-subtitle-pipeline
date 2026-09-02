# Changelog

English | [简体中文](CHANGELOG.zh-CN.md)

This file records user-visible and architectural changes. Dates and commit links are included only where the local Git history provides a release boundary.

The repository begins with the V3.3 source snapshot. V3.0–V3.2 have no separate commits or tags; their entries below are reconstructed from the version notes stored in that initial commit.

## [Unreleased]

### Added

- Structured dual-ASR evidence with native model metrics, provenance, aligned windows, decisions, rescue plans, and audit manifests.
- High-precision quarantine for repeated fixed-hallucination templates that lack independent support. Original candidates remain available for review.
- Bounded rescue transcription: large-v3 first scans suspicious windows without VAD, and Turbo runs only where V3 finds possible speech.
- Optional deterministic fusion timelines in which the LLM selects text and evidence IDs while local code owns ordering and timestamps.
- Long-cue timing analysis with `apply` and non-mutating `shadow` modes.
- Optional browser-based human review on `127.0.0.1`, including bilingual context, Japanese and romaji difference highlighting, manual playback, timing edits, cue splitting, merging, and collapsed fixed-hallucination candidates.
- Lossless script reading for UTF-8/BOM, CP932/Shift-JIS, and validated mixed-encoding lines.
- Script-unit classification and deterministic alignment of dialogue to ASR windows while preserving action/psychology notes, titles, decorative separators, and unknown source spans.
- SHA-256 input-bound manifests and staged or atomic writes for preprocessing, ASR, fusion, Japanese review, translation, human review, and final review.
- Centralized audio collection and output-path handling while retaining duplicate input-base rejection.
- Romaji review hints through `pykakasi`; romaji does not participate in ASR decisions.

### Changed

- Script mode now runs both large-v3 and large-v3-turbo. Formal script fusion uses large-v3 timing plus script text; Turbo remains independent evidence and supports mismatch fallback.
- Script mapping verification is bound to audio and source-script content rather than modification times alone.
- Non-interactive script mapping now fails closed unless `SCRIPT_AUTO_VERIFY=1` is explicitly configured.
- Human review is an optional pipeline gate between translation and final review. Missing or stale review output pauses or blocks downstream processing instead of being silently bypassed.
- Long subtitle splitting and timing changes are locally validated before they can replace canonical SRT output.
- Stage reuse now depends on input, output, configuration, generation, and upstream fingerprints rather than file existence.
- Runtime switches moved to environment-driven configuration where supported; `.env.example` documents the user-facing settings included in the template.

### Fixed

- Prevented stale or partially written subtitles from being treated as completed cache entries.
- Prevented LLM-selected text without valid evidence from creating arbitrary deterministic timestamps.
- Reduced human-review noise from identical ASR candidates, harmless boundary overlap, and known fixed hallucinations.
- Added migration and fingerprint checks for legacy human-review submissions.
- Preserved legacy artifacts without silently overwriting them when their origin cannot be verified.

## [3.6.1] - 2026-08-19

Commit: [`1a976e8`](https://github.com/Kochiya3309/asmr-subtitle-pipeline/commit/1a976e8)

### Fixed

- Repaired all four `start_*.bat` launchers after V3.6 had joined Python commands, arguments, and `pause` on the same line.
- Kept command arguments separate from script paths, restored a standalone `pause`, used CRLF line endings, and kept launcher text ASCII-compatible for Windows `cmd`.

## [3.6.0] - 2026-08-18

Commit: [`0a53f93`](https://github.com/Kochiya3309/asmr-subtitle-pipeline/commit/0a53f93)

### Added

- OpenAI-compatible LLM configuration through `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_ENABLE_THINKING`, and `OPENAI_MAX_TOKENS`.
- `get_llm_client` as the provider-neutral client entry point while retaining the historical DeepSeek-named alias.
- Targeted error guidance when a provider rejects the configured maximum output-token limit.

### Changed

- DeepSeek remained the default endpoint, while other OpenAI-compatible providers could be selected without editing source files.
- The non-standard `thinking` request parameter became opt-in so standard providers would not reject it.
- LLM configuration became process-cached to avoid configuration drift during a run.
- Usage reporting stopped estimating provider-specific cost and retained call and token counts only.
- Empty optional provider settings were no longer injected into child processes.
- The legacy `DEEPSEEK_API_KEY` remained as a fallback when `OPENAI_API_KEY` was absent.

## [3.5.0] - 2026-08-14

Commit: [`3f923bb`](https://github.com/Kochiya3309/asmr-subtitle-pipeline/commit/3f923bb)

### Added

- Direct Tavily and Exa search providers, with parallel requests and URL deduplication when both keys were configured.
- Automatic compute-device detection and CPU fallback when CUDA was unavailable.
- Float16 fallback for GPUs that could not load `int8_float16`.
- Duplicate audio-base detection to prevent different extensions from overwriting the same output name.
- `sitecustomize.py` protection against an unexpected first Ctrl+C signal during startup.
- Hugging Face mirror configuration and project-path setup in the Windows launchers.

### Changed

- Removed the former Zhipu search integration and its key; search calls used Tavily/Exa directly.
- Required `faster-whisper>=1.2.1` for hotword support.
- Moved script-mismatch Turbo recovery into a serial main-thread phase so GPU inference did not compete with parallel LLM fusion.
- Converted input and output paths to absolute paths for launches outside the project directory.
- Made token estimation more conservative for long requests.
- Non-interactive script matching no longer blocked on `input()`; at this release it automatically accepted the generated mapping. The Unreleased version replaces that behavior with explicit `SCRIPT_AUTO_VERIFY` fail-closed control.

### Fixed

- Corrected invalid tool-call history during empty-response retries that could produce HTTP 400 errors.
- Stopped low-confidence markers such as `低信頼度` and `認識不良` from being classified as fatal API artifacts.
- Preserved valid translations when another item in the same batch was empty.
- Backed up an existing rename target instead of silently overwriting it.
- Recovered cleanly when all merged-script split attempts failed.
- Prevented too-small full-review batches from crashing or silently skipping work.
- Made malformed manually edited subtitle blocks warn and skip instead of crashing the entire run.
- Corrected device detection to use CTranslate2 instead of an undeclared PyTorch dependency.

## [3.4.1] - 2026-07-15

Commit: [`9dc0419`](https://github.com/Kochiya3309/asmr-subtitle-pipeline/commit/9dc0419)

### Security

- Removed hard-coded DeepSeek and Zhipu API keys from `run_all.py` and read them from the environment instead.
- Added a project `.env` loader that used `os.environ.setdefault`, so existing process variables took precedence.
- Added `.env.example` and ignored `.env` to keep real credentials out of Git.

### Packaging

- Added the audio-preprocessing dependencies introduced by the V3.4 feature work: librosa, noisereduce, SciPy, and soundfile.

## [3.4.0]

The V3.4 implementation and the V3.4.1 credential hardening were committed together in `9dc0419`; the Git history contains no independent V3.4 tag or commit date.

### Added

- Optional preprocessing before ASR: 16 kHz mono loading, a fourth-order 80 Hz Butterworth high-pass filter, conservative stationary noise reduction, RMS normalization to -20 dBFS, peak limiting to 0.95, and PCM-16 WAV output.
- Cached `*_preprocessed.wav` files for restartable transcription.
- ASMR-oriented `initial_prompt` guidance and faster-whisper hotword support for names and domain terms.

### Changed

- Tuned VAD for quiet ASMR speech: threshold `0.3 → 0.2`, minimum speech duration `100 ms → 50 ms`, and speech padding `600 ms → 800 ms`.
- Loaded preprocessing dependencies lazily so disabled preprocessing did not pay their import cost.

### Limitations recorded at the time

- Stationary noise reduction was intended for steady background noise such as hiss or rain. It was not presented as a solution for music or other non-stationary interference.

## [3.3.0] - 2026-07-14

Initial repository snapshot: [`1f90643`](https://github.com/Kochiya3309/asmr-subtitle-pipeline/commit/1f90643)

### Added

- LLM filename matching for scripts that could not be resolved by local rules.
- Detection of one merged script being matched by multiple audio files, forcing it into the split path.
- Track identifiers containing digits and letters, including `1A` and `1B`.
- Mapping invalidation when scripts, audio files, or their file lists changed.

### Changed

- Script mode skipped Turbo and fused large-v3 timing with script text. This historical optimization is no longer the current behavior.
- A severe script mismatch triggered Turbo transcription and automatic fallback to ordinary large-v3 + Turbo fusion instead of terminating the pipeline.
- `script_mismatch.json` selected only mismatch files for Japanese review; script-matched files skipped STEP2.
- Translation discovered work from `*_ensemble.srt`, preferring `*_reviewed.srt` when present, so mixed reviewed/unreviewed inputs remained valid.

### Fixed

- Removed the high-false-positive Japanese-kana check from Chinese final-subtitle validation.

## [3.2.0]

Historical reconstruction from the V3.3 snapshot; no standalone V3.2 commit or tag exists.

### Added

- Optional untimed Japanese-script assistance controlled by `ENABLE_SCRIPT`.
- `match_scripts.py` as STEP0 for same-name/fuzzy matching, regular-expression-first merged-script splitting, LLM split fallback, and interactive mapping verification.
- Script text as a reference for fusion with large-v3 timestamps.
- A structured fusion error field for detecting a script that did not correspond to the audio. V3.2 stopped on this mismatch; V3.3 introduced automatic fallback.

## [3.1.0]

Historical reconstruction from the V3.3 snapshot; no standalone V3.1 commit or tag exists.

### Changed

- Split STEP1 into serial GPU transcription for all files followed by parallel LLM fusion for all files.
- Removed fixed waits between audio files.
- Moved the fusion parser to module scope and initialized the shared LLM client before starting fusion workers.

## [3.0.0]

Historical reconstruction from the V3.3 snapshot; no standalone V3.0 commit or tag exists.

### Added

- Cross-file and cross-batch parallelism for Japanese review, translation review, and final review. STEP1 fusion joined this parallel model in V3.1.
- Central configuration in `run_all.py`, passed to child scripts through environment variables.
- Append-only `output/pipeline.log` logging shared by every script.
- A Whisper model cache that reused each loaded ASR model across audio files.
- `strip_japanese.py` as an optional Chinese-only export stage.
- A generic `rename_suffix.py` tool replacing separate rename utilities.

### Changed

- Improved translation prompts for incomplete speech, onomatopoeia, puns, and punctuation consistency.
- Extended structured Function Calling to subtitle fusion and consolidated review/correction output handling, fallback parsing, and retries.
- Parallelized both the batched local-context and full-context Japanese review phases.
- Generalized final-review context inference instead of assuming one work's setting.
- Included web-search instructions in prompts only when search was enabled.
- Returned an empty translation placeholder on failure instead of copying Japanese text into the Chinese field, allowing validation to surface the problem.

### Fixed

- Stopped caching search errors and corrected search-cache miss accounting when no provider key was configured.

## Documentation-only commits

- [`e3ea919`](https://github.com/Kochiya3309/asmr-subtitle-pipeline/commit/e3ea919) (2026-07-15): added SciPy and soundfile to the credits.
- [`d12acef`](https://github.com/Kochiya3309/asmr-subtitle-pipeline/commit/d12acef) (2026-08-27): replaced the former single-language usage guide with an English README and a Simplified Chinese counterpart; also adjusted ignore rules.
