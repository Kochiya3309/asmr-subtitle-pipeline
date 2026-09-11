# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
from output_layout import artifact_name, discover_outputs, prepare_output
import os
import re
import sys
import common  # 触发日志初始化

# ====== 配置 ======
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
PATTERN = "*_final.srt"

# 修复：清理重复项（"校正をお引き受けできません"和"安全で適切な内容に限られます"原各出现两次）
API_ARTIFACT_PATTERNS = [
    "申し訳ありません",
    "字幕が不完全です",
    "続きのテキストをいただけますか",
    "校正をお引き受けできません",
    "安全で適切な内容に限られます",
    "可协助的内容仅限于",
    "恕难提供校对服务",
    "如有其他文档需要帮助",
    "他の文書でお手伝いが必要でしたら",
    "搜索不可用",
    "未配置 TAVILY_API_KEY",
    "search is not available",
]

BARE_INDEX = re.compile(r'^\[\d+\]$')

PUNCTUATION_CHARS = (
    r'!！?？.。…,，、:：;；"「」『』""''（）()[]［］{}｛｝'
    r'·・♪♯#*＊&＆^＾%％$＄@＠~～‐\-/／\＼|｜_＿=＝+＋'
    r'<＜>＞`｀¨＾'
)
BARE_PUNCT = re.compile(r'^[\s' + re.escape(PUNCTUATION_CHARS) + r']+$')


def validate_file(filepath):
    """扫描单个 SRT 文件，返回问题列表"""
    issues = []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    raw_blocks = re.split(r'\n\n+', content.strip())
    subs = []
    for block in raw_blocks:
        lines = block.strip().split('\n')
        if len(lines) >= 4:
            # 修复：index 非数字（用户手改字幕/外部工具产物）时跳过该块并警告，
            # 原实现直接 int() 崩溃且不提示位置
            try:
                idx = int(lines[0])
            except ValueError:
                print(f"  ⚠ 跳过格式异常块（首行非序号）：{lines[0][:40]!r} ...")
                continue
            subs.append({
                "index": idx,
                "timecode": lines[1],
                "text_ja": lines[2],
                "text_zh": '\n'.join(lines[3:]).strip() if len(lines) > 3 else "",
            })

    for s in subs:
        idx = s["index"]
        zh = s["text_zh"].strip()
        ja = s["text_ja"].strip()

        if not zh:
            issues.append((idx, "空译文", f"[{idx}] 中文译文为空", zh))
            continue

        if BARE_INDEX.match(zh):
            issues.append((idx, "编号残留", f"[{idx}] 中文译文为裸编号 '{zh}'", zh))
            continue

        if BARE_PUNCT.match(zh):
            issues.append((idx, "纯标点", f"[{idx}] 中文译文仅含标点 '{zh}'", zh))
            continue

        for pattern in API_ARTIFACT_PATTERNS:
            if pattern in zh:
                issues.append((idx, "API 残留", f"[{idx}] 中文译文疑似 API 拒绝/错误消息: '{zh[:80]}...'", zh))
                break

        # 硬省略标记：AI 主动省略内容，属严重问题（critical）
        omission_markers = ["中略", "省略"]
        # 低置信度标记：上游 prompt（review_japanese / translate）明确要求生成的
        # 猜测提示，属设计内产物，仅作警告（warning），不应阻断流水线
        low_confidence_markers = ["認識不良", "认识不良", "低信頼度", "低信赖度"]

        for marker in omission_markers:
            if marker in zh or marker in ja:
                issues.append((
                    idx, "内容省略",
                    f"[{idx}] 包含省略标记 '{marker}'",
                    f"日: {ja[:50]}... | 中: {zh[:50]}..."
                ))
                break
        else:
            for marker in low_confidence_markers:
                if marker in zh or marker in ja:
                    issues.append((
                        idx, "低置信度标记",
                        f"[{idx}] 包含低置信度标记 '{marker}'（建议人工复查该行）",
                        f"日: {ja[:50]}... | 中: {zh[:50]}..."
                    ))
                    break

        if not ja:
            issues.append((
                idx, "日语缺失",
                f"[{idx}] 日语原文为空",
                f"中: {zh[:50]}..."
            ))

        if re.search(r'[※（(][^)）]*(?:文脈|修正|検討|注|注意)[^)）]*[）)]', ja):
            issues.append((
                idx, "日语注释残留",
                f"[{idx}] 日语原文疑似残留审校注释",
                f"日: {ja[:80]}..."
            ))

    return issues


def main():
    prepare_output(OUTPUT_DIR)
    files = discover_outputs(OUTPUT_DIR, PATTERN)

    if not files:
        print(f"❌ 在 {OUTPUT_DIR}/<音频名>/final/ 中未找到 <音频名>_final.srt 文件")
        sys.exit(1)

    print("=" * 60)
    print("  自动化验证 — 扫描 Final SRT 中的残留问题")
    print("=" * 60)
    print(f"📋 {len(files)} 个文件待扫描\n")

    total_issues = 0
    has_critical = False

    for f in files:
        base = artifact_name(f)
        issues = validate_file(f)

        if issues:
            critical = [i for i in issues if i[1] in ("API 残留", "编号残留", "空译文", "日语缺失", "日语注释残留", "内容省略")]
            warnings = [i for i in issues if i[1] not in ("API 残留", "编号残留", "空译文", "日语缺失", "日语注释残留", "内容省略")]

            print(f"\n{'#'*55}")
            print(f"  {base}")
            print(f"{'#'*55}")

            if critical:
                has_critical = True
                print(f"\n  🔴 严重问题（{len(critical)} 处）：")
                for idx, tag, desc, txt in critical:
                    print(f"     [{idx}] {tag}")
                    print(f"           当前: {txt[:80]}")

            if warnings:
                print(f"\n  🟡 警告（{len(warnings)} 处）：")
                for idx, tag, desc, txt in warnings:
                    print(f"     [{idx}] {tag}")
                    print(f"           当前: {txt[:80]}")

            total_issues += len(issues)
        else:
            print(f"\n  ✅ {base} — 未发现问题")

    print(f"\n{'='*60}")
    print(f"  扫描完成：共 {len(files)} 个文件，发现 {total_issues} 个问题")
    print(f"{'='*60}")

    if has_critical:
        print(f"\n⚠ 发现严重问题（API 残留/编号残留/空译文），建议修复后重新生成对应字幕。")
        sys.exit(1)
    elif total_issues > 0:
        print(f"\n⚠ 发现警告级别问题，建议人工复查。")
        sys.exit(0)
    else:
        print(f"\n✅ 全部通过！")
        sys.exit(0)


if __name__ == "__main__":
    main()
