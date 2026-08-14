# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
"""
将 output 目录下所有指定后缀的 SRT 文件重命名（去掉后缀）。
用法: python rename_suffix.py <suffix>
  python rename_suffix.py _cn_only      # *_cn_only.srt → *.srt
  python rename_suffix.py _final        # *_final.srt → *.srt
独立脚本，不依赖项目其他模块。
"""

import os
import sys
import glob

TARGET_DIR = os.environ.get("OUTPUT_DIR", "./output")


def main():
    if len(sys.argv) < 2:
        print("用法: python rename_suffix.py <suffix>")
        print("示例: python rename_suffix.py _cn_only")
        sys.exit(1)

    suffix = sys.argv[1]
    pattern = os.path.join(TARGET_DIR, f"*{suffix}.srt")
    files = sorted(glob.glob(pattern))

    if not files:
        print(f"❌ 在 {TARGET_DIR}/ 中未找到 *{suffix}.srt 文件")
        sys.exit(1)

    print("=" * 60)
    print(f"  重命名 *{suffix}.srt → *.srt")
    print("=" * 60)
    print(f"📋 {len(files)} 个文件待处理\n")

    renamed = 0

    for old_path in files:
        base = os.path.basename(old_path).replace(f"{suffix}.srt", "")
        new_path = os.path.join(TARGET_DIR, f"{base}.srt")

        if os.path.exists(new_path):
            # 修复：静默覆盖会丢数据（如先 rename _cn_only 生成 XXX.srt，
            # 再 rename _final 会覆盖同一文件）。目标已存在时先备份旧文件。
            backup_path = os.path.join(TARGET_DIR, f"{base}.bak.srt")
            n = 1
            while os.path.exists(backup_path):
                backup_path = os.path.join(TARGET_DIR, f"{base}.bak{n}.srt")
                n += 1
            os.replace(new_path, backup_path)
            print(f"  ⚠ {os.path.basename(old_path)} → {os.path.basename(new_path)}"
                  f"（旧文件已备份为 {os.path.basename(backup_path)}）")
        else:
            print(f"  ✅ {os.path.basename(old_path)} → {os.path.basename(new_path)}")

        os.replace(old_path, new_path)
        renamed += 1

    print(f"\n🏁 完成！重命名 {renamed} 个文件")


if __name__ == "__main__":
    main()
