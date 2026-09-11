# SPDX-License-Identifier: GPL-3.0-or-later
"""Interactive, opt-in hard-subtitle delivery helper.

This is intentionally separate from run_all.py: subtitle production remains
cheap and resumable, while video encoding is only performed on demand.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
FONT_CANDIDATES = (
    ("Microsoft YaHei", ("msyh", "msyhl")),
    ("Noto Sans CJK SC", ("notosanscjk", "notoserifcjk")),
    ("Microsoft JhengHei", ("msjh",)),
    ("SimSun", ("simsun", "simsunb")),
)


def _dragged_path(label: str) -> Path:
    value = input(f"拖入或粘贴{label}路径：").strip().strip('"').strip("'")
    if not value:
        raise ValueError(f"未提供{label}路径")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label}不存在：{path}")
    return path


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
        result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], check=True,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"无法读取 FFmpeg 编码器列表：{exc}") from exc
    return {line.split()[1] for line in result.stdout.splitlines()
            if line.startswith(" ") and len(line.split()) >= 2}


def select_font() -> tuple[str, bool]:
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    names = {path.stem.casefold() for path in fonts_dir.glob("*.*")} if fonts_dir.is_dir() else set()
    for family, markers in FONT_CANDIDATES:
        if any(any(marker in name for marker in markers) for name in names):
            return family, family != FONT_CANDIDATES[0][0]
    # libass can still use its own configured fallback; make that explicit.
    return "sans-serif", True


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


def select_encoding_profile() -> tuple[str, bool]:
    """Let the user trade compression efficiency for faster delivery."""
    choice = input(
        "编码预设：[1] 标准（默认，质量优先） [2] 快速（速度优先）[1/2] "
    ).strip()
    if choice in {"", "1"}:
        return "标准（质量优先）", False
    if choice == "2":
        return "快速（速度优先，文件可能稍大）", True
    raise ValueError("编码预设只能输入 1 或 2")


def apply_encoding_profile(video_args: list[str], encoder: str, *, fast: bool) -> list[str]:
    """Keep the standard settings intact, with a modest fast preset on demand."""
    if not fast:
        return video_args
    args = list(video_args)
    try:
        preset_index = args.index("-preset") + 1
    except ValueError:
        return args
    args[preset_index] = "p3" if encoder.endswith("_nvenc") else "faster"
    return args


def _filter_path(path: Path) -> str:
    # FFmpeg filter syntax requires the drive colon to be escaped.
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def build_filter(subtitle: Path, *, hdr_to_sdr: bool) -> str:
    style = (
        "FontName=Microsoft YaHei,FontSize=32,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,Outline=2,Shadow=0,Alignment=2,MarginV=54"
    )
    subtitle_filter = f"subtitles=filename='{_filter_path(subtitle)}':charenc=UTF-8:force_style='{style}'"
    if not hdr_to_sdr:
        return subtitle_filter
    # zscale/tonemap is the compatible SDR fallback; FFmpeg will report a clear
    # missing-filter error instead of silently producing an unlabelled result.
    return (
        "zscale=t=linear:npl=100,tonemap=mobius,"
        "zscale=t=bt709:m=bt709:p=bt709,format=yuv420p," + subtitle_filter
    )


def _time_options() -> tuple[list[str], str]:
    start = input("试压起点（留空=完整视频）：").strip()
    duration = input("试压时长秒数（留空=完整视频）：").strip()
    if bool(start) != bool(duration):
        raise ValueError("试压必须同时填写起点和时长")
    if not start:
        return [], "完整视频"
    try:
        if float(duration) <= 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError("试压时长必须为正数") from exc
    return ["-ss", start, "-t", duration], f"试压：{start}s 起、持续 {duration}s"


def media_duration_seconds(media: dict) -> float | None:
    try:
        duration = float((media.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _format_time(seconds: float | None) -> str:
    if seconds is None:
        return "计算中"
    rounded = max(0, round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def _show_progress(current: float, total: float | None, started: float) -> None:
    elapsed = time.monotonic() - started
    if total is None:
        print(f"\r  编码进度：已处理 {_format_time(current)}｜已用 {_format_time(elapsed)}", end="", flush=True)
        return
    ratio = min(1.0, max(0.0, current / total))
    width = 24
    filled = round(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    remaining = (elapsed * (total - current) / current) if current > 0 else None
    print(
        f"\r  编码进度：[{bar}] {ratio:6.2%}｜已用 {_format_time(elapsed)}｜预计剩余 {_format_time(remaining)}",
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


def main() -> None:
    print("硬字幕压制（按 Ctrl+C 随时取消；不会修改原视频）")
    video = _dragged_path("视频文件")
    subtitle = _dragged_path("SRT 字幕文件")
    if subtitle.suffix.lower() != ".srt":
        raise ValueError("只接受 .srt 字幕文件")
    media = probe_media(video)
    encoders = available_encoders()
    encoder, video_args, hdr_to_sdr, encoding_note = select_encoding(media, encoders)
    profile_note, fast_profile = select_encoding_profile()
    video_args = apply_encoding_profile(video_args, encoder, fast=fast_profile)
    font, font_fallback = select_font()
    # Keep the chosen fallback explicit in the style string rather than relying
    # on an unreported libass substitution.
    subtitle_utf8 = subtitle_is_utf8(subtitle)
    active_subtitle = subtitle if subtitle_utf8 else conversion_target(subtitle)
    time_args, time_note = _time_options()
    suffix = "_preview_subtitled" if time_args else "_subtitled"
    output = video.with_name(f"{video.stem}{suffix}{video.suffix}")
    if output.exists():
        raise FileExistsError(f"输出文件已存在，不会覆盖：{output}")

    print("\n预检摘要")
    print(f"  视频：{video}")
    print(f"  字幕：{subtitle}")
    print(f"  输出：{output}")
    print(f"  视频流：第一条；音轨：保留 {len(media['audio'])} 条")
    print(f"  编码：{encoding_note}")
    print(f"  预设：{profile_note}")
    print(f"  字体：{font}" + ("（降级）" if font_fallback else ""))
    print(f"  字幕编码：{'UTF-8' if subtitle_utf8 else '将转换为 UTF-8 校对副本'}")
    print(f"  模式：{time_note}")
    if hdr_to_sdr:
        print("  注意：本次会把 HDR/10-bit 色调映射为 SDR，以提高兼容性。")
    if input("确认开始编码？[y/N] ").strip().lower() != "y":
        print("已取消，未生成视频。")
        return
    if not subtitle_utf8:
        convert_subtitle_to_utf8(subtitle, active_subtitle)

    staged = output.with_name(f".{output.stem}.pending-{time.time_ns()}{output.suffix}")
    filtergraph = build_filter(active_subtitle, hdr_to_sdr=hdr_to_sdr)
    # Replace the requested font after build_filter so the fallback is reflected.
    filtergraph = filtergraph.replace("FontName=Microsoft YaHei", f"FontName={font}")
    container_args = ["-movflags", "+faststart"] if video.suffix.lower() in {".mp4", ".mov"} else []
    preview_duration = float(time_args[-1]) if time_args else None
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *time_args, "-i", str(video),
               "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-vf", filtergraph,
               "-c:v", encoder, *video_args, "-c:a", "copy", *container_args, str(staged)]
    try:
        run_encoding(command, total_seconds=preview_duration or media_duration_seconds(media))
        os.replace(staged, output)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
    print(f"完成：{output}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
