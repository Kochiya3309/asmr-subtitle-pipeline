# 纯新手使用指南

这份指南面向第一次使用本项目、只想把日语 ASMR 音频生成中日字幕的 Windows 用户。先按本文完成一次无台本运行；台本、人工复核和高级开关都可以在确认基础流程可用后再开启。

项目会在本机运行两个 Whisper 模型进行语音识别，但日文融合、审校、翻译和终审会把**文本**发送给你配置的 OpenAI 兼容 LLM。音频不会上传。处理私密或 NSFW 内容时，建议保持联网搜索关闭。

## 你将得到什么

成功运行后，`output/` 中会出现：

| 文件 | 用途 |
| --- | --- |
| `*_final.srt` | 最终中日双语字幕；在播放器中优先加载它 |
| `*_cn_only.srt` | 纯中文字幕 |
| `pipeline.log` | 运行日志；出错时先查看它 |

不要急着删除 `output/` 中其余文件。它们是断点续跑、人工复核和定位问题所需的中间产物。

## 1. 准备软件

需要安装：

- Python 3.10 或 3.11（推荐）；
- FFmpeg；
- 一个 OpenAI 兼容 LLM 服务的 API Key。

打开 PowerShell，分别执行下面命令确认 Python、FFmpeg 和 ffprobe 可用：

```powershell
python --version
ffmpeg -version
ffprobe -version
```

三条命令都应输出版本信息。如果 `python` 找不到，重新安装 Python 时勾选 “Add Python to PATH”；如果 `ffmpeg` 或 `ffprobe` 找不到，需要完成 FFmpeg 安装并把其 `bin` 目录加入 `PATH`，然后关闭并重新打开 PowerShell。

## 2. 获取项目

可以在 GitHub 下载 ZIP 并解压，也可以使用 Git：

```powershell
git clone https://github.com/Kochiya3309/asmr-subtitle-pipeline.git
cd asmr-subtitle-pipeline
```

之后所有命令都应在项目根目录执行，也就是包含 `start.bat`、`run_all.py` 和 `requirements.txt` 的目录。

## 3. 创建隔离环境并安装依赖

在项目根目录执行：

```powershell
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

看到命令行前面出现 `(venv)` 说明隔离环境已启用。它只影响当前项目，不会改动系统全局 Python 包。

如果 `python -m venv venv` 使用的不是 Python 3.10/3.11，请先在 PowerShell 执行 `py -0p` 查看已安装版本，再例如使用：

```powershell
py -3.11 -m venv venv
```

## 4. 创建并配置 `.env`

复制模板：

```powershell
Copy-Item .env.example .env
```

用文本编辑器打开 `.env`，至少填写并确认下面几项：

```dotenv
OPENAI_API_KEY=在这里填入你的真实 API Key

# 首次运行：不联网搜索、不使用台本、不进入人工复核。
ENABLE_SEARCH=0
ENABLE_SCRIPT=0
STEP0_MATCH_SCRIPTS=0
ENABLE_HUMAN_REVIEW=0
```

如果你使用默认 DeepSeek 接口，通常只需要 API Key。若使用其他 OpenAI 兼容服务，还要填写服务商提供的地址和模型名：

```dotenv
OPENAI_BASE_URL=https://服务商提供的地址/v1
OPENAI_MODEL=服务商提供的模型标识
```

`.env` 含密钥，不能提交到 Git，也不要截图、发送给他人或贴进报错求助内容。

## 5. 放入音频

将待处理音频直接放入项目的 `audio/` 目录。支持 `.mp3`、`.m4a`、`.wav`、`.flac`、`.ogg` 和 `.opus`。

首次测试只放一段短音频，且不要同时放置同名但扩展名不同的文件，例如 `sample.mp3` 与 `sample.wav`。这两者会生成相同的输出文件名，流水线会拒绝运行以避免覆盖。

## 6. 开始运行

最简单的方式是双击项目根目录的 `start.bat`。窗口会在完成后保留，不会立刻关闭。

也可以在已启用 venv 的 PowerShell 中运行：

```powershell
venv\Scripts\python.exe run_all.py
```

首次运行会下载数 GB 的模型文件，耗时取决于网络和显卡。`start.bat` 已配置 Hugging Face 镜像；运行时 GPU 占用不一定持续很高，因为本地 ASR、磁盘读写和远程 LLM 调用交替进行。

## 7. 查看结果

完成后打开 `output/`，在播放器中加载与音频同名的 `*_final.srt`。如果只需要中文，使用 `*_cn_only.srt`。

不要把 `*_ensemble.srt`、`*_reviewed.srt` 或 `*_zh.srt` 当作最终交付文件：它们分别是融合、日文二审和翻译阶段的检查点，仍可能被后续阶段更新。

## 之后再开启的功能

### 使用无时间轴台本

将日文台本放入 `scripts/`，再把以下配置改为 `1`：

```dotenv
ENABLE_SCRIPT=1
STEP0_MATCH_SCRIPTS=1
```

首次运行会要求你确认音频与台本的映射。总台本可以对应多个音频；请确认拆分结果后再继续。

### 人工复核难点片段

将下列配置改为 `1`：

```dotenv
ENABLE_HUMAN_REVIEW=1
```

翻译完成后会打开 `127.0.0.1:8765` 的本地页面。页面可播放音频、显示日文/罗马音/中文和上下文，并允许修改文字与时间轴。未提交时流水线会暂停；完成后再次运行同一命令即可继续。

### 联网搜索

只有确实需要查词或文化背景时才配置 `TAVILY_API_KEY` 或 `EXA_API_KEY`，并设置：

```dotenv
ENABLE_SEARCH=1
```

搜索服务会收到自动生成的查询词。处理私密或 NSFW 内容时，通常应保持 `0`。

## 能否中断或重新运行？

可以。直接再次运行 `start.bat` 或 `run_all.py`，系统会验证已有产物是否仍对应当前音频、配置和上游字幕，并尽可能从有效缓存继续。

但以下操作会使相关阶段重新执行：

- 修改源音频；
- 修改影响该阶段的 `.env` 配置；
- 编辑或删除上游 SRT；
- 修改台本或重新确认台本映射；
- 删除 `output/` 中的 manifest 或中间产物。

如需从头测试，请先另建一个空的输出目录并通过 `OUTPUT_DIR` 指向它；不要在不清楚依赖关系时批量删除 `output/`。

## 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| 找不到 `python` | 重新打开 PowerShell；确认 Python 已加入 PATH；运行 `python --version` |
| 找不到 `ffmpeg` 或 `ffprobe` | FFmpeg 的 `bin` 是否已加入 PATH；重新打开 PowerShell |
| 提示找不到 `venv\\Scripts\\python.exe` | 尚未完成第 3 步，或当前不在项目根目录 |
| LLM 调用失败 | `.env` 的 API Key、Base URL、模型名和账户额度；不要把真实 Key 写入日志或截图 |
| 首次模型下载失败 | 检查网络与磁盘空间；可重试，已完成的下载通常会复用缓存 |
| `127.0.0.1` 拒绝连接 | 确认人工复核步骤仍在运行；检查 `HUMAN_REVIEW_PORT` 是否被其他程序占用 |
| 没有 `*_final.srt` | 从终端最后一个错误和 `output/pipeline.log` 开始检查；不要只看中间 SRT |

## 接下来读什么

- [README.zh-CN.md](../README.zh-CN.md)：功能概览、运行模式和系统要求。
- [开发者文档](developer-guide.zh-CN.md)：完整 STEP0–STEP6、缓存契约、审计产物和故障边界。
- [.env.example](../.env.example)：每一项可配置开关的说明。
