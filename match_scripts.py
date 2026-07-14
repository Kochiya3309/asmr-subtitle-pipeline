# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
"""V3.2 新增：台本匹配与拆分脚本（STEP0）

工作流程：
  阶段1：同名匹配 + 模糊匹配
  阶段2：合并台本拆分（正则优先，LLM 兜底）
  阶段3：写入映射文件
  阶段4：人工验证（交互式阻塞）
"""
import os
import sys
import re
import json
import time
from common import get_deepseek_client, call_deepseek, read_text_file

# ====== 配置 ======
AUDIO_DIR  = os.environ.get("AUDIO_DIR", "./audio")
SCRIPT_DIR = os.environ.get("SCRIPT_DIR", "./scripts")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
SPLIT_DIR  = os.path.join(OUTPUT_DIR, "scripts_split")
MAPPING_FILE = os.path.join(OUTPUT_DIR, "script_mapping.json")
VERIFIED_FILE = os.path.join(OUTPUT_DIR, "script_mapping.verified")
FORCE_RESPLIT = os.environ.get("SCRIPT_FORCE_RESPLIT", "0") == "1"
FALLBACK_FULL = os.environ.get("SCRIPT_FALLBACK_FULL", "1") == "1"
AUDIO_EXTS = ['.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus']
# ==================

# ====== SPLIT_TOOL 定义（内部使用）======
SPLIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_split",
        "description": (
            "統合台本を各音声ファイルに対応する部分に分割した結果を提出してください。\n"
            "result配列の各要素は、audio_name（音声ファイル名・拡張子なし）と"
            "content（対応する台本部分）を含めてください。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "audio_name": {"type": "string"},
                            "content": {"type": "string"}
                        },
                        "required": ["audio_name", "content"]
                    }
                }
            },
            "required": ["result"]
        }
    }
}

def _split_parser(args):
    """SPLIT_TOOL 解析器：返回 [(audio_name, content), ...] 列表"""
    result = args.get("result", [])
    if not result:
        return None
    return [(item.get("audio_name", ""), item.get("content", "")) for item in result]
# =========================================

# ====== MATCH_TOOL 定义（内部使用）======
MATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_match",
        "description": (
            "音声ファイル名リストと台本ファイル名リストから、"
            "各音声に対応する台本を判定した結果を提出してください。\n"
            "各音声について以下のいずれかを判定:\n"
            "- match: 対応する台本が1つ存在（script_fileに台本ファイル名を指定）\n"
            "- split: 1つの台本が複数音声に対応する統合台本（script_fileにその台本ファイル名を指定）\n"
            "- none: 対応する台本なし"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "audio_name": {"type": "string", "description": "音声ファイル名（拡張子なし）"},
                            "action": {"type": "string", "enum": ["match", "split", "none"]},
                            "script_file": {"type": "string", "description": "対応する台本ファイル名（拡張子含む）。action=matchまたはsplitの場合必須。"}
                        },
                        "required": ["audio_name", "action"]
                    }
                }
            },
            "required": ["result"]
        }
    }
}

def _match_parser(args):
    """MATCH_TOOL 解析器：返回 [(audio_name, action, script_file), ...] 列表"""
    result = args.get("result", [])
    if not result:
        return None
    return [
        (item.get("audio_name", ""), item.get("action", "none"), item.get("script_file", ""))
        for item in result
    ]
# =========================================


def collect_audio_files():
    """收集音频文件，返回完整路径列表"""
    files = []
    if os.path.isfile(AUDIO_DIR):
        return [AUDIO_DIR]
    if os.path.isdir(AUDIO_DIR):
        for f in sorted(os.listdir(AUDIO_DIR)):
            if any(f.lower().endswith(ext) for ext in AUDIO_EXTS):
                files.append(os.path.join(AUDIO_DIR, f))
    return files


def collect_script_files():
    """收集台本文件，返回完整路径列表"""
    if not os.path.isdir(SCRIPT_DIR):
        return []
    files = []
    for f in sorted(os.listdir(SCRIPT_DIR)):
        if f.lower().endswith('.txt'):
            files.append(os.path.join(SCRIPT_DIR, f))
    return files


