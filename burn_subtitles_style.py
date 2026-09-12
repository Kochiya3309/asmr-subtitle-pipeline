# SPDX-License-Identifier: GPL-3.0-or-later
"""Subtitle style model, font discovery, and input validation."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path


FONT_CANDIDATES = (
    ("Microsoft YaHei", ("msyh", "msyhl")),
    ("Noto Sans CJK SC", ("notosanscjk", "notoserifcjk")),
    ("Microsoft JhengHei", ("msjh",)),
    ("SimSun", ("simsun", "simsunb")),
)

ALIGNMENT_NAMES = {
    1: "左下", 2: "底部居中", 3: "右下",
    4: "左侧居中", 5: "画面中央", 6: "右侧居中",
    7: "左上", 8: "顶部居中", 9: "右上",
}


@dataclass(frozen=True)
class SubtitleStyle:
    font_name: str
    font_size: float = 32
    primary_colour: str = "&H00FFFFFF"
    outline_colour: str = "&H00000000"
    outline: float = 2
    shadow: float = 0
    alignment: int = 2
    margin_v: int = 54

    def to_ass(self) -> str:
        return ",".join([
            f"FontName={self.font_name}",
            f"FontSize={ass_number(self.font_size)}",
            f"PrimaryColour={self.primary_colour}",
            f"OutlineColour={self.outline_colour}",
            f"Outline={ass_number(self.outline)}",
            f"Shadow={ass_number(self.shadow)}",
            f"Alignment={self.alignment}",
            f"MarginV={self.margin_v}",
        ])


def select_font() -> tuple[str, bool]:
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    names = {path.stem.casefold() for path in fonts_dir.glob("*.*")} if fonts_dir.is_dir() else set()
    for family, markers in FONT_CANDIDATES:
        if any(any(marker in name for marker in markers) for name in names):
            return family, family != FONT_CANDIDATES[0][0]
    # libass can still use its own configured fallback; make that explicit.
    return "sans-serif", True


def ass_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def parse_font_name(value: str) -> str:
    if not value:
        raise ValueError("字体名称不能为空")
    if any(character in value for character in (",", "'", "\\", "\r", "\n")):
        raise ValueError("字体名称不能包含英文逗号、单引号、反斜杠或换行")
    return value


def parse_positive_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError("请输入大于 0 的数字") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("请输入大于 0 的数字")
    return number


def parse_nonnegative_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError("请输入大于或等于 0 的数字") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError("请输入大于或等于 0 的数字")
    return number


def parse_nonnegative_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError("请输入大于或等于 0 的整数") from exc
    if number < 0:
        raise ValueError("请输入大于或等于 0 的整数")
    return number


def parse_alignment(value: str) -> int:
    try:
        alignment = int(value)
    except ValueError as exc:
        raise ValueError("请输入 1 到 9 的位置编号") from exc
    if not 1 <= alignment <= 9:
        raise ValueError("请输入 1 到 9 的位置编号")
    return alignment


def parse_ass_colour(value: str) -> str:
    text = value.strip().upper()
    if text.startswith("#"):
        text = text[1:]
    if text.startswith("&H"):
        digits = text[2:]
        if len(digits) == 6:
            digits = "00" + digits
        if len(digits) != 8 or any(char not in "0123456789ABCDEF" for char in digits):
            raise ValueError("ASS 颜色应为 &HAABBGGRR")
        return "&H" + digits
    if len(text) != 6 or any(char not in "0123456789ABCDEF" for char in text):
        raise ValueError("请输入 #RRGGBB，例如 #FFFFFF")
    red, green, blue = text[0:2], text[2:4], text[4:6]
    return f"&H00{blue}{green}{red}"
