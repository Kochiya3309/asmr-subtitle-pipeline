# SPDX-License-Identifier: GPL-3.0-or-later
"""Compatibility entry point for interactive hard-subtitle delivery."""

import sys

from burn_subtitles_cli import main


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
