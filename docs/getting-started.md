# Beginner's Guide

English | [简体中文](getting-started.zh-CN.md)

This guide gets a Windows user through one basic Japanese-ASMR-to-Japanese–Chinese
subtitle run. Start without scripts, human review, video input, or web search.
For every setting, default, dependency, and safety implication, use the
[Configuration Reference](configuration.md).

## 1. Get the project

Clone the repository, then enter its root directory:

```powershell
git clone https://github.com/Kochiya3309/asmr-subtitle-pipeline.git
cd asmr-subtitle-pipeline
```

You can instead download and extract the repository ZIP. In either case, run
the remaining commands from the directory containing `start.bat`, `run_all.py`,
and `requirements.txt`.

## 2. Install prerequisites

Install Python 3.10 or 3.11, FFmpeg, and ffprobe. Then, from the project root:

```powershell
python --version
ffmpeg -version
ffprobe -version
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Each version command should print version information. If `python`, `ffmpeg`,
or `ffprobe` is unavailable, finish its installation, add the required program
directory to `PATH`, then reopen PowerShell. If `python --version` is not 3.10
or 3.11, run `py -0p` to list installed interpreters and create the environment
with a supported version, for example `py -3.11 -m venv venv`.

## 3. Create the minimum configuration

Copy the template:

```powershell
Copy-Item .env.example .env
```

Set your real LLM key and retain these first-run-safe values:

```dotenv
OPENAI_API_KEY=enter_your_real_API_key_here
ENABLE_SEARCH=0
ENABLE_SCRIPT=0
STEP0_MATCH_SCRIPTS=0
ENABLE_HUMAN_REVIEW=0
```

With the default DeepSeek endpoint, the key is normally the only required LLM
change. For another OpenAI-compatible provider, also set the provider-supplied
`OPENAI_BASE_URL` and `OPENAI_MODEL`. Never commit, share, screenshot, or put
the real key in a bug report.

## 4. Add one audio file

Put one short test file directly in `audio/`. Supported first-run input formats
are `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, and `.opus`. Do not put two files
with the same base name, such as `sample.mp3` and `sample.wav`, in the directory.

## 5. Run and find the result

Double-click `start.bat`, or run:

```powershell
venv\Scripts\python.exe run_all.py
```

The first run downloads several gigabytes of model files. After success, load
`output/<audio>/final/<audio>_final.srt` in a media player. The Chinese-only
result is `output/<audio>/final/<audio>_cn_only.srt`.

Audio stays local; required subtitle text is sent to the configured LLM. Keep
`ENABLE_SEARCH=0` for private or NSFW material unless lookup is necessary.

## Next steps

Use the [Configuration Reference](configuration.md) before enabling scripts,
human review, video preparation, timing controls, search, or stage switches.
It is the authoritative source for those settings and their dependencies.
