# ASMR 字幕自动生成流水线 — 使用说明（更新 V3.4.1）

## 这是什么？

一套全自动工具，把你手头的日语 ASMR 音频（mp3/m4a/wav/flac 等）变成中日双语字幕。

工作流程：音频 → Whisper 双模型转写日文字幕 → AI 审校日语错误 → 结合上下文审查可能的识别错误 → 翻译成中文 → AI 逐段审校翻译 → AI 全篇终审（宏观+微观） → 自动化规则验证 → 双语字幕 .srt 文件。

V3.4.1 的主要更新：API Key 安全加固——run_all.py 中的 DEEPSEEK_API_KEY 和 ZHIPU_API_KEY 从硬编码改为通过环境变量读取，避免源码泄露风险；项目根目录新增 .env 文件（已被 .gitignore 忽略）存放真实 Key，run_all.py 启动时自动加载（手动解析，未引入 python-dotenv 依赖）；新增 .env.example 作为模板提交到仓库，其他用户克隆后复制为 .env 填入 Key 即可；移除 run_all.py 的 git skip-worktree 保护标记（代码已无敏感信息，可直接提交）。已设置的系统环境变量优先于 .env 文件。

V3.4 的主要更新：Whisper 转写准确率改进——新增音频预处理模块（librosa 读取 + 80Hz 高通滤波去低频隆隆声 + noisereduce 稳态噪声抑制（保守强度）+ RMS 归一化（峰值限幅）），预处理结果缓存为 *_preprocessed.wav 支持断点续跑；VAD 参数针对 ASMR 耳语场景调优（threshold 0.3→0.2、min_speech_duration_ms 100→50、speech_pad_ms 600→800）并通过环境变量暴露供用户覆盖；新增 initial_prompt 引导（默认 ASMR 通用日语 prompt，可在 run_all.py 覆盖为具体作品术语）；新增 hotwords 热词支持（faster-whisper 1.2.1 特性，传入角色名/专有名词提升识别率）；所有新参数集中在 run_all.py 配置区。预处理对稳态底噪、雨声等有效，对背景音乐干扰效果有限（需人声分离，见开发者文档 Phase 2）。

V3.3 的主要更新：台本模式效率优化——有台本时跳过 Turbo 转写（仅用 V3 提供时间轴 + 台本作为权威文本），融合 prompt 简化；台本与 Whisper 严重不匹配时不再终止流水线，改为自动回退到无台本模式（补跑 Turbo + V3+Turbo 融合）并记录至 script_mismatch.json；日语二审（STEP2）在台本模式下默认跳过，仅当存在 mismatch 时触发，且仅审校 mismatch 文件；translate.py 输入逻辑改为以 *_ensemble.srt 为基础，*_reviewed.srt 存在则优先使用否则回退到 *_ensemble.srt，使 STEP2 跳过时 STEP3 仍可正常运行；match_scripts.py 新增 LLM 文件名匹配阶段（正则失败后由 LLM 判定 match/split/none）与合并台本检测（单一台本被多音频命中时自动转为待拆分）；幂等检查扩展（音频文件列表/修改时间变化、台本列表变化均触发重新匹配）；track 标识符支持 1A/1B 等数字+字母组合；validate_final.py 移除假名残留检查。

V3.2 的主要更新：新增台本辅助功能——支持发行商提供的日语台本（纯台词无时间轴，或合并台本）作为 Whisper 融合阶段的权威文本参考；新增 match_scripts.py 脚本负责台本的自动匹配、合并台本拆分、人工验证流程；ensemble 融合阶段注入台本文本并扩展 FUSION_TOOL 增加 error 字段，AI 检测到台本与 Whisper 输出严重不对应时报错终止流水线（V3.3 起改为自动回退）。默认关闭，需在 run_all.py 手动开启。

V3.1 的主要更新：融合阶段改为两阶段分离架构——先串行转写所有文件（GPU 独占），再并行调用 DeepSeek 融合所有文件（网络 I/O 并发），ensemble 阶段总耗时进一步缩短约 20%；去掉文件间固定等待；_fusion_parser 提升为模块级函数；阶段2前预初始化 DeepSeek client 消除多线程首次调用 race。

