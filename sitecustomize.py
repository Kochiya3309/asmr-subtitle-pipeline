# -*- coding: utf-8 -*-
"""幽灵 Ctrl+C 防御（2026-08-14 添加）。

问题背景：用户机器上有未知程序/驱动会向控制台进程组注入 Ctrl+C 信号
（用户未按键盘，且在多个项目出现过），导致 Python 在启动 import 阶段
抛出 KeyboardInterrupt 崩溃。

机制：Python 启动时（site 模块初始化阶段）自动加载本文件——早于任何
第三方库 import。将 SIGINT 处理替换为防抖逻辑：第 1 次收到 Ctrl+C 仅
打印提示并忽略；第 2 次才真正抛出 KeyboardInterrupt（保留人工强制
中断能力）。通过 PYTHONPATH 环境变量生效（见 start.bat 的 set 行）。
"""
import signal
import sys

_sigint_received = False


def _sigint_guard(signum, frame):
    global _sigint_received
    if _sigint_received:
        # 第 2 次 Ctrl+C：恢复默认行为，允许人工强制中断
        signal.signal(signal.SIGINT, signal.default_int_handler)
        raise KeyboardInterrupt
    _sigint_received = True
    try:
        print(
            "\n⚠ 检测到 Ctrl+C 信号（第 1 次，已忽略）。"
            "流水线将继续运行；如需强制中断，请再次按 Ctrl+C。",
            flush=True,
        )
    except Exception:
        pass


try:
    signal.signal(signal.SIGINT, _sigint_guard)
except (ValueError, OSError):
    # 非主线程或无信号支持的环境：跳过防御
    pass
