# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
import os
import sys
import time
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from faster_whisper import WhisperModel
from common import (
    get_deepseek_client, call_deepseek, extract_srt_from_response,
    format_timestamp, FUSION_TOOL, read_text_file,
    reset_usage, get_usage_report
)

# ====== 配置 ======
AUDIO_DIR  = os.environ.get("AUDIO_DIR", "./audio")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
ENABLE_SEARCH = os.environ.get("ENABLE_SEARCH", "1") == "1"
AUDIO_EXTS = ['.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus']
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
# ====== 台本功能（V3.2 新增）======
ENABLE_SCRIPT = os.environ.get("ENABLE_SCRIPT", "0") == "1"
SCRIPT_MAPPING_FILE = os.path.join(OUTPUT_DIR, "script_mapping.json")
SCRIPT_VERIFIED_FILE = os.path.join(OUTPUT_DIR, "script_mapping.verified")
MISMATCH_FILE = os.path.join(OUTPUT_DIR, "script_mismatch.json")
# =================================

# ====== mismatch 跟踪（线程安全集合）======
_mismatch_files = set()
# ==========================================

# ====== Whisper 模型缓存 ======
_model_cache = {}

def get_whisper_model(model_name):
    if model_name not in _model_cache:
        print(f"    ⏳ 首次加载 {model_name} ...")
        _model_cache[model_name] = WhisperModel(
            model_name, device="cuda", compute_type="int8_float16"
        )
    else:
        print(f"    ♻ 复用已加载的 {model_name}")
    return _model_cache[model_name]
# ===============================