V3.0 的主要更新：全阶段并行处理（跨文件+跨批次），耗时大幅缩短；所有参数集中到 run_all.py 顶部配置区；新增文件日志；融合阶段改用 Function Calling 结构化输出；翻译和审校提示词优化；可选纯中文字幕输出。

全程只需把音频放进文件夹，双击运行，等它跑完。用 PotPlayer / VLC / MPC 加载输出的字幕文件即可。

---

## 系统要求

| 项目 | 最低要求 | 推荐 |
|------|---------|------|
| 操作系统 | Windows 10/11（Mac/Linux 可运行但需自行调整路径） | Win11 |
| GPU | NVIDIA 显卡，8GB 显存 | RTX 3060 或以上 |
| 内存 | 16GB | 32GB |
| 硬盘 | 10GB 空闲（存模型） | SSD |
| Python | 3.10 或 3.11 | 3.10 |
| 网络 | 能访问 api.deepseek.com | 稳定宽带 |

如果没有 NVIDIA 显卡，也可以用 CPU 跑，但转写速度会慢 5 到 10 倍。32GB 内存下 CPU 模式仍可正常使用。。

---

## 第一步：安装基础软件

### 1.1 安装 Python

打开 python.org，下载 Python 3.10 或 3.11 的安装包。运行安装程序，务必勾选「Add Python to PATH」（这一步非常重要）。安装完成后，按 Win+R，输入 cmd 回车，输入 python --version，看到 Python 版本号即成功。

### 1.2 安装 FFmpeg

浏览器打开 gyan.dev/ffmpeg/builds/，找到 ffmpeg-release-essentials.zip 并下载（约 100MB）。解压到固定位置，例如 C:\ffmpeg。把 C:\ffmpeg\bin 添加到系统环境变量：Win+R → 输入 sysdm.cpl → 高级 → 环境变量 → 在「系统变量」里找到 Path → 编辑 → 新建 → 粘贴 C:\ffmpeg\bin → 确定。重新打开 CMD，输入 ffmpeg -version，看到版本信息即安装成功。

### 1.3 安装 NVIDIA 显卡驱动（如用 GPU）

打开 nvidia.cn/geforce/drivers/，选择你的显卡型号，下载并安装最新的 Game Ready 驱动。安装时选择「自定义」→ 勾选「执行清洁安装」。

---

## 第二步：获取 API Keys

### 2.1 DeepSeek API Key（必须）

打开 platform.deepseek.com，注册账号（支持手机号）。进入「API Keys」页面，点击「创建 API Key」。复制生成的那串 sk-xxxxxxxx 密钥，保存好。新用户赠送 500 万 tokens 免费额度，够处理几十到上百小时的音频（具体以deepseek官网为准）。用完后按量付费，1 小时音频全程约 0.18 到 0.35 元。

### 2.2 智谱 API Key（可选，用于联网搜索）

打开 open.bigmodel.cn，注册账号 → 进入「API Keys」页面 → 创建 API Key。复制密钥保存。

如果不需要联网搜索功能，可以不申请。搜索功能帮助 AI 在遇到不确定的日语词汇或文化概念时上网查证，能提升翻译质量，但会增加少许费用和耗时。注意：智谱搜索存在内容审查机制，不适合用于 NSFW 等成人内容的翻译，建议处理此类内容时关闭搜索功能。

---

## 第三步：安装并配置流水线

### 3.1 获取脚本

