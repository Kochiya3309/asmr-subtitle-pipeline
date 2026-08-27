# ASMR Subtitle Pipeline

English | [简体中文](README.zh-CN.md)

Turn Japanese ASMR audio into bilingual Japanese–Chinese SRT subtitles. The pipeline combines two Whisper transcriptions, reviews Japanese with an OpenAI-compatible LLM, translates into Simplified Chinese, performs a final review and rule-based validation, and can create a Chinese-only SRT.

**V3.6.1** supports OpenAI-compatible LLM APIs (DeepSeek is the default), optional Tavily/Exa search, CPU fallback when CUDA is unavailable, and repaired `start_*.bat` launchers.

## Pipeline

| Step | Script | Purpose |
| --- | --- | --- |
| STEP0 | `match_scripts.py` | Optional script matching and splitting |
| STEP1 | `ensemble_transcribe.py` | Whisper large-v3 + large-v3-turbo transcription and fusion |
| STEP2 | `review_japanese.py` | Japanese subtitle review |
| STEP3 | `translate.py` | Translation and per-segment review |
| STEP4 | `review_final.py` | Full-document final review |
| STEP5 | `validate_final.py` | Rule-based validation |
| STEP6 | `strip_japanese.py` | Optional Chinese-only SRT |

Script assistance and web search are disabled by default. Keep search disabled for NSFW material unless you explicitly need it.

## Requirements

| Item | Minimum | Recommended |
| --- | --- | --- |
| OS | Windows 10/11; macOS/Linux require path adjustments | Windows 11 |
| GPU | NVIDIA GPU with 8 GB VRAM | RTX 3060 or newer |
| Memory | 16 GB | 32 GB |
| Disk | 10 GB free for models | SSD |
| Python | 3.10 or 3.11 | 3.10 |
| Other | FFmpeg on `PATH`, access to the chosen LLM API | Stable broadband |

CPU mode is selected automatically when CUDA is unavailable and is typically 5–10 times slower. The first run downloads about 4.5 GB of Whisper models.

## Quick start

1. Create and activate a virtual environment:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env`, then set `OPENAI_API_KEY`.
3. Put `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, or `.opus` audio files in `audio/`.
4. Double-click `start.bat`, or run:

```powershell
venv\Scripts\python.exe run_all.py
```

5. Load `*_final.srt` from `output/` in your player. Enable STEP6 to create `*_cn_only.srt`.

`start.bat` sets `HF_ENDPOINT=https://hf-mirror.com`, which can help download models on China-based networks.

## Configuration

Runtime switches live near the top of `run_all.py`. `.env` holds secrets and is ignored by Git; environment variables take precedence over `.env`.

### LLM API

With DeepSeek, normally only `OPENAI_API_KEY` is required. For a different OpenAI-compatible service, set its base URL and model name:

```dotenv
OPENAI_API_KEY=sk-你的LLM密钥
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o
OPENAI_ENABLE_THINKING=0
```

Set `OPENAI_ENABLE_THINKING=1` only for providers that support DeepSeek's non-standard `thinking` parameter. If `OPENAI_API_KEY` is absent, the project falls back to `DEEPSEEK_API_KEY`.

### Optional web search

Fill these keys only when you plan to search, then change `ENABLE_SEARCH = True` in `run_all.py`:

```dotenv
TAVILY_API_KEY=tvly-你的tavily密钥（可选）
EXA_API_KEY=你的exa密钥（可选）
```

With both keys, searches run in parallel and results are deduplicated by URL. One provider also works; with neither key, search disables itself with a warning.

### Pipeline switches

```python
ENABLE_SEARCH = False
ENABLE_SCRIPT = False
STEP0_MATCH_SCRIPTS = False
STEP1_ENSEMBLE = True
STEP2_REVIEW_JP = True
STEP3_TRANSLATE = True
STEP4_FINAL = True
STEP5_VALIDATE = True
STEP6_STRIP = True
```

Other primary controls are `MAX_WORKERS`, `REVIEW_JP_BATCH_SIZE`, `REVIEW_JP_OVERLAP`, `REVIEW_JP_FULL_REVIEW`, `REVIEW_JP_FULL_REVIEW_BATCH`, `TRANSLATE_BATCH_SIZE`, and `TRANSLATE_REVIEW_BATCH_SIZE`.

## Outputs and resuming