def transcribe_with_model(model_name, audio_path, output_srt):
    model = get_whisper_model(model_name)
    print(f"    🎙 转写中 ...")
    segments, _ = model.transcribe(
        audio_path,
        language="ja",
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=500,
            threshold=0.3,              
            min_speech_duration_ms=100,  
            speech_pad_ms=600, 
        ),
        condition_on_previous_text=False,
        no_repeat_ngram_size=5,
        repetition_penalty=1.5,
        temperature=0.0,
        compression_ratio_threshold=2.0,
        log_prob_threshold=-1.0,
    )
    with open(output_srt, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(
                f"{i}\n"
                f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n"
                f"{seg.text.strip()}\n\n"
            )
    with open(output_srt, "r", encoding="utf-8") as f:
        count = len(re.findall(r'\n\n+', f.read().strip())) + 1
    print(f"    ✅ {model_name} → {output_srt} ({count} 条)")


def _fix_srt_indexing(srt_text):
    """修复 SRT 文件的序号：确保第一个字幕块有序号 '1'，并顺次编号。"""
    blocks = re.split(r'\n\n+', srt_text.strip())
    if not blocks:
        return srt_text

    fixed_blocks = []
    for i, block in enumerate(blocks, start=1):
        lines = block.strip().split('\n')
        if lines and re.match(r'^\d+$', lines[0]):
            lines[0] = str(i)
        else:
            lines.insert(0, str(i))
        fixed_blocks.append('\n'.join(lines))

    return '\n\n'.join(fixed_blocks) + '\n'

def _fusion_parser(args):
    """FUSION_TOOL 输出解析器。优先检测 error 字段返回特殊标记。"""
    error = args.get("error")
    if error:
        return f"__SCRIPT_MISMATCH__:{error}"
    subtitles = args.get("subtitles", [])
    if not subtitles:
        return None
    blocks = []
    for i, sub in enumerate(subtitles, start=1):
        timecode = sub.get("timecode", "")
        text = sub.get("text", "")
        if timecode and text:
            blocks.append(f"{i}\n{timecode}\n{text}")
    if not blocks:
        return None
    return "\n\n".join(blocks)

def transcribe_one_audio(audio_path, has_script=False):
    """阶段1：V3 + Turbo 双模型转写（GPU 独占，串行执行）
    有台本时跳过 Turbo（mismatch 回退时按需补跑）。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(audio_path))[0]
    v3_srt    = os.path.join(OUTPUT_DIR, f"{base}_v3.srt")
    turbo_srt = os.path.join(OUTPUT_DIR, f"{base}_turbo.srt")

    print(f"\n{'='*60}")
    print(f"📁 {audio_path}")
    print(f"{'='*60}")

    # Step 1: V3
    if os.path.exists(v3_srt):
        print(f"  ⏭ V3 SRT 已存在，跳过")
    else:
        transcribe_with_model("large-v3", audio_path, v3_srt)

    # Step 2: Turbo（有台本时跳过，mismatch 回退会按需补跑）
    if has_script:
        print(f"  ⏭ 有台本，跳过 Turbo（mismatch 时再补跑）")
    elif os.path.exists(turbo_srt):
        print(f"  ⏭ Turbo SRT 已存在，跳过")
    else:
        transcribe_with_model("large-v3-turbo", audio_path, turbo_srt)

    return base, v3_srt, turbo_srt


def fuse_one_audio(audio_path, log_prefix="", script_text="", _is_retry=False):
    """阶段2：DeepSeek 融合（网络 I/O，可并行）
    有台本时走 V3+台本 模式（简化 prompt，跳过 Turbo）；
    无台本或 mismatch 回退时走 V3+Turbo 模式（原 prompt）。"""
    base = os.path.splitext(os.path.basename(audio_path))[0]
    v3_srt    = os.path.join(OUTPUT_DIR, f"{base}_v3.srt")
    turbo_srt = os.path.join(OUTPUT_DIR, f"{base}_turbo.srt")
    ensemble_srt = os.path.join(OUTPUT_DIR, f"{base}_ensemble.srt")

    if os.path.exists(ensemble_srt):
        print(f"{log_prefix}⏭ {ensemble_srt} 已存在，跳过")
        return ("ok", ensemble_srt)

    # Read V3
    with open(v3_srt, "r", encoding="utf-8") as f:
        v3_text = f.read().strip()
    v3_lines = len(re.findall(r'\n\n+', v3_text)) + 1

    use_script = bool(script_text) and not _is_retry

    client = get_deepseek_client()

    if use_script:
        # ===== 台本模式：V3 + 台本（不需要 Turbo）=====
        print(f"{log_prefix}📄 台本模式：V3 {v3_lines} 条 + 台本")
        user_input = (
            "以下はWhisperモデルlarge-v3で書き起こした字幕と、公式台本です。\n\n"
            "【字幕：large-v3】\n```srt\n" + v3_text + "\n```\n\n"
            "【公式台本（時間軸なし、権威テキスト、非台詞説明を含む可能性あり）】\n"
            "```\n" + script_text + "\n```\n\n"
            "【台本の取り扱い】\n"
            "1. 台本中の※注記・シーン説明などの非台詞内容は文脈理解のみに用い、字幕に入れないこと\n"
            "2. テキストは台本を優先し、時間軸はWhisperを使用すること\n"
            "3. 台本にのみ存在する発話も必ず採用すること（Whisperが拾えなかった発話の補完）。\n"
            "   その際、時間軸が不明な場合は前後の発話の時間軸を参考に推定すること\n"
            "4. Whisperのみの発話は台本の抜け漏れの可能性があるため残すこと\n"
            "5. 台本とWhisper出力が著しく不一致する場合\n"
            "   （別作品の台本・内容の交差なし・音声と台本の完全なミスマッチ等）、\n"
            "   subtitlesを空配列にし、errorフィールドに理由を記入すること。\n"
            "   ただし部分的不一致は修正対象であり、error報告の対象外とする\n\n"
            "Whisperの時間軸と台本のテキストを統合して字幕を生成してください。"
        )
        system_prompt = (
            "あなたは日本語音声認識の字幕校正エキスパートです。"
            "Whisperモデル（large-v3）の転写結果と公式台本を統合し、"
            "台本を権威テキストとして字幕を生成してください。\n"
            "1. 時間軸はWhisperの出力を使用し、テキストは台本を優先\n"
            "2. 台本中の非台詞内容（※注記・シーン説明）は字幕に入れない\n"
            "3. 断片化は統合、重複は削除（意図的繰り返しは残す）\n"
            "4. わからない単語があれば web_search で検索して確認\n"
            "5. 【最重要】冒頭の発話の扱い：\n"
            "   音声の冒頭は環境音や囁きのために認識精度が低下することがありますが、\n"
            "   それは実際の台詞が存在しないことを意味しません。\n"
            "   Whisper・台本のどちらか一方でも最初の数秒間に発話があれば、\n"
            "   必ず統合後の字幕の1行目として採用してください。\n"
            "   たとえ「あ…」「えっと…」のような一言でも残してください。\n"
            "6. 「(中略)」「(省略)」などの省略記号を絶対に使用しないでください。\n"
            "【提出方法（絶対厳守）】\n"
            "統合が完了したら、submit_fusion 関数を呼び出して結果を提出してください。\n"
            "subtitles配列の各要素に timecode と text を含めてください。\n"
            "番号は不要です（自動付与されます）。説明や分析は一切不要です。"
        )
    else:
        # ===== 无台本模式：V3 + Turbo（原逻辑）=====
        if not os.path.exists(turbo_srt):
            print(f"{log_prefix}⏳ 补跑 Turbo ...")
            transcribe_with_model("large-v3-turbo", audio_path, turbo_srt)
        with open(turbo_srt, "r", encoding="utf-8") as f:
            turbo_text = f.read().strip()
        turbo_lines = len(re.findall(r'\n\n+', turbo_text)) + 1
        print(f"{log_prefix}📄 V3: {v3_lines} 条 / Turbo: {turbo_lines} 条"
              + ("（mismatch 回退）" if _is_retry else ""))

        user_input = (
            "以下の2つの字幕ファイルは、同じ日本語音声を異なるWhisperモデルで書き起こしたものです。\n\n"
            "【字幕A：large-v3（高精度）】\n```srt\n" + v3_text + "\n```\n\n"
            "【字幕B：large-v3-turbo（高速）】\n```srt\n" + turbo_text + "\n```\n\n"
        )
        user_input += "2つの字幕を比較し、より正確な字幕を1つに統合してください。"
        system_prompt = (
            "あなたは日本語音声認識の字幕校正エキスパートです。"
            "2つのWhisperモデルの字幕を比較・統合してください。\n"
            "1. 時刻が近いもの同士を同じ発話とみなし、より自然な方を採用\n"
            "2. 文法的正しさ・文脈整合性・同音異義語誤りをチェック\n"
            "3. 断片化は統合、重複は削除（意図的繰り返しは残す）\n"
            "4. 時刻は原則 V3 を採用、turbo のみの発話は turbo の時刻を使用\n"
            "5. わからない単語があれば web_search で検索して確認\n"
            "6. 【重要】片方のモデルにしか存在しない発話について：\n"
            "   特に字幕A（V3）にのみ存在し字幕B（turbo）にない発話は、\n"
            "   たとえ断片的でも必ず採用してください。\n"
            "6.5. 【最重要】冒頭の字幕（最初の数行）の扱い：\n"
            "   音声の冒頭は環境音や囁きのために認識精度が低下することがありますが、\n"
            "   それは実際の台詞が存在しないことを意味しません。\n"
            "   字幕A・字幕Bのどちらか一方でも最初の数秒間に発話があれば、\n"
            "   必ずそれを統合後の字幕の1行目として採用してください。\n"
            "   冒頭の発話は断片的でも挨拶や状況説明であることが多いため、\n"
            "   それを削除するとストーリーの冒頭が失われます。\n"
            "   たとえ「あ…」「えっと…」のような一言でも残してください。\n"
            "   自然な発話か判断できない場合でも決して省略・削除しないでください。\n"
            "7. 「(中略)」「(省略)」などの省略記号を絶対に使用しないでください。\n"
            "【提出方法（絶対厳守）】\n"
            "統合が完了したら、submit_fusion 関数を呼び出して結果を提出してください。\n"
            "subtitles配列の各要素に timecode と text を含めてください。\n"
            "番号は不要です（自動付与されます）。説明や分析は一切不要です。"
        )

    input_len = len(user_input)
    estimated_tokens = input_len // 2
    print(f"{log_prefix}📨 发送融合请求：{input_len} 字符（约 {estimated_tokens} tokens）")

    if estimated_tokens > 100000:
        print(f"{log_prefix}⚠ 输入过大，可能超出模型限制。建议将音频切割后再处理。")
        return ("failed", None)

    if ENABLE_SEARCH:
        system_prompt += "   不确定的文化概念请先 web_search 搜索确认。\n\n"

    result = call_deepseek(
        client, system_prompt, user_input,
        verbose=True, enable_search=ENABLE_SEARCH,
        output_tool=FUSION_TOOL,
        output_tool_parser=_fusion_parser,
        stream=False,
        log_prefix=log_prefix,
        show_reasoning=False
    )

    # 检测台本不匹配（仅台本模式）
    if use_script and result and result.startswith("__SCRIPT_MISMATCH__:"):
        error_msg = result[len("__SCRIPT_MISMATCH__:"):]
        print(f"{log_prefix}⚠ 台本不匹配：{error_msg}，回退到无台本模式（V3+Turbo）")
        _mismatch_files.add(base)
        return fuse_one_audio(audio_path, log_prefix, "", _is_retry=True)

    if not result:
        print(f"{log_prefix}❌ 融合失败（可能原因：API 错误 / max_tokens 不足 / 输出格式异常）")
        print(f"{log_prefix}   输入大小：{input_len} 字符，V3：{v3_lines} 条")
        return ("failed", None)

    fixed = extract_srt_from_response(result)
    if '-->' not in fixed:
        raw = os.path.join(OUTPUT_DIR, f"{base}_raw.txt")
        with open(raw, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"{log_prefix}⚠ 格式异常，原始响应 → {raw}")
        return ("failed", None)

    fixed = _fix_srt_indexing(fixed)
    with open(ensemble_srt, "w", encoding="utf-8") as f:
        f.write(fixed + "\n")
    el = len(re.findall(r'\n\n+', fixed.strip())) + 1
    print(f"{log_prefix}✅ {ensemble_srt} ({el} 条)")
    return ("ok", ensemble_srt)


def check_script_mapping():
    """启动时检查台本映射文件与验证标记，返回 {audio_base: script_text} 字典。"""
    if not ENABLE_SCRIPT:
        return {}

    if not os.path.exists(SCRIPT_MAPPING_FILE):
        print("❌ 台本映射文件不存在，请先运行 match_scripts.py")
        sys.exit(1)
    if not os.path.exists(SCRIPT_VERIFIED_FILE):
        print("❌ 台本映射未通过人工验证，请运行 match_scripts.py 完成验证")
        sys.exit(1)

    with open(SCRIPT_MAPPING_FILE, "r", encoding="utf-8") as f:
        mapping = json.load(f)

    # 检查台本文件是否在验证后被修改
    verified_mtime = os.path.getmtime(SCRIPT_VERIFIED_FILE)
    for audio_base, info in mapping.items():
        script_path = info.get("script_path")
        if script_path and os.path.exists(script_path):
            if os.path.getmtime(script_path) > verified_mtime:
                print(f"❌ 台本 {script_path} 在验证后被修改，请重新运行 match_scripts.py")
                sys.exit(1)

    # 读取所有台本文本
    script_texts = {}
    for audio_base, info in mapping.items():
        script_path = info.get("script_path")
        if script_path and os.path.exists(script_path):
            try:
                script_texts[audio_base] = read_text_file(script_path).strip()
            except Exception as e:
                print(f"⚠ 读取台本 {script_path} 失败：{e}")
                script_texts[audio_base] = ""
        else:
            script_texts[audio_base] = ""
    return script_texts


def collect_audio_files():
    files = []
    if os.path.isfile(AUDIO_DIR):
        return [AUDIO_DIR]
    if os.path.isdir(AUDIO_DIR):
        for f in sorted(os.listdir(AUDIO_DIR)):
            if any(f.lower().endswith(ext) for ext in AUDIO_EXTS):
                files.append(os.path.join(AUDIO_DIR, f))
    return files


def main():
    reset_usage()

    audio_files = collect_audio_files()
    if not audio_files:
        print(f"❌ 在 {AUDIO_DIR} 中未找到音频文件！")
        print(f"   支持的格式：{', '.join(AUDIO_EXTS)}")
        sys.exit(1)

    # V3.2 新增：台本映射检查
    script_texts = check_script_mapping()
    if ENABLE_SCRIPT:
        with_open = sum(1 for v in script_texts.values() if v)
        print(f"📜 台本功能已开启：{with_open} / {len(audio_files)} 个音频有台本")

    print("=" * 60)
    print("  Whisper Ensemble Batch — 批量双模型融合转写")
    print(f"  模式：阶段1串行转写 + 阶段2并行融合（并发数 {MAX_WORKERS}）")
    if ENABLE_SCRIPT:
        print(f"  台本辅助：开启")
    print("=" * 60)
    print(f"📋 {len(audio_files)} 个文件")
    for f in audio_files:
        print(f"   • {f}")
    print(f"📂 输出：{OUTPUT_DIR}/")
    print(f"🔍 搜索：{'开' if ENABLE_SEARCH else '关'}")
    print()

    t0 = time.time()

    # ================================================================
    #  阶段1：串行转写所有文件（GPU 独占）
    # ================================================================
    print(f"\n{'#'*60}")
    print("# 阶段1：双模型转写（串行，GPU 独占）")
    print(f"{'#'*60}\n")

    transcribed = []
    for i, af in enumerate(audio_files, 1):
        print(f"\n{'#'*50}")
        print(f"### [转写 {i}/{len(audio_files)}] {os.path.basename(af)}")
        print(f"{'#'*50}")
        base = os.path.splitext(os.path.basename(af))[0]
        has_script = bool(script_texts.get(base, ""))
        try:
            transcribe_one_audio(af, has_script=has_script)
            transcribed.append(af)
        except Exception as e:
            print(f"  ❌ 转写异常：{e}")

    # ================================================================
    #  阶段2：并行融合所有文件（网络 I/O）
    # ================================================================
    print(f"\n{'#'*60}")
    print(f"# 阶段2：DeepSeek 融合（并行，并发数 {MAX_WORKERS}）")
    print(f"{'#'*60}\n")

    # 预初始化 client，避免多线程首次调用时重复创建
    get_deepseek_client()

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for af in transcribed:
            base = os.path.splitext(os.path.basename(af))[0]
            log_prefix = f"[{base}] "
            script_text = script_texts.get(base, "")  # V3.2 新增
            future = executor.submit(
                fuse_one_audio, af, log_prefix, script_text
            )
            futures[future] = af

        for future in as_completed(futures):
            af = futures[future]
            base = os.path.splitext(os.path.basename(af))[0]
            try:
                status, data = future.result()
                if status == "ok":
                    results.append((af, data))
                else:
                    results.append((af, None))
            except Exception as e:
                print(f"[{os.path.basename(af)}] ❌ 融合异常: {e}")
                results.append((af, None))

    # 写入 mismatch 列表（供下游 STEP2 选择性触发）
    with open(MISMATCH_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(_mismatch_files), f, ensure_ascii=False, indent=2)

    if _mismatch_files:
        print(f"\n⚠ 检测到 {len(_mismatch_files)} 个文件台本不匹配，已回退无台本模式：")
        for name in sorted(_mismatch_files):
            print(f"   • {name}")
        print(f"   已记录至 {MISMATCH_FILE}")
        print(f"   下游 STEP2（日语二审）将仅对这些文件执行")
    else:
        print(f"\n✅ 无台本不匹配（mismatch 列表为空）")
        if ENABLE_SCRIPT:
            print(f"   下游 STEP2（日语二审）可跳过")

    print(f"\n{'='*60}")
    print(f"🏁 完成！总耗时 {(time.time()-t0)/60:.1f} 分钟")
    print(f"{'='*60}")
    for af, out in results:
        print(f"  {'✅' if out else '❌'} {os.path.basename(af)}")
    print(f"\n💡 下一步：output/*_ensemble.srt → review_japanese.py")

    print()
    print(get_usage_report())


if __name__ == "__main__":
    main()