将作者分享的整个文件夹复制到你电脑上的任意位置，例如 D:\whisper_asmr。文件夹包含以下文件：common.py（核心函数库）、match_scripts.py（第零步：台本匹配与拆分，V3.2 新增，可选）、ensemble_transcribe.py（第一步：双模型转写加融合）、review_japanese.py（第二步：日语审校加上下文审查）、translate.py（第三步：翻译加审校）、review_final.py（第四步：全篇终审）、validate_final.py（第五步：自动化验证）、strip_japanese.py（去除日文仅留中文，可选）、rename_suffix.py（重命名工具）、run_all.py（一键启动脚本）、requirements.txt（依赖清单）、audio 文件夹（存放音频）、scripts 文件夹（存放发行商提供的日语台本，V3.2 新增，可选）、output 文件夹（存放输出）。

### 3.2 创建虚拟环境

打开 CMD，输入：

cd /d 你的项目路径
python -m venv venv
venv\Scripts\activate

看到命令行前出现 (venv) 即激活成功。

### 3.3 安装 Python 依赖

pip install faster-whisper openai

等待安装完成（约 2 到 5 分钟）。
如果下载慢，可以加国内镜像：pip install faster-whisper openai -i https://pypi.tuna.tsinghua.edu.cn/simple。

### 3.4 配置 API Keys

项目根目录下有一个 `.env.example` 模板文件。复制它为 `.env` 并填入你的 API Key：

```
DEEPSEEK_API_KEY=sk-你的deepseek密钥
ZHIPU_API_KEY=你的智谱密钥
```

`.env` 文件已在 `.gitignore` 中，不会被提交到 git。run_all.py 启动时会自动加载 `.env`（V3.4.1 起，无需修改任何 .py 文件）。如果已设置系统环境变量 `DEEPSEEK_API_KEY` / `ZHIPU_API_KEY`，则优先使用系统环境变量。

ENABLE_SEARCH 设为 True 开启联网搜索，False 关闭。处理 NSFW 内容时建议设为 False。

V3.0 起无需修改 common.py，所有配置只需在 run_all.py 顶部一处完成。

---

## 第四步：使用

### 4.1 准备音频

把你要处理的音频文件（.mp3 / .m4a / .wav / .flac / .ogg / .opus）全部丢进项目目录下的 audio 文件夹。支持同时放入多个文件，脚本会依次处理。

### 4.2 运行

双击 start.bat
或打开 CMD 输入：

cd /d 你的项目路径
venv\Scripts\activate
python run_all.py

### 4.3 首次运行

首次运行时，Whisper 会自动下载两个模型（large-v3 约 3GB，large-v3-turbo 约 1.5GB，合计约 4.5GB），请耐心等待。下载一次后永久缓存，后续运行秒加载。

### 4.4 获得结果

全部完成后，output 目录下会生成以下文件：

| 文件 | 说明 |
|------|------|
| *_ensemble.srt | Whisper 双模型融合字幕（纯日语） |
| *_reviewed.srt | 日语 AI 审校后字幕 |
| *_zh.srt | 翻译加审校后的双语字幕 |
| *_final.srt | 最终版双语字幕（用这个观看） |
| *_cn_only.srt | 纯中文字幕（需开启 STEP6_STRIP） |
| pipeline.log | 完整运行日志（追加模式，保留历史） |

用 PotPlayer / VLC / MPC 等播放器打开对应音频，将 _final.srt 拖入播放窗口即可看到字幕。

每个脚本跑完后会自动打印 Token 消耗报告，包含本次调用的 DeepSeek API 次数、输入和输出 token 数量、缓存命中率、以及精确费用。所有输出同时写入 output/pipeline.log，终端关闭后仍可回溯。
---

## 第五步：断点续跑和步骤开关

如果某个步骤中途失败（比如网络断了），修好问题后重新运行同一个命令即可。已经完成的步骤会跳过（检测到输出文件已存在），不会重复处理。

在 run_all.py 中可以单独开关每一步（True 为开启，False 为跳过）：

STEP1_ENSEMBLE = True    # 双模型转写
STEP2_REVIEW_JP = True   # 日语审校
STEP3_TRANSLATE = True   # 翻译
STEP4_FINAL = True       # 终审
STEP5_VALIDATE = True    # 自动化验证
STEP6_STRIP = True      # 去除日文仅留中文（默认开启）

