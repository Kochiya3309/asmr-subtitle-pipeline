# ASMR 字幕自动生成流水线

[English](README.md) | 简体中文

将日语 ASMR 音频生成中日双语 SRT 字幕。流水线在本地使用 `large-v3` 和 `large-v3-turbo` 转写音频，再通过 OpenAI 兼容 LLM 融合、审校和翻译文本，最终输出可直接加载到播放器的字幕。

## 核心能力

- 本地双 ASR，并保留可复核候选证据；对可疑的低音量窗口执行定向救援。
- 高精度隔离已知固定幻觉模板，同时保留原始证据。
- 可选无时间轴日文台本，并支持一份总台本对应多个音频文件。
- 可选本地浏览器复核，提供日文、罗马音、中文、相邻上下文、播放、时间编辑、拆分和合并。
- 输入绑定的断点缓存，可识别音频、配置、证据和上游字幕变化。
- 异常长字幕时间修复和最终规则验证。

## 隐私与数据流向

- 音频预处理和两个 Whisper 模型均在本地运行。音频文件不会上传给 LLM 或搜索服务商。
- 转写文本、字幕文本以及启用台本时的台本文本会发送给所配置的 OpenAI 兼容 LLM，用于融合、审校或翻译。
- 联网搜索默认关闭。启用后，生成的查询词会发送给所配置的 Tavily 或 Exa 服务商。
- 可选人工复核服务器只监听 `127.0.0.1`。

处理隐私或 NSFW 内容时，除非确实需要外部检索，否则应保持 `ENABLE_SEARCH=0`。启用的处理阶段仍会把必要文本发送给 LLM 服务商。

## 快速开始

第一次使用请优先阅读[纯新手使用指南](docs/getting-started.zh-CN.md)，其中包含安装、配置、首次运行、结果查看和常见报错的逐步说明。

