# Beginner's Guide

English | [简体中文](getting-started.zh-CN.md)

This guide is for Windows users who are running the project for the first time and only want to generate Japanese–Chinese subtitles from Japanese ASMR audio. Complete one run without a script first. Enable scripts, human review, and advanced switches only after the basic pipeline works.

The project runs two Whisper models locally for speech recognition. Japanese fusion, review, translation, and final review send **text** to your configured OpenAI-compatible LLM. Audio is not uploaded. Keep web search disabled when processing private or NSFW material.

## What you will get

After a successful run, `output/` contains:

| File | Purpose |
| --- | --- |
| `*_final.srt` | Final bilingual Japanese–Chinese subtitles; load this file first in your media player |
| `*_cn_only.srt` | Chinese-only subtitles |
| `pipeline.log` | Runtime log; check this first when an error occurs |

Do not immediately delete the other files in `output/`. They are intermediate artifacts used for resuming, human review, and troubleshooting.

## 1. Install the required software

You need:

- Python 3.10 or 3.11 (recommended);
- FFmpeg;
- an API key for an OpenAI-compatible LLM service.

Open PowerShell and run the following commands to confirm that Python, FFmpeg, and ffprobe are available:

```powershell
python --version
ffmpeg -version
ffprobe -version
```

All three commands should print version information. If `python` is not found, reinstall Python and select “Add Python to PATH.” If `ffmpeg` or `ffprobe` is not found, finish installing FFmpeg, add its `bin` directory to `PATH`, then close and reopen PowerShell.

## 2. Get the project

Download and extract the ZIP archive from GitHub, or use Git:

```powershell
git clone https://github.com/Kochiya3309/asmr-subtitle-pipeline.git
cd asmr-subtitle-pipeline
```

Run all subsequent commands from the project root—the directory containing `start.bat`, `run_all.py`, and `requirements.txt`.

## 3. Create an isolated environment and install dependencies

Run these commands from the project root:

```powershell
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

When `(venv)` appears at the beginning of the command prompt, the isolated environment is active. It affects only this project and does not modify globally installed Python packages.

If `python -m venv venv` does not use Python 3.10 or 3.11, run `py -0p` in PowerShell to list installed versions, then use a specific version, for example:

```powershell
py -3.11 -m venv venv
```

## 4. Create and configure `.env`

Copy the template:

```powershell
Copy-Item .env.example .env
```

Open `.env` in a text editor. At minimum, fill in and confirm these settings:

```dotenv
OPENAI_API_KEY=enter_your_real_API_key_here

# First run: no web search, script, or human review.
ENABLE_SEARCH=0
ENABLE_SCRIPT=0
STEP0_MATCH_SCRIPTS=0
ENABLE_HUMAN_REVIEW=0
```

If you use the default DeepSeek endpoint, the API key is usually the only required change. For another OpenAI-compatible service, also enter the endpoint and model name supplied by that provider:

```dotenv
OPENAI_BASE_URL=https://provider-endpoint.example/v1
OPENAI_MODEL=provider-model-id
```

`.env` contains secrets. Do not commit it to Git, take screenshots of it, send it to anyone, or include it in a bug report.

## 5. Add audio

Place the audio files directly in the project's `audio/` directory. Supported formats are `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, and `.opus`.

For the first test, use one short audio file. Do not place files with the same base name but different extensions together, such as `sample.mp3` and `sample.wav`. They would generate the same output name, so the pipeline refuses to run to prevent overwriting files.

## 6. Run the pipeline

The simplest method is to double-click `start.bat` in the project root. The window remains open after the run finishes.

You can also run the pipeline from PowerShell with the venv active:

```powershell
venv\Scripts\python.exe run_all.py
```

The first run downloads several gigabytes of model files. The required time depends on your network and GPU. `start.bat` configures a Hugging Face mirror. GPU usage may not remain high throughout the run because local ASR, disk I/O, and remote LLM calls alternate.

## 7. View the results

When the run finishes, open `output/` and load the `*_final.srt` file that matches the audio name in your media player. Use `*_cn_only.srt` if you only need Chinese subtitles.

Do not treat `*_ensemble.srt`, `*_reviewed.srt`, or `*_zh.srt` as final deliverables. They are checkpoints from the fusion, Japanese review, and translation stages and may still be changed by later stages.

## Features to enable later

### Use an untimed script

Place Japanese scripts in `scripts/`, then change these settings to `1`:

```dotenv
ENABLE_SCRIPT=1
STEP0_MATCH_SCRIPTS=1
```

The first run asks you to confirm the audio-to-script mapping. A single merged script can correspond to multiple audio files; verify the split before continuing.

### Review difficult segments manually

Change this setting to `1`:

```dotenv
ENABLE_HUMAN_REVIEW=1
```

After translation, the pipeline opens a local page at `127.0.0.1:8765`. It can play audio, display Japanese, romaji, Chinese, and context, and let you edit text and timestamps. The pipeline pauses until you submit the review. After submitting, run the same command again to continue.

### Web search

Configure `TAVILY_API_KEY` or `EXA_API_KEY` only when term or cultural-background lookup is genuinely needed, then set:

```dotenv
ENABLE_SEARCH=1
```

The search provider receives automatically generated queries. Keep this setting at `0` for private or NSFW material in most cases.

## Can I interrupt or rerun the pipeline?

Yes. Run `start.bat` or `run_all.py` again. The system verifies whether existing artifacts still correspond to the current audio, configuration, and upstream subtitles, then resumes from valid caches where possible.

The following changes cause the affected stages to run again:

- modifying the source audio;
- modifying `.env` settings that affect a stage;
- editing or deleting an upstream SRT file;
- modifying a script or confirming the script mapping again;
- deleting manifests or intermediate artifacts from `output/`.

To test from the beginning, create a separate empty output directory and point `OUTPUT_DIR` to it. Do not bulk-delete `output/` unless you understand the artifact dependencies.

## Troubleshooting

| Symptom | What to check first |
| --- | --- |
| `python` is not found | Reopen PowerShell; confirm that Python is on `PATH`; run `python --version` |
| `ffmpeg` or `ffprobe` is not found | Confirm that FFmpeg's `bin` directory is on `PATH`; reopen PowerShell |
| `venv\\Scripts\\python.exe` is not found | Step 3 has not been completed, or the current directory is not the project root |
| LLM request fails | Check the API key, Base URL, model name, and account quota in `.env`; do not put the real key in logs or screenshots |
| First model download fails | Check the network and available disk space; retrying normally reuses completed downloads |
| `127.0.0.1` refuses the connection | Confirm that the human-review stage is still running; check whether another program is using `HUMAN_REVIEW_PORT` |
| No `*_final.srt` is generated | Start with the last terminal error and `output/pipeline.log`; do not inspect only intermediate SRT files |

## What to read next

- [README.md](../README.md): feature overview, operating modes, and system requirements.
- [Developer guide](developer-guide.md): the complete STEP0–STEP6 pipeline, cache contracts, audit artifacts, and failure boundaries.
- [.env.example](../.env.example): documentation for every configurable switch.
