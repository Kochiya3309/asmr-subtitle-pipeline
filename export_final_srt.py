# SPDX-License-Identifier: GPL-3.0-or-later
"""将各音频 final/ 中的 <音频名>_final.srt 集中复制到 output/_transfer/final/。

复制后的文件名为 <音频名>.srt，便于批量转移到播放器、手机等设备。
规范文件（<音频名>_final.srt）保留在原处，继续用于缓存校验与续跑。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil

from output_layout import artifact_name, discover_outputs, prepare_output

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
TRANSFER_ROOT = os.environ.get("SRT_TRANSFER_DIR", "./output/_transfer")
SUFFIX = "_final.srt"
TARGET_SUBDIR = "final"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--transfer-dir", default=TRANSFER_ROOT)
    args = parser.parse_args()

    prepare_output(args.output_dir)
    files = discover_outputs(args.output_dir, f"*{SUFFIX}")

    if not files:
        print(f"❌ 在 {args.output_dir}/<音频名>/final/ 中未找到 <音频名>{SUFFIX} 文件")
        raise SystemExit(1)

    target_dir = Path(args.transfer_dir) / TARGET_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)

    copied = skipped = backed_up = 0
    print("=" * 60)
    print(f"  导出双语终稿 → {target_dir}")
    print("=" * 60)
    print(f"📋 {len(files)} 个文件待处理\n")

    for source in files:
        base = artifact_name(source)[: -len(SUFFIX)]
        target = target_dir / f"{base}.srt"
        if target.is_file() and target.read_bytes() == Path(source).read_bytes():
            print(f"  ⏭ {base}.srt 已存在且内容一致")
            skipped += 1
            continue
        if target.is_file():
            backup_path = target.with_suffix(".bak.srt")
            n = 1
            while backup_path.exists():
                backup_path = target.with_suffix(f".bak{n}.srt")
                n += 1
            os.replace(target, backup_path)
            backed_up += 1
            print(f"  ⚠ 旧副本已备份为 {backup_path.name}")
        shutil.copy2(source, target)
        copied += 1
        print(f"  ✅ → {target.name}")

    print(f"\n🏁 完成！复制 {copied} 个、跳过 {skipped} 个、备份 {backed_up} 个。")
    print(f"输出目录：{target_dir}")


if __name__ == "__main__":
    main()