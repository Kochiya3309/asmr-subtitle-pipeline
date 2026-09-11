# ASMR 字幕自动生成流水线 — 开发者文档（当前开发树）

[English](developer-guide.md) | 简体中文

本文描述当前开发树的实现，不承担版本历史职责；已发布和未发布变更见[更新日志](../CHANGELOG.zh-CN.md)。安装、用户任务和配置含义分别见[纯新手使用指南](getting-started.zh-CN.md)、[高级操作](advanced-operations.zh-CN.md)与[配置参考](configuration.zh-CN.md)。

## 项目边界

本流水线从日语 ASMR 音频生成中日双语字幕。ASR、预处理、证据收集、救援转写和确定性时间轴在本地运行；LLM 调用只处理所需文本，可选搜索会把生成的查询词发送给已配置服务商。台本是辅助证据，不是逐字口语转写。

## 运行编排

`run_all.py` 加载 `.env`、快照活动输入路径、按顺序运行正式阶段，并向子进程传递受限环境变量。

| 阶段 | 入口 | 主要输入 → 输出 | 重跑 / 失败边界 |
| --- | --- | --- | --- |
| STEP-1 | `video_inputs.py` | `video/` → 受管理 FLAC 与来源 manifest | 来源、选中音轨或提取契约变化时重建；缺少音轨或基本名冲突会停止运行。 |
| STEP0 | `match_scripts.py` | 音频与台本 → 映射及可选拆分台本 | 音频/台本文本哈希变化会使确认失效；未确认或无效映射会失败关闭。 |
| STEP1 | `ensemble_transcribe.py` | 活动音频 → ASR、证据、融合与时间轴产物 | 输入、模型、配置或生成指纹变化时重建受影响产物；没有有效 ensemble SRT 会阻止下游。 |
| STEP2 | `review_japanese.py` | ensemble SRT → reviewed SRT | 输入或审校配置变化时重建；台本模式只审校 mismatch。 |
| STEP3 | `translate.py` | ensemble/reviewed SRT → 双语 `review/zh.srt` | 来源或翻译配置变化时重建；活动输入快照无效会停止本阶段。 |
| STEP3.25 | `script_units_shadow_stage.py` | 已确认台本与时间轴 → units、alignment 产物 | 正式过滤要求产物有效，否则失败；末尾影子分析只给出警告。 |
| STEP3.5 | `human_review_gate.py` | 双语草稿与音频 → 复核 SRT 和完成记录 | 当前输入必须与复核记录匹配；未提交复核返回可恢复的退出码 75。 |
| STEP4 | `review_final.py` | 当前双语来源 → final SRT 与 manifest | 来源或终审配置变化时重建；上游缺失或无效会停止运行。 |
| STEP5 | `validate_final.py` | final SRT → 规则验证报告 | 严重问题会使流水线失败；开启 STEP6 时会先尝试导出，再返回失败。 |
| STEP6 | `strip_japanese.py` | final SRT → 纯中文字幕 final SRT | 每次从当前 final 重新生成；提取失败会阻止转移副本导出。 |
| STEP7 | `export_final_srt.py` | final SRT → `_transfer/final/` 副本 | 依赖 STEP4；缺少 final 会导致导出失败。 |
| STEP8 | `export_cn_only_srt.py` | 纯中文字幕 final SRT → `_transfer/cn_only/` 副本 | 依赖 STEP6；缺少纯中文字幕 final 会导致导出失败。 |

所有正式下游阶段接收同一活动输入快照。视频派生 FLAC 会加入该快照而不复制到 `audio/`；历史输出任务不会混入新一轮运行。

## 阶段契约与失败边界

- STEP0 同时要求台本模式及其阶段开关。映射确认绑定音频和台本哈希；非交互运行默认失败关闭，除非显式开启自动确认。
- STEP1 保留候选证据。完全匹配台本使用台本文本与 large-v3 时间轴；mismatch 使用普通融合并进入 STEP2。救援仅限可疑窗口。
- STEP3 写入双语 `review/zh.srt`。后续终审的每条字幕必须严格包含四个物理行：编号、时间轴、单行日文和单行简体中文。
- 未提交人工复核时，STEP3.5 返回退出码 75；`run_all.py` 将其视为可恢复暂停。STEP4 前必须验证复核输入和输出仍匹配。
- STEP5 报告严重规则失败。启用 STEP6 时，流水线仍尝试生成纯中文字幕，并在导出路径后以失败状态结束。STEP6 提取失败会阻断转移导出。
- STEP7 依赖 STEP4，STEP8 依赖 STEP6；两者都不修改规范终稿。

## 模块职责

| 范围 | 模块 |
| --- | --- |
| 编排与通用服务 | `run_all.py`、`common.py`、`pipeline_inputs.py`、`pipeline_cache.py` |
| ASR、证据与时间轴 | `ensemble_transcribe.py`、`asr_evidence.py`、`asr_rescue.py`、`asr_timeline.py`、`long_cue_alignment.py` |
| 台本 | `match_scripts.py`、`script_text_io.py`、`script_mapping_state.py`、`script_units.py`、`script_alignment.py`、`script_units_shadow_stage.py` |
| 审校 | `review_japanese.py`、`translate.py`、`human_review.py`、`review_app.py`、`human_review_gate.py`、`review_final.py`、`validate_final.py`、`strip_japanese.py` |

## 配置来源

`run_all.py` 加载 `.env`；已存在的进程环境变量优先。面向用户的配置含义、默认值、依赖和隐私影响由[配置参考](configuration.zh-CN.md)维护。

`RESCUE_WINDOW_SECONDS`、`RESCUE_OVERLAP_SECONDS`、`INITIAL_PROMPT`、`HOTWORDS` 与 `VAD_*` 当前由 `run_all.py` 设置并注入子进程；只在 `.env` 中设置不会覆盖实际值。

