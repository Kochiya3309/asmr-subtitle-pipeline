# SPDX-License-Identifier: GPL-3.0-or-later
"""Media probing, FFmpeg planning, benchmarking, and subtitle encoding."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from burn_subtitles_style import SubtitleStyle


HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def _json_command(command: list[str]) -> dict:
    result = subprocess.run(
        command, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout)


def probe_media(video: Path) -> dict:
    try:
        data = _json_command([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video),
        ])
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取视频流信息：{exc}") from exc
    streams = data.get("streams", [])
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not isinstance(video_stream, dict):
        raise ValueError("视频中没有可处理的视频流")
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    transfer = str(video_stream.get("color_transfer") or "").lower()
    bits = str(video_stream.get("bits_per_raw_sample") or video_stream.get("bits_per_sample") or "")
    pix_fmt = str(video_stream.get("pix_fmt") or "")
    is_hdr = transfer in HDR_TRANSFERS or "10" in bits or "p10" in pix_fmt
    return {
        "video": video_stream,
        "audio": audio_streams,
        "format": data.get("format") or {},
        "is_hdr": is_hdr,
    }


def available_encoders() -> set[str]:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], check=True,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"无法读取 FFmpeg 编码器列表：{exc}") from exc
    return {
        line.split()[1] for line in result.stdout.splitlines()
        if line.startswith(" ") and len(line.split()) >= 2
    }


def subtitle_is_utf8(path: Path) -> bool:
    try:
        path.read_text(encoding="utf-8-sig")
        return True
    except UnicodeDecodeError:
        return False


def conversion_target(subtitle: Path) -> Path:
    return subtitle.with_name(f"{subtitle.stem}.utf8{subtitle.suffix}")


def convert_subtitle_to_utf8(source: Path, target: Path) -> None:
    for encoding in ("utf-8-sig", "utf-8", "cp932", "gb18030"):
        try:
            text = source.read_text(encoding=encoding)
            target.write_text(text, encoding="utf-8", newline="\n")
            return
        except UnicodeDecodeError:
            continue
    raise UnicodeError("无法识别字幕文本编码；请在 Subtitle Edit 中另存为 UTF-8 SRT")


@contextmanager
def subtitle_for_estimation(source: Path, *, is_utf8: bool) -> Iterator[Path]:
    """Yield a UTF-8 subtitle path and remove any temporary conversion."""
    if is_utf8:
        yield source
        return
    with tempfile.TemporaryDirectory(prefix="whisper_asmr_subtitle_") as folder:
        converted = Path(folder) / f"{source.stem}.utf8{source.suffix}"
        convert_subtitle_to_utf8(source, converted)
        yield converted


def select_encoding(media: dict, encoders: set[str]) -> tuple[str, list[str], bool, str]:
    """Return encoder, extra video args, uses SDR conversion, explanation."""
    hdr = media["is_hdr"]
    if hdr:
        if "hevc_nvenc" in encoders:
            return "hevc_nvenc", ["-preset", "p5", "-cq", "19", "-pix_fmt", "p010le"], False, "HDR/10-bit：HEVC NVENC"
        if "libx265" in encoders:
            return "libx265", ["-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p10le"], False, "HDR/10-bit：CPU libx265 10-bit 降级"
        if "h264_nvenc" in encoders:
            return "h264_nvenc", ["-preset", "p5", "-cq", "19", "-pix_fmt", "yuv420p"], True, "HDR/10-bit：转 SDR 后使用 H.264 NVENC"
        if "libx264" in encoders:
            return "libx264", ["-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p"], True, "HDR/10-bit：转 SDR 后使用 CPU libx264"
    else:
        if "h264_nvenc" in encoders:
            return "h264_nvenc", ["-preset", "p5", "-cq", "19", "-pix_fmt", "yuv420p"], False, "SDR：H.264 NVENC"
        if "libx264" in encoders:
            return "libx264", ["-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p"], False, "SDR：CPU libx264 降级"
    raise RuntimeError("FFmpeg 不提供可用的 H.264/H.265 编码器")


def source_video_bitrate(media: dict) -> int | None:
    """Return the source video bitrate, deriving it from the container if needed."""
    try:
        bitrate = int((media.get("video") or {}).get("bit_rate"))
    except (TypeError, ValueError):
        bitrate = 0
    if bitrate > 0:
        return bitrate

    try:
        container_bitrate = int((media.get("format") or {}).get("bit_rate"))
    except (TypeError, ValueError):
        return None
    audio_bitrate = 0
    for stream in media.get("audio") or []:
        try:
            audio_bitrate += max(0, int(stream.get("bit_rate")))
        except (AttributeError, TypeError, ValueError):
            continue
    derived = container_bitrate - audio_bitrate
    return derived if derived > 0 else None


def _remove_video_options(args: list[str], options: set[str]) -> list[str]:
    cleaned: list[str] = []
    index = 0
    while index < len(args):
        if args[index] in options:
            index += 2
            continue
        cleaned.append(args[index])
        index += 1
    return cleaned


def apply_encoding_profile(
    video_args: list[str],
    encoder: str,
    *,
    profile: str,
    source_bitrate: int | None = None,
) -> list[str]:
    """Apply standard, fast, high-quality, or source-bitrate settings."""
    if profile == "standard":
        return list(video_args)
    args = list(video_args)
    try:
        preset_index = args.index("-preset") + 1
    except ValueError:
        preset_index = None
    if profile == "fast":
        if preset_index is not None:
            args[preset_index] = "p3" if encoder.endswith("_nvenc") else "faster"
        return args
    if profile == "high":
        if preset_index is not None:
            args[preset_index] = "p6" if encoder.endswith("_nvenc") else "slow"
        quality_option = "-cq" if encoder.endswith("_nvenc") else "-crf"
        try:
            args[args.index(quality_option) + 1] = "17"
        except ValueError:
            args.extend([quality_option, "17"])
        return args
    if profile != "source":
        raise ValueError(f"未知编码预设：{profile}")

    args = _remove_video_options(
        args, {"-cq", "-crf", "-rc", "-b:v", "-maxrate", "-bufsize"},
    )
    try:
        preset_index = args.index("-preset") + 1
    except ValueError:
        preset_index = None
    if preset_index is not None:
        args[preset_index] = "p6" if encoder.endswith("_nvenc") else "slow"

    if source_bitrate is None:
        args.extend(["-cq" if encoder.endswith("_nvenc") else "-crf", "15"])
        return args

    if encoder.endswith("_nvenc"):
        args.extend([
            "-rc", "vbr",
            "-b:v", str(source_bitrate),
            "-maxrate", str(round(source_bitrate * 1.5)),
            "-bufsize", str(source_bitrate * 2),
        ])
    else:
        args.extend(["-b:v", str(source_bitrate)])
    return args


def encoding_profile_note(profile: str, source_bitrate: int | None) -> str:
    if profile == "standard":
        return "标准（质量优先）"
    if profile == "fast":
        return "快速（速度优先，文件可能稍大）"
    if profile == "high":
        return "高质量（CQ/CRF 17，细节优先，文件更大）"
    if source_bitrate is None:
        return "接近原片（源码率不可读，改用高质量 CQ/CRF 15）"
    return f"接近原片（目标视频码率 {source_bitrate / 1_000_000:.2f} Mbps，速度较慢）"


def media_duration_seconds(media: dict) -> float | None:
    try:
        duration = float((media.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def estimate_profile_times(
    video: Path,
    subtitle: Path,
    media: dict,
    encoder: str,
    base_video_args: list[str],
    *,
    subtitle_style: SubtitleStyle,
    hdr_to_sdr: bool,
    target_duration: float | None,
    subtitle_utf8: bool,
) -> dict[str, float | None]:
    estimates = {name: None for name in ("standard", "fast", "high", "source")}
    source_duration = media_duration_seconds(media)
    if not source_duration or not target_duration:
        return estimates

    sample_duration = min(8.0, source_duration)
    sample_start = max(0.0, (source_duration - sample_duration) * 0.35)
    with subtitle_for_estimation(subtitle, is_utf8=subtitle_utf8) as benchmark_subtitle:
        filtergraph = build_filter(
            benchmark_subtitle, hdr_to_sdr=hdr_to_sdr, style=subtitle_style,
        )
        source_bitrate = source_video_bitrate(media)
        warmup_args = apply_encoding_profile(
            base_video_args, encoder, profile="standard", source_bitrate=source_bitrate,
        )
        warmup_command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{sample_start:.3f}", "-t", f"{min(2.0, sample_duration):.3f}",
            "-i", str(video), "-map", "0:v:0", "-vf", filtergraph,
            "-an", "-c:v", encoder, *warmup_args, "-f", "null", os.devnull,
        ]
        try:
            subprocess.run(
                warmup_command, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            pass
        print("\n正在用 8 秒短片测算四个预设的预计耗时，请稍候……")
        for profile in estimates:
            video_args = apply_encoding_profile(
                base_video_args, encoder, profile=profile, source_bitrate=source_bitrate,
            )
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-ss", f"{sample_start:.3f}", "-t", f"{sample_duration:.3f}",
                "-i", str(video), "-map", "0:v:0", "-vf", filtergraph,
                "-an", "-c:v", encoder, *video_args, "-f", "null", os.devnull,
            ]
            started = time.monotonic()
            try:
                subprocess.run(
                    command, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.CalledProcessError):
                continue
            elapsed = max(0.001, time.monotonic() - started)
            estimates[profile] = target_duration * elapsed / sample_duration
    return estimates


def _filter_path(path: Path) -> str:
    # FFmpeg filter syntax requires the drive colon to be escaped.
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def build_filter(
    subtitle: Path,
    *,
    hdr_to_sdr: bool,
    style: SubtitleStyle | None = None,
) -> str:
    active_style = style or SubtitleStyle(font_name="Microsoft YaHei")
    subtitle_filter = (
        f"subtitles=filename='{_filter_path(subtitle)}':charenc=UTF-8:"
        f"force_style='{active_style.to_ass()}'"
    )
    if not hdr_to_sdr:
        return subtitle_filter
    # zscale/tonemap is the compatible SDR fallback; FFmpeg will report a clear
    # missing-filter error instead of silently producing an unlabelled result.
    return (
        "zscale=t=linear:npl=100,tonemap=mobius,"
        "zscale=t=bt709:m=bt709:p=bt709,format=yuv420p," + subtitle_filter
    )


def format_time(seconds: float | None) -> str:
    if seconds is None:
        return "计算中"
    rounded = max(0, round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def _show_progress(current: float, total: float | None, started: float) -> None:
    elapsed = time.monotonic() - started
    if total is None:
        print(f"\r  编码进度：已处理 {format_time(current)}｜已用 {format_time(elapsed)}", end="", flush=True)
        return
    ratio = min(1.0, max(0.0, current / total))
    width = 24
    filled = round(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    remaining = (elapsed * (total - current) / current) if current > 0 else None
    print(
        f"\r  编码进度：[{bar}] {ratio:6.2%}｜已用 {format_time(elapsed)}｜预计剩余 {format_time(remaining)}",
        end="", flush=True,
    )


def run_encoding(command: list[str], *, total_seconds: float | None) -> None:
    """Run FFmpeg while translating machine progress into a compact status line."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout is None:
        raise RuntimeError("无法读取 FFmpeg 编码进度")
    started = time.monotonic()
    current = 0.0
    try:
        for raw_line in process.stdout:
            key, separator, value = raw_line.strip().partition("=")
            if not separator:
                continue
            if key in {"out_time_us", "out_time_ms"}:
                try:
                    # FFmpeg progress reports these values in microseconds.
                    current = max(current, int(value) / 1_000_000)
                except ValueError:
                    continue
            elif key == "progress":
                _show_progress(current, total_seconds, started)
        return_code = process.wait()
    finally:
        process.stdout.close()
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)
    _show_progress(total_seconds or current, total_seconds, started)
    print()


def encode_subtitled_video(
    *,
    video: Path,
    subtitle: Path,
    output: Path,
    encoder: str,
    video_args: list[str],
    hdr_to_sdr: bool,
    subtitle_style: SubtitleStyle,
    time_args: list[str],
    total_seconds: float | None,
) -> None:
    """Encode a staged hard-subtitle output and atomically publish it."""
    staged = output.with_name(f".{output.stem}.pending-{time.time_ns()}{output.suffix}")
    filtergraph = build_filter(
        subtitle, hdr_to_sdr=hdr_to_sdr, style=subtitle_style,
    )
    container_args = ["-movflags", "+faststart"] if video.suffix.lower() in {".mp4", ".mov"} else []
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
        "-progress", "pipe:1", "-nostats", *time_args, "-i", str(video),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-vf", filtergraph,
        "-c:v", encoder, *video_args, "-c:a", "copy", *container_args, str(staged),
    ]
    try:
        run_encoding(command, total_seconds=total_seconds)
        os.replace(staged, output)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
