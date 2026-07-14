# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
"""
从 *_final.srt 中去除日文字幕，仅保留中文翻译。
依赖 common.py 的日志功能。
"""

import os
import sys
import glob
import re
import common  # 触发日志初始化

# ====== 配置 ======
INPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
PATTERN = "*_final.srt"
# ==================


def remove_japanese_from_srt(input_path, output_path):
    """
    读取双语 SRT，删除日语行，重新编号后写入新的 SRT。
    支持任意行数的字幕条目（日语行和中文行本身可能含换行）。
    """
    with open(input_path, "r", encoding="utf-8") as f:
        content = f.read()

    raw_blocks = re.split(r'\n\n+', content.strip())

    new_blocks = []
    new_index = 1

    for block in raw_blocks:
        lines = block.strip().split('\n')

        # 标准 4 行格式：序号、时间轴、日语、中文
        if len(lines) >= 4:
            timecode = lines[1]
            # 中文字幕可能跨行，取第 3 行起所有内容
            zh_text = '\n'.join(lines[3:]).strip()
            if not zh_text:
                continue
            new_blocks.append(f"{new_index}\n{timecode}\n{zh_text}")
            new_index += 1

        # 3 行格式（可能日语为空）：序号、时间轴、中文
        elif len(lines) == 3:
            timecode = lines[1]
            zh_text = lines[2].strip()
            if not zh_text:
                continue
            new_blocks.append(f"{new_index}\n{timecode}\n{zh_text}")
            new_index += 1

        else:
            continue

    if not new_blocks:
        print(f"  ⚠ 未提取到任何中文条目")
        return False

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(new_blocks) + "\n")

    return True


def main():
    pattern = os.path.join(INPUT_DIR, PATTERN)
    files = sorted(glob.glob(pattern))

    if not files:
        print(f"❌ 在 {INPUT_DIR}/ 中未找到 *_final.srt 文件")
        sys.exit(1)

    print("=" * 60)
    print("  去除日文字幕 — 仅保留中文翻译")
    print("=" * 60)
    print(f"📋 {len(files)} 个文件待处理\n")

    for f in files:
        base = os.path.basename(f).replace("_final.srt", "")
        output_path = os.path.join(OUTPUT_DIR, f"{base}_cn_only.srt")

        print(f"  📄 {os.path.basename(f)} ...", end=" ", flush=True)

        success = remove_japanese_from_srt(f, output_path)
        if success:
            # 修复条目数统计 bug
            with open(output_path, "r", encoding="utf-8") as out_f:
                content = out_f.read().strip()
            count = len(re.findall(r'\n\n+', content)) + 1 if content else 0
            print(f"✅ → {os.path.basename(output_path)}（{count} 条）")
        else:
            print("❌ 失败")

    print(f"\n🏁 完成！输出文件以 _cn_only.srt 结尾")


if __name__ == "__main__":
    main()