def extract_keywords(audio_base):
    """从音频文件名提取关键词（去掉扩展名、track 编号、常见后缀）"""
    kw = audio_base
    kw = re.sub(r'^track\s*\d+[\s._-]*', '', kw, flags=re.IGNORECASE)
    kw = re.sub(r'^\d+[\s._-]*', '', kw)
    kw = re.sub(r'[\s._-]*(本編|おまけ|bonus|extra).*$', '', kw, flags=re.IGNORECASE)
    kw = kw.strip()
    return kw if kw else audio_base


def extract_track_id(name):
    """从文件名提取开头标识符（数字或数字+字母），返回字符串或 None。
    支持 '1A_xxx'、'1B'、'01A'、'track1A'、'1'、'01' 等形式。
    统一大写，前导零去除，使 '1a'、'01A'、'1A' 都返回 '1A'。"""
    m = re.match(r'^track\s*0*(\d+[A-Za-z]?)', name, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.match(r'^0*(\d+[A-Za-z]?)', name)
    if m:
        return m.group(1).upper()
    return None


def try_same_name_match(audio_base):
    """同名匹配：scripts/<audio_base>.txt"""
    candidate = os.path.join(SCRIPT_DIR, audio_base + ".txt")
    if os.path.exists(candidate):
        return candidate
    return None


def try_fuzzy_match(audio_base, script_files):
    """模糊匹配：track 标识符优先，关键词兜底"""
    # 策略1：track 标识符匹配（音频文件名开头 vs 台本文件名开头）
    audio_id = extract_track_id(audio_base)
    if audio_id is not None:
        for script_path in script_files:
            script_base = os.path.splitext(os.path.basename(script_path))[0]
            script_id = extract_track_id(script_base)
            if script_id is not None and script_id == audio_id:
                return script_path

    # 策略2：关键词匹配（在台本内容中搜索音频标题关键词）
    keywords = extract_keywords(audio_base)
    if keywords:
        for script_path in script_files:
            try:
                content = read_text_file(script_path)
            except Exception:
                continue
            if keywords in content:
                return script_path
    return None


def split_by_regex(merge_path, audio_names):
    """正则识别标题分隔符拆分合并台本。
    返回 {audio_name: content} 字典，失败返回 None。"""
    try:
        content = read_text_file(merge_path)
    except Exception:
        return None

    patterns = [
        r'Track\s*(\d+)',
        r'第\s*(\d+)\s*[話章]',
        r'^【([^】]+)】',
        r'^(\d+)[\.、]\s',
        r'^■\s*(.+?)$',
    ]

    split_points = []
    for pat in patterns:
        for m in re.finditer(pat, content, re.MULTILINE):
            split_points.append((m.start(), m.group(0).strip()))

    if len(split_points) < 2:
        return None

    split_points = sorted(split_points, key=lambda x: x[0])
    segments = []
    for i, (pos, title) in enumerate(split_points):
        end = split_points[i+1][0] if i+1 < len(split_points) else len(content)
        segment_text = content[pos:end].strip()
        segments.append((title, segment_text))

    # 数量一致时按顺序匹配
    if len(segments) == len(audio_names):
        result = {}
        for audio_name, (title, seg_text) in zip(audio_names, segments):
            result[audio_name] = seg_text
        return result

    # 否则按关键词匹配
    result = {}
    for audio_name in audio_names:
        kw = extract_keywords(audio_name)
        matched = False
        for title, seg_text in segments:
            if kw in title or kw in seg_text[:200]:
                result[audio_name] = seg_text
                matched = True
                break
        if not matched:
            return None

    if len(result) != len(audio_names):
        return None
    return result


def split_by_llm(merge_path, audio_names):
    """LLM 拆分合并台本。返回 {audio_name: content} 字典，失败返回 None。"""
    try:
        content = read_text_file(merge_path)
    except Exception:
        return None

    client = get_deepseek_client()
    system_prompt = (
        "あなたは音声作品の台本分割エキスパートです。"
        "統合された台本を、指定された音声ファイルリストに対応するよう分割してください。\n"
        "1. 各音声ファイルに対応する台本部分を見つけ、submit_split関数で提出してください\n"
        "2. 分割位置は明確な区切り（Track番号、章題、ファイル名の記述など）で判断してください\n"
        "3. 台本中の非台詞説明（※注記、シーン描写）は対応する音声に含めてください\n"
        "4. 対応する音声が見つからない部分は無理に割り当てず、その音声のcontentは空文字にしてください\n"
        "5. 各contentにはその音声の台詞部分のみを含め、ファイル名や区切り記号は含めないでください"
    )
    user_input = (
        f"音声ファイル名リスト（拡張子なし）:\n{json.dumps(audio_names, ensure_ascii=False)}\n\n"
        f"統合台本:\n```\n{content}\n```\n\n"
        "submit_split関数を呼び出して分割結果を提出してください。"
    )

    result = call_deepseek(
        client, system_prompt, user_input,
        verbose=True, enable_search=False,
        output_tool=SPLIT_TOOL,
        output_tool_parser=_split_parser,
        stream=False,
        log_prefix="[台本拆分] ",
        show_reasoning=False
    )

    if not result or not isinstance(result, list):
        return None

    output = {}
    for audio_name, seg_content in result:
        if audio_name in audio_names:
            output[audio_name] = seg_content.strip()
    return output if output else None


def llm_match(unmatched_audios, candidate_scripts):
    """LLM 根据文件名匹配未匹配音频与候选台本。
    返回 {audio_name: {"action": "match"/"split"/"none", "script_path": path}} 字典。"""
    if not unmatched_audios or not candidate_scripts:
        return {}

    script_names = [os.path.basename(p) for p in candidate_scripts]
    script_map = {os.path.basename(p): p for p in candidate_scripts}

    client = get_deepseek_client()
    system_prompt = (
        "あなたは音声作品の台本マッチングエキスパートです。"
        "音声ファイル名リストと台本ファイル名リストを比較し、"
        "各音声に対応する台本を判定してください。\n"
        "1. ファイル名から対応関係を推測（Track番号、数字の対応、タイトルの一致等）\n"
        "2. 1つの台本が複数音声に対応すると思われる場合、その台本を split と判定\n"
        "3. 対応する台本がない音声は none と判定\n"
        "4. script_file は台本ファイル名（拡張子含む）を正確に記入すること\n"
        "5. ファイル名の先頭の数字や英字（1A, 1B, track01 等）は重要な手がかり"
    )
    user_input = (
        f"音声ファイル名リスト（拡張子なし）:\n{json.dumps(unmatched_audios, ensure_ascii=False)}\n\n"
        f"台本ファイル名リスト:\n{json.dumps(script_names, ensure_ascii=False)}\n\n"
        "submit_match 関数を呼び出して判定結果を提出してください。"
    )

    result = call_deepseek(
        client, system_prompt, user_input,
        verbose=True, enable_search=False,
        output_tool=MATCH_TOOL,
        output_tool_parser=_match_parser,
        stream=False,
        log_prefix="[台本LLM匹配] ",
        show_reasoning=False
    )

    if not result or not isinstance(result, list):
        return {}

    output = {}
    for audio_name, action, script_file in result:
        if audio_name in unmatched_audios:
            script_path = script_map.get(script_file, "")
            output[audio_name] = {
                "action": action,
                "script_path": script_path
            }
    return output


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(SPLIT_DIR, exist_ok=True)

    audio_files = collect_audio_files()
    if not audio_files:
        print(f"❌ 在 {AUDIO_DIR} 中未找到音频文件！")
        sys.exit(1)

    script_files = collect_script_files()

    print("=" * 60)
    print("  台本匹配与拆分")
    print("=" * 60)
    print(f"📋 {len(audio_files)} 个音频")
    for af in audio_files:
        print(f"   • {os.path.basename(af)}")
    if script_files:
        print(f"📜 {len(script_files)} 个台本文件：")
        for sf in script_files:
            print(f"   • {os.path.basename(sf)}")
    else:
        print("📜 未找到台本文件")
    print()

    # 幂等检查：已验证且无变化则跳过
    if os.path.exists(VERIFIED_FILE) and not FORCE_RESPLIT:
        need_redo = False
        reason = ""
        if os.path.exists(MAPPING_FILE):
            try:
                with open(MAPPING_FILE, "r", encoding="utf-8") as f:
                    old_mapping = json.load(f)
                verified_mtime = os.path.getmtime(VERIFIED_FILE)

                # 检查1：台本文件在验证后被修改
                if not need_redo:
                    for info in old_mapping.values():
                        sp = info.get("script_path")
                        if sp and os.path.exists(sp) and os.path.getmtime(sp) > verified_mtime:
                            need_redo = True
                            reason = f"台本文件在验证后被修改：{os.path.basename(sp)}"
                            break

                # 检查2：音频文件列表变化（新增/移除）
                if not need_redo:
                    old_audio_names = set(old_mapping.keys())
                    current_audio_names = set(
                        os.path.splitext(os.path.basename(af))[0] for af in audio_files
                    )
                    if old_audio_names != current_audio_names:
                        need_redo = True
                        added = current_audio_names - old_audio_names
                        removed = old_audio_names - current_audio_names
                        parts = []
                        if added:
                            parts.append(f"新增 {sorted(added)}")
                        if removed:
                            parts.append(f"移除 {sorted(removed)}")
                        reason = "音频文件列表变化（" + "，".join(parts) + "）"

                # 检查3：音频文件在验证后被修改
                if not need_redo:
                    for af in audio_files:
                        if os.path.getmtime(af) > verified_mtime:
                            need_redo = True
                            reason = f"音频文件在验证后被修改：{os.path.basename(af)}"
                            break

                # 检查4：台本文件列表变化（新增台本未被映射）
                if not need_redo:
                    old_script_sources = {
                        info.get("script_source") for info in old_mapping.values()
                        if info.get("script_source")
                    }
                    new_scripts = set(script_files) - old_script_sources
                    if new_scripts:
                        need_redo = True
                        reason = f"检测到新台本文件：{sorted(os.path.basename(s) for s in new_scripts)}"

            except Exception as e:
                need_redo = True
                reason = f"旧映射文件解析失败：{e}"
        else:
            need_redo = True
            reason = "映射文件不存在"

        if not need_redo:
            print("✅ 台本映射已存在且已验证，跳过")
            print(f"   映射：{MAPPING_FILE}")
            print(f"   标记：{VERIFIED_FILE}")
            return
        else:
            print(f"⚠ 缓存失效，将重新匹配：{reason}")

    # 清理旧拆分文件（若 FORCE_RESPLIT）
    if FORCE_RESPLIT and os.path.isdir(SPLIT_DIR):
        for f in os.listdir(SPLIT_DIR):
            if f.endswith('.txt'):
                os.remove(os.path.join(SPLIT_DIR, f))
        print("🧹 已清理旧的拆分文件")

    # 删除旧验证标记
    if os.path.exists(VERIFIED_FILE):
        os.remove(VERIFIED_FILE)

    audio_names = [os.path.splitext(os.path.basename(af))[0] for af in audio_files]
    audio_map = dict(zip(audio_names, audio_files))

    # ========== 阶段1：本地匹配（同名 + track + 关键词）==========
    print(f"\n{'#'*40}")
    print("# 阶段1：本地台本匹配")
    print(f"{'#'*40}\n")

    mapping = {}
    unmatched = []

    for af, audio_name in zip(audio_files, audio_names):
        # 同名匹配
        path = try_same_name_match(audio_name)
        if path:
            mapping[audio_name] = {
                "audio": af,
                "script_source": path,
                "script_path": path,
                "split": False,
                "range": None
            }
            print(f"  ✅ 同名匹配：{audio_name} ↔ {os.path.basename(path)}")
            continue

        # 模糊匹配（track 标识符 + 关键词）
        path = try_fuzzy_match(audio_name, script_files)
        if path:
            mapping[audio_name] = {
                "audio": af,
                "script_source": path,
                "script_path": path,
                "split": False,
                "range": None
            }
            print(f"  ✅ 模糊匹配：{audio_name} ↔ {os.path.basename(path)}")
            continue

        unmatched.append(audio_name)
        print(f"  ❓ 未匹配：{audio_name}")

    # ========== 阶段1 补充：合并台本检测 ==========
    # 若某台本被 >=2 个音频通过模糊匹配命中，认定是整合台本，转 pending_split
    script_hit_count = {}
    for name, info in mapping.items():
        src = info.get("script_source")
        if src and not try_same_name_match(name) == src:
            script_hit_count.setdefault(src, []).append(name)
    for src, names in script_hit_count.items():
        if len(names) >= 2:
            print(f"\n  🔀 检测到合并台本：{os.path.basename(src)}（被 {len(names)} 个音频命中）")
            for name in names:
                mapping[name] = {
                    "audio": audio_map[name],
                    "script_source": src,
                    "script_path": None,
                    "split": True,
                    "range": None,
                    "pending_split": True
                }
                print(f"     → {name} 改为待拆分")

    # ========== 阶段1.5：LLM 匹配（V3.2 新增）==========
    if unmatched:
        used_scripts = {info["script_source"] for info in mapping.values() if info["script_source"]}
        candidate_scripts = [s for s in script_files if s not in used_scripts]

        if candidate_scripts:
            print(f"\n{'#'*40}")
            print("# 阶段1.5：LLM 文件名匹配")
            print(f"{'#'*40}\n")

            llm_results = llm_match(unmatched, candidate_scripts)

            for audio_name in list(unmatched):
                info = llm_results.get(audio_name)
                if not info:
                    continue
                action = info["action"]
                script_path = info["script_path"]

                if action == "match" and script_path:
                    mapping[audio_name] = {
                        "audio": audio_map[audio_name],
                        "script_source": script_path,
                        "script_path": script_path,
                        "split": False,
                        "range": None
                    }
                    unmatched.remove(audio_name)
                    print(f"  ✅ LLM 匹配：{audio_name} ↔ {os.path.basename(script_path)}")
                elif action == "split" and script_path:
                    mapping[audio_name] = {
                        "audio": audio_map[audio_name],
                        "script_source": script_path,
                        "script_path": None,
                        "split": True,
                        "range": None,
                        "pending_split": True
                    }
                    unmatched.remove(audio_name)
                    print(f"  🔀 LLM 判定需拆分：{audio_name} ← {os.path.basename(script_path)}")
                elif action == "none":
                    print(f"  ❌ LLM 判定无台本：{audio_name}")
                # 其他情况保留在 unmatched

    # ========== 阶段2：拆分（对 pending_split 的合并台本）==========
    pending_split = [name for name, info in mapping.items() if info.get("pending_split")]
    if pending_split:
        print(f"\n{'#'*40}")
        print("# 阶段2：合并台本拆分")
        print(f"{'#'*40}\n")

        # 按合并台本分组
        split_groups = {}
        for name in pending_split:
            src = mapping[name]["script_source"]
            split_groups.setdefault(src, []).append(name)

        for merge_path, group_audio_names in split_groups.items():
            print(f"\n  🔍 尝试拆分：{os.path.basename(merge_path)}（{len(group_audio_names)} 个音频）")

            segments = split_by_regex(merge_path, group_audio_names)
            if segments:
                print(f"    ✅ 正则拆分成功")
            else:
                print(f"    ⚠ 正则失败，转 LLM 拆分...")
                segments = split_by_llm(merge_path, group_audio_names)
                if segments:
                    print(f"    ✅ LLM 拆分成功")
                else:
                    print(f"    ❌ LLM 拆分失败")

            if segments:
                for audio_name, seg_content in segments.items():
                    if audio_name in group_audio_names and seg_content:
                        split_path = os.path.join(SPLIT_DIR, audio_name + ".txt")
                        with open(split_path, "w", encoding="utf-8") as f:
                            f.write(seg_content)
                        mapping[audio_name] = {
                            "audio": audio_map[audio_name],
                            "script_source": merge_path,
                            "script_path": split_path,
                            "split": True,
                            "range": None
                        }
                        print(f"    → 拆分：{audio_name} → {split_path}")
                    elif audio_name in group_audio_names:
                        # 拆分结果中该音频为空
                        mapping[audio_name] = {
                            "audio": audio_map[audio_name],
                            "script_source": merge_path,
                            "script_path": None,
                            "split": False,
                            "range": None,
                            "fallback_full": False
                        }
                        unmatched.append(audio_name)
                        print(f"    ⚠ 拆分结果中 {audio_name} 为空，标记无台本")

    # ========== 阶段3：兜底处理 ==========
    if unmatched:
        print(f"\n{'#'*40}")
        print("# 阶段3：兜底处理")
        print(f"{'#'*40}\n")
        print(f"  ⚠ 以下音频仍未匹配：")
        for name in unmatched:
            print(f"    • {name}")

        # 找可用的合并台本作为 fallback
        used_scripts = {info["script_source"] for info in mapping.values() if info["script_source"]}
        fallback_candidates = [s for s in script_files if s not in used_scripts]

        if FALLBACK_FULL and fallback_candidates:
            print(f"\n  📜 FALLBACK_FULL=True：将整本台本附加给以下音频")
            fallback_path = fallback_candidates[0]
            for name in unmatched:
                if name in mapping and mapping[name].get("script_path"):
                    continue  # 已有台本，跳过
                mapping[name] = {
                    "audio": audio_map[name],
                    "script_source": fallback_path,
                    "script_path": fallback_path,
                    "split": False,
                    "range": None,
                    "fallback_full": True
                }
                print(f"    • {name} ← {os.path.basename(fallback_path)} (整本)")
        else:
            for name in unmatched:
                if name in mapping and mapping[name].get("script_path"):
                    continue
                mapping[name] = {
                    "audio": audio_map[name],
                    "script_source": None,
                    "script_path": None,
                    "split": False,
                    "range": None,
                    "fallback_full": False
                }
                print(f"    • {name} ← 无台本")

    # ========== 阶段3：写入映射 ==========
    with open(MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"\n📝 映射写入：{MAPPING_FILE}")

    # ========== 阶段4：人工验证 ==========
    print(f"\n{'='*60}")
    print("  人工验证环节")
    print(f"{'='*60}\n")

    has_split = any(info.get("split") for info in mapping.values())

    if has_split:
        # 模式 B：拆分
        print("  以下文件已从合并台本拆分，请逐个打开检查：\n")
        for audio_name, info in mapping.items():
            if info.get("split"):
                print(f"    → {info['script_path']}")
        print(f"\n  检查要点：")
        print(f"    1. 每个文件的台词是否属于对应音频")
        print(f"    2. 是否有台词被错误地分到其他音频")
        print(f"    3. 非台词说明（※注记）是否合理保留")
    else:
        # 模式 A：匹配信息
        print("  匹配结果：\n")
        for audio_name, info in mapping.items():
            print(f"  ┌─ 音频：{audio_name}")
            print(f"  │  台本来源：{info.get('script_source') or '（无台本）'}")
            sp = info.get("script_path")
            if sp and os.path.exists(sp):
                try:
                    content = read_text_file(sp)
                    lines = content.splitlines()[:3]
                    print(f"  │  预览：")
                    for line in lines:
                        if line.strip():
                            print(f"  │    {line.rstrip()}")
                except Exception:
                    pass
            if info.get("fallback_full"):
                print(f"  │  ⚠ 注意：此音频使用整本合并台本（fallback）")
            print(f"  └─")

    print(f"\n  确认无误输入 y 继续，有问题输入 n 退出：", end="", flush=True)
    try:
        feedback = input().strip().lower()
    except EOFError:
        feedback = ""

    if feedback == 'y':
        with open(VERIFIED_FILE, "w", encoding="utf-8") as f:
            f.write(f"verified at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        print("\n  ✅ 验证通过，可继续 ensemble 阶段")
    else:
        print("\n  ❌ 验证未通过，未创建验证标记")
        print(f"     请检查 {SCRIPT_DIR} 目录后重新运行 match_scripts.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