## 5.1 可调参数（V3.0 新增）

所有关键参数已集中到 run_all.py 顶部配置区，无需翻进子脚本修改：

MAX_WORKERS = 10                       # DeepSeek API 并发数（V3.0 起用于审校/翻译/终审，V3.1 起亦用于 ensemble 融合阶段）
REVIEW_JP_BATCH_SIZE = 30             # 日语逐批审校每批条数
REVIEW_JP_OVERLAP = 5                 # 相邻批次重叠条数
REVIEW_JP_FULL_REVIEW = True          # 日语全篇上下文审校开关
REVIEW_JP_FULL_REVIEW_BATCH = 200     # 全篇审校每批最大条数
TRANSLATE_BATCH_SIZE = 10             # 翻译阶段每批条数
TRANSLATE_REVIEW_BATCH_SIZE = 20      # 翻译审校阶段每批条数

## 5.4 Whisper 转写优化（V3.4 新增）

针对 ASMR 音频特征（耳语低音量、气声多、停顿长、背景音干扰）的转写参数与预处理：

ENABLE_AUDIO_PREPROCESS = True    # 音频预处理开关。开启后转写前先降噪+归一化+高通滤波
INITIAL_PROMPT = "ASMR作品、囁き、耳かき、癒し系、日本語、優しい声"  # 通用 ASMR prompt，可改为具体作品术语
HOTWORDS = ""                     # 逗号分隔热词，如 "佐倉綾音,媚薬,触手"，提升专有名词识别率
VAD_THRESHOLD = 0.2               # VAD 语音检测阈值（0-1，越低越敏感）
VAD_MIN_SPEECH_MS = 50            # 最短语音段（ms），过短会被丢弃
VAD_SPEECH_PAD_MS = 800           # 语音前后缓冲（ms），避免削头去尾
VAD_MIN_SILENCE_MS = 500          # 触发分割的最短静音（ms）

预处理流程：librosa 读取 16kHz 单声道 → 80Hz 高通滤波去低频隆隆声 → noisereduce 稳态噪声抑制 → RMS 归一化到 -20dBFS（峰值限幅 0.95 避免削波）→ 输出 *_preprocessed.wav 缓存。预处理文件存在则跳过（支持断点续跑）。

调优建议：
- 背景底噪/雨声明显时，ENABLE_AUDIO_PREPROCESS=True 收效显著
- 作品有特定术语时，将 INITIAL_PROMPT 改为含角色名/题材的短句（如 "妖狐、神社、狐耳、甘えん坊"）
- 反复出现的专有名词识别错误，填入 HOTWORDS（逗号分隔）
- VAD_THRESHOLD 过低（<0.15）会产生大量碎片化假阳性，过高（>0.4）会漏掉耳语
- 背景音乐干扰严重时，预处理效果有限，需考虑人声分离（见开发者文档 Phase 2）

## 5.2 纯中文字幕

如果想要纯中文字幕（不要日语原文），在 run_all.py 中把 STEP6_STRIP 设为 True。流水线会自动从 _final.srt 中去除日文行，输出 _cn_only.srt。

如果还需要把 _cn_only.srt 重命名为 .srt（方便播放器自动加载），运行 rename_suffix.py:
python rename_suffix.py _cn_only
或双击 start_rename_cn_only.bat。

## 5.3 台本辅助功能（V3.2 新增）

若发行商提供了日语台本（仅台词无时间轴，或多个音频合并的台本），可启用台本辅助功能提升翻译准确性。台本作为权威文本参考注入 Whisper 融合阶段，帮助 AI 修正 Whisper 的误听并补全漏识别的台词。

### 开启方式

在 run_all.py 中设置：
ENABLE_SCRIPT = True             # 总开关
STEP0_MATCH_SCRIPTS = True       # STEP0 开关
SCRIPT_DIR = "./scripts"         # 台本所在目录

### 台本文件准备

将台本 .txt 文件放入 scripts/ 目录。支持两种形式：
- 一对一：每个音频对应一个同名 .txt（如 A.m4a ↔ A.txt）
- 合并台本：多个音频的台本合并在一个 .txt 中，脚本会自动拆分

