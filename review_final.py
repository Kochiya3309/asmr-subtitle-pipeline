# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
from output_layout import output_file, artifact_name, discover_outputs, prepare_output, output_root
import re
import os
import sys
import json
import shutil
import time
import tempfile
import hashlib
import unicodedata
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (
    get_llm_client, get_llm_config, call_deepseek, parse_srt_full,
    reset_usage, get_usage_report
)
from human_review import validate_completed_review_outputs
from pipeline_inputs import discover_active_audio
from review_app import build_bundle_for_base
from asr_evidence import canonical_fingerprint, write_json_atomic

INPUT_DIR  = os.environ.get("OUTPUT_DIR", "./output")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
PATTERN    = "*_zh.srt"
ENABLE_SEARCH = os.environ.get("ENABLE_SEARCH", "1") == "1"
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
ENABLE_HUMAN_REVIEW = os.environ.get("ENABLE_HUMAN_REVIEW", "0") == "1"
FINAL_REVIEW_GENERATION_VERSION = "final-review-manifest-v1"
FINAL_REVIEW_PROMPT_VERSION = "final-review-prompt-2026-08-28"

CORRECTION_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_corrections",
        "description": "提交终审修正清单。只列出确实需要修改的条目。如果全篇完美无需修改，返回空数组。",
        "parameters": {
            "type": "object",
            "properties": {
                "corrections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "字幕序号（从1开始）"},
                            "issue": {"type": "string", "description": "一句话说明问题所在"},
                            "fix": {"type": "string", "description": "修正后的完整中文译文"}
                        },
                        "required": ["index", "issue", "fix"]
                    }
                }
            },
            "required": ["corrections"]
        }
    }
}

def _corrections_parser(args):
    return args.get("corrections", [])

def parse_corrections_from_text(raw_text):
    p = re.compile(r'索引[：:]\s*(\d+).*?\n.*?问题[：:]\s*(.*?)\n.*?修正[：:]\s*(.*?)(?=\n---|\n索引|\Z)', re.DOTALL)
    corrections = {}
    for m in p.finditer(raw_text):
        idx = int(m.group(1))
        if idx in corrections:
            raise ValueError(f"终审修正包含重复 index：{idx}")
        corrections[idx] = {"issue": m.group(2).strip(), "fix": m.group(3).strip()}
    return corrections


def _safe_final_translation(value, *, field="fix"):
    if not isinstance(value, str):
        raise ValueError(f"终审 {field} 必须是字符串")
    text = value.strip()
    if not text:
        raise ValueError(f"终审 {field} 不能为空")
    if "\n" in text or "\r" in text or "-->" in text:
        raise ValueError(f"终审 {field} 不得包含 SRT 结构")
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise ValueError(f"终审 {field} 包含控制字符")
    return text


def _validate_corrections(raw_corrections, valid_indices):
    if isinstance(raw_corrections, dict):
        rows = [
            {"index": index, **value}
            for index, value in raw_corrections.items()
            if isinstance(value, dict)
        ]
        if len(rows) != len(raw_corrections):
            raise ValueError("终审修正格式无效")
    elif isinstance(raw_corrections, list):
        rows = raw_corrections
    else:
        raise ValueError("终审修正必须是数组或映射")

    corrections = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("终审修正条目必须是对象")
        index = row.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("终审修正 index 必须是整数")
        if index not in valid_indices:
            raise ValueError(f"终审修正引用未知 index：{index}")
        if index in corrections:
            raise ValueError(f"终审修正包含重复 index：{index}")
        issue = row.get("issue", "")
        if not isinstance(issue, str) or any(
            unicodedata.category(char) == "Cc" and char not in "\t"
            for char in issue
        ):
            raise ValueError(f"终审 issue 格式无效：{index}")
        corrections[index] = {
            "issue": issue.strip(),
            "fix": _safe_final_translation(row.get("fix"), field=f"fix[{index}]"),
        }
    return corrections


