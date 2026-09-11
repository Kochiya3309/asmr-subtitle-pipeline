# Configuration Reference

English | [简体中文](configuration.zh-CN.md)

Copy `.env.example` to `.env`, then set only the values needed for the run.
`.env` can contain secrets: never commit, share, screenshot, or attach it to a
bug report. `run_all.py` loads `.env`; an already defined process environment
variable with the same name takes precedence. Use `0` or `1` for all switches.
For a first run, see [Getting Started](getting-started.md).

## API, model, and search

| Setting | Default | Meaning, dependency, and risk |
| --- | --- | --- |
| `OPENAI_API_KEY` | empty | Required key for the configured OpenAI-compatible LLM. `DEEPSEEK_API_KEY` is read only as a legacy fallback when this setting is empty. |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` when empty | Base URL for an OpenAI-compatible provider. Set it with `OPENAI_MODEL` when changing provider. |
| `OPENAI_MODEL` | `deepseek-v4-pro` when empty | Provider model identifier. It must be supported by the selected endpoint. |
| `OPENAI_ENABLE_THINKING` | `0` | Set to `1` only if the selected provider supports its nonstandard thinking parameter; otherwise the request can fail. |
| `OPENAI_MAX_TOKENS` | `131072` when empty or invalid | Maximum output tokens for one LLM response. Choose a value within the provider and model limit. |
| `TAVILY_API_KEY` | empty | Optional Tavily credential; it is useful only when search is enabled. |
| `EXA_API_KEY` | empty | Optional Exa credential; it is useful only when search is enabled. |
| `ENABLE_SEARCH` | `0` | Allows eligible review calls to use configured search providers. Generated queries can leave the machine; keep this disabled for private or NSFW material unless lookup is necessary. |

Audio preprocessing and ASR run locally. Fusion, Japanese review, translation,
and final review send the necessary subtitle text—not audio—to the configured
LLM. A search provider is not called when `ENABLE_SEARCH=0`.

## Input, output, and delivery paths

Relative paths are resolved from the project root by `run_all.py`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `AUDIO_DIR` | `./audio` | Directory of main audio inputs. |
| `VIDEO_DIR` | `./video` | Directory scanned for video inputs only when `ENABLE_VIDEO_PREP=1`. |
| `OUTPUT_DIR` | `./output` | Root for outputs and pipeline caches. Point this to an empty directory for an isolated test. |
| `SRT_TRANSFER_DIR` | `./output/_transfer` | Destination root used by `export_final_srt.py` and `export_cn_only_srt.py`; copies are placed in `final/` or `cn_only/`. |
| `ENABLE_VIDEO_PREP` | `0` | Scans top-level `.mp4`, `.mkv`, `.mov`, `.webm`, and `.avi` files in `VIDEO_DIR`; each accepted video contributes its first audio stream as a managed FLAC input. |

Managed video-derived input is written under `output/<audio>/input/`. Do not
copy that FLAC into `AUDIO_DIR`: it is owned by the pipeline's input record.

## Script mode

| Setting | Default | Meaning and dependency |
| --- | --- | --- |
| `ENABLE_SCRIPT` | `0` | Enables reading `SCRIPT_DIR`. Scripts are not auto-detected while this is `0`. |
| `STEP0_MATCH_SCRIPTS` | `0` | Runs script matching and merged-script splitting; it is effective only with `ENABLE_SCRIPT=1`. |
| `SCRIPT_DIR` | `./scripts` | Directory containing source Japanese scripts. |
| `SCRIPT_FALLBACK_FULL` | `1` | Lets an unused complete script be a fallback candidate when matching or splitting cannot resolve an audio file. Review the mapping because this can increase mismatch risk. |
| `SCRIPT_FORCE_RESPLIT` | `0` | Rebuilds verified split-script cache artifacts. Use only when intentionally redoing the split. |
| `SCRIPT_AUTO_VERIFY` | `0` | Lets a non-interactive job confirm `script_mapping.json`. This accepts mapping risk without interactive review. |

## ASR, evidence, and timing

| Setting | Default | Meaning and dependency |
| --- | --- | --- |
| `ENABLE_AUDIO_PREPROCESS` | `1` | Enables the project's 16 kHz mono preprocessing before ASR. Changing it invalidates affected preprocessing and ASR caches. |
| `ENABLE_ASR_EVIDENCE` | `1` | Retains model candidates, metrics, provenance, alignment, and audit manifests used by evidence-based processing. |
| `ENABLE_HALLUCINATION_FILTER` | `1` | Quarantines high-precision fixed hallucination templates from fusion input without deleting original candidates. It requires `ENABLE_ASR_EVIDENCE=1`; otherwise it has no effect. |
| `ENABLE_ASR_RESCUE` | `1` | Runs bounded rescue transcription only for suspicious windows. It requires `ENABLE_ASR_EVIDENCE=1`; it does not enable a full-audio Turbo-without-VAD scan. |
| `ENABLE_DETERMINISTIC_TIMELINE` | `0` | Enables experimental deterministic timeline resolution from selected text and evidence IDs. Changing it invalidates fusion caches. |
| `ENABLE_LONG_CUE_ALIGNMENT` | `1` | Checks unusually long cues. This setting is read directly by STEP1, so use only `0` or `1`. |
| `LONG_CUE_ALIGNMENT_MODE` | `apply` | `apply` commits only locally validated boundary plans; `shadow` writes an audit report without changing the SRT. No other value is supported. |
| `LONG_CUE_MAX_SECONDS` | `15` | Positive duration threshold, in seconds, for long-cue processing. |

## Pipeline stages and human review

| Setting | Default | Meaning and dependency |
| --- | --- | --- |
| `STEP1_ENSEMBLE` | `1` | Runs preprocessing, dual ASR, evidence, rescue, fusion, and applicable long-cue work. |
| `STEP2_REVIEW_JP` | `1` | Runs Japanese second-pass review. In valid script mode, only mismatches are reviewed. |
| `STEP3_TRANSLATE` | `1` | Runs Japanese-to-Simplified-Chinese translation and per-segment review. |
| `ENABLE_HUMAN_REVIEW` | `0` | Enables the local browser review gate. The server binds only to `127.0.0.1`; the workflow pauses until a review is submitted. |
| `HUMAN_REVIEW_PORT` | `8765` | Port for the local review server. Set `0` for a system-selected available port. |
| `STEP35_HUMAN_REVIEW` | `1` | Sub-switch for the browser review gate; it runs only with `ENABLE_HUMAN_REVIEW=1`. |
| `ENABLE_SCRIPT_REVIEW_FILTER` | `0` | Enables the formal script-structure review filter only when `ENABLE_SCRIPT=1` and `ENABLE_HUMAN_REVIEW=1`. It invokes an LLM. |
| `ENABLE_SCRIPT_UNITS_SHADOW` | `0` | With `ENABLE_SCRIPT=1`, runs non-blocking script-structure shadow analysis after formal stages. It produces audit data and does not alter formal subtitles or timelines. |
| `SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS` | `120` | Positive timeout in seconds for script-structure classification and alignment. A trailing shadow-analysis timeout does not fail an already completed formal pipeline. |
| `STEP4_FINAL` | `1` | Runs full-document bilingual final review. |
| `STEP5_VALIDATE` | `1` | Runs local final-subtitle rule validation. |
| `STEP6_STRIP` | `1` | Produces Chinese-only subtitles. If enabled, a STEP5 failure is reported after STEP6 instead of blocking the export. The overall pipeline still finishes with failure status. |
| `STEP7_EXPORT_FINAL` | `1` | Copies bilingual finals to `SRT_TRANSFER_DIR/final/`; it is effective only with `STEP4_FINAL=1`. |
| `STEP8_EXPORT_CN_ONLY` | `1` | Copies Chinese-only subtitles to `SRT_TRANSFER_DIR/cn_only/`; it is effective only with `STEP6_STRIP=1`. |

## Names that are not supported `.env` overrides

`RESCUE_WINDOW_SECONDS`, `RESCUE_OVERLAP_SECONDS`, `INITIAL_PROMPT`,
`HOTWORDS`, and `VAD_*` are set by `run_all.py` and injected into child
processes. Adding them to `.env` does not replace active values. `ACTIVE_AUDIO_PATHS`,
`ACTIVE_AUDIO_BASES`, and `LOG_FILE` are run-scoped internal state, not normal
user configuration. Changing any of these requires implementation work rather
than a `.env` edit.
