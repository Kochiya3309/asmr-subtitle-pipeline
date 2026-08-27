# ASMR 字幕自动生成流水线

[English](README.md) | 简体中文

将日语 ASMR 音频转换为中日双语 SRT 字幕。流水线融合两个 Whisper 转写结果，通过兼容 OpenAI 的 LLM 审校日文、翻译为简体中文、进行终审与规则验证，并可生成纯中文 SRT。

**V3.6.1** 支持 OpenAI 兼容 LLM API（默认仍为 DeepSeek）、可选 Tavily/Exa 搜索、无 CUDA 时的 CPU 回退，以及已修复的 `start_*.bat` 启动脚本。

## 流水线

| 步骤 | 脚本 | 用途 |
| --- | --- | --- |
| STEP0 | `match_scripts.py` | 可选的台本匹配与拆分 |
| STEP1 | `ensemble_transcribe.py` | Whisper large-v3 + large-v3-turbo 转写与融合 |
| STEP2 | `review_japanese.py` | 日语字幕二审 |
| STEP3 | `translate.py` | 翻译与逐段审校 |
| STEP4 | `review_final.py` | 全文终审 |
| STEP5 | `validate_final.py` | 规则验证 |
| STEP6 | `strip_japanese.py` | 可选纯中文 SRT |

台本模式和联网搜索均默认关闭。对于 NSFW 内容，除非明确需要，否则请保持搜索关闭。

## 系统要求

| 项目 | 最低要求 | 推荐 |
| --- | --- | --- |
| 系统 | Windows 10/11；macOS/Linux 需调整路径 | Windows 11 |
| GPU | 8 GB 显存的 NVIDIA GPU | RTX 3060 或更高 |
| 内存 | 16 GB | 32 GB |
| 硬盘 | 10 GB 空闲空间（模型） | SSD |
| Python | 3.10 或 3.11 | 3.10 |
| 其他 | FFmpeg 已加入 `PATH`，且可访问所选 LLM API | 稳定宽带 |

无 CUDA 时自动选择 CPU，通常比 GPU 慢 5–10 倍。首次运行会下载约 4.5 GB Whisper 模型。

## 快速开始

1. 创建并激活虚拟环境：

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

2. 将 `.env.example` 复制为 `.env`，填写 `OPENAI_API_KEY`。
3. 将 `.mp3`、`.m4a`、`.wav`、`.flac`、`.ogg` 或 `.opus` 音频放入 `audio/`。
4. 双击 `start.bat`，或执行：

```powershell
venv\Scripts\python.exe run_all.py
```

5. 在播放器中加载 `output/` 内的 `*_final.srt`。如需 `*_cn_only.srt`，请开启 STEP6。

`start.bat` 会设置 `HF_ENDPOINT=https://hf-mirror.com`，有助于中国大陆网络环境下载模型。

## 配置

运行开关位于 `run_all.py` 顶部。`.env` 存放密钥且被 Git 忽略；系统环境变量优先于 `.env`。

### LLM API

使用 DeepSeek 时通常只需 `OPENAI_API_KEY`。使用其他 OpenAI 兼容服务时，填写对应地址和模型名：

```dotenv
OPENAI_API_KEY=sk-你的LLM密钥
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o
OPENAI_ENABLE_THINKING=0
```

`OPENAI_ENABLE_THINKING=1` 仅适用于支持 DeepSeek 非标准 `thinking` 参数的服务商。未设置 `OPENAI_API_KEY` 时，会回退使用 `DEEPSEEK_API_KEY`。

### 可选联网搜索

仅在需要搜索时填写以下 Key，并在 `run_all.py` 中将 `ENABLE_SEARCH = True`：

```dotenv
TAVILY_API_KEY=tvly-你的tavily密钥（可选）
EXA_API_KEY=你的exa密钥（可选）
```

两个 Key 同时存在时会并行查询并按 URL 去重；仅填一个也可用；都未填时搜索会提示并自动关闭。

### 流水线开关

```python
ENABLE_SEARCH = False
ENABLE_SCRIPT = False
STEP0_MATCH_SCRIPTS = False
STEP1_ENSEMBLE = True
STEP2_REVIEW_JP = True
STEP3_TRANSLATE = True
STEP4_FINAL = True
STEP5_VALIDATE = True
STEP6_STRIP = True
```

其他主要控制项为 `MAX_WORKERS`、`REVIEW_JP_BATCH_SIZE`、`REVIEW_JP_OVERLAP`、`REVIEW_JP_FULL_REVIEW`、`REVIEW_JP_FULL_REVIEW_BATCH`、`TRANSLATE_BATCH_SIZE` 和 `TRANSLATE_REVIEW_BATCH_SIZE`。

## 输出与断点续跑