| File | Description |
| --- | --- |
| `*_ensemble.srt` | Fused Japanese Whisper subtitles |
| `*_reviewed.srt` | Japanese-review output |
| `*_zh.srt` | Bilingual translated and reviewed subtitles |
| `*_final.srt` | Final bilingual subtitle file |
| `*_cn_only.srt` | Chinese-only subtitle file when STEP6 is enabled |
| `pipeline.log` | Append-only pipeline log |

Existing outputs cause their completed step to be skipped. After fixing a transient failure, run the same command again. To rename a Chinese-only SRT for automatic player loading, run `python rename_suffix.py _cn_only` or `start_rename_cn_only.bat`; existing targets are backed up as `.bak.srt` before replacement.

## Audio preprocessing

`ENABLE_AUDIO_PREPROCESS = True` enables 16 kHz mono processing, an 80 Hz high-pass filter, conservative stationary-noise reduction, RMS normalization to -20 dBFS, and peak limiting. Cached `*_preprocessed.wav` files support resuming. Use `INITIAL_PROMPT`, `HOTWORDS`, and the `VAD_*` settings in `run_all.py` for title-specific terminology and whisper detection.

## Optional script-assisted mode

Place Japanese scripts in `scripts/`, then enable:

```python
ENABLE_SCRIPT = True
STEP0_MATCH_SCRIPTS = True
SCRIPT_DIR = "./scripts"
```

The matcher supports one-to-one filenames and merged scripts, detects UTF-8/BOM and Shift-JIS (`cp932`), and asks for confirmation where needed. Script mode uses large-v3 timing plus script text and skips Turbo unless it detects a mismatch. Mismatches fall back to V3+Turbo, are recorded in `output/script_mismatch.json`, and STEP2 reviews only those files.

## Performance reference

Historical RTX 4060/8 GB VRAM and 32 GB RAM measurement: three one-hour ASMR files of about 300 segments each, search disabled. Prices are historical and can change; V3.6+ reports tokens rather than estimated cost.

| Stage | Time | Historical DeepSeek cost |
| --- | --- | --- |
| Dual-model Whisper transcription | about 30 min | 0 CNY |
| LLM fusion | about 2 min | about 0.15–0.24 CNY |
| Japanese review | about 4 min | about 0.24–0.39 CNY |
| Translation and review | about 6 min | about 0.30–0.45 CNY |
| Final review | about 2 min | about 0.09–0.15 CNY |
| Validation | about 2 sec | 0 CNY |
| Total | about 44 min | about 0.78–1.23 CNY |

## Changelog

### V3.6.1

Rewrote the four `start_*.bat` files as ASCII with CRLF line endings; separated Python commands, arguments, and `pause` to fix immediate launcher failures.

### V3.6.0

Migrated to `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_ENABLE_THINKING`, and `OPENAI_MAX_TOKENS`; retained the legacy key fallback; and changed reporting from cost estimates to token counts.

### V3.5.0

Replaced Zhipu search with Tavily and Exa. Fixed empty-content retries, low-confidence markers, partial empty translations, GPU fallback, output paths, rename backup, script fallback, and non-interactive script confirmation. Added first-Ctrl+C protection and the Hugging Face mirror launcher setting.

### V3.4.1–V3.0

Added `.env` loading, audio preprocessing and VAD controls, script-assisted transcription, GPU/LLM two-phase processing, parallelism, structured Function Calling output, token logs, and Chinese-only subtitles.

## Credits

[faster-whisper](https://github.com/SYSTRAN/faster-whisper), [DeepSeek](https://platform.deepseek.com), [Tavily](https://tavily.com), [Exa](https://exa.ai), [FFmpeg](https://ffmpeg.org), [librosa](https://librosa.org), [noisereduce](https://github.com/timsainb/noisereduce), [SciPy](https://scipy.org), and [soundfile](https://python-soundfile.readthedocs.io).

## AI disclosure

The Python scripts and initial documentation drafts were generated from human design requirements: `deepseek-v4-pro` through V2.0, `GLM-5.2` from V3.0 through V3.4.1, and `deepseek-v4-pro-0813` from V3.5 onward. The human author defined the project, selected architecture and parameters, tested output quality, and made release and licensing decisions.

## License

GNU General Public License v3.0 or later (`GPL-3.0-or-later`). See [LICENSE](LICENSE). Copyright (c) 2025–2026 Kochiya3309.
