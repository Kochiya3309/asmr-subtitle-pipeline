# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
import os
import sys
import time
import glob
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (
    get_llm_client, get_llm_config, call_deepseek, parse_srt,
    parse_review_result, OUTPUT_TOOL,
    reset_usage, get_usage_report
)
from asr_evidence import canonical_fingerprint
from pipeline_cache import (
    artifact_cache_is_current, backup_stale_artifacts,
    commit_text_artifact, parse_file_snapshot,
)

# ====== 配置 ======
INPUT_DIR  = os.environ.get("OUTPUT_DIR", "./output")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
ENABLE_SEARCH_IN_REVIEW = os.environ.get("ENABLE_SEARCH", "1") == "1"
TRANSLATE_BATCH_SIZE = int(os.environ.get("TRANSLATE_BATCH_SIZE", "10"))
TRANSLATE_REVIEW_BATCH_SIZE = int(os.environ.get("TRANSLATE_REVIEW_BATCH_SIZE", "20"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
TRANSLATE_GENERATION_VERSION = "translate-review-2026-08-29.1"
# ==================


def _translate_generation_fingerprint():
    _, base_url, model, enable_thinking, max_tokens = get_llm_config()
    return canonical_fingerprint({
        "version": TRANSLATE_GENERATION_VERSION,
        "tool": OUTPUT_TOOL,
        "llm": {
            "base_url": base_url,
            "model": model,
            "enable_thinking": enable_thinking,
            "max_tokens": max_tokens,
            "temperature": 0.25,
        },
        "search_in_review": ENABLE_SEARCH_IN_REVIEW,
        "translate_batch_size": TRANSLATE_BATCH_SIZE,
        "review_batch_size": TRANSLATE_REVIEW_BATCH_SIZE,
    })


def select_translation_inputs(ensemble_files):
    files = []
    fallback_count = 0
    for ensemble_path in ensemble_files:
        base = os.path.basename(ensemble_path).replace("_ensemble.srt", "")
        reviewed = os.path.join(INPUT_DIR, f"{base}_reviewed.srt")
        reviewed_manifest = os.path.join(INPUT_DIR, f"{base}_reviewed_manifest.json")
        if artifact_cache_is_current(
            reviewed_manifest, "reviewed_srt_manifest",
            ensemble_path, reviewed, None,
        ):
            files.append(reviewed)
        else:
            files.append(ensemble_path)
            fallback_count += 1
    return files, fallback_count


# ============================================================
#  第一阶段：批量翻译（并行）
# ============================================================
def _translate_batch(client, batch_texts, batch_start, batch_end, log_prefix):
    """翻译单个批次（可并行）"""
    separator = "\n---SPLIT---\n"
    combined = separator.join(batch_texts)

    system_prompt = (
        "你是一个专业的日语 ASMR 字幕翻译助手。"
        "将以下日语文本翻译为简体中文。规则：\n"
        "1. 保留拟声词和语气词（如「はぁ…」「ふふっ」「ん…」），"
        "无中文对应的拟声词保留原文假名；\n"
        "2. 敬语和亲密称呼自然中文化（如「お兄ちゃん」→「哥哥」，"
        "「ご主人様」→「主人」）；\n"
        "3. 不完整句子按字面翻译，不要补全主语或谓语；\n"
        "4. 双关语优先保留语义，无法兼顾时取主要含义；\n"
        "5. 日语括号「」转为中文引号「」或保留原样；\n"
        "6. 每条译文之间用「---SPLIT---」分隔；\n"
        "7. 不要添加序号或解释，纯译文。\n"
        "8. 日语原文中若含「〔認識不良〕」「（低信頼度）」等审校标记，说明该处原文不可靠：\n"
        "   请根据上下文尽力猜测正常译出，并在译文末尾保留「（低信頼度）」标注；\n"
        "   不要把标记本身当作台词翻译（如不要把「認識不良」译成\"认知不良\"）。"
    )

    print(f"{log_prefix}🔄 翻译：{batch_start+1}~{batch_end}")

    result = call_deepseek(
        client, system_prompt, combined,
        verbose=True, enable_search=False,
        stream=True,
        log_prefix=log_prefix,
        show_reasoning=False
    )

    if result is None:
        print(f"{log_prefix}❌ 翻译失败，保留空占位")
        return [""] * len(batch_texts)
    else:
        # Bug 修复：不再过滤空串。一条空译文不应导致整批丢弃——
        # 保留空串才能与输入行数对齐；空条目用原文占位，交给审校阶段兜底。
        parts = [p.strip() for p in result.split("---SPLIT---")]

        if len(parts) == len(batch_texts):
            parts = [p if p else orig for p, orig in zip(parts, batch_texts)]
            print(f"{log_prefix}✅ 翻译完成（{len(parts)} 条）")
            return parts
        else:
            print(f"{log_prefix}⚠ 不匹配（{len(parts)}/{len(batch_texts)}），保留空占位")
            return [""] * len(batch_texts)


# ============================================================
#  第二阶段：逐段审校（并行）
# ============================================================
def _review_batch(client, batch_subs, batch_trans, batch_start, batch_len, log_prefix):
    """审校单个批次（可并行）"""
    review_input_parts = []
    for i, (s, t) in enumerate(
        zip(batch_subs, batch_trans), start=batch_start + 1
    ):
        review_input_parts.append(
            f"[{i}]\n原文：{s['text']}\n初译：{t}"
        )
    review_input = "\n\n".join(review_input_parts)

    system_prompt = (
        "你是一位资深日语音声作品（ASMR）字幕审校。\n"
        "请以简体中文为最终输出语言,逐条审查以下字幕的翻译质量，重点关注：\n"
        "1. 语气词和拟声词是否自然保留；\n"
        "2. 敬语、亲昵称呼是否恰当中文化；\n"
        "3. 是否符合上下文语境的连贯表达；\n"
        "4. 有无明显误译或漏译。\n"
    )

    if ENABLE_SEARCH_IN_REVIEW:
        system_prompt += "5. 遇到不确定的日语词汇或文化概念，请先调用 web_search 搜索确认。\n\n"
    else:
        system_prompt += "\n"

    system_prompt += (
        "**【提交方式（绝对遵守）】**\n"
        "所有审查完成后，调用 submit_review 函数提交最终结果。\n"
        f"lines 数组必须恰好包含 {batch_len} 条修正后的简体中文译文，\n"
        "顺序与输入一一对应。修正不需的行保留初译原文。\n"
        "不要输出序号、不要输出原文、不要解释原因。\n"
        "注意：如果某条日语原文质量极差（Whisper 误识别导致语义不通），请不要跳过该条或输出错误消息。应尽量根据上下文猜测正确译文，猜测困难则根据上下文给出最佳猜测的中文译法，并在末尾添加（低信頼度）标注，确保译文始终是中文。\n"
        "不要输出任何 API 错误消息或字幕不完整等系统提示。\n\n"
    )

    print(f"{log_prefix}🔎 审校：{batch_start+1}~{batch_start+batch_len}")

    result = call_deepseek(
        client, system_prompt, review_input,
        verbose=True,
        enable_search=ENABLE_SEARCH_IN_REVIEW,
        output_tool=OUTPUT_TOOL,
        log_prefix=log_prefix,
        show_reasoning=False
    )

    def per_line_retry(i):
        s = batch_subs[i]
        t = batch_trans[i]
        single_input = f"原文：{s['text']}\n初译：{t}"
        simple_prompt = (
            "请审查以下翻译。原文和初译如下。"
            "只输出修正后的中文译文，不要序号和解释。"
        )
        return call_deepseek(
            client, simple_prompt, single_input,
            verbose=False, enable_search=False,
            stream=True,
            log_prefix=log_prefix,
            show_reasoning=False
        )

    texts, tier = parse_review_result(
        result, batch_len, batch_trans,
        per_line_retry_fn=per_line_retry, verbose=True,
        log_prefix=log_prefix
    )

    return texts


# ============================================================
#  主流程
# ============================================================
def main():
    reset_usage()

    # 以 *_ensemble.srt 为基础输入，若存在 *_reviewed.srt 则优先使用
    ensemble_pattern = os.path.join(INPUT_DIR, "*_ensemble.srt")
    ensemble_files = sorted(glob.glob(ensemble_pattern))
    if not ensemble_files:
        print(f"❌ 未找到 {ensemble_pattern}")
        sys.exit(1)

    files, fallback_count = select_translation_inputs(ensemble_files)

    print("=" * 60)
    print("  翻译 + 逐段审校 — 批量日→中")
    print(f"  输出模式：Function Calling（submit_review）")
    print(f"  并发数：{MAX_WORKERS}")
    if fallback_count:
        print(f"  ⚠ {fallback_count} 个文件无 _reviewed.srt，回退使用 _ensemble.srt")
    print("=" * 60)
    print(f"📋 {len(files)} 个文件")
    for f in files:
        print(f"   • {f}")
    print()

    # ---- 读取所有文件 ----
    all_files_data = []
    for f in files:
        base = os.path.basename(f).replace("_reviewed.srt", "").replace("_ensemble.srt", "")
        output_path = os.path.join(OUTPUT_DIR, f"{base}_zh.srt")
        manifest_path = os.path.join(OUTPUT_DIR, f"{base}_zh_manifest.json")
        if os.path.exists(output_path):
            if artifact_cache_is_current(
                manifest_path, "translated_srt_manifest", f, output_path,
                _translate_generation_fingerprint(),
            ):
                print(f"  ⏭ {base} 翻译缓存有效，跳过")
                continue
            print(f"  ⚠ {base} 翻译缓存来源已变化，备份旧产物后重建")
            backup_stale_artifacts(output_path, manifest_path)
        subs, input_sha256 = parse_file_snapshot(f, parse_srt)
        print(f"  📖 {base} — {len(subs)} 条")
        all_files_data.append({
            "base": base, "path": f, "subs": subs,
            "output_path": output_path,
            "manifest_path": manifest_path,
            "input_sha256": input_sha256,
            "translated": [],
            "revised": []
        })

    if not all_files_data:
        print("  全部文件已处理完毕。")
        print(get_usage_report())
        return

    client = get_llm_client()

    # ================================================================
    #  第一阶段：批量翻译（跨文件+跨批次并行）
    # ================================================================
    print(f"\n{'='*60}")
    print("  第一阶段：批量翻译（并行）")
    print(f"{'='*60}\n")

    all_translate_batches = []
    for fd_idx, fd in enumerate(all_files_data):
        total = len(fd["subs"])
        for bs in range(0, total, TRANSLATE_BATCH_SIZE):
            be = min(bs + TRANSLATE_BATCH_SIZE, total)
            batch = fd["subs"][bs:be]
            batch_texts = [s["text"] for s in batch]
            all_translate_batches.append({
                "fd_idx": fd_idx,
                "bs": bs, "be": be,
                "batch_texts": batch_texts
            })

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for bdata in all_translate_batches:
            fd = all_files_data[bdata["fd_idx"]]
            log_prefix = f"[{fd['base']}] "
            future = executor.submit(
                _translate_batch, client, bdata["batch_texts"],
                bdata["bs"], bdata["be"], log_prefix
            )
            futures[future] = bdata

        for future in as_completed(futures):
            bdata = futures[future]
            fd = all_files_data[bdata["fd_idx"]]
            try:
                result_texts = future.result()
                # 确保 translated 列表已初始化到正确长度
                total = len(fd["subs"])
                while len(fd["translated"]) < total:
                    fd["translated"].append(None)
                for i, text in enumerate(result_texts):
                    fd["translated"][bdata["bs"] + i] = text
            except Exception as e:
                print(f"[{fd['base']}] ❌ 翻译批次 {bdata['bs']+1}~{bdata['be']} 异常: {e}")
                total = len(fd["subs"])
                while len(fd["translated"]) < total:
                    fd["translated"].append(None)
                for i in range(bdata["bs"], bdata["be"]):
                    if fd["translated"][i] is None:
                        fd["translated"][i] = fd["subs"][i]["text"]

    # 填充缺失
    for fd in all_files_data:
        total = len(fd["subs"])
        for i in range(total):
            if i >= len(fd["translated"]) or fd["translated"][i] is None:
                while len(fd["translated"]) < total:
                    fd["translated"].append(fd["subs"][len(fd["translated"])]["text"])
                if fd["translated"][i] is None:
                    fd["translated"][i] = fd["subs"][i]["text"]

    # ================================================================
    #  第二阶段：逐段审校（跨文件+跨批次并行）
    # ================================================================
    print(f"\n{'='*60}")
    print("  第二阶段：逐段审校（并行）")
    print(f"  搜索：{'开' if ENABLE_SEARCH_IN_REVIEW else '关'}")
    print(f"{'='*60}\n")

    all_review_batches = []
    for fd_idx, fd in enumerate(all_files_data):
        total = len(fd["subs"])
        for bs in range(0, total, TRANSLATE_REVIEW_BATCH_SIZE):
            be = min(bs + TRANSLATE_REVIEW_BATCH_SIZE, total)
            batch_subs = fd["subs"][bs:be]
            batch_trans = fd["translated"][bs:be]
            all_review_batches.append({
                "fd_idx": fd_idx,
                "bs": bs, "be": be,
                "batch_subs": batch_subs,
                "batch_trans": batch_trans
            })

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for bdata in all_review_batches:
            fd = all_files_data[bdata["fd_idx"]]
            log_prefix = f"[{fd['base']}] "
            batch_len = len(bdata["batch_subs"])
            future = executor.submit(
                _review_batch, client, bdata["batch_subs"],
                bdata["batch_trans"], bdata["bs"], batch_len, log_prefix
            )
            futures[future] = bdata

        for future in as_completed(futures):
            bdata = futures[future]
            fd = all_files_data[bdata["fd_idx"]]
            try:
                result_texts = future.result()
                total = len(fd["subs"])
                while len(fd["revised"]) < total:
                    fd["revised"].append(None)
                for i, text in enumerate(result_texts):
                    fd["revised"][bdata["bs"] + i] = text
            except Exception as e:
                print(f"[{fd['base']}] ❌ 审校批次 {bdata['bs']+1}~{bdata['be']} 异常: {e}")
                total = len(fd["subs"])
                while len(fd["revised"]) < total:
                    fd["revised"].append(None)
                for i in range(bdata["bs"], bdata["be"]):
                    if fd["revised"][i] is None:
                        fd["revised"][i] = fd["translated"][i]

    # 填充缺失
    for fd in all_files_data:
        total = len(fd["subs"])
        for i in range(total):
            if i >= len(fd["revised"]) or fd["revised"][i] is None:
                while len(fd["revised"]) < total:
                    fd["revised"].append(fd["translated"][len(fd["revised"])])
                if fd["revised"][i] is None:
                    fd["revised"][i] = fd["translated"][i]

    # ---- 写入所有文件 ----
    print(f"\n{'='*60}")
    print("  写入输出文件")
    print(f"{'='*60}\n")
    for fd in all_files_data:
        srt_text = "".join(
            f"{sub['index']}\n"
            f"{sub['start']} --> {sub['end']}\n"
            f"{sub['text']}\n"
            f"{zh}\n\n"
            for sub, zh in zip(fd["subs"], fd["revised"])
        )
        commit_text_artifact(
            fd["manifest_path"], "translated_srt_manifest", fd["path"],
            fd["input_sha256"], fd["output_path"], srt_text,
            _translate_generation_fingerprint(),
        )
        changed_count = sum(
            1 for t, r in zip(fd["translated"], fd["revised"])
            if t.strip() != r.strip()
        )
        print(f"  ✅ {fd['output_path']} — 审校修正 {changed_count} 处")

    print(f"\n🏁 翻译全部完成！")
    print()
    print(get_usage_report())


if __name__ == "__main__":
    main()
