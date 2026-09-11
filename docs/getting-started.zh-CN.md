# 新手指南

[English](getting-started.md) | 简体中文

本指南帮助 Windows 用户完成一次基础的“日语 ASMR 音频 → 中日双语字幕”运行。第一次请不要启用台本、人工复核、视频输入或联网搜索。全部配置项、默认值、依赖和安全含义以[配置参考](configuration.zh-CN.md)为准。

## 1. 获取项目

克隆仓库后进入项目根目录：

```powershell
git clone https://github.com/Kochiya3309/asmr-subtitle-pipeline.git
cd asmr-subtitle-pipeline
```

也可以下载并解压仓库 ZIP。无论使用哪种方式，后续命令都应在包含
`start.bat`、`run_all.py` 和 `requirements.txt` 的目录中运行。

## 2. 安装前置软件

安装 Python 3.10 或 3.11、FFmpeg 和 ffprobe。随后在项目根目录运行：

```powershell
python --version
ffmpeg -version
ffprobe -version
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

每条版本命令都应输出版本信息。如果找不到 `python`、`ffmpeg` 或 `ffprobe`，请完成对应软件安装、将所需程序目录加入 `PATH`，再重新打开 PowerShell。若 `python --version` 不是 3.10 或 3.11，运行 `py -0p` 查看已安装解释器，并使用受支持版本创建环境，例如 `py -3.11 -m venv venv`。

## 3. 创建最小配置

复制模板：

```powershell
Copy-Item .env.example .env
```

填写真实的 LLM 密钥，并保留以下首次运行安全值：

```dotenv
OPENAI_API_KEY=enter_your_real_API_key_here
ENABLE_SEARCH=0
ENABLE_SCRIPT=0
STEP0_MATCH_SCRIPTS=0
ENABLE_HUMAN_REVIEW=0
```

使用默认 DeepSeek endpoint 时，通常只需修改密钥。使用其他 OpenAI 兼容服务商时，还要填写服务商提供的 `OPENAI_BASE_URL` 和 `OPENAI_MODEL`。不要提交、分享、截图真实密钥，也不要把它放进问题报告。

## 4. 放入一个音频文件

把一个较短的测试文件直接放入 `audio/`。首次运行支持 `.mp3`、`.m4a`、`.wav`、`.flac`、`.ogg`、`.opus`。不要放入同名不同扩展名的文件，例如同时放 `sample.mp3` 和 `sample.wav`。

## 5. 运行并找到结果

双击 `start.bat`，或运行：

```powershell
venv\Scripts\python.exe run_all.py
```

首次运行会下载数 GB 的模型文件。成功后，在播放器中加载 `output/<audio>/final/<audio>_final.srt`。纯中文字幕文件是 `output/<audio>/final/<audio>_cn_only.srt`。

音频保留在本地；所需字幕文本会发送给配置的 LLM。处理私密或 NSFW 内容时，除非确有查阅需要，应保持 `ENABLE_SEARCH=0`。

## 下一步

启用台本、人工复核、视频准备、定时控制、搜索或阶段开关前，请阅读[配置参考](configuration.zh-CN.md)。它是这些设置及其依赖关系的权威来源。
