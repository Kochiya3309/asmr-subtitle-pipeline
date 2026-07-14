# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
import os
import sys
import json
import subprocess

# 设置日志文件路径（必须在 import common 之前）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("LOG_FILE", os.path.join(_SCRIPT_DIR, "output", "pipeline.log"))

import common  # 触发日志初始化

# ============================================================
#  配置区
# ============================================================

DEEPSEEK_API_KEY = "your-deepseek-api-key-here"
ZHIPU_API_KEY = "your-zhipu-api-key-here"

AUDIO_DIR = "./audio"
OUTPUT_DIR = "./output"

ENABLE_SEARCH = False

MAX_WORKERS = 10              # DeepSeek API 并发数

# 台本功能（V3.2 新增）
ENABLE_SCRIPT = False             # 总开关，False 时整条台本流程跳过
STEP0_MATCH_SCRIPTS = False       # STEP0 开关
SCRIPT_DIR = "./scripts"          # 台本目录
SCRIPT_FALLBACK_FULL = True       # 合并台本无法拆分时整本发送
SCRIPT_FORCE_RESPLIT = False      # 强制重新拆分（忽略缓存）

# 流水线步骤开关
STEP0_MATCH_SCRIPTS = STEP0_MATCH_SCRIPTS and ENABLE_SCRIPT
STEP1_ENSEMBLE = True
STEP2_REVIEW_JP = True
STEP3_TRANSLATE = True
STEP4_FINAL = True
STEP5_VALIDATE = True
STEP6_STRIP = True          # 去除日文仅留中文

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
    env["DEEPSEEK_API_KEY"] = DEEPSEEK_API_KEY
    env["ZHIPU_API_KEY"] = ZHIPU_API_KEY
    env["AUDIO_DIR"] = AUDIO_DIR
    env["OUTPUT_DIR"] = OUTPUT_DIR
    env["ENABLE_SEARCH"] = "1" if ENABLE_SEARCH else "0"
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
    return env


def run_step(step_num, total_steps, step_name, script_name, env):
    print()
    print("=" * 60)
    print(f"  [{step_num}/{total_steps}] {step_name}")
    print("=" * 60)
    print()

    result = subprocess.run(
        [sys.executable, script_name],
        env=env,
        cwd=os.path.dirname(os.path.abspath(__file__))
    )
    if result.returncode != 0:
        print()
        print(f"*** 错误：{step_name} 失败！ ***")
        sys.exit(1)

    print()
    print(f"[ OK ] {step_name} 完成")
    print()


def main():
    print()
    print("=" * 60)
    print("    ASMR 字幕全自动流水线 v3.0")
    print("    模型：DeepSeek-V4-Pro + Whisper Ensemble")
    print("=" * 60)
    print()
    print(f"    音频目录：{AUDIO_DIR}")
    print(f"    输出目录：{OUTPUT_DIR}")
    print(f"    自主搜索：{'开启' if ENABLE_SEARCH else '关闭'}")

    env = get_env()

    all_steps = [
        (STEP0_MATCH_SCRIPTS, "台本匹配与拆分",              "match_scripts.py"),
        (STEP1_ENSEMBLE, "Whisper 双模型融合转写",        "ensemble_transcribe.py"),
        (STEP2_REVIEW_JP, "日语字幕二审",                  "review_japanese.py"),
        (STEP3_TRANSLATE,  "翻译 + 逐段审校",              "translate.py"),
        (STEP4_FINAL,      "全篇终审（宏观+微观一致性检查）", "review_final.py"),
        (STEP5_VALIDATE,   "自动化验证（规则扫描残留问题）",  "validate_final.py"),
        (STEP6_STRIP,      "去除日文（仅保留中文）",        "strip_japanese.py"),
    ]

    # 分两阶段：先跑 STEP0+STEP1，再根据 mismatch 决定是否跑 STEP2，最后跑剩余
    pre_steps = [(name, script) for flag, name, script in all_steps[:2] if flag]
    mid_step = all_steps[2]  # STEP2
    post_steps = [(name, script) for flag, name, script in all_steps[3:] if flag]

    # 决定 STEP2 是否运行（台本模式下基于 mismatch 文件）
    run_step2 = False
    if STEP2_REVIEW_JP:
        if ENABLE_SCRIPT:
            mismatch_file = os.path.join(OUTPUT_DIR, "script_mismatch.json")
            if os.path.exists(mismatch_file):
                try:
                    with open(mismatch_file, "r", encoding="utf-8") as f:
                        mismatch_list = json.load(f)
                    run_step2 = bool(mismatch_list)
                except Exception:
                    run_step2 = True
            if run_step2:
                print(f"    检测到台本 mismatch，将执行 STEP2（仅审校 mismatch 文件）")
            else:
                print(f"    台本模式无 mismatch，跳过 STEP2（日语二审）")
        else:
            run_step2 = True

    total = len(pre_steps) + (1 if run_step2 else 0) + len(post_steps)

    executed = 0
    for name, script in pre_steps:
        executed += 1
        run_step(executed, total, name, script, env)

    if run_step2:
        executed += 1
        run_step(executed, total, mid_step[1], mid_step[2], env)

    for name, script in post_steps:
        executed += 1
        run_step(executed, total, name, script, env)

    print()
    print("=" * 60)
    print("                    全部完成！")
    print("=" * 60)
    print()
    print(f"    输出文件在 {OUTPUT_DIR} 目录下：")
    print(f"      *_ensemble.srt   -- Whisper 双模型融合字幕（日语）")
    print(f"      *_reviewed.srt   -- 日语二审后字幕")
    print(f"      *_zh.srt         -- translate + 审校双语字幕")
    print(f"      *_final.srt      -- 全篇终审终稿（用这个观看）")
    print(f"      *_cn_only.srt    -- 纯中文字幕（需开启 STEP6）")
    print()
    print("    用播放器加载 *_final.srt 即可观看")
    print()
    print(f"    音频目录：{AUDIO_DIR}")
    print(f"    输出目录：{OUTPUT_DIR}")
    print(f"    自主搜索：{'开启' if ENABLE_SEARCH else '关闭'}")
    print(f"    日志文件：{os.environ.get('LOG_FILE', '无')}")
    if sys.stdin.isatty():
        input("按回车键退出...")
    else:
        print("非交互式终端，自动退出。")


if __name__ == "__main__":
    main()
