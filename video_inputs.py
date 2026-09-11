# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional video-to-audio preparation for one pipeline invocation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

from asr_evidence import canonical_fingerprint, file_fingerprint, write_json_atomic
from output_layout import output_file, validate_base


VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
EXTRACTION_CONTRACT_VERSION = 1


def _content_identity(path: Path) -> dict:
    """Stable cache identity: source timestamps alone must not force extraction."""
    fingerprint = file_fingerprint(str(path))
    return {
        "name": fingerprint["name"],
        "size_bytes": fingerprint["size_bytes"],
        "sha256": fingerprint["sha256"],
    }


def _probe_video(path: Path, ffprobe_bin: str) -> dict:
    command = [
        ffprobe_bin, "-v", "error", "-show_streams", "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot inspect video {path.name}: {exc}") from exc
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise ValueError(f"ffprobe returned no stream list for {path.name}")
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not isinstance(audio, dict):
        raise ValueError(f"video has no audio stream: {path.name}")
    index = audio.get("index")
    if not isinstance(index, int):
        raise ValueError(f"video audio stream has no index: {path.name}")
    return {
        "index": index,
        "codec_name": audio.get("codec_name"),
        "channels": audio.get("channels"),
        "sample_rate": audio.get("sample_rate"),
        "language": (audio.get("tags") or {}).get("language"),
    }


def _manifest_is_current(manifest_path: Path, source: Path, output: Path, config: dict) -> bool:
    if not manifest_path.is_file() or not output.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        return (
            manifest.get("schema_version") == 1
            and manifest.get("artifact_type") == "video_audio_extract"
            and manifest.get("status") == "complete"
            and manifest.get("generation_fingerprint") == canonical_fingerprint(config)
            and manifest.get("source_identity") == _content_identity(source)
            and manifest.get("output_identity") == _content_identity(output)
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


def _extract(source: Path, output: Path, ffmpeg_bin: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{output.stem}.pending-{time.time_ns()}{output.suffix}")
    command = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
        "-map", "0:a:0", "-vn", "-c:a", "flac", str(staged),
    ]
    try:
        subprocess.run(
            command, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if not staged.is_file() or staged.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced no audio output")
        os.replace(staged, output)
    except subprocess.CalledProcessError as exc:
        diagnostic = (exc.stderr or exc.stdout or "").strip()
        tail = "\n".join(diagnostic.splitlines()[-8:])
        message = f"音频提取失败：{source.name}"
        if tail:
            message += f"\nFFmpeg 诊断（末尾）：\n{tail}"
        raise RuntimeError(message) from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 FFmpeg：{exc}") from exc
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def prepare_video_inputs(video_dir: os.PathLike | str, output_dir: os.PathLike | str,
                         *, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe") -> dict[str, Path]:
    """Extract first audio streams from videos and return base-to-FLAC paths.

    The generated FLACs live under the formal per-task input directory and are
    reusable only while both source bytes and extraction settings still match.
    """
    root = Path(video_dir).resolve()
    if not root.exists():
        return {}
    if not root.is_dir():
        raise NotADirectoryError(f"VIDEO_DIR is not a directory: {root}")
    videos = sorted((item for item in root.iterdir()
                     if item.is_file() and item.suffix.lower() in VIDEO_SUFFIXES
                     and not item.stem.endswith(("_subtitled", "_preview_subtitled"))),
                    key=lambda item: item.name.lower())
    by_base: dict[str, Path] = {}
    for source in videos:
        base = validate_base(source.stem)
        if base in by_base:
            raise ValueError(f"video base name conflict: {by_base[base].name} / {source.name}")
        by_base[base] = source

    prepared: dict[str, Path] = {}
    for base, source in by_base.items():
        stream = _probe_video(source, ffprobe_bin)
        # Downstream stages derive the task key from the input filename.  Keep
        # that key stable even though the FLAC is stored in a managed input dir.
        output = Path(output_dir).resolve() / base / "input" / f"{base}.flac"
        manifest_path = Path(output_file(output_dir, f"{base}_source_manifest.json"))
        config = {
            "contract_version": EXTRACTION_CONTRACT_VERSION,
            "audio_stream_index": stream["index"], "audio_codec": "flac",
            "ffmpeg_map": "0:a:0",
        }
        details = ["第一条音轨", str(stream.get("codec_name") or "未知编码")]
        if stream.get("sample_rate"):
            details.append(f"{stream['sample_rate']} Hz")
        if stream.get("channels"):
            details.append(f"{stream['channels']} 声道")
        print(f"\n  [视频输入] {source.name}")
        if _manifest_is_current(manifest_path, source, output, config):
            print("    ↳ 已有可用音频缓存，跳过提取")
            print(f"    ✓ {output}")
        else:
            if output.exists() or manifest_path.exists():
                print("    ↳ 缓存已过期，将重新提取")
            print(f"    ↳ 选中：{' | '.join(details)}")
            print("    ↳ 正在提取为无损 FLAC，请稍候…")
            _extract(source, output, ffmpeg_bin)
            write_json_atomic(str(manifest_path), {
                "schema_version": 1,
                "artifact_type": "video_audio_extract",
                "status": "complete",
                "generation_fingerprint": canonical_fingerprint(config),
                "source": file_fingerprint(str(source)),
                "source_identity": _content_identity(source),
                "source_path": str(source),
                "selected_audio_stream": stream,
                "output": file_fingerprint(str(output)),
                "output_identity": _content_identity(output),
            })
            print(f"    ✓ 完成：{output}")
        prepared[base] = output
    return prepared