台本文件编码支持 UTF-8（含 BOM）和 Shift-JIS（cp932）自动识别，无需手动转换。大多数发行商提供的 Shift-JIS 台本可直接使用。

### 工作流程

1. match_scripts.py 自动匹配或拆分台本（同名匹配 → track 标识符匹配 → 关键词匹配 → 合并台本检测 → LLM 文件名匹配 → 正则拆分 → LLM 拆分）
2. 人工验证：脚本暂停等待你检查匹配或拆分结果，输入 y 确认后创建验证标记
3. ensemble_transcribe.py 有台本的音频跳过 Turbo，仅用 V3 提供时间轴 + 台本作为权威文本进行融合
4. 若 AI 检测到台本与 Whisper 严重不对应（如别作品台本、完全错配），自动回退到无台本模式（补跑 Turbo + V3+Turbo 融合），并记录到 script_mismatch.json
5. 无台本模式或无 mismatch 时，STEP2（日语二审）跳过；存在 mismatch 时 STEP2 仅审校 mismatch 文件

### 兜底机制

- 合并台本无法拆分时，SCRIPT_FALLBACK_FULL=True 会把整本台本附加给每个未匹配音频
- 整本台本加 Whisper 输出超 token 限制时直接报错退出，提示手动拆分
- 台本文件在验证后被修改、音频文件列表变化、音频文件修改、台本列表变化均自动失效缓存，下次运行需重新匹配
- 台本 mismatch 时自动回退无台本模式，mismatch 文件仍可正常产出 ensemble.srt 并进入下游

### 重要约束

台本中的非台词内容（※注记、场景说明等）不会进入字幕正文，仅作为 AI 理解上下文之用。AI 会优先采用台本文本，时间轴沿用 Whisper 输出。

---

## 流水线各步骤详解

第一步 ensemble_transcribe.py 用 Whisper large-v3 和 large-v3-turbo 两个模型分别转写同一份音频。large-v3 日语识别精度最高但偶尔漏短句，large-v3-turbo 速度快约八倍且 VAD 切分更密集，能捕捉到 V3 漏掉的短片段。V3.1 起脚本分为两个阶段执行：阶段1串行处理所有音频文件的双模型转写（GPU 独占，避免显存争用），Whisper 模型在处理多个音频文件时只加载一次，后续文件复用已加载的模型实例；阶段2用 ThreadPoolExecutor 并行调用 DeepSeek 对所有文件的两份 SRT 进行逐条比对融合（并发数由 MAX_WORKERS 控制）。V3.3 起台本模式优化：有台本的音频在阶段1跳过 Turbo（仅跑 V3），阶段2采用简化的 V3+台本融合 prompt（不再对比 V3 与 Turbo），节省约一半 GPU 转写时间。若融合阶段 AI 检测到台本与 Whisper 严重不匹配，自动补跑 Turbo 并回退到 V3+Turbo 融合（原 prompt），mismatch 文件记录到 script_mismatch.json 供下游 STEP2 选择性触发。无台本模式下行为与 V3.2 完全一致。V3.4 起转写前新增音频预处理（librosa 读取 + 80Hz 高通滤波 + noisereduce 降噪 + 归一化，缓存为 *_preprocessed.wav），VAD 参数针对 ASMR 耳语调优（threshold 0.2、min_speech 50ms、speech_pad 800ms）并通过环境变量暴露，新增 initial_prompt 引导与 hotwords 热词支持提升专有名词识别率。融合逻辑：时间轴接近的条目视为同一段语音，AI 选择两个版本中语法更正确、上下文更通顺的一方；只有某一方能捕捉到的条目，如果确实是自然的日语表达就保留。融合提示词明确要求 AI 不得省略或丢弃仅在一个模型中存在的内容，即使片段化也必须保留。V3.0 起融合阶段改用 submit_fusion 工具进行 Function Calling 结构化输出，格式可靠性大幅提升，纯文本回退仍保留作为兜底。双模型融合可消除单模型约百分之三十的个体偏差。两阶段分离后，转写阶段仍是串行瓶颈（GPU 独占），但融合阶段的网络等待被并发摊薄，多文件场景下 ensemble 总耗时缩短约 20%。

