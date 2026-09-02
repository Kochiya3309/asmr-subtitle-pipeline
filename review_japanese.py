# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
import os
import sys
import time
import json
import glob
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (
    get_llm_client, get_llm_config, call_deepseek, parse_srt,
    parse_review_result, OUTPUT_TOOL, try_extract_lines_from_analysis,
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
PATTERN    = "*_ensemble.srt"
ENABLE_SEARCH = os.environ.get("ENABLE_SEARCH", "1") == "1"
BATCH_SIZE = int(os.environ.get("REVIEW_JP_BATCH_SIZE", "30"))
OVERLAP = int(os.environ.get("REVIEW_JP_OVERLAP", "5"))
ENABLE_FULL_REVIEW = os.environ.get("REVIEW_JP_FULL_REVIEW", "1") == "1"
FULL_REVIEW_BATCH = int(os.environ.get("REVIEW_JP_FULL_REVIEW_BATCH", "200"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
# 台本模式下用于选择性审校（仅 mismatch 文件）
MISMATCH_FILE = os.path.join(OUTPUT_DIR, "script_mismatch.json")
REVIEW_JP_GENERATION_VERSION = "review-japanese-2026-08-29.1"
# ==================


def _review_generation_fingerprint():
    _, base_url, model, enable_thinking, max_tokens = get_llm_config()
    return canonical_fingerprint({
        "version": REVIEW_JP_GENERATION_VERSION,
        "tool": OUTPUT_TOOL,
        "llm": {
            "base_url": base_url,
            "model": model,
            "enable_thinking": enable_thinking,
            "max_tokens": max_tokens,
            "temperature": 0.25,
        },
        "search": ENABLE_SEARCH,
        "batch_size": BATCH_SIZE,
        "overlap": OVERLAP,
        "full_review": ENABLE_FULL_REVIEW,
        "full_review_batch": FULL_REVIEW_BATCH,
    })


def _build_japanese_corpus(subs, texts):
    """构建全篇日语语料视图，供全篇上下文审校使用"""
    lines = []
    for s, text in zip(subs, texts):
        lines.append(f"[{s['index']}] {text}")
    return "\n".join(lines)


def _full_review_batch(subs_batch, texts_batch, client, batch_start, batch_end, log_prefix=""):
    """处理单批全篇上下文审校"""
    batch_len = len(texts_batch)
    corpus = _build_japanese_corpus(subs_batch, texts_batch)

    system_prompt = (
        "あなたは日本語ASMR字幕の校正専門家です。\n"
        "今回は特別に、字幕全体を通読できる状態で審査を行います。\n\n"
        "**【あなたの役割】**\n"
        "これまでのバッチ処理では見えなかった「作品全体の文脈」を活用し、\n"
        "局所的な審査では判断できなかった誤認識を修正してください。\n\n"
        "**【背景情報】**\n"
        "この音声作品は成人向け（アダルト）ASMRであり、\n"
        "催淫・媚薬・触手・機械実験・快楽調教などの性的・SFテーマが含まれています。\n"
        "Whisper（音声認識AI）は、以下のような誤りを犯すことがあります：\n"
        "・同音異義語の誤選択（例：「美薬」→正しくは「媚薬」、「温度」→正しくは「感度」）\n"
        "・文法的に正しいが文脈的にありえない単語の出力\n"
        "  （例：実験室なのに「海岸」「仏教」「獅子」など）\n"
        "・専門用語の誤認識\n"
        "  （例：「プログラマー」→正しくは「プログラム」）\n\n"
        "**【審査手順】**\n"
        "1. まず全行を通読し、この作品の世界観・設定・テーマを把握する\n"
        "2. その上で各行をチェックし、以下の観点で修正が必要か判断する：\n"
        "   - 作品の世界観・テーマと矛盾する単語がないか\n"
        "   - 同音異義語の誤選択がないか（全文の用語傾向と比較）\n"
        "   - 全文で使われている専門用語・固有表現と整合しているか\n"
        "3. 確信がある場合のみ修正し、判断に迷う場合は原文のままとする\n\n"
    )

    system_prompt += (
        "**【提出方法（絶対厳守）】**\n"
        "すべての審査が完了したら、submit_review 関数を呼び出して結果を提出してください。\n"
        f"lines 配列には {batch_len} 行すべての修正後テキストを、\n"
        "入力と同じ順序で入れてください。修正不要な行は原文のまま。\n"
        "1行も欠かさず、1行も多くなく。説明や分析は一切不要です。"
    )

    print(f"{log_prefix}📄 全篇语料已打包（{len(corpus)} 字符）...")

    result = call_deepseek(
        client, system_prompt, corpus,
        verbose=True,
        enable_search=False,
        output_tool=OUTPUT_TOOL,
        log_prefix=log_prefix,
        show_reasoning=False
    )

    if result is None:
        print(f"{log_prefix}❌ 全篇审校失败，保留批次审校结果")
        return texts_batch

    parts = result.split("---SPLIT---")
    parts = [p.strip() for p in parts]
    parts = [p for p in parts if p]

    if len(parts) == batch_len:
        changed = 0
        corrected = []
        for i, (orig, part) in enumerate(zip(texts_batch, parts)):
            new_text = part.strip()
            corrected.append(new_text)
            if orig.strip() != new_text:
                changed += 1
                idx = batch_start + i + 1
                print(f"{log_prefix}   [{idx}] {orig[:30]}... → {new_text[:30]}...")
        print(f"{log_prefix}✅ 全篇审校完成（{changed} 处修正）")
        return corrected
    else:
        print(f"{log_prefix}⚠ 件数不一致（{len(parts)}/{batch_len}），降级提取...")
        fallback = try_extract_lines_from_analysis(result, batch_len)
        if fallback:
            corrected = []
            for orig, fb in zip(texts_batch, fallback):
                corrected.append(fb if fb else orig)
            print(f"{log_prefix}   ✅ 降级提取成功")
            return corrected
        else:
            print(f"{log_prefix}   ❌ 降级提取失败，保留批次审校结果")
            return texts_batch


def _full_context_review(subs, reviewed_texts, client, log_prefix=""):
    """全篇上下文审校"""
    total = len(reviewed_texts)
    print(f"{log_prefix}🔍 全篇上下文审校（{total} 条）")

    if total <= FULL_REVIEW_BATCH:
        return _full_review_batch(subs, reviewed_texts, client, 0, total, log_prefix)

    # 修复：FULL_REVIEW_BATCH <= 10 时 range step 为 0（ValueError）或负数
    # （空 range → 全篇审校被静默跳过）。钳制为最小步长 1。
    step = max(FULL_REVIEW_BATCH - 10, 1)

    all_corrected = list(reviewed_texts)
    for batch_start in range(0, total, step):
        batch_end = min(batch_start + FULL_REVIEW_BATCH, total)
        batch_result = _full_review_batch(
            subs[batch_start:batch_end],
            all_corrected[batch_start:batch_end],
            client, batch_start, batch_end, log_prefix
        )
        for i, (bi, text) in enumerate(zip(range(batch_start, batch_end), batch_result)):
            if all_corrected[bi] != text:
                all_corrected[bi] = text

    return all_corrected


def _process_batch(client, batch, batch_start, batch_end, log_prefix):
    """处理单个审校批次（可并行）"""
    batch_len = len(batch)
    review_input = "\n".join([f"[{s['index']}] {s['text']}" for s in batch])

    system_prompt = (
        "あなたは日本語ASMR字幕の校正専門家です。\n"
        "以下の字幕を1行ずつ慎重に審査し、誤認識があれば修正してください。\n\n"
        "注意：この音声作品は成人向け（アダルト）コンテンツであり、\n"
        "催淫・媚薬・触手などの性的テーマが含まれている可能性があります。\n"
        "同音異義語のチェック時はこの文脈を考慮してください。\n\n"
        "**【審査の観点】**\n"
        "1. まず全行を通読し、場面の流れ・話者の関係・会話のトーンを把握\n"
        "2. 前後2〜3行を参照しながら各行をチェック：\n"
        "   - 同音異義語の誤認識（文脈から明らかにおかしい単語）\n"
        "   - 息遣い・囁きが意味のある単語として誤認識されていないか\n"
        "   - 助詞の誤り（「が」⇔「は」など）\n"
        "   - 文の不自然な途切れや連結\n"
        "   - 同一話者の呼称ブレ\n"
        "   - 前後の行と矛盾する内容\n"
        "3. ASMR特有の注意：左右交互セリフの統一、環境音説明の保持\n"
        "   無意味な文字列は推測修正、不能なら「…」に置き換え\n"
        "3.5. **低品質音声による誤認識の検出**：\n"
        "   音声の乱れや環境音により、Whisperが意味不明な単語を出力することがあります。\n"
        "   以下のようなケースでは、前後の文脈と作品の世界観から正しい単語を推測してください。\n"
        "   - 作品の舞台と矛盾する単語（実験室なのに「海岸」「獅子」など）\n"
        "   - 文法的に正しいが意味が通らない組み合わせ（「海岸が破裂寸前」など）\n"
        "   - 前前後の技術用語や性的用語と明らかに異質な単語\n"
        "   推測不能の場合は、無理に修正せず「〔認識不良〕」とマークしてください。\n"
    )

    if ENABLE_SEARCH:
        system_prompt += "4. わからない単語があれば web_search で検索\n\n"
    else:
        system_prompt += "\n"

    system_prompt += (
        "**【提出方法（絶対厳守）】**\n"
        "すべての審査が完了したら、submit_review 関数を呼び出して結果を提出してください。\n"
        f"lines 配列には {batch_len} 行すべての修正後テキストを、\n"
        "入力と同じ順序で入れてください。修正不要な行は原文のまま。\n"
        "1行も欠かさず、1行も多くなく。説明や分析は一切不要です。"
    )

    print(f"{log_prefix}🔎 批次 {batch_start+1}~{batch_end}")

    result = call_deepseek(
        client, system_prompt, review_input,
        verbose=True,
        enable_search=ENABLE_SEARCH,
        output_tool=OUTPUT_TOOL,
        log_prefix=log_prefix,
        show_reasoning=False
    )

    batch_texts = [s["text"] for s in batch]

    def per_line_retry(i):
        s = batch[i]
        return call_deepseek(
            client,
            "以下の日本語字幕を校正してください。修正後の字幕のみ出力。説明不要。",
            s["text"],
            verbose=False, enable_search=False,
            stream=True,
            log_prefix=log_prefix,
            show_reasoning=False
        )

    texts, tier = parse_review_result(
        result, batch_len, batch_texts,
        per_line_retry_fn=per_line_retry, verbose=True,
        log_prefix=log_prefix
    )

    return texts


def main():
    reset_usage()

    pattern = os.path.join(INPUT_DIR, PATTERN)
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"❌ 未找到匹配 {pattern} 的文件")
        sys.exit(1)

    # 台本模式下：若存在 mismatch 列表，仅审校 mismatch 文件
    if os.path.exists(MISMATCH_FILE):
        try:
            with open(MISMATCH_FILE, "r", encoding="utf-8") as f:
                mismatch_list = set(json.load(f))
        except Exception:
            mismatch_list = set()
        if mismatch_list:
            filtered = [f for f in files if os.path.basename(f).replace("_ensemble.srt", "") in mismatch_list]
            print(f"🔬 检测到 mismatch 列表（{len(mismatch_list)} 个），仅审校这些文件")
            files = filtered
            if not files:
                print(f"   ⏭ 无需审校的文件（mismatch 文件均已有 reviewed 或未生成 ensemble）")
                print(get_usage_report())
                return

    print("=" * 60)
    print("  日语字幕二审 — 批量审查转写质量")
    print(f"  输出模式：Function Calling（submit_review）")
    print(f"  全篇上下文审校：{'开' if ENABLE_FULL_REVIEW else '关'}")
    print(f"  并发数：{MAX_WORKERS}")
    print("=" * 60)
    print(f"📋 {len(files)} 个文件")
    for f in files:
        print(f"   • {f}")
    print(f"🔍 搜索：{'开' if ENABLE_SEARCH else '关'}")
    print()

    # ---- 读取所有文件 ----
    all_files_data = []
    for f in files:
        base = os.path.basename(f).replace("_ensemble.srt", "")
        output_path = os.path.join(OUTPUT_DIR, f"{base}_reviewed.srt")
        manifest_path = os.path.join(OUTPUT_DIR, f"{base}_reviewed_manifest.json")
        if os.path.exists(output_path):
            if artifact_cache_is_current(
                manifest_path, "reviewed_srt_manifest", f, output_path,
                _review_generation_fingerprint(),
            ):
                print(f"  ⏭ {base} 审校缓存有效，跳过")
                continue
            print(f"  ⚠ {base} 审校缓存来源已变化，备份旧产物后重建")
            backup_stale_artifacts(output_path, manifest_path)
        subs, input_sha256 = parse_file_snapshot(f, parse_srt)
        print(f"  📖 {base} — {len(subs)} 条")
        all_files_data.append({
            "base": base, "path": f, "subs": subs,
            "output_path": output_path,
            "manifest_path": manifest_path,
            "input_sha256": input_sha256,
            "reviewed": [None] * len(subs),
            "all_changes": []
        })

    if not all_files_data:
        print("  全部文件已处理完毕，无需重新审校。")
        print(get_usage_report())
        return

    client = get_llm_client()

    # ================================================================
    #  阶段一：逐批审校（跨文件+跨批次并行）
    # ================================================================
    print(f"\n{'='*60}")
    print("  阶段一：逐批审校（并行）")
    print(f"{'='*60}\n")

    all_batches = []
    for fd_idx, fd in enumerate(all_files_data):
        bs = 0
        while bs < len(fd["subs"]):
            be = min(bs + BATCH_SIZE, len(fd["subs"]))
            batch = fd["subs"][bs:be]
            all_batches.append({
                "fd_idx": fd_idx,
                "batch": batch,
                "bs": bs, "be": be,
            })
            bs += (BATCH_SIZE - OVERLAP)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for bdata in all_batches:
            fd = all_files_data[bdata["fd_idx"]]
            log_prefix = f"[{fd['base']}] "
            future = executor.submit(
                _process_batch, client, bdata["batch"],
                bdata["bs"], bdata["be"], log_prefix
            )
            futures[future] = bdata

        for future in as_completed(futures):
            bdata = futures[future]
            fd = all_files_data[bdata["fd_idx"]]
            try:
                texts = future.result()
                changed_in_batch = 0
                for i, (s, text) in enumerate(zip(bdata["batch"], texts)):
                    real_idx = bdata["bs"] + i
                    if fd["reviewed"][real_idx] is None:
                        fd["reviewed"][real_idx] = text
                        if s["text"].strip() != text:
                            changed_in_batch += 1
                            fd["all_changes"].append({
                                "index": s["index"],
                                "original": s["text"],
                                "revised": text,
                            })
                print(f"[{fd['base']}] ✅ 批次 {bdata['bs']+1}~{bdata['be']} 完成（{changed_in_batch} 件修正）")
            except Exception as e:
                print(f"[{fd['base']}] ❌ 批次 {bdata['bs']+1}~{bdata['be']} 异常: {e}")
                # 回退：用原文填充
                for i, s in enumerate(bdata["batch"]):
                    real_idx = bdata["bs"] + i
                    if fd["reviewed"][real_idx] is None:
                        fd["reviewed"][real_idx] = s["text"]

    # ---- 组装阶段一结果 ----
    for fd in all_files_data:
        total = len(fd["subs"])
        reviewed = [t for t in fd["reviewed"] if t is not None]
        if len(reviewed) < total:
            print(f"[{fd['base']}] ⚠ 缺少 {total - len(reviewed)} 条，用原文补充")
            for i in range(total):
                if fd["reviewed"][i] is None:
                    fd["reviewed"][i] = fd["subs"][i]["text"]
        fd["reviewed"] = [t for t in fd["reviewed"] if t is not None]
        # 确保 reviewed 列表长度和 subs 一致
        while len(fd["reviewed"]) < total:
            fd["reviewed"].append(fd["subs"][len(fd["reviewed"])]["text"])

    # ================================================================
    #  阷段二：全篇上下文审校（跨文件并行）
    # ================================================================
    if ENABLE_FULL_REVIEW:
        print(f"\n{'='*60}")
        print("  阶段二：全篇上下文审校（并行）")
        print(f"{'='*60}\n")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}
            for fd in all_files_data:
                log_prefix = f"[{fd['base']}] "
                future = executor.submit(
                    _full_context_review, fd["subs"], fd["reviewed"], client, log_prefix
                )
                futures[future] = fd

            for future in as_completed(futures):
                fd = futures[future]
                try:
                    fd["reviewed"] = future.result()
                    print(f"[{fd['base']}] ✅ 全篇审校完成")
                except Exception as e:
                    print(f"[{fd['base']}] ❌ 全篇审校异常: {e}")

    # ---- 写入所有文件 ----
    print(f"\n{'='*60}")
    print("  写入输出文件")
    print(f"{'='*60}\n")
    for fd in all_files_data:
        srt_text = "".join(
            f"{sub['index']}\n"
            f"{sub['start']} --> {sub['end']}\n"
            f"{text}\n\n"
            for sub, text in zip(fd["subs"], fd["reviewed"])
        )
        commit_text_artifact(
            fd["manifest_path"], "reviewed_srt_manifest", fd["path"],
            fd["input_sha256"], fd["output_path"], srt_text,
            _review_generation_fingerprint(),
        )
        print(f"  ✅ {fd['output_path']} — 修正 {len(fd['all_changes'])} 处")

    print(f"\n🏁 日语二审全部完成！")
    print()
    print(get_usage_report())


if __name__ == "__main__":
    main()