| 文件 | 说明 |
| --- | --- |
| `*_ensemble.srt` | Whisper 双模型融合后的日语字幕 |
| `*_reviewed.srt` | 日语二审结果 |
| `*_zh.srt` | 翻译并逐段审校后的双语字幕 |
| `*_final.srt` | 最终双语字幕 |
| `*_cn_only.srt` | 开启 STEP6 后生成的纯中文字幕 |
| `pipeline.log` | 追加写入的流水线日志 |

已存在输出的步骤会跳过。临时失败后，修正问题并使用同一命令重跑即可。若要让播放器自动加载纯中文字幕，可执行 `python rename_suffix.py _cn_only` 或 `start_rename_cn_only.bat`；覆盖已存在目标前会创建 `.bak.srt` 备份。

## 音频预处理

`ENABLE_AUDIO_PREPROCESS = True` 会启用 16 kHz 单声道处理、80 Hz 高通滤波、保守的稳态降噪、RMS 归一化至 -20 dBFS 和峰值限制。缓存的 `*_preprocessed.wav` 支持断点续跑。可在 `run_all.py` 用 `INITIAL_PROMPT`、`HOTWORDS` 和 `VAD_*` 控制项适配作品术语与耳语识别。

## 可选台本模式

将日语台本放入 `scripts/`，再开启：

```python
ENABLE_SCRIPT = True
STEP0_MATCH_SCRIPTS = True
SCRIPT_DIR = "./scripts"
```

匹配器支持一对一同名台本和合并台本，可识别 UTF-8/BOM 与 Shift-JIS（`cp932`），并在需要时请求确认。台本模式使用 large-v3 时间轴与台本内容，除非检测到不匹配，否则跳过 Turbo；不匹配时回退至 V3+Turbo，记录到 `output/script_mismatch.json`，STEP2 仅审校这些文件。

## 性能参考

以下历史实测使用 RTX 4060（8 GB 显存）和 32 GB 内存，处理 3 个各约 1 小时、约 300 条字幕的 ASMR 音频，关闭联网搜索。服务商价格会变化；V3.6 起程序报告 token 而不再估算费用。

| 阶段 | 耗时 | 历史 DeepSeek 费用 |
| --- | --- | --- |
| 双模型 Whisper 转写 | 约 30 分钟 | 0 元 |
| LLM 融合 | 约 2 分钟 | 约 0.15–0.24 元 |
| 日语审校 | 约 4 分钟 | 约 0.24–0.39 元 |
| 翻译与审校 | 约 6 分钟 | 约 0.30–0.45 元 |
| 全篇终审 | 约 2 分钟 | 约 0.09–0.15 元 |
| 自动化验证 | 约 2 秒 | 0 元 |
| 合计 | 约 44 分钟 | 约 0.78–1.23 元 |

## 更新日志

### V3.6.1

将 4 个 `start_*.bat` 重写为 ASCII 与 CRLF 行尾；分离 Python 命令、参数和 `pause`，修复双击启动脚本立即失败的问题。

### V3.6.0

迁移至 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_ENABLE_THINKING` 和 `OPENAI_MAX_TOKENS`；保留旧 Key 回退；将用量报告由费用估算改为 token 统计。

### V3.5.0

将智谱搜索替换为 Tavily 与 Exa。修复空内容重试、低置信度标记、部分空译文、GPU 降级、输出路径、重命名备份、台本回退和非交互台本确认；新增首次 Ctrl+C 防护与 Hugging Face 镜像启动设置。

### V3.4.1–V3.0

新增 `.env` 加载、音频预处理与 VAD 控制、台本辅助转写、GPU/LLM 两阶段处理、并行处理、Function Calling 结构化输出、token 日志和纯中文字幕输出。

## 致谢

[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[DeepSeek](https://platform.deepseek.com)、[Tavily](https://tavily.com)、[Exa](https://exa.ai)、[FFmpeg](https://ffmpeg.org)、[librosa](https://librosa.org)、[noisereduce](https://github.com/timsainb/noisereduce)、[SciPy](https://scipy.org) 和 [soundfile](https://python-soundfile.readthedocs.io)。

## AI 参与说明

本项目 Python 脚本和文档初稿由 AI 根据人类设计要求生成：V2.0 及以前使用 `deepseek-v4-pro`，V3.0 至 V3.4.1 使用 `GLM-5.2`，V3.5 起使用 `deepseek-v4-pro-0813`。人类作者负责定义项目、选择架构与参数、测试输出质量，以及做出发布和许可证决策。

## 许可证

采用 GNU General Public License v3.0 or later（`GPL-3.0-or-later`）。详见 [LICENSE](LICENSE)。Copyright (c) 2025–2026 Kochiya3309.
