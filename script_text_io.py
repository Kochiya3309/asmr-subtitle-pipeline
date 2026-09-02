# SPDX-License-Identifier: GPL-3.0-or-later
"""Lossless decoding for publisher scripts, including mixed-encoding files."""

from __future__ import annotations


WHOLE_FILE_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc-jp")
MIXED_LINE_ENCODINGS = ("utf-8", "cp932", "euc-jp")


def _strict_roundtrip_decode(raw: bytes, encoding: str) -> str | None:
    try:
        text = raw.decode(encoding)
        if text.encode(encoding) != raw:
            return None
        return text
    except UnicodeError:
        return None


def _decode_mixed_lines(raw: bytes) -> str:
    lines = raw.splitlines(keepends=True)
    if not lines:
        return ""

    decoded = []
    for line_number, line in enumerate(lines, start=1):
        options = {
            encoding: text
            for encoding in MIXED_LINE_ENCODINGS
            if (text := _strict_roundtrip_decode(line, encoding)) is not None
        }
        if not options:
            raise UnicodeError(
                f"script line {line_number} cannot be decoded losslessly"
            )
        decoded.append(options)

    infinity = len(lines) + 1
    forward = []
    for index, options in enumerate(decoded):
        costs = {}
        for encoding in options:
            if index == 0:
                costs[encoding] = 0
            else:
                costs[encoding] = min(
                    forward[index - 1].get(previous, infinity)
                    + (previous != encoding)
                    for previous in MIXED_LINE_ENCODINGS
                )
        forward.append(costs)

    backward = [None] * len(decoded)
    for index in range(len(decoded) - 1, -1, -1):
        costs = {}
        for encoding in decoded[index]:
            if index == len(decoded) - 1:
                costs[encoding] = 0
            else:
                costs[encoding] = min(
                    backward[index + 1].get(following, infinity)
                    + (following != encoding)
                    for following in MIXED_LINE_ENCODINGS
                )
        backward[index] = costs

    minimum_switches = min(forward[-1].values())
    if minimum_switches == 0:
        raise UnicodeError("mixed decoder reached an invalid zero-switch state")

    result = []
    for index, options in enumerate(decoded):
        feasible = [
            encoding
            for encoding in options
            if forward[index][encoding] + backward[index][encoding]
            == minimum_switches
        ]
        texts = {options[encoding] for encoding in feasible}
        if len(texts) != 1:
            raise UnicodeError(
                f"script line {index + 1} has ambiguous mixed-encoding decodings"
            )
        result.append(texts.pop())
    return "".join(result)


def decode_script_bytes_exact(raw: bytes) -> str:
    """Decode without replacement, ignored bytes, or newline translation."""
    if not isinstance(raw, bytes):
        raise TypeError("raw script input must be bytes")
    for encoding in WHOLE_FILE_ENCODINGS:
        text = _strict_roundtrip_decode(raw, encoding)
        if text is not None:
            return text
    return _decode_mixed_lines(raw)


def read_script_text_exact(path: str) -> str:
    with open(path, "rb") as handle:
        return decode_script_bytes_exact(handle.read())