## 关键实现契约

### 双 ASR、证据与时间轴

缓存需要重建时，STEP1 会运行 `Systran/faster-whisper-large-v3` 和 `mobiuslabsgmbh/faster-whisper-large-v3-turbo`。无台本输出融合两个模型；完全匹配台本保留台本文本并使用 large-v3 时间轴，Turbo 仍作为独立证据。不能恢复“因为有台本就跳过 Turbo”的旧行为。

幻觉隔离只从自动融合输入中排除高精度固定模板，原始候选仍保留在证据产物中供审计。救援先在可疑窗口对 large-v3 关闭 VAD 转写，只有 V3 检测到可能语音时才追加 Turbo 对照；不要将其改成全音频的无 VAD Turbo 扫描。

`ENABLE_DETERMINISTIC_TIMELINE=1` 时，LLM 返回文本选择和证据 ID，`asr_timeline.py` 负责顺序、重叠和边界。没有有效证据的文本不得进入正式时间轴。长字幕 `apply` 模式只提交本地校验通过的边界方案；`shadow` 模式只写审计报告。

### 人工复核与台本处理

复核页面绑定 `127.0.0.1`，展示上下文和候选差异，并允许编辑文本与字幕时间。提交会写入 `review/human_reviewed.srt` 和 `review/human_review.json`，其完成记录绑定当前输入和输出。

台本分割必须保留台词、动作、心理、标题和装饰分隔符。LLM 仅负责分类和文本判断；确定性代码维护原始 span、分割边界和时间轴。正式台本复核过滤同时需要台本模式、人工复核和 `ENABLE_SCRIPT_REVIEW_FILTER`；正式阶段结束后的影子分析不阻塞。

## 输出、缓存与迁移

```text
output/
  <音频名>/
    input/     # 视频派生 FLAC 与来源 manifest（按需）
    asr/       # 预处理音频、v3/turbo SRT、manifest
    evidence/  # candidates、alignment、windows、decisions、rescue、manifest
    review/    # 融合、审校、翻译、人工复核、时间轴产物
    final/     # <音频名>_final.srt、final_manifest.json、可选纯中文字幕
    script/    # 可选 units 与 alignment 产物
  _shared/     # 日志、台本映射、分割台本、迁移备份
  _transfer/   # 派生的批量转移副本
```

`output_layout.py` 统一管理路径。缓存有效性依赖内容哈希、配置与生成指纹、产物类型、schema 版本和完成状态。可安全重建的过期产物会在替换前备份；来源不可验证时下游处理停止。迁移保留已知产物内容和时间戳、保留未知用户文件并拒绝冲突目标。面向用户的迁移与导出步骤见[高级操作](advanced-operations.zh-CN.md)。

### 产物身份

| 类别 | 典型产物 |
| --- | --- |
| 原始 ASR | `asr/v3.srt`、`asr/turbo.srt` 及 manifest |
| 证据 | `evidence/candidates.json`、`alignment.json`、`windows.json`、`decisions.json`、`rescue_windows.json` |
| 融合 | `review/fusion_timeline.json`、`review/ensemble.srt`、融合 manifest |
| 下游字幕 | `review/reviewed.srt`、`review/zh.srt`、`review/human_reviewed.srt`、最终 SRT |
| 人工复核 | `review/human_review.json` |
| 日志 | `_shared/pipeline.log` |

## 隐私与外部服务

音频不会上传到 LLM 或搜索服务商。日文和中文字幕文本可能发送给配置的 OpenAI 兼容 LLM。`ENABLE_SEARCH=0` 时不会调用 Tavily 或 Exa。人工复核服务只绑定 `127.0.0.1`。

## 排障索引

| 现象 | 首先检查 | 再决定 |
| --- | --- | --- |
| 流水线中断或以非零状态退出 | `output/_shared/pipeline.log`；确认首个失败阶段与活动输入 | 修正该阶段报告的原因后重跑；不要先删除缓存。 |
| 终审拒绝双语 SRT | 同一字幕条目的 `review/ensemble.srt`、`review/reviewed.srt` 与 `review/zh.srt` | 在修改下游文件前，确认四个物理行契约。 |
| 缓存意外重建 | 对应 manifest、源内容以及配置或生成指纹 | 找出哪项记录依赖发生变化；文件存在本身不代表缓存有效。 |
| 台本映射或人工复核阻塞进度 | `_shared/script_mapping.json`、其确认状态或 `review/human_review.json` | 确认当前输入与记录一致；未提交复核是可恢复的退出码 75 暂停。 |
| 视频输入在 ASR 前停止 | 视频第一条音轨与该任务的 `input/source_manifest.json` | 解决缺少音轨或基本名冲突后再重跑。 |
| 缺少转移副本 | `final/` 中的规范 SRT，以及 STEP4/STEP6、STEP7/STEP8 开关 | 恢复缺失的上游终稿；需要单独导出时再使用导出辅助脚本。 |

## 开发与验证

Python 检查使用项目虚拟环境。提交前应编译相关 Python 文件并运行 `git diff --check`；存在针对性内部测试时运行它们。运行时验证必须使用隔离的音频和输出目录。临时测试与诊断放在 `_cache/`，不得在日志、测试夹具或文档中暴露密钥。

发布前一致性检查包括双语导航、命令和配置值一致、`.env.example` 不含真实密钥、依赖一致、敏感信息和媒体未进入待提交内容，以及相关语法或针对性测试。

## 许可证

GNU 通用公共许可证 v3.0 或更高版本（`GPL-3.0-or-later`）。详见 [LICENSE](../LICENSE)。