第二步 review_japanese.py 分为两个阶段。阶段一是逐批审校：读取融合后的日语字幕，按每 30 条一组发送给 DeepSeek，让 AI 修正同音异义词、助词错误、气息误判和不自然的断句。相邻组之间有 5 条重叠以消除上下文断裂。系统提示词中包含了成人向内容的类型提示（催淫、媚药、触手等），以及低品质音频误识别的检测指引，帮助 AI 在局部上下文中识别 Whisper 的典型误听模式。无法识别的内容统一标记为「〔認識不良〕」以便后续人工复查。阶段二是全篇上下文审校：在所有批次处理完成后，将全篇日语字幕打包发送给 AI，让 AI 利用完整的作品世界观和术语一致性来修正逐批审校无法发现的同音词误判。V3.0 起两个阶段均支持跨文件并行处理，多个文件的批次同时发送 API 请求。全篇上下文审校的输出通过 submit_review 工具以保证格式可靠，同样享受三层兜底保护。V3.3 起在台本模式下该步骤默认跳过（台本即权威文本无需审校），仅当 STEP1 产出 script_mismatch.json 且非空时触发，且仅审校 mismatch 文件（自动回退为 V3+Turbo 融合的产物）。无台本模式下行为与 V3.2 完全一致。

第三步 translate.py 分两个阶段。翻译阶段按每 10 条一组将日语批量翻译为简体中文，纯流式调用，不开启搜索以节省时间和费用。V3.0 优化了翻译提示词，新增拟声词处理、不完整句子翻译、双关语处理、括号格式统一等规则。审校阶段按每 20 条一组将原文和初译并列发送给 AI 进行比对修正，输出修正后的中文译文，使用 submit_review 结构化输出。低质量原文的处理方式改为始终输出中文译文并标注「（低信頼度）」，不再在译文栏保留日语原文。两个阶段均支持跨文件并行。搜索相关提示词根据 ENABLE_SEARCH 开关动态拼接，搜索关闭时 AI 不会看到任何关于 web_search 的提及。V3.3 起输入逻辑改为以 *_ensemble.srt 为基础扫描，对每个文件优先使用 *_reviewed.srt，不存在则回退到 *_ensemble.srt，使台本模式跳过 STEP2 时 STEP3 仍可正常运行；混合场景下（部分文件经审校、部分未审校）也可正确处理。

第四步 review_final.py 将全篇字幕（日中对）打包为一个整体发送给 DeepSeek，同时从宏观和微观两个角度进行终审。宏观检查关注角色称呼是否统一、上下文是否连贯、语气风格是否一致、文化概念是否适配。微观检查关注日文假名残留、主语被动态是否颠倒、术语是否统一、是否有 API 拒绝消息等非台词文本混入。系统提示词中的上下文推断能力经过泛化优化：V3.0 要求 AI 先判断作品题材和舞台设定，再以此为基准检查不合理词汇；误听案例保留但标注为"仅供理解，不可套用"；新增通用的误听模式分类（同音异义词、场景矛盾词、拟声词混淆、专有名词误识别），适用于任何题材。终审支持跨文件并行。

第五步 validate_final.py 是纯规则扫描，不调用任何 AI，几秒钟跑完。检查所有 _final.srt 文件中是否存在空译文、裸编号残留（如 [41]）、已知的 API 安全拒绝模板、省略标记（如「（中略）」「（省略）」「〔認識不良〕」「低信頼度」）、以及日语原文栏中残留的 AI 审校注释。V3.3 起移除中文译文中的日文假名残留检查（误报率高且实际意义有限）。发现严重问题后打印详细清单，返回非零退出码终止流水线。

---

## 注意事项

