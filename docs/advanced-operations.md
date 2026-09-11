# Advanced Operations

English | [简体中文](advanced-operations.zh-CN.md)

This guide covers user-facing tasks beyond a first run. For installation and a
first subtitle run, see the [Beginner's guide](getting-started.md). For setting
meanings and defaults, see the [Configuration reference](configuration.md).

## Use video input

Use video input when the source is a video rather than a separate audio file.

1. Put `.mp4`, `.mkv`, `.mov`, `.webm`, or `.avi` files in `video/`.
2. Set `ENABLE_VIDEO_PREP=1` in `.env`.
3. Run the normal pipeline with `start.bat` or `venv\Scripts\python.exe run_all.py`.

The pipeline extracts the first audio stream to a managed FLAC under
`output/<audio>/input/` and adds it to the same active input set as ordinary
audio. Do not copy that FLAC into `audio/`. Use `VIDEO_DIR` when `video/` is not
the desired source directory.

### Expected result and first check

After video preparation succeeds, the job has a managed FLAC and source
manifest under `output/<audio>/input/`, then continues through the normal
stages. If it stops before STEP1, first check the source video's first audio
stream and whether another active input has the same base name; use
`output/_shared/pipeline.log` for the first failure message.

## Burn reviewed subtitles into a video

Use this only after reviewing the final SRT.

1. Run `start_burn_subtitles.bat`.
2. Drag in the source video and reviewed SRT when prompted.
3. Optionally provide a preview start time and duration.
4. Choose Standard or Fast, inspect the displayed plan, then enter `y` to encode.

The helper burns subtitles into the first video stream, copies all audio tracks,
does not overwrite the source, and writes a `_subtitled` or
`_preview_subtitled` file beside it. It accepts UTF-8 SRT directly and can offer
a UTF-8 working-copy conversion for recognizable CP932 or GB18030 input after
confirmation.

### Expected result and first check

Success creates the non-overwriting output beside the source video. If encoding
does not start, first check the displayed media and encoding plan in the helper
window, then rerun `start_burn_subtitles.bat` from the project root; do not use
an unreviewed or unreadable SRT as a substitute.

## Inspect or migrate legacy flat outputs

Preview the proposed migration first:

```powershell
venv\Scripts\python.exe migrate_output.py --output-dir output
```

To back up and apply migration without starting ASR or LLM stages:

```powershell
venv\Scripts\python.exe migrate_output.py --output-dir output --apply
```

To roll back unchanged migrated artifacts, use the actual journal path:

```powershell
venv\Scripts\python.exe migrate_output.py --rollback "output/_shared/layout_backups/<migration-id>/migration.json"
```

Migration preserves unknown user files and stops on conflicts rather than
overwriting them. Starting a current pipeline entry point classifies legacy
files again; use rollback only when returning to the old layout/code context.

### Expected result and first check

Preview prints a plan without changing files. A successful `--apply` creates
the journal at the displayed `output/_shared/layout_backups/<migration-id>/migration.json`
path. If migration stops, resolve the reported conflict or unexpected file
before rerunning `--apply`; do not delete output files to force it through.

## Export transfer copies on demand

The normal pipeline runs STEP7 and STEP8 when their upstream stages are enabled.
Use these helpers only when standalone export is needed:

```powershell
venv\Scripts\python.exe export_final_srt.py
venv\Scripts\python.exe export_cn_only_srt.py
```

Both helpers accept `--output-dir` and `--transfer-dir`. They retain canonical
subtitles under each audio job and create audio-named copies under
`_transfer/final/` or `_transfer/cn_only/`.

### Expected result and first check

Success leaves the canonical subtitle in `output/<audio>/final/` and adds its
copy under the selected transfer directory. If an export is absent or fails,
first verify that canonical final exists and that its upstream STEP4 or STEP6
stage is enabled; the helpers do not reconstruct missing finals.

## Resume or rerun safely

Run `start.bat` or:

```powershell
venv\Scripts\python.exe run_all.py
```

### Expected result and first check

An interrupted run reuses only artifacts whose cache checks still pass. When a
rerun stops, inspect `output/_shared/pipeline.log`, identify the first failing
stage and active input, then correct that cause before retrying. Do not start by
deleting caches or unrelated output jobs.

The pipeline checks cache validity before reuse. To start an isolated
experiment, set `OUTPUT_DIR` to an empty directory. Do not bulk-delete `output/`
unless you understand artifact dependencies.
