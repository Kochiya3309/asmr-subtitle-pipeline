# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared active-audio discovery for pipeline stages."""

from __future__ import annotations

from pathlib import Path
import os
import json


AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus"}


def _active_audio_paths_override() -> dict[str, Path] | None:
    """Return the invocation-scoped source paths supplied by run_all.py."""
    raw = os.environ.get("ACTIVE_AUDIO_PATHS")
    if raw is None:
        return None
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("ACTIVE_AUDIO_PATHS must be a JSON object") from exc
    if not isinstance(values, dict) or not values:
        raise ValueError("ACTIVE_AUDIO_PATHS must be a non-empty JSON object")
    result: dict[str, Path] = {}
    for base, value in values.items():
        if not isinstance(base, str) or not base or not isinstance(value, str):
            raise ValueError("ACTIVE_AUDIO_PATHS has an invalid entry")
        path = Path(value).resolve()
        if path.suffix.lower() not in AUDIO_SUFFIXES or not path.is_file():
            raise FileNotFoundError(f"active audio source is unavailable: {path}")
        if path.stem != base:
            raise ValueError(f"active audio base does not match source: {base!r}")
        result[base] = path
    return result


def discover_active_audio(audio_input: os.PathLike | str) -> dict[str, Path]:
    """Return the exact active input set for either one file or one directory."""
    override = _active_audio_paths_override()
    if override is not None:
        return dict(sorted(override.items()))
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
