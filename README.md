# ASMR Subtitle Pipeline

English | [简体中文](README.zh-CN.md)

Generate Japanese–Simplified Chinese SRT subtitles from Japanese ASMR audio. The pipeline transcribes audio locally with `large-v3` and `large-v3-turbo`, uses an OpenAI-compatible LLM to fuse, review, and translate the text, and produces subtitles ready for a media player.

## Highlights

- Dual local ASR with reviewable candidate evidence and targeted rescue for suspicious low-volume windows.
- High-precision quarantine for known fixed hallucination templates without deleting the original evidence.
- Optional support for untimed Japanese scripts, including one merged script shared by multiple audio files.
- Optional local browser review with Japanese, romaji, Chinese, neighboring context, playback, timing edits, splitting, and merging.
- Input-bound resume caches that detect changed audio, configuration, evidence, and upstream subtitles.
- Long-cue timing repair and final rule-based validation.

## Privacy and data flow

- Audio preprocessing and both Whisper models run locally. Audio files are not uploaded to the LLM or search providers.
- Transcript, subtitle, and—when enabled—script text are sent to the configured OpenAI-compatible LLM for fusion, review, or translation.
- Web search is disabled by default. If enabled, generated search queries are sent to the configured Tavily or Exa provider.
- The optional review server listens only on `127.0.0.1`.

Keep `ENABLE_SEARCH=0` for private or NSFW material unless external lookup is necessary. Your LLM provider still receives text required by the enabled processing stages.

## Quick start