def _validate_final_srt_text(srt_text, expected_subs):
    normalized = srt_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if any(unicodedata.category(char) == "Cc" and char not in "\n\t" for char in normalized):
        raise ValueError("终审 SRT 包含控制字符")
    blocks = re.split(r"\n{2,}", normalized) if normalized else []
    if len(blocks) != len(expected_subs):
        raise ValueError(f"终审 SRT 条目数异常：{len(blocks)}/{len(expected_subs)}")
    seen = set()
    for block, expected in zip(blocks, expected_subs):
        lines = block.split("\n")
        if len(lines) != 4:
            raise ValueError("终审 SRT 每条必须严格包含 index、时间轴、日文、中文四行")
        try:
            index = int(lines[0])
        except ValueError as exc:
            raise ValueError("终审 SRT index 无效") from exc
        if index in seen or index != expected["index"]:
            raise ValueError("终审 SRT index 重复或顺序变化")
        seen.add(index)
        if lines[1] != expected["timecode"]:
            raise ValueError(f"终审不得修改时间轴：{index}")
        if lines[2] != expected["text_ja"]:
            raise ValueError(f"终审不得修改日文：{index}")
        _safe_final_translation(lines[3], field=f"中文[{index}]")

def build_corpus_view(subs):
    lines = []
    for s in subs:
        lines.append(f"[{s['index']}]")
        lines.append(f"日：{s['text_ja']}")
        lines.append(f"中：{s['text_zh']}")
        lines.append("")
    return "\n".join(lines)