API Key 安全：不要把包含真实 API Key 的脚本发给别人。分享前务必将 run_all.py 中的密钥替换为占位文字。

内容审查：本工具的转写（Whisper）和翻译（DeepSeek）均在本地或 API 端执行，无内容过滤。但将字幕上传到其他平台（如 B 站、YouTube）时仍需遵守对应平台的社区准则。智谱联网搜索存在内容审查机制，处理 NSFW 内容时建议关闭搜索功能。

模型选择：large-v3 加 large-v3-turbo 双模型融合是目前日语识别的最优方案。如果显存不足，低于 6GB，可以只使用 large-v3-turbo。

时长限制：单次转写超过 3 小时的音频可能导致显存不足。建议用 ffmpeg 预先切割成 1 小时以内的片段。极短音频，低于 30 秒，转写效果可能不佳，因为 Whisper 对极短语音的上下文判断能力有限。

费用说明：DeepSeek 按量计费，1 小时 ASMR 音频全程约 0.18 到 0.35 元人民币，视话密度和是否开启搜索。每个脚本跑完后自动打印精确费用报告，包含缓存命中率和按缓存命中与未命中分别计算的真实输入费用。智谱搜索约每次 0.0005 元，同一关键词缓存命中后不重复计费。全篇上下文审校会额外增加一次 API 调用，约 0.01 到 0.03 元。V3.0 并行处理不增加额外费用，只是缩短等待时间。

版本更新：以实际源码为准，部分小更新可能不会同步更新本文档。

NSFW 相关：本项目处理的内容可能包含成人向音频，请使用者遵守所在地法律法规。

---

## 常见问题

Q：提示 cublas64_12.dll is not found。
A：在激活虚拟环境后运行 pip install nvidia-cublas-cu12 nvidia-cudnn-cu12。

Q：提示 CUDA out of memory。
A：显存不够。修改 ensemble_transcribe.py 中的 get_whisper_model 函数，将 device 改为 cpu，或只用 large-v3-turbo 单模型。

Q：终端输出交错混乱，不同文件的批次信息混在一起。
A：V3.0 并行处理时每行输出带文件名前缀（如 [文件名]），区分不同文件。完整有序的输出在 pipeline.log 中。

Q：翻译质量不行怎么办。
A：确保 ENABLE_SEARCH 为 True 开启联网搜索。检查智谱 API Key 是否有效，无效时搜索会自动关闭但有警告提示。如果是特定术语翻译不准，可以手动编辑 _final.srt。如果某条字幕出现明显误译，可以沿着文件链逐级对比排查：_final.srt 到 _zh.srt 到 _reviewed.srt 到 _ensemble.srt 到 _v3.srt 和 _turbo.srt，追踪错误是从哪个环节引入的。

Q：想要纯中文不要中日双语怎么办。
A：在 run_all.py 中把 STEP6_STRIP 设为 True，流水线会自动输出 _cn_only.srt。如果还需要去掉 _cn_only 后缀，运行 python rename_suffix.py _cn_only。

Q：Mac 能用吗。
A：需要用 brew 安装 FFmpeg，brew install ffmpeg，其他步骤基本相同。但 GPU 加速在 Mac 上支持有限，M 系列芯片可用 MPS 后端。建议修改 ensemble_transcribe.py 中的 get_whisper_model 函数，将 device 改为 cpu 或 auto。

Q：自动化验证报错了怎么办。
A：validate_final.py 会在终审后自动扫描所有 _final.srt 文件。如果发现「API 残留」这类严重问题，说明 DeepSeek 在处理某条字幕时触发了安全拒绝，该条译文需要手动补译。如果发现「内容省略」警告，说明字幕中存在省略标记，该处可能有内容丢失，需要追溯到上游产物检查。

Q：纯触发音或极短音频能处理吗。
A：可以，但转写质量可能不佳。Whisper 对纯环境音（如掏耳、敲击）可能产生碎片化假阳性输出或者几乎无输出。日语二审和全篇上下文审校会尝试修正这些问题，但效果受限于原始音频质量。如果整段音频没有台词，成品字幕可能只有少数条目或完全为空，这属于正常现象而非脚本故障。