1. 安装 Python、FFmpeg 和 ffprobe。推荐使用 Python 3.10 或 3.11。
2. 创建隔离环境并安装依赖：

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
```

3. 打开 `.env` 并填写 `OPENAI_API_KEY`。使用默认 DeepSeek 接口以外的服务商时，还需设置 `OPENAI_BASE_URL` 和 `OPENAI_MODEL`。
4. 将 `.mp3`、`.m4a`、`.wav`、`.flac`、`.ogg` 或 `.opus` 文件放入 `audio/`。
5. 双击 `start.bat`，或运行：

```powershell
venv\Scripts\python.exe run_all.py
```

6. 在播放器中加载 `output/*_final.srt`。

首次运行需要下载数 GB 的模型数据。`start.bat` 会设置 `HF_ENDPOINT=https://hf-mirror.com`，供需要 Hugging Face 镜像的网络使用。

## 运行模式

### 默认无台本模式

无需额外设置。默认配置会关闭台本辅助、浏览器复核、确定性时间轴和联网搜索，同时执行完整的双 ASR、审校、翻译、终审、规则验证和纯中文导出流程。

### 台本辅助模式

将日文台本放入 `scripts/`，然后设置：

```dotenv
ENABLE_SCRIPT=1
STEP0_MATCH_SCRIPTS=1
```

STEP0 支持同名匹配、模糊匹配，以及把一份总台本拆分给多个音频文件。台词、动作说明、心理描写、章节标题和装饰分隔符都会保留。当前版本只有在设置 `ENABLE_SCRIPT=1` 后才会检测台本。

交互运行会要求确认生成的映射。无人值守任务如需自动确认，请先阅读 [.env.example](.env.example) 中 `SCRIPT_AUTO_VERIFY` 的风险说明。

### 人工复核模式

设置：

```dotenv
ENABLE_HUMAN_REVIEW=1
```

翻译完成后，流水线会为需要检查的片段打开本地浏览器页面。如果尚未提交审核，流水线会安全暂停；完成审核后再次运行，即可从缓存继续。

## 一次运行会做什么

1. 预处理音频，并在本地运行 `large-v3` 和 `large-v3-turbo`。
2. 记录候选证据，隔离符合条件的固定幻觉，并对可疑窗口执行有边界的救援转写。
3. 由所配置的 LLM 融合日文转写、审校原文、翻译为简体中文并审校译文。
4. 可选浏览器复核允许检查困难片段并修改文本或时间轴。
5. 全篇终审、本地规则验证和可选纯中文导出生成最终结果。

准确的内部阶段顺序、缓存契约和审计产物见[开发者文档](docs/developer-guide.zh-CN.md)。

## 输出文件

| 文件 | 用途 |
| --- | --- |
| `*_final.srt` | 最终中日双语字幕，播放时使用这个文件 |
| `*_cn_only.srt` | 已启用 STEP6 导出的纯中文字幕 |
| `*_human_reviewed.srt` | 可选人工复核检查点，之后仍会进入 LLM 终审 |
| `pipeline.log` | 追加写入的运行日志，用于排查问题 |

中间 SRT、ASR 证据、时间轴报告、manifest 和复核记录会保留在 `output/` 中，用于恢复和诊断。编辑或删除前请先阅读[开发者文档](docs/developer-guide.zh-CN.md)。

## 系统要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | 主要面向 Windows 10/11；macOS/Linux 需要调整路径 |
| Python | 推荐 3.10 或 3.11；当前 Windows venv 也已在 3.14.5 实测通过 |
| GPU | 可选但强烈推荐；测试基线为 8 GB 显存的 NVIDIA GPU |
| CPU 回退 | CTranslate2 支持，但速度会明显下降 |
| 内存 | 最低 16 GB，推荐 32 GB |
| 磁盘 | 模型和中间音频约需 10 GB 可用空间 |
| 其他 | `PATH` 中可用 FFmpeg 和 ffprobe；首次下载模型及调用所选 API 时需要网络 |

本项目不依赖 PyTorch；GPU 检测使用 `ctranslate2.get_cuda_device_count()`。

## 已知限制

- 极弱轻语、吹气声、强背景音效、削波或含糊发音仍可能漏识别或误识别。
- 两人声音重叠时，两个 Whisper 模型都可能把内容合并到同一条字幕。
- 幻觉隔离只针对已知的高精度模板，无法证明其余每一句都是真实语音。
- LLM 审校能改善上下文和翻译，但也可能引入新错误。困难音频或用于发布的字幕仍建议人工复核。
- `ENABLE_DETERMINISTIC_TIMELINE` 仍属实验功能，默认关闭。
- 台本不会自动启用：当前必须先设置 `ENABLE_SCRIPT=1`，流水线才会检查 `scripts/`。

## 高级配置与开发

- [.env.example](.env.example)：面向用户的环境配置及其默认值、依赖关系和安全说明。
- [开发者文档](docs/developer-guide.zh-CN.md)：架构、阶段契约、缓存失效、审计产物和发布检查。
- [CHANGELOG.md](CHANGELOG.md) / [CHANGELOG.zh-CN.md](CHANGELOG.zh-CN.md)：当前与历史版本变化。

不要提交 `.env`、音频、模型文件、生成字幕、日志或 `_cache/` 诊断产物。

## 致谢

[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[DeepSeek](https://platform.deepseek.com)、[Tavily](https://tavily.com)、[Exa](https://exa.ai)、[FFmpeg](https://ffmpeg.org)、[librosa](https://librosa.org)、[noisereduce](https://github.com/timsainb/noisereduce)、[SciPy](https://scipy.org)、[soundfile](https://python-soundfile.readthedocs) 和 [pykakasi](https://github.com/miurahr/pykakasi)。

## AI 参与说明

Python 脚本与文档由人类和 AI 迭代协作完成。人类作者定义使用场景、架构、参数、验收标准、基于听辨的质量决策和发布策略；AI 系统协助实现、编写文档、审查和构建测试。

## 许可证

GNU 通用公共许可证 v3.0 或更高版本（`GPL-3.0-or-later`）。详见 [LICENSE](LICENSE)。Copyright (c) 2025–2026 Kochiya3309。
