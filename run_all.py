# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
import os
import sys
import json
import subprocess
from output_layout import output_file, prepare_output
from pipeline_inputs import discover_active_audio
from video_inputs import prepare_video_inputs


PIPELINE_PAUSE_EXIT_CODE = 75
# run_step 返回值：阶段失败但调用方允许继续（当前用于 STEP5 不阻断 STEP6）。
RUN_STEP_FAILED_ALLOWED = "failed_continue"


def _bool_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _positive_int_env(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _port_env(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if 0 <= value <= 65535 else default

# 设置日志文件路径（必须在 import common 之前）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 加载 .env 文件（不依赖 python-dotenv，避免引入新依赖）
_env_file = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_env_file):
    with open(_env_file, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Relocate before common opens the log (Windows cannot move an open log).
_output_root = os.path.abspath(os.environ.get("OUTPUT_DIR", os.path.join(_SCRIPT_DIR, "output")))
if __name__ == "__main__":
    prepare_output(_output_root)
_legacy_log = os.path.join(_output_root, "pipeline.log")
if not os.environ.get("LOG_FILE") or os.path.abspath(os.environ["LOG_FILE"]) == _legacy_log:
    os.environ["LOG_FILE"] = output_file(_output_root, "pipeline.log")

print("正在加载依赖库，请稍候（首次启动可能稍慢）...", flush=True)
import common  # 触发日志初始化

# ============================================================
#  配置区
# ============================================================

# V3.6 起 LLM 改为 OpenAI 兼容配置（OPENAI_* 前缀），旧 DEEPSEEK_API_KEY 自动回退
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
OPENAI_ENABLE_THINKING = os.environ.get("OPENAI_ENABLE_THINKING", "0")
OPENAI_MAX_TOKENS = os.environ.get("OPENAI_MAX_TOKENS", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
EXA_API_KEY = os.environ.get("EXA_API_KEY", "")

# 修复：改为绝对路径。相对路径依赖 cwd，用户从其他目录直接运行脚本时
# 会静默写错位置（subprocess 虽设置了 cwd，但直接运行单个脚本时无保护）。
AUDIO_DIR = os.path.abspath(os.environ.get("AUDIO_DIR", os.path.join(_SCRIPT_DIR, "audio")))
VIDEO_DIR = os.path.abspath(os.environ.get("VIDEO_DIR", os.path.join(_SCRIPT_DIR, "video")))
OUTPUT_DIR = os.path.abspath(os.environ.get("OUTPUT_DIR", os.path.join(_SCRIPT_DIR, "output")))

ENABLE_SEARCH = _bool_env("ENABLE_SEARCH", False)
ENABLE_VIDEO_PREP = _bool_env("ENABLE_VIDEO_PREP", False)

MAX_WORKERS = 10              # LLM API 并发数

# 台本功能（V3.2 新增）
ENABLE_SCRIPT = _bool_env("ENABLE_SCRIPT", False)
STEP0_MATCH_SCRIPTS = _bool_env("STEP0_MATCH_SCRIPTS", False)
SCRIPT_DIR = os.path.abspath(
    os.environ.get("SCRIPT_DIR", os.path.join(_SCRIPT_DIR, "scripts"))
)
SCRIPT_FALLBACK_FULL = _bool_env("SCRIPT_FALLBACK_FULL", True)
SCRIPT_FORCE_RESPLIT = _bool_env("SCRIPT_FORCE_RESPLIT", False)
SCRIPT_AUTO_VERIFY = _bool_env("SCRIPT_AUTO_VERIFY", False)
ENABLE_SCRIPT_REVIEW_FILTER = _bool_env("ENABLE_SCRIPT_REVIEW_FILTER", False)
ENABLE_SCRIPT_UNITS_SHADOW = _bool_env("ENABLE_SCRIPT_UNITS_SHADOW", False)
SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS = _positive_int_env(
    "SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS", 120,
)

# Whisper 转写优化（V3.4 新增）
ENABLE_AUDIO_PREPROCESS = _bool_env("ENABLE_AUDIO_PREPROCESS", True)
ENABLE_ASR_EVIDENCE = _bool_env("ENABLE_ASR_EVIDENCE", True)
ENABLE_HALLUCINATION_FILTER = _bool_env("ENABLE_HALLUCINATION_FILTER", True)
ENABLE_ASR_RESCUE = _bool_env("ENABLE_ASR_RESCUE", True)
ENABLE_DETERMINISTIC_TIMELINE = _bool_env("ENABLE_DETERMINISTIC_TIMELINE", False)
ENABLE_HUMAN_REVIEW = _bool_env("ENABLE_HUMAN_REVIEW", False)
HUMAN_REVIEW_PORT = _port_env("HUMAN_REVIEW_PORT", 8765)
RESCUE_WINDOW_SECONDS = 12.0      # large-v3 无 VAD 扫描窗口
RESCUE_OVERLAP_SECONDS = 2.0      # 相邻救援窗口重叠
INITIAL_PROMPT = "ASMR作品、囁き、耳かき、癒し系、日本語、優しい声"
HOTWORDS = "触手,媚薬,絶頂,雌,自縛,おまんこ,クリトリス,チンチン,囁き,耳舐め,サキュバス,乳首,睾丸,愛液,潮吹き"
VAD_THRESHOLD = 0.2               # VAD 语音检测阈值（越低越敏感）
VAD_MIN_SPEECH_MS = 50            # 最短语音段（ms）
VAD_SPEECH_PAD_MS = 800           # 语音前后缓冲（ms）
VAD_MIN_SILENCE_MS = 500          # 触发分割的最短静音（ms）

# 流水线步骤开关
STEP0_MATCH_SCRIPTS = STEP0_MATCH_SCRIPTS and ENABLE_SCRIPT
STEP1_ENSEMBLE = _bool_env("STEP1_ENSEMBLE", True)
STEP2_REVIEW_JP = _bool_env("STEP2_REVIEW_JP", True)
STEP3_TRANSLATE = _bool_env("STEP3_TRANSLATE", True)
STEP325_SCRIPT_REVIEW_ALIGNMENT = (
    ENABLE_SCRIPT and ENABLE_HUMAN_REVIEW and ENABLE_SCRIPT_REVIEW_FILTER
)
STEP35_HUMAN_REVIEW = (
    ENABLE_HUMAN_REVIEW and _bool_env("STEP35_HUMAN_REVIEW", True)
)
STEP4_FINAL = _bool_env("STEP4_FINAL", True)
STEP5_VALIDATE = _bool_env("STEP5_VALIDATE", True)
STEP6_STRIP = _bool_env("STEP6_STRIP", True)
# 导出副本步骤：STEP7 依赖 STEP4 终稿，STEP8 依赖 STEP6 纯中文字幕。
STEP7_EXPORT_FINAL = STEP4_FINAL and _bool_env("STEP7_EXPORT_FINAL", True)
STEP8_EXPORT_CN_ONLY = STEP6_STRIP and _bool_env("STEP8_EXPORT_CN_ONLY", True)

# 日语二审参数
REVIEW_JP_BATCH_SIZE = 30          # 逐批审校每批条数
REVIEW_JP_OVERLAP = 5              # 相邻批次重叠条数
REVIEW_JP_FULL_REVIEW = True       # 是否进行全篇上下文审校
REVIEW_JP_FULL_REVIEW_BATCH = 200  # 全篇审校每批最大条数

# 翻译参数
TRANSLATE_BATCH_SIZE = 10          # 翻译阶段每批条数
TRANSLATE_REVIEW_BATCH_SIZE = 20   # 翻译审校阶段每批条数

# ============================================================


def get_env():
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = OPENAI_API_KEY
    # 可选配置：非空才注入（空值注入会让子进程 env 键存在但为空，
    # 依赖 common 的 ValueError 兜底，绕弯且易混）
    if OPENAI_BASE_URL:
        env["OPENAI_BASE_URL"] = OPENAI_BASE_URL
    if OPENAI_MODEL:
        env["OPENAI_MODEL"] = OPENAI_MODEL
    if OPENAI_ENABLE_THINKING:
        env["OPENAI_ENABLE_THINKING"] = OPENAI_ENABLE_THINKING
    if OPENAI_MAX_TOKENS:
        env["OPENAI_MAX_TOKENS"] = OPENAI_MAX_TOKENS
    env["TAVILY_API_KEY"] = TAVILY_API_KEY
    env["EXA_API_KEY"] = EXA_API_KEY
    env["AUDIO_DIR"] = AUDIO_DIR
    env["VIDEO_DIR"] = VIDEO_DIR
    env["OUTPUT_DIR"] = OUTPUT_DIR
    env["ENABLE_SEARCH"] = "1" if ENABLE_SEARCH else "0"
    env["ENABLE_VIDEO_PREP"] = "1" if ENABLE_VIDEO_PREP else "0"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["LOG_FILE"] = os.environ.get("LOG_FILE", "")
    env["REVIEW_JP_BATCH_SIZE"] = str(REVIEW_JP_BATCH_SIZE)
    env["REVIEW_JP_OVERLAP"] = str(REVIEW_JP_OVERLAP)
    env["REVIEW_JP_FULL_REVIEW"] = "1" if REVIEW_JP_FULL_REVIEW else "0"
    env["REVIEW_JP_FULL_REVIEW_BATCH"] = str(REVIEW_JP_FULL_REVIEW_BATCH)
    env["TRANSLATE_BATCH_SIZE"] = str(TRANSLATE_BATCH_SIZE)
    env["TRANSLATE_REVIEW_BATCH_SIZE"] = str(TRANSLATE_REVIEW_BATCH_SIZE)
    env["MAX_WORKERS"] = str(MAX_WORKERS)
    env["ENABLE_SCRIPT"] = "1" if ENABLE_SCRIPT else "0"
    env["SCRIPT_DIR"] = SCRIPT_DIR
    env["SCRIPT_FALLBACK_FULL"] = "1" if SCRIPT_FALLBACK_FULL else "0"
    env["SCRIPT_FORCE_RESPLIT"] = "1" if SCRIPT_FORCE_RESPLIT else "0"
    env["SCRIPT_AUTO_VERIFY"] = "1" if SCRIPT_AUTO_VERIFY else "0"
    env["ENABLE_SCRIPT_REVIEW_FILTER"] = "1" if ENABLE_SCRIPT_REVIEW_FILTER else "0"
    env["ENABLE_SCRIPT_UNITS_SHADOW"] = "1" if ENABLE_SCRIPT_UNITS_SHADOW else "0"
    env["SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS"] = str(SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS)
    env["ENABLE_AUDIO_PREPROCESS"] = "1" if ENABLE_AUDIO_PREPROCESS else "0"
    env["ENABLE_ASR_EVIDENCE"] = "1" if ENABLE_ASR_EVIDENCE else "0"
    env["ENABLE_HALLUCINATION_FILTER"] = "1" if ENABLE_HALLUCINATION_FILTER else "0"
    env["ENABLE_ASR_RESCUE"] = "1" if ENABLE_ASR_RESCUE else "0"
    env["ENABLE_DETERMINISTIC_TIMELINE"] = "1" if ENABLE_DETERMINISTIC_TIMELINE else "0"
    env["ENABLE_HUMAN_REVIEW"] = "1" if ENABLE_HUMAN_REVIEW else "0"
    env["HUMAN_REVIEW_PORT"] = str(HUMAN_REVIEW_PORT)
    env["RESCUE_WINDOW_SECONDS"] = str(RESCUE_WINDOW_SECONDS)
    env["RESCUE_OVERLAP_SECONDS"] = str(RESCUE_OVERLAP_SECONDS)
    env["INITIAL_PROMPT"] = INITIAL_PROMPT
    env["HOTWORDS"] = HOTWORDS
    env["VAD_THRESHOLD"] = str(VAD_THRESHOLD)
    env["VAD_MIN_SPEECH_MS"] = str(VAD_MIN_SPEECH_MS)
    env["VAD_SPEECH_PAD_MS"] = str(VAD_SPEECH_PAD_MS)
    env["VAD_MIN_SILENCE_MS"] = str(VAD_MIN_SILENCE_MS)
    return env


def run_step(step_num, total_steps, step_name, script_name, env, *, timeout=None,
             allow_failure=False):
    print()
    print("=" * 60)
    print(f"  [{step_num}/{total_steps}] {step_name}")
    print("=" * 60)
    print()

    try:
        result = subprocess.run(
            [sys.executable, script_name],
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print()
        print(f"*** 错误：{step_name} 超过 {timeout} 秒，已停止 ***")
        sys.exit(1)
    if result.returncode == PIPELINE_PAUSE_EXIT_CODE:
        print()
        print(f"[ PAUSED ] {step_name} 已暂停；再次运行将从缓存恢复")
        print()
        return False
    if result.returncode != 0:
        if allow_failure:
            print()
            print(f"*** 警告：{step_name} 未通过（退出码 {result.returncode}），"
                  "按配置继续后续步骤 ***")
            print()
            return RUN_STEP_FAILED_ALLOWED
        print()
        print(f"*** 错误：{step_name} 失败！ ***")
        sys.exit(1)

    print()
    print(f"[ OK ] {step_name} 完成")
    print()
    return True


def run_optional_script_units_shadow(env):
    """Run script classification only after every production subtitle step."""
    if (
        not ENABLE_SCRIPT
        or not ENABLE_SCRIPT_UNITS_SHADOW
        or STEP325_SCRIPT_REVIEW_ALIGNMENT
    ):
        return
    print()
    print("=" * 60)
    print("  [旁路] 台本结构影子分析（不参与本轮字幕）")
    print("=" * 60)
    try:
        result = subprocess.run(
            [sys.executable, "script_units_shadow_stage.py"],
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            print(f"    警告：台本结构影子分析退出码 {result.returncode}；正式字幕不受影响")
    except subprocess.TimeoutExpired:
        print(
            f"    警告：台本结构影子分析超过 {SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS}s，"
            "已终止；正式字幕不受影响"
        )
    except (OSError, ValueError) as exc:
        print(f"    警告：无法启动台本结构影子分析（{exc}）；正式字幕不受影响")


def load_script_mismatches():
    """读取本轮 STEP1 产出的 mismatch 列表；缺失或损坏时失败关闭。"""
    mismatch_file = output_file(OUTPUT_DIR, "script_mismatch.json")
    if not os.path.exists(mismatch_file):
        print("    警告：本轮未生成 script_mismatch.json，将执行 STEP2")
        return None
    try:
        with open(mismatch_file, "r", encoding="utf-8-sig") as f:
            value = json.load(f)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("mismatch 列表格式无效")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as e:
        print(f"    警告：无法验证本轮 mismatch 状态（{e}），将执行 STEP2")
        return None


def main():
    print()
    print("=" * 60)
    print("    ASMR 字幕全自动流水线 v4.0")
    print(f"    模型：{OPENAI_MODEL or 'deepseek-v4-pro'} + Whisper Ensemble")
    print("=" * 60)
    print()
    print(f"    音频目录：{AUDIO_DIR}")
    if ENABLE_VIDEO_PREP:
        print(f"    视频目录：{VIDEO_DIR}")
    print(f"    输出目录：{OUTPUT_DIR}")
    print(f"    自主搜索：{'开启' if ENABLE_SEARCH else '关闭'}")

    # A parent shell may have retained values from an earlier invocation.  This
    # run owns the active-source snapshot and must rebuild it from disk.
    os.environ.pop("ACTIVE_AUDIO_PATHS", None)
    os.environ.pop("ACTIVE_AUDIO_BASES", None)
    try:
        active_audio = discover_active_audio(AUDIO_DIR)
    except FileNotFoundError:
        # Video-only runs are valid when the optional preparation stage is on.
        if not ENABLE_VIDEO_PREP:
            raise
        active_audio = {}
    if ENABLE_VIDEO_PREP:
        print("\n  [输入准备] 检查视频并提取第一音轨（如有）")
        video_audio = prepare_video_inputs(VIDEO_DIR, OUTPUT_DIR)
        overlap = sorted(set(active_audio) & set(video_audio))
        if overlap:
            raise ValueError(
                "video/audio task name conflict; rename one source: " + ", ".join(overlap)
            )
        active_audio.update(video_audio)
    if not active_audio:
        raise FileNotFoundError("no supported audio or video input was found")
    env = get_env()
    env["ACTIVE_AUDIO_BASES"] = json.dumps(sorted(active_audio), ensure_ascii=False)
    env["ACTIVE_AUDIO_PATHS"] = json.dumps(
        {base: str(path) for base, path in sorted(active_audio.items())}, ensure_ascii=False,
    )

    all_steps = [
        (STEP0_MATCH_SCRIPTS, "台本匹配与拆分",              "match_scripts.py"),
        (STEP1_ENSEMBLE, "Whisper 双模型融合转写",        "ensemble_transcribe.py"),
        (STEP2_REVIEW_JP, "日语字幕二审",                  "review_japanese.py"),
        (STEP3_TRANSLATE,  "翻译 + 逐段审校",              "translate.py"),
        (
            STEP325_SCRIPT_REVIEW_ALIGNMENT,
            "台本结构分类与审核对齐",
            "script_units_shadow_stage.py",
        ),
        (STEP35_HUMAN_REVIEW, "人工复核（本地浏览器）",      "human_review_gate.py"),
        (STEP4_FINAL,      "全篇终审（宏观+微观一致性检查）", "review_final.py"),
        (STEP5_VALIDATE,   "自动化验证（规则扫描残留问题）",  "validate_final.py"),
        (STEP6_STRIP,      "去除日文（仅保留中文）",        "strip_japanese.py"),
        (STEP7_EXPORT_FINAL, "导出双语终稿副本（_transfer/final）", "export_final_srt.py"),
        (STEP8_EXPORT_CN_ONLY, "导出纯中文字幕副本（_transfer/cn_only）", "export_cn_only_srt.py"),
    ]

    # 分两阶段：必须先跑 STEP0+STEP1，再读取本轮 mismatch 决定是否跑 STEP2。
    pre_steps = [(name, script) for flag, name, script in all_steps[:2] if flag]
    mid_step = all_steps[2]  # STEP2
    post_steps = [(name, script) for flag, name, script in all_steps[3:] if flag]

    # 台本模式的 STEP2 要等 STEP1 写出本轮 mismatch 才能确定是否跳过。
    # 前置阶段先显示诚实的总步数范围，避免输出没有信息量的 "?"。
    total_without_step2 = len(pre_steps) + len(post_steps)
    step2_is_conditional = STEP2_REVIEW_JP and ENABLE_SCRIPT
    total_with_step2 = total_without_step2 + (1 if STEP2_REVIEW_JP else 0)
    pre_step_total = (
        f"{total_without_step2}–{total_with_step2}"
        if step2_is_conditional else str(total_with_step2)
    )

    executed = 0
    for name, script in pre_steps:
        executed += 1
        if run_step(executed, pre_step_total, name, script, env) is False:
            return

    # STEP1 已完成，现在读取它刚写出的 mismatch 文件。
    run_step2 = False
    if STEP2_REVIEW_JP:
        if ENABLE_SCRIPT:
            mismatch_list = load_script_mismatches()
            run_step2 = mismatch_list is None or bool(mismatch_list)
            if run_step2:
                print(f"    检测到台本 mismatch，将执行 STEP2（仅审校 mismatch 文件）")
            else:
                print(f"    台本模式无 mismatch，跳过 STEP2（日语二审）")
        else:
            run_step2 = True

    total = len(pre_steps) + (1 if run_step2 else 0) + len(post_steps)

    if run_step2:
        executed += 1
        if run_step(executed, total, mid_step[1], mid_step[2], env) is False:
            return

    step5_failed = False
    for name, script in post_steps:
        executed += 1
        kwargs = {}
        if script == "script_units_shadow_stage.py":
            kwargs["timeout"] = SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS
        # STEP6_STRIP 开启时，STEP5 验证失败不再阻断纯中文导出：
        # run_step 返回 RUN_STEP_FAILED_ALLOWED，流水线继续并在最后以失败退出。
        allow_failure = script == "validate_final.py" and STEP6_STRIP
        outcome = run_step(executed, total, name, script, env,
                           allow_failure=allow_failure, **kwargs)
        if outcome is False:
            return
        if outcome == RUN_STEP_FAILED_ALLOWED:
            step5_failed = True

    # It deliberately runs last so its API usage cannot starve production stages.
    run_optional_script_units_shadow(env)

    if step5_failed:
        print()
        print("=" * 60)
        print("  ⚠ STEP5 自动化验证未通过；已按配置继续生成纯中文字幕")
        print("    请人工复核 final 字幕确认无误后再使用。")
        print("=" * 60)

    print()
    print("=" * 60)
    print("                    全部完成！")
    print("=" * 60)
    print()
    print(f"    输出按音频分类保存在 {OUTPUT_DIR}：")
    print("      <音频名>/asr/      -- 预处理音频与双模型转写")
    print("      <音频名>/evidence/ -- ASR 证据、隔离决策与救援记录")
    print("      <音频名>/review/   -- 融合、日语二审、翻译及审核检查点")
    if ENABLE_HUMAN_REVIEW:
        print("      <音频名>/review/human_reviewed.srt -- 人工复核后的双语字幕")
    print("      <音频名>/final/<音频名>_final.srt   -- 全篇终审终稿（用这个观看）")
    print("      <音频名>/final/<音频名>_cn_only.srt -- 纯中文字幕（需开启 STEP6）")
    if STEP7_EXPORT_FINAL:
        print("    _transfer/final/   -- 双语终稿音频同名副本（批量转移用）")
    if STEP8_EXPORT_CN_ONLY:
        print("    _transfer/cn_only/ -- 纯中文字幕音频同名副本（批量转移用）")
    if ENABLE_SCRIPT and (
        ENABLE_SCRIPT_UNITS_SHADOW or ENABLE_SCRIPT_REVIEW_FILTER
    ):
        print("      <音频名>/script/ -- 台本结构分类与时间窗对齐")
    print()
    print("    用播放器加载 <音频名>/final/<音频名>_final.srt 即可观看")
    print()
    print(f"    音频目录：{AUDIO_DIR}")
    print(f"    输出目录：{OUTPUT_DIR}")
    print(f"    自主搜索：{'开启' if ENABLE_SEARCH else '关闭'}")
    print(f"    日志文件：{os.environ.get('LOG_FILE', '无')}")
    if sys.stdin.isatty():
        input("按回车键退出...")
    else:
        print("非交互式终端，自动退出。")
    if step5_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
