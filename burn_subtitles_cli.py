# SPDX-License-Identifier: GPL-3.0-or-later
"""Interactive command-line workflow for hard-subtitle delivery."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from burn_subtitles_engine import (
    apply_encoding_profile,
    available_encoders,
    conversion_target,
    convert_subtitle_to_utf8,
    encode_subtitled_video,
    encoding_profile_note,
    estimate_profile_times,
    format_time,
    media_duration_seconds,
    probe_media,
    select_encoding,
    source_video_bitrate,
    subtitle_is_utf8,
)
from burn_subtitles_style import (
    ALIGNMENT_NAMES,
    SubtitleStyle,
    ass_number,
    parse_alignment,
    parse_ass_colour,
    parse_font_name,
    parse_nonnegative_integer,
    parse_nonnegative_number,
    parse_positive_number,
    select_font,
)

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows launchers are the primary UI.
    msvcrt = None


def _dragged_path(label: str) -> Path:
    value = input(f"拖入或粘贴{label}路径：").strip().strip('"').strip("'")
    if not value:
        raise ValueError(f"未提供{label}路径")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label}不存在：{path}")
    return path


def _select_menu(
    title: str,
    options: list[tuple[str, object]],
    *,
    default_index: int = 0,
):
    if not options:
        raise ValueError("菜单至少需要一个选项")
    if not 0 <= default_index < len(options):
        raise ValueError("默认菜单项超出范围")

    # Redirected/non-Windows sessions retain a plain numbered fallback for
    # automation and development. Interactive Windows terminals use arrows.
    if msvcrt is None or not sys.stdin.isatty():
        print(f"\n{title}")
        for index, (name, _) in enumerate(options, start=1):
            print(f"  [{index}] {name}")
        while True:
            choice = input(
                f"请选择 [1-{len(options)}]（直接回车使用 {default_index + 1}）："
            ).strip()
            if not choice:
                return options[default_index][1]
            try:
                selected = int(choice) - 1
            except ValueError:
                print("输入无效，请输入选项编号。")
                continue
            if 0 <= selected < len(options):
                return options[selected][1]
            print("输入无效，请重新选择。")

    if os.name == "nt":
        os.system("")  # Enable ANSI cursor control in the legacy CMD fallback.
    print(f"\n{title}（↑/↓选择，Enter 确认）")
    selected = default_index
    first_render = True
    while True:
        if not first_render:
            sys.stdout.write(f"\x1b[{len(options)}A")
        for index, (name, _) in enumerate(options):
            marker = ">" if index == selected else " "
            sys.stdout.write(f"\x1b[2K\r  {marker} {name}\n")
        sys.stdout.flush()
        first_render = False

        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            key = msvcrt.getwch()
            if key == "H":
                selected = (selected - 1) % len(options)
            elif key == "P":
                selected = (selected + 1) % len(options)
            continue
        if key in {"\r", "\n", " "}:
            return options[selected][1]
        if key.lower() == "w":
            selected = (selected - 1) % len(options)
        elif key.lower() == "s":
            selected = (selected + 1) % len(options)
        elif key in {"\x03", "\x1b"}:
            raise KeyboardInterrupt


def _choose_style_option(
    label: str,
    options: list[tuple[str, object]],
    custom_prompt: str,
    parser,
):
    custom_marker = object()
    selected = _select_menu(
        label, [*options, ("自行填写", custom_marker)], default_index=0,
    )
    if selected is not custom_marker:
        return selected
    while True:
        raw = input(custom_prompt).strip()
        try:
            return parser(raw)
        except ValueError as exc:
            print(f"输入无效：{exc}")


def select_subtitle_style(auto_font: str) -> tuple[SubtitleStyle, str]:
    mode = _select_menu(
        "字幕样式",
        [("默认（使用当前预设参数）", "default"), ("自定义", "custom")],
    )
    if mode == "default":
        return SubtitleStyle(font_name=auto_font), "默认"

    font_name = _choose_style_option(
        "字体",
        [
            (f"自动选择（当前：{auto_font}）", auto_font),
            ("Microsoft YaHei（微软雅黑）", "Microsoft YaHei"),
            ("Noto Sans CJK SC", "Noto Sans CJK SC"),
            ("Microsoft JhengHei（微软正黑体）", "Microsoft JhengHei"),
            ("SimSun（宋体）", "SimSun"),
        ],
        "输入已安装的字体系列名称：",
        parse_font_name,
    )
    font_size = _choose_style_option(
        "字号",
        [("32（当前默认）", 32), ("24（较小）", 24), ("40（较大）", 40), ("48（特大）", 48)],
        "输入字号（大于 0）：",
        parse_positive_number,
    )
    primary_colour = _choose_style_option(
        "文字颜色",
        [
            ("白色（当前默认）", "&H00FFFFFF"),
            ("黄色", "&H0000FFFF"),
            ("青色", "&H00FFFF00"),
            ("浅绿色", "&H0090EE90"),
        ],
        "输入 #RRGGBB 或 &HAABBGGRR：",
        parse_ass_colour,
    )
    outline_colour = _choose_style_option(
        "描边颜色",
        [
            ("黑色（当前默认）", "&H00000000"),
            ("深灰色", "&H00202020"),
            ("白色", "&H00FFFFFF"),
        ],
        "输入 #RRGGBB 或 &HAABBGGRR：",
        parse_ass_colour,
    )
    outline = _choose_style_option(
        "描边宽度",
        [("2（当前默认）", 2), ("1（较细）", 1), ("3（较粗）", 3), ("0（关闭）", 0)],
        "输入描边宽度（大于或等于 0）：",
        parse_nonnegative_number,
    )
    shadow = _choose_style_option(
        "阴影大小",
        [("0（当前默认，关闭）", 0), ("1（轻微）", 1), ("2（明显）", 2), ("3（较强）", 3)],
        "输入阴影大小（大于或等于 0）：",
        parse_nonnegative_number,
    )
    alignment = _choose_style_option(
        "字幕位置",
        [
            ("底部居中（当前默认）", 2),
            ("顶部居中", 8),
            ("画面中央", 5),
            ("左下", 1),
            ("右下", 3),
        ],
        "输入 ASS 位置编号 1-9：",
        parse_alignment,
    )
    margin_v = _choose_style_option(
        "垂直边距",
        [("54（当前默认）", 54), ("30（更靠近边缘）", 30), ("80（稍向内）", 80), ("120（明显向内）", 120)],
        "输入垂直边距（大于或等于 0 的整数）：",
        parse_nonnegative_integer,
    )
    return SubtitleStyle(
        font_name=str(font_name),
        font_size=float(font_size),
        primary_colour=str(primary_colour),
        outline_colour=str(outline_colour),
        outline=float(outline),
        shadow=float(shadow),
        alignment=int(alignment),
        margin_v=int(margin_v),
    ), "自定义"


def _format_estimate(seconds: float | None) -> str:
    if seconds is None:
        return "暂无法估算"
    return f"约 {format_time(seconds)}"


def select_encoding_profile(estimates: dict[str, float | None] | None = None) -> str:
    """Let the user choose delivery speed or source-like output."""
    estimates = estimates or {}
    return _select_menu(
        "编码预设（根据当前机器短片实测推算，实际耗时可能波动）",
        [
            (f"标准（默认，质量优先）        {_format_estimate(estimates.get('standard'))}", "standard"),
            (f"快速（速度优先）              {_format_estimate(estimates.get('fast'))}", "fast"),
            (f"高质量（更清晰，文件更大）    {_format_estimate(estimates.get('high'))}", "high"),
            (f"接近原片（按源视频码率）      {_format_estimate(estimates.get('source'))}", "source"),
        ],
    )


def _format_size(size: object) -> str:
    try:
        value = int(size)
    except (TypeError, ValueError):
        return "未知"
    return f"{value / (1024 ** 2):.1f} MiB（{value / 1_000_000:.1f} MB）"


def _format_bitrate(bitrate: object) -> str:
    try:
        value = int(bitrate)
    except (TypeError, ValueError):
        return "未知"
    return f"{value / 1_000_000:.2f} Mbps"


def _format_fps(rate: object) -> str:
    text = str(rate or "")
    try:
        numerator, denominator = text.split("/", 1)
        value = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return text or "未知"
    return f"{value:.3f}".rstrip("0").rstrip(".") + " fps"


def print_source_media_summary(video: Path, media: dict) -> None:
    video_stream = media["video"]
    format_info = media.get("format") or {}
    audio_streams = media.get("audio") or []
    duration = media_duration_seconds(media)
    video_codec = " ".join(
        value for value in (
            str(video_stream.get("codec_name") or "未知").upper(),
            str(video_stream.get("profile") or ""),
        ) if value
    )
    dimensions = f"{video_stream.get('width', '?')}×{video_stream.get('height', '?')}"
    color_parts = [
        str(video_stream.get(key)) for key in
        ("pix_fmt", "color_space", "color_transfer", "color_primaries")
        if video_stream.get(key)
    ]
    audio_bitrate = sum(
        int(stream["bit_rate"]) for stream in audio_streams
        if str(stream.get("bit_rate") or "").isdigit()
    )
    audio_details = []
    for index, stream in enumerate(audio_streams, start=1):
        codec = str(stream.get("codec_name") or "未知").upper()
        profile = str(stream.get("profile") or "")
        sample_rate = stream.get("sample_rate")
        channels = stream.get("channel_layout") or (
            f"{stream.get('channels')} 声道" if stream.get("channels") else "声道未知"
        )
        detail = f"{codec}{' ' + profile if profile else ''} / {channels}"
        if sample_rate:
            try:
                sample_rate_khz = int(sample_rate) / 1000
            except (TypeError, ValueError):
                pass
            else:
                detail += f" / {sample_rate_khz:g} kHz"
        if stream.get("bit_rate"):
            detail += f" / {_format_bitrate(stream.get('bit_rate'))}"
        audio_details.append(f"    音轨 {index}：{detail}")

    print("\n原视频信息")
    print(f"  文件：{video}")
    print(f"  大小：{_format_size(format_info.get('size'))}")
    print(f"  时长：{format_time(duration)}")
    print(f"  封装：{format_info.get('format_name') or video.suffix.lstrip('.').upper()}")
    print(f"  总码率：{_format_bitrate(format_info.get('bit_rate'))}")
    print(f"  视频：{video_codec} / {dimensions} / {_format_fps(video_stream.get('avg_frame_rate') or video_stream.get('r_frame_rate'))}")
    print(f"  视频码率：{_format_bitrate(source_video_bitrate(media))}")
    if color_parts:
        print(f"  像素与色彩：{' / '.join(color_parts)}")
    print(f"  音频：{len(audio_streams)} 条 / 合计 {_format_bitrate(audio_bitrate) if audio_bitrate else '码率未知'}")
    for detail in audio_details:
        print(detail)


def _time_options() -> tuple[list[str], str]:
    start = input("试压起点（留空=完整视频）：").strip()
    duration = input("试压时长秒数（留空=完整视频）：").strip()
    if bool(start) != bool(duration):
        raise ValueError("试压必须同时填写起点和时长")
    if not start:
        return [], "完整视频"
    try:
        parse_positive_number(duration)
    except ValueError as exc:
        raise ValueError("试压时长必须为正数") from exc
    return ["-ss", start, "-t", duration], f"试压：{start}s 起、持续 {duration}s"


def main() -> None:
    print("硬字幕压制（按 Ctrl+C 随时取消；不会修改原视频）")
    video = _dragged_path("视频文件")
    subtitle = _dragged_path("SRT 字幕文件")
    if subtitle.suffix.lower() != ".srt":
        raise ValueError("只接受 .srt 字幕文件")
    media = probe_media(video)
    encoders = available_encoders()
    encoder, video_args, hdr_to_sdr, encoding_note = select_encoding(media, encoders)
    font, font_fallback = select_font()
    subtitle_style, subtitle_style_mode = select_subtitle_style(font)
    # Keep the chosen fallback explicit in the style string rather than relying
    # on an unreported libass substitution.
    subtitle_utf8 = subtitle_is_utf8(subtitle)
    active_subtitle = subtitle if subtitle_utf8 else conversion_target(subtitle)
    time_args, time_note = _time_options()
    suffix = "_preview_subtitled" if time_args else "_subtitled"
    output = video.with_name(f"{video.stem}{suffix}{video.suffix}")
    if output.exists():
        raise FileExistsError(f"输出文件已存在，不会覆盖：{output}")

    preview_duration = float(time_args[-1]) if time_args else None
    target_duration = preview_duration or media_duration_seconds(media)
    print_source_media_summary(video, media)
    estimates = estimate_profile_times(
        video,
        subtitle,
        media,
        encoder,
        video_args,
        subtitle_style=subtitle_style,
        hdr_to_sdr=hdr_to_sdr,
        target_duration=target_duration,
        subtitle_utf8=subtitle_utf8,
    )
    profile = select_encoding_profile(estimates)
    source_bitrate = source_video_bitrate(media) if profile == "source" else None
    video_args = apply_encoding_profile(
        video_args, encoder, profile=profile, source_bitrate=source_bitrate,
    )
    profile_note = encoding_profile_note(profile, source_bitrate)

    print("\n预检摘要")
    print(f"  视频：{video}")
    print(f"  字幕：{subtitle}")
    print(f"  输出：{output}")
    print(f"  视频流：第一条；音轨：保留 {len(media['audio'])} 条")
    print(f"  编码：{encoding_note}")
    print(f"  预设：{profile_note}")
    font_note = "（自动降级）" if font_fallback and subtitle_style.font_name == font else ""
    print(f"  字幕样式：{subtitle_style_mode}")
    print(f"    字体：{subtitle_style.font_name}{font_note}；字号：{ass_number(subtitle_style.font_size)}")
    print(f"    文字颜色：{subtitle_style.primary_colour}；描边颜色：{subtitle_style.outline_colour}")
    print(f"    描边：{ass_number(subtitle_style.outline)}；阴影：{ass_number(subtitle_style.shadow)}")
    print(f"    位置：{ALIGNMENT_NAMES.get(subtitle_style.alignment, str(subtitle_style.alignment))}；垂直边距：{subtitle_style.margin_v}")
    print(f"  字幕编码：{'UTF-8' if subtitle_utf8 else '将转换为 UTF-8 校对副本'}")
    print(f"  模式：{time_note}")
    if hdr_to_sdr:
        print("  注意：本次会把 HDR/10-bit 色调映射为 SDR，以提高兼容性。")
    should_encode = _select_menu(
        "确认开始编码？",
        [("开始编码", True), ("取消，不生成视频", False)],
    )
    if not should_encode:
        print("已取消，未生成视频。")
        return
    if not subtitle_utf8:
        convert_subtitle_to_utf8(subtitle, active_subtitle)

    encode_subtitled_video(
        video=video,
        subtitle=active_subtitle,
        output=output,
        encoder=encoder,
        video_args=video_args,
        hdr_to_sdr=hdr_to_sdr,
        subtitle_style=subtitle_style,
        time_args=time_args,
        total_seconds=target_duration,
    )
    print(f"完成：{output}")