Q：如何调整并行数。
A：在 run_all.py 配置区修改 MAX_WORKERS。默认 10，DeepSeek API 并发限制为 500，可以放心调大。数值越大并行度越高、耗时越短，但对本地网络带宽和 CPU 调度压力也越大。

---

## 性能参考

以下数据基于 RTX 4060，8GB 显存，搭配 32GB RAM，处理 3 个 1 小时 ASMR 音频（每个约 300 条字幕），关闭搜索功能。

V3.1 并行处理（MAX_WORKERS=10）：

| 步骤 | 耗时 | 费用 |
|------|------|------|
| Whisper 双模型转写（阶段1，串行 GPU 独占） | 约 30 分钟（3 文件） | 0 元 |
| DeepSeek 融合（阶段2，跨文件并行） | 约 2 分钟 | 约 0.15 到 0.24 元 |
| 日语 AI 审校（跨文件并行） | 约 4 分钟 | 约 0.24 到 0.39 元 |
| 翻译加逐段审校（跨文件并行） | 约 6 分钟 | 约 0.30 到 0.45 元 |
| 全篇终审（跨文件并行） | 约 2 分钟 | 约 0.09 到 0.15 元 |
| 自动化验证 | 约 2 秒 | 0 元 |
| 合计 | 约 44 分钟 | 约 0.78 到 1.23 元 |

对比 V3.0：ensemble 阶段从约 40 分钟降至约 32 分钟（转写 30 分钟 + 并行融合 2 分钟），节省约 8 分钟。原 V3.0 中每个文件转写完后再串行调用融合，文件间还有 5 秒固定等待；V3.1 把所有融合请求攒到阶段2一次性并发发送，网络等待被 MAX_WORKERS 摊薄。

对比 V2.0（全串行处理）：3 个文件约 82 分钟，总费用相同。文件越多 V3.1 的并行优势越明显。

单个文件（1 小时音频）的耗时和费用与 V3.0 基本一致：约 25 分钟，约 0.21 到 0.35 元（单文件时两阶段分离无明显收益）。纯触发音（台词极少）的作品耗时和费用都会更低，话密的剧情向作品耗时和费用略高。开启搜索后翻译时长将大幅增加。

---

## 故障排除

详见开发者文档

---

## 致谢

本项目依赖以下优秀的开源项目和服务：

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — OpenAI Whisper 模型的 CTranslate2 重写版，提供本地 GPU 语音转写能力（MIT 协议）
- [DeepSeek](https://platform.deepseek.com) — 提供 DeepSeek-V4-Pro 大语言模型 API，用于翻译、审校和终审
- [智谱 AI](https://open.bigmodel.cn) — 提供 GLM-4-Flash 模型 API，用于可选的联网搜索功能
- [FFmpeg](https://ffmpeg.org) — 音视频解码，Whisper 读取音频的底层依赖
- [librosa](https://librosa.org) — 音频分析库，用于音频预处理中的重采样与响度归一化（ISC 协议）
- [noisereduce](https://github.com/timsainb/noisereduce) — 稳态噪声抑制算法，用于音频预处理中的降噪（MIT 协议）

---

## AI 参与说明

本项目的所有 Python 脚本和文档初稿均由 deepseek-v4-pro(V2.0及以前)和GLM-5.2(V3.0至V3.4.1)根据人类作者的设计要求生成。人类作者负责：定义项目目标和应用场景、选定技术路线和架构方案、确定所有关键参数、测试和验证输出质量、以及做出发布和许可证决策

---

## 许可证

本项目采用 GNU General Public License v3.0 or later (GPL-3.0-or-later) 协议发布。

你可以自由地使用、修改和分发本软件。但必须保留原作者署名，且衍生作品必须以相同的 GPL-3.0-or-later 协议发布。

Copyright (c) 2025-2026 Kochiya3309