def _file_fingerprint(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _final_generation_fingerprint():
    _, base_url, model, enable_thinking, max_tokens = get_llm_config()
    return canonical_fingerprint({
        "generation_version": FINAL_REVIEW_GENERATION_VERSION,
        "prompt_version": FINAL_REVIEW_PROMPT_VERSION,
        "correction_tool": CORRECTION_TOOL,
        "enable_search": ENABLE_SEARCH,
        "base_url": base_url,
        "model": model,
        "thinking": enable_thinking,
        "max_tokens": max_tokens,
        "temperature": 0.25,
    })


def _final_manifest_path(output_path):
    name = artifact_name(output_path).removesuffix("_final.srt")
    return output_file(output_root(output_path), f"{name}_final_manifest.json")


def final_cache_is_current(output_path, input_path):
    manifest_path = _final_manifest_path(output_path)
    if not os.path.isfile(output_path) or not os.path.isfile(manifest_path):
        return False
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    required = {
        "schema_version", "artifact_type", "status", "generation_fingerprint",
        "input_srt_name", "input_srt_fingerprint", "output_srt_name",
        "output_srt_fingerprint",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        return False
    if (
        manifest["schema_version"] != 1
        or manifest["artifact_type"] != "final_review_manifest"
        or manifest["status"] != "complete"
        or manifest["generation_fingerprint"] != _final_generation_fingerprint()
        or manifest["input_srt_name"] != artifact_name(input_path)
        or manifest["output_srt_name"] != artifact_name(output_path)
    ):
        return False
    try:
        return (
            manifest["input_srt_fingerprint"] == _file_fingerprint(input_path)
            and manifest["output_srt_fingerprint"] == _file_fingerprint(output_path)
        )
    except OSError:
        return False


def _snapshot_review_input(input_path):
    with open(input_path, "rb") as handle:
        raw = handle.read()
    fingerprint = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig")
    snapshot_dir = os.path.dirname(os.path.abspath(input_path))
    fd, snapshot_path = tempfile.mkstemp(
        prefix=".final-input-snapshot-", suffix=".srt", dir=snapshot_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        subs, _ = parse_srt_full(snapshot_path)
    finally:
        try:
            os.unlink(snapshot_path)
        except OSError:
            pass
    if not subs:
        raise ValueError(f"终审输入未解析出有效双语字幕：{input_path}")
    return text, fingerprint, subs


def _commit_final_output(
    output_path,
    input_path,
    input_snapshot_fingerprint,
    srt_text,
    expected_subs,
):
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".final-review-", suffix=".srt", dir=output_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(srt_text)
        _validate_final_srt_text(srt_text, expected_subs)
        if _file_fingerprint(input_path) != input_snapshot_fingerprint:
            raise ValueError("终审输入在处理期间发生变化，拒绝提交旧快照结果")
        staged_output_fingerprint = _file_fingerprint(temp_path)
        manifest_path = _final_manifest_path(output_path)
        if os.path.exists(output_path):
            backup_dir = os.path.join(output_dir, "_review_backups")
            os.makedirs(backup_dir, exist_ok=True)
            backup_suffix = time.time_ns()
            backup_path = os.path.join(
                backup_dir, f"{artifact_name(output_path)}.{backup_suffix}.bak",
            )
            shutil.copy2(output_path, backup_path)
            if os.path.exists(manifest_path):
                shutil.copy2(
                    manifest_path,
                    os.path.join(
                        backup_dir,
                        f"{os.path.basename(manifest_path)}.{backup_suffix}.bak",
                    ),
                )
        manifest = {
            "schema_version": 1,
            "artifact_type": "final_review_manifest",
            "status": "complete",
            "generation_fingerprint": _final_generation_fingerprint(),
            "input_srt_name": artifact_name(input_path),
            "input_srt_fingerprint": input_snapshot_fingerprint,
            "output_srt_name": artifact_name(output_path),
            "output_srt_fingerprint": staged_output_fingerprint,
        }
        write_json_atomic(manifest_path, manifest)
        os.replace(temp_path, output_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def process_one_file(input_path, log_prefix="", force=False):
    base = artifact_name(input_path)
    if base.endswith("_human_reviewed.srt"):
        base = base[:-len("_human_reviewed.srt")]
    else:
        base = base.replace("_zh.srt", "")
    output_path = output_file(OUTPUT_DIR, f"{base}_final.srt")

    if os.path.exists(output_path) and not force:
        print(f"{log_prefix}⏭ 已存在，跳过")
        return output_path

    print(f"{log_prefix}📖 {input_path}")
    input_snapshot_text, input_snapshot_fingerprint, subs = _snapshot_review_input(input_path)
    total = len(subs)
    print(f"{log_prefix}   共 {total} 条字幕")

    client = get_llm_client()
    corpus_view = build_corpus_view(subs)

    # ====== system_prompt 由用户自行填充 ======
    system_prompt = (
        "你是一位资深日语音声作品字幕终审编辑。通读整篇字幕，从宏观和微观两个角度进行最终审查。"
        "这是最后一道防线——前面已经有逐段审校，但仍可能有遗漏的个体错误需要你来捕获。\n\n"
        "**【你的特殊能力：上下文推断】**\n"
        "你拥有前面环节没有的优势——你可以看到整篇字幕的全文。请利用这个优势进行以下推断。\n\n"
        "**题材自适应**：在开始审校前，请先通读全文，自行判断本作品属于什么题材、时代背景和舞台设定"
        "（例如：现代日常、SF实验、古风奇幻、恋爱、成人向治疗、历史等）。"
        "后续所有的误听推断都将以这个题材框架为基准。不同题材下'合理词汇'的范围完全不同——"
        "实验室场景中出现'海岸'几乎肯定是误听，但海滨约会场景中'海岸'完全正常。\n\n"
        "**Whisper误听模式示例（仅供理解，不可套用）**：\n"
        "以下是某部SF实验题材作品中真实出现的误听案例，仅用于说明Whisper的输出特征。"
        "请勿在其他题材中机械地照搬查找这些具体词汇。\n"
        "以下案例仅用于说明误听的判断方法，请勿将其中的具体词汇作为全局搜索目标。\n"
        "  - 「仏教レベルの恐怖」→ SF实验中不可能出现佛教，推测为「物狂い」或「恐慌」的误听\n"
        "  - 「美薬」→ 在全篇均使用「媚薬」的成人向作品中，可确定为同音词误选\n"
        "  - 「海岸が破裂寸前」→ 实验室中应是「膀胱」，属于场景矛盾型误听\n"
        "  - 「獅子を左右に広げた」→ 全文大量出现「クリトリス」，故「獅子」为误识别\n\n"
        "**通用误听模式分类**（请结合题材自行判断）：\n"
        "1. **同音异义词**（最常见）：读音完全相同但汉字选错。\n"
        "   判断依据：审查该词在全文其他地方出现的同音词用的是哪个汉字；孤立出现的那个大概率是误听。\n"
        "2. **场景矛盾词**：词汇本身是正常日语，但放在当前场景下完全不合理。\n"
        "   判断依据：作品的整体舞台设定是什么，这个词是否属于那个世界。\n"
        "3. **拟声词与实义词混淆**：喘息、呼吸、环境音被Whisper识别成了有意义的词汇（或相反）。\n"
        "   判断依据：前后文是在描述具体动作/状态，还是单纯的生理反应或环境音。\n"
        "4. **专有名词误识别**：角色名、地名、术语被识别成了发音相近的常见词。\n"
        "   判断依据：全文中该名称是否以正确形式出现过。\n\n"
        "**推断方法**（当你发现日语原文中某个词在整篇作品的语境下明显不合理时）：\n"
        "   a) 首先检查该词在全文其他地方是否出现过。若只出现一次且语境突兀，大概率是误听。\n"
        "   b) 结合前后文推断该位置实际在描述什么内容。\n"
        "   c) 根据日语发音规律，推测Whisper可能把什么词误听成了当前词。\n"
        "   d) 有把握时直接使用推断后的正确翻译；没有把握时在issue中说明'疑似Whisper误听，建议人工复查'，但仍给出你推断的最佳译法。\n\n"
        "**标志性异常模式的识别**：\n"
        "   - 日语原文中出现括号注释（如「※文脈によっては…」），这是AI审校的残留，应删除括号内容并报告。\n"
        "   - 日语或中文中出现LLM错误消息（如'申し訳ありません''可协助的内容仅限于'），应标记为删除。\n"
        "   - 日语原文为纯数字/纯标点/纯假名碎片（如「ワキワキ」「じゅうじゅー」），结合上下文判断是否为拟声词残留，应翻译或删除。\n\n"
        "**【常规检查】**\n"
        "【宏观检查】（跨条目模式）\n"
        "1. 角色称呼一致性：同一角色全篇称呼是否统一？\n"
        "2. 上下文连贯性：相邻字幕对话是否自然衔接？\n"
        "3. 语气风格一致性：全篇语体风格是否统一？\n"
        "4. 文化适配：日语特有表达是否恰当中文化？\n\n"
        "【微观检查】（逐条比对）\n"
        "5. 每条中文译文中是否残留未翻译的日文假名\n"
        "6. 每条译文是否与日语原文的主语、宾语、施受关系一致\n"
        "7. 译文中是否有明显的非台词文本（API错误信息、安全拒绝模板等）\n"
        "8. 全文术语是否统一（同样的日语词汇在全文中的中文译法是否一致）\n"
        "9. 是否有条目中文译文为空或明显是机器残留\n\n"
        "**【提交方式】**\n"
        "审查完成后，调用 submit_corrections 函数提交修正清单。\n"
        "只列出确实需要修改的条目。全篇完美则提交空数组 []。\n"
        "每条修正必须包含：index（序号）、issue（问题说明）、fix（修正后完整中文译文）。\n"
        "issue中请明确说明是'翻译错误'还是'疑似日语原文Whisper误听'，以便人工复查。"
    )
    # ==========================================

    if ENABLE_SEARCH:
        system_prompt += "不确定的文化概念请先 web_search 搜索确认。\n\n"

    print(f"{log_prefix}📄 全篇字幕已打包（{len(corpus_view)} 字符），等待 AI 审校...")

    result = call_deepseek(
        client, system_prompt, corpus_view,
        verbose=True,
        enable_search=ENABLE_SEARCH,
        output_tool=CORRECTION_TOOL,
        output_tool_parser=_corrections_parser,
        log_prefix=log_prefix,
        show_reasoning=False
    )

    if result is None:
        print(f"{log_prefix}❌ API 调用失败，保留二审稿")
        return None

    corrections = {}
    valid_indices = {sub["index"] for sub in subs}

    if isinstance(result, list):
        corrections = _validate_corrections(result, valid_indices)
        if len(corrections) == 0:
            print(f"{log_prefix}✅ AI 判定：全篇无需修改！")
            _commit_final_output(
                output_path, input_path, input_snapshot_fingerprint,
                input_snapshot_text, subs,
            )
            print(f"{log_prefix}   {output_path}（与二审稿相同）")
            return output_path

    elif isinstance(result, str):
        if "无需修改" in result:
            print(f"{log_prefix}✅ AI 判定：全篇无需修改！")
            _commit_final_output(
                output_path, input_path, input_snapshot_fingerprint,
                input_snapshot_text, subs,
            )
            print(f"{log_prefix}   {output_path}（与二审稿相同）")
            return output_path
        corrections = _validate_corrections(
            parse_corrections_from_text(result), valid_indices,
        )

    if not corrections:
        print(f"{log_prefix}⚠ 未能解析出修正条目")
        if isinstance(result, str):
            print(f"{log_prefix}   AI 原始输出：{result[:200]}...")
        return None

    print(f"{log_prefix}📋 AI 发现 {len(corrections)} 处需要修正：")
    for idx in sorted(corrections.keys()):
        c = corrections[idx]
        sub = next((s for s in subs if s["index"] == idx), None)
        if sub:
            ja_preview = sub['text_ja'][:40]
            old_preview = sub['text_zh'][:50]
            new_preview = c['fix'][:50]
            print(f"{log_prefix}  [{idx}] {ja_preview}...")
            print(f"{log_prefix}      旧译：{old_preview}...")
            print(f"{log_prefix}      问题：{c['issue']}")
            print(f"{log_prefix}      新译：{new_preview}...")

    sub_map = {s["index"]: s for s in subs}
    for idx, c in corrections.items():
        if idx in sub_map:
            sub_map[idx]["text_zh"] = c["fix"]

    srt_text = "".join(
        f"{s['index']}\n"
        f"{s['timecode']}\n"
        f"{s['text_ja']}\n"
        f"{s['text_zh']}\n\n"
        for s in subs
    )
    _commit_final_output(
        output_path, input_path, input_snapshot_fingerprint, srt_text, subs,
    )

    print(f"{log_prefix}✅ {output_path} — 修正 {len(corrections)} 处")
    return output_path


def select_review_inputs(zh_files, audio_dir, enable_human_review):
    if not enable_human_review:
        return list(zh_files)
    active = discover_active_audio(audio_dir)
    zh_by_base = {
        artifact_name(path).replace("_zh.srt", ""): path for path in zh_files
    }
    files = []
    for base in sorted(active):
        zh_path = zh_by_base.get(base)
        if not zh_path:
            raise FileNotFoundError(f"当前音频缺少 STEP3 产物：{base}_zh.srt")
        input_dir = output_root(zh_path)
        human_path = output_file(input_dir, f"{base}_human_reviewed.srt")
        result_path = output_file(input_dir, f"{base}_human_review.json")
        current_job = build_bundle_for_base(
            base, Path(input_dir), Path(audio_dir),
        )
        validate_completed_review_outputs(
            human_path,
            result_path,
            expected_bundle_id=current_job["bundle"]["bundle_id"],
            expected_bundle_fingerprint=current_job["bundle"]["bundle_fingerprint"],
            current_input_srt=zh_path,
        )
        files.append(human_path)
    return files

def main():
    prepare_output(INPUT_DIR)
    reset_usage()

    pattern = os.path.join(INPUT_DIR, "*", "review", "zh.srt")
    zh_files = discover_outputs(INPUT_DIR, PATTERN)
    if not zh_files:
        print(f"❌ 未找到 {pattern}")
        sys.exit(1)

    audio_dir = os.environ.get("AUDIO_DIR", "./audio")
    try:
        files = select_review_inputs(zh_files, audio_dir, ENABLE_HUMAN_REVIEW)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"❌ 人工复核产物不完整或已失效：{exc}")
        print("   为避免静默丢弃人工修改，终审已停止。请重新运行人工复核步骤。")
        sys.exit(1)

    print("=" * 60)
    print("  全篇终审 — 批量宏观一致性检查")
    print(f"  输出模式：Function Calling（submit_corrections）")
    print(f"  并发数：{MAX_WORKERS}")
    print("=" * 60)
    print(f"📋 {len(files)} 个文件")
    for f in files:
        print(f"   • {f}")
    print()

    all_files = []
    for f in files:
        base = artifact_name(f)
        if base.endswith("_human_reviewed.srt"):
            base = base[:-len("_human_reviewed.srt")]
        else:
            base = base.replace("_zh.srt", "")
        output_path = output_file(OUTPUT_DIR, f"{base}_final.srt")
        force = False
        if os.path.exists(output_path):
            if final_cache_is_current(output_path, f):
                print(f"  ⏭ {base} 的 final manifest 与当前输入匹配，跳过")
                continue
            manifest_exists = os.path.isfile(_final_manifest_path(output_path))
            if not artifact_name(f).endswith("_human_reviewed.srt") and not manifest_exists:
                print(f"  ⏭ {base} 为旧版无 manifest 缓存，人工复核关闭时保留")
                continue
            force = True
        if force:
            print(f"  ♻ {base} 的 final 来源与当前输入不匹配，将在终审成功后原子替换")
        all_files.append({
            "path": f, "base": base, "output_path": output_path, "force": force,
        })

    if not all_files:
        print("  全部文件已处理完毕。")
        print(get_usage_report())
        return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for fd in all_files:
            log_prefix = f"[{fd['base']}] "
            future = executor.submit(
                process_one_file, fd["path"], log_prefix, fd["force"],
            )
            futures[future] = fd

        failed = False
        for future in as_completed(futures):
            fd = futures[future]
            try:
                if future.result() is None:
                    failed = True
            except Exception as e:
                failed = True
                print(f"[{fd['base']}] ❌ 异常: {e}")

    if failed:
        print("\n❌ 至少一个终审任务失败；保留已有 final，并停止后续步骤。")
        sys.exit(1)

    print(f"\n🏁 全篇终审全部完成！")
    print()
    print(get_usage_report())

if __name__ == "__main__":
    main()