For a click-by-click first-run guide, see the [Beginner's guide](docs/getting-started.md).

1. Install Python, FFmpeg, and ffprobe. Python 3.10 or 3.11 is recommended.
2. Create an isolated environment and install the dependencies:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
```

3. Open `.env` and set `OPENAI_API_KEY`. Set `OPENAI_BASE_URL` and `OPENAI_MODEL` when using a provider other than the default DeepSeek endpoint.
4. Put `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, or `.opus` files in `audio/`.
   To start from a video instead, put an `.mp4`, `.mkv`, `.mov`, `.webm`, or
   `.avi` file in `video/` and set `ENABLE_VIDEO_PREP=1`. The pipeline extracts
   its first audio stream to a managed FLAC input automatically.
5. Double-click `start.bat`, or run:

```powershell
venv\Scripts\python.exe run_all.py
```

6. Load `output/<audio>/final/<audio>_final.srt` in your media player.

The first run downloads several gigabytes of model data. When Windows Terminal is available, `start.bat` opens one PowerShell tab and runs the pipeline there; otherwise it falls back to its CMD window. It sets `HF_ENDPOINT=https://hf-mirror.com` for networks that need a Hugging Face mirror.

## Running modes

### Default scriptless mode

No extra switch is required. The defaults keep script assistance, browser review, deterministic timing, and web search disabled while running the complete dual-ASR, review, translation, final-review, validation, Chinese-only export, and copy-export path. An active audio with no usable script is treated as a script mismatch and receives the Japanese second-pass review.

### Script-assisted mode

Place the Japanese script files in `scripts/`, then set:

```dotenv
ENABLE_SCRIPT=1
STEP0_MATCH_SCRIPTS=1
```

STEP0 supports same-name and fuzzy matching, as well as splitting a merged script across multiple audio files. Script content such as dialogue, action notes, psychology notes, headings, and decorative separators is preserved. The current version does not detect scripts until `ENABLE_SCRIPT=1` is set.

Interactive runs ask you to verify the generated mapping. For unattended jobs, read the `SCRIPT_AUTO_VERIFY` warning in [.env.example](.env.example) before enabling automatic confirmation.

### Human-review mode

Set:

```dotenv
ENABLE_HUMAN_REVIEW=1
```

After translation, the pipeline opens a local browser page for flagged segments. If the review is not submitted, the pipeline pauses safely; run it again after completing the review to continue from the cache.

## What happens during a run

1. The pipeline preprocesses the audio and runs `large-v3` plus `large-v3-turbo` locally.
2. It records candidate evidence, quarantines supported fixed-hallucination patterns, and runs bounded rescue transcription where needed.
3. The configured LLM fuses the Japanese transcript, reviews it, translates it into Simplified Chinese, and reviews the translation.
4. Optional browser review lets you inspect difficult segments and edit their text or timing.
5. A full-document review, local rule validation, the Chinese-only export, and audio-named copies under `_transfer/` produce the deliverables for easy batch transfer.

The exact internal stage order, cache contracts, and audit artifacts are documented in the [developer guide](docs/developer-guide.md).

## Optional hard-subtitle video delivery

Hard-subtitle encoding is an on-demand delivery task separate from the subtitle
pipeline. See [Advanced operations](docs/advanced-operations.md) for the
reviewed-SRT requirement, preview workflow, encoding confirmation, and
non-overwriting output behavior.

## Output files

Paths below are relative to `output/`. Each audio job groups artifacts under `asr/`, `evidence/`, `review/`, and `final/`, with `script/` created when needed. Entry points back up and migrate legacy flat outputs while retaining valid caches.

| File | Use |
| --- | --- |
| `<audio>/final/<audio>_final.srt` | Canonical bilingual final; use this for playback or hard-subtitle rendering |
| `<audio>/final/<audio>_cn_only.srt` | Canonical Chinese-only subtitle, when STEP6 is enabled |
| `_transfer/final/<audio>.srt` | Audio-named copy of the bilingual final (STEP7 export, for batch transfer) |
| `_transfer/cn_only/<audio>.srt` | Audio-named copy of the Chinese-only subtitles (STEP8 export, for batch transfer) |
| `<audio>/review/human_reviewed.srt` | Optional human-reviewed checkpoint before the final LLM review |
| `_shared/pipeline.log` | Append-only runtime log for diagnosis |

Intermediate SRT files, ASR evidence, timeline reports, manifests, and review records remain in `output/` for recovery and diagnosis. See the developer documentation before editing or deleting them.

## Requirements

| Item | Requirement |
| --- | --- |
| Operating system | Windows 10/11 is the primary target; macOS/Linux require path adjustments |
| Python | 3.10 or 3.11 recommended; the current Windows venv was also tested with 3.14.5 |
| GPU | Optional but strongly recommended; an NVIDIA GPU with 8 GB VRAM is the tested baseline |
| CPU fallback | Supported through CTranslate2, but substantially slower |
| Memory | 16 GB minimum; 32 GB recommended |
| Disk | Approximately 10 GB free for models and intermediate audio |
| Other | FFmpeg and ffprobe on `PATH`; network access for the first model download and selected APIs |

The project does not depend on PyTorch. GPU detection uses `ctranslate2.get_cuda_device_count()`.

## Known limitations

- Extremely quiet whispers, breath sounds, strong background effects, and clipped or ambiguous speech can still be omitted or mistranscribed.
- Overlapping speakers remain difficult for both Whisper models and may be merged into one cue.
- Hallucination quarantine targets known high-precision patterns; it cannot prove that every remaining sentence is real speech.
- LLM review improves context and translation but can still introduce errors. Human review remains advisable for difficult audio or publication-quality subtitles.
- `ENABLE_DETERMINISTIC_TIMELINE` is experimental and disabled by default.
- Script selection is not automatic: `ENABLE_SCRIPT=1` is currently required before the pipeline checks `scripts/`.

## Documentation

- [Beginner's guide](docs/getting-started.md): install the project and complete a first run.
- [Advanced operations](docs/advanced-operations.md): video-input details, hard subtitles, migration, export, and recovery tasks.
- [Configuration reference](docs/configuration.md): supported settings, defaults, dependencies, and privacy implications.
- [Developer guide](docs/developer-guide.md): stage contracts, caching, architecture, and maintenance validation.
- [Changelog](CHANGELOG.md): released and unreleased changes.

Do not commit `.env`, audio, model files, generated subtitles, logs, or `_cache/` diagnostics.

## Credits

[faster-whisper](https://github.com/SYSTRAN/faster-whisper), [DeepSeek](https://platform.deepseek.com), [Tavily](https://tavily.com), [Exa](https://exa.ai), [FFmpeg](https://ffmpeg.org), [librosa](https://librosa.org), [noisereduce](https://github.com/timsainb/noisereduce), [SciPy](https://scipy.org), [soundfile](https://python-soundfile.readthedocs.io), and [pykakasi](https://github.com/miurahr/pykakasi).

## AI disclosure

The Python scripts and documentation were developed through iterative human–AI collaboration. The human author defined the use case, architecture, parameters, acceptance criteria, listening-based quality decisions, and release policy. AI systems assisted with implementation, documentation, review, and test construction.

## License

GNU General Public License v3.0 or later (`GPL-3.0-or-later`). See [LICENSE](LICENSE). Copyright (c) 2025–2026 Kochiya3309.
