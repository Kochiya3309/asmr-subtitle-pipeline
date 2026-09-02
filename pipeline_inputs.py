# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared active-audio discovery for pipeline stages."""

from __future__ import annotations

from pathlib import Path
import os


AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus"}


def discover_active_audio(audio_input: os.PathLike | str) -> dict[str, Path]:
    """Return the exact active input set for either one file or one directory."""
    path = Path(audio_input).resolve()
    if path.is_file():
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            raise ValueError(f"不支持的音频格式：{path.suffix}")
        files = [path]
    elif path.is_dir():
        files = sorted(
            (item for item in path.iterdir() if item.is_file() and item.suffix.lower() in AUDIO_SUFFIXES),
            key=lambda item: item.name.lower(),
        )
    else:
        raise FileNotFoundError(f"音频输入不存在：{path}")
    if not files:
        raise FileNotFoundError(f"未找到受支持的音频：{path}")
    by_base = {}
    for item in files:
        if item.stem in by_base:
            raise ValueError(
                f"音频基础名冲突：{by_base[item.stem].name} / {item.name}"
            )
        by_base[item.stem] = item
    return by_base
