# ASMR 字幕自动生成流水线 — 开发者文档（Unreleased）

本文描述当前开发树的真实实现。面向使用者的安装与配置说明见 [README.zh-CN.md](../README.zh-CN.md)，英文主文档见 [README.md](../README.md)。

## 项目边界

本项目把日语 ASMR 音频转换为中日双语 SRT：本地 `large-v3` 与 `large-v3-turbo` 负责 ASR，OpenAI 兼容 LLM 负责文本融合、日文审校、翻译和终审。本地确定性代码负责证据绑定、时间轴、缓存、规则验证和人工复核门控。

设计原则：

- ASR 候选、置信指标、来源和救援结果可审计；可疑文本优先隔离，不直接销毁证据。
- LLM 可以判断文本，不应凭空制造时间戳；启用确定性时间轴后，边界只由本地代码生成。
- 每个可复用产物必须绑定输入内容与生成配置，不能把“文件存在”视为缓存有效。
- 台本是辅助证据，不等于逐字语音稿；动作、心理描写和装饰符号必须保留但不能当作口语。
- 人工复核必须看到上下文、日文、罗马音、中文和候选差异，并能直接修改时间边界。

## 运行编排

`run_all.py` 是统一入口，当前逻辑顺序如下：

| 阶段 | 入口 | 条件与职责 |
| --- | --- | --- |
| STEP0 | `match_scripts.py` | `ENABLE_SCRIPT=1` 且 `STEP0_MATCH_SCRIPTS=1`；匹配、分割并确认台本映射 |
| STEP1 | `ensemble_transcribe.py` | 预处理、双 ASR、证据/救援、融合、可选长字幕定时 |
| STEP2 | `review_japanese.py` | 无台本时运行；台本模式只处理 mismatch，完全匹配时跳过 |
| STEP3 | `translate.py` | 翻译及逐段审校，输出双语 SRT |
| STEP3.25 | `script_units_shadow_stage.py` | 正式台本复核过滤启用时，分类台本结构并对齐时间轴 |
| STEP3.5 | `human_review_gate.py` | 人工复核启用时启动本地浏览器页面；未提交则以退出码 75 暂停 |
| STEP4 | `review_final.py` | 读取正确的上游来源，执行全篇中日双语终审 |
| STEP5 | `validate_final.py` | 扫描内容残留和规则问题；当前不做完整时间轴验证 |
| STEP6 | `strip_japanese.py` | 生成可选纯中文字幕 |

所有正式阶段完成后，`ENABLE_SCRIPT_UNITS_SHADOW=1` 可再运行非阻塞台本结构旁路。旁路超时不会使已完成的正式字幕失败。

## 完整阶段参考

以下说明以 `run_all.py` 编排为准。除 STEP3.5 的“等待人工提交”使用退出码 75 表示可恢复暂停外，正式阶段返回非零状态都会终止本轮流水线；重新运行时由各阶段自行验证缓存并续跑。

### STEP0：台本匹配与分割

- **执行条件**：`ENABLE_SCRIPT=1` 且 `STEP0_MATCH_SCRIPTS=1`。关闭台本模式时整步跳过。
- **输入**：`AUDIO_DIR` 中的活动音频、`SCRIPT_DIR` 中的文本台本，以及已有映射和确认标记。
- **处理与外部服务**：先执行同名和模糊文件名匹配；总台本优先用本地规则分割，规则无法可靠分割时调用 LLM。未匹配文件也可由 LLM 根据文件名判断。音频内容不会上传，可能发送的是文件名和台本文本。
- **输出**：`script_mapping.json`、`script_mapping.verified`，以及需要分割时的 `scripts_split/*.txt`。
- **缓存与失效**：确认标记绑定映射、活动音频和台本内容指纹。相关内容变化后旧确认失效并重新匹配；`SCRIPT_FORCE_RESPLIT=1` 会强制重建拆分结果。
- **停止条件**：缺少音频、文件基本名冲突、映射无效或用户拒绝确认都会失败。非交互终端默认拒绝冒充人工确认，只有 `SCRIPT_AUTO_VERIFY=1` 才会自动写入确认标记。

### STEP1：预处理、双 ASR、证据与融合

- **执行条件**：`STEP1_ENSEMBLE=1`，默认开启。无论有无台本，只要相应缓存失效，双 ASR 都会执行。
- **输入**：活动音频；台本模式还读取已确认的 `script_mapping.json` 与对应台本。
- **处理与外部服务**：本地完成可选音频预处理、large-v3 与 large-v3-turbo 串行 GPU 转写、幻觉隔离、定向救援及时间轴计算；随后由 LLM 并行选择或融合文本。台本完全匹配时使用 large-v3 时间轴与台本文本，mismatch 时回退普通双模型融合。启用搜索时，融合阶段可把查询词发送给 Tavily/Exa，但音频始终留在本地。
- **输出**：预处理音频及 manifest、`*_v3.srt`、`*_turbo.srt`、ASR 证据/窗口/决策/救援 JSON、`*_fusion_timeline.json`、`*_ensemble.srt`、融合 manifest、可选 `*_long_cue_alignment.json`，以及本轮 `script_mismatch.json`。
- **缓存与失效**：预处理、ASR、证据和融合分别绑定源音频、实际输入音频、模型/参数、prompt、LLM 配置、搜索开关和输出指纹。可安全重建的过期产物先备份；无 manifest 或来源无法验证的旧融合产物不会被静默覆盖，也不会放行下游。
- **停止条件**：缺少音频、去掉扩展名后重名、台本映射未确认，或任一活动音频最终没有有效 `*_ensemble.srt` 时失败。长字幕局部定时属于 fail-closed 旁路：失败时保留已经有效的融合字幕，不单独拖垮 STEP1。

### STEP2：日文二审

- **执行条件**：`STEP2_REVIEW_JP=1`。无台本模式处理全部活动融合字幕；台本模式仅在 `script_mismatch.json` 非空或状态无法验证时运行，并只审校 mismatch 文件。完全匹配时由 `run_all.py` 跳过。
- **输入**：`*_ensemble.srt`，台本模式还读取 `script_mismatch.json`。
- **处理与外部服务**：LLM 先分批修订日文，再结合全篇上下文复查。启用搜索时，全篇复查可调用 Tavily/Exa；批处理和单条兜底不搜索。
- **输出**：`*_reviewed.srt` 与 `*_reviewed_manifest.json`。
- **缓存与失效**：manifest 绑定输入融合字幕、LLM/批次配置、搜索开关和输出内容。输入或生成配置变化时，旧产物先备份再重建。
- **停止条件**：找不到融合字幕时返回非零状态。单个 LLM 批次失败时使用原日文兜底，全篇复查失败时保留分批结果，因此这类 API 失败会降级质量但不会自动使整步失败。

### STEP3：翻译与逐段审校

- **执行条件**：`STEP3_TRANSLATE=1`，默认开启。
- **输入**：以每个 `*_ensemble.srt` 为索引；仅当 `*_reviewed_manifest.json` 证明 `*_reviewed.srt` 与当前融合字幕一致时，才优先使用二审结果，否则回退到 `*_ensemble.srt`。
- **处理与外部服务**：LLM 先分批日译简中，再分批审校译文；长句由本地约束进行安全拆分。初译与单条兜底不搜索，启用搜索时只有译文审校可调用 Tavily/Exa。
- **输出**：中日双语 `*_zh.srt` 与 `*_zh_manifest.json`。
- **缓存与失效**：manifest 绑定实际采用的日文输入、LLM/批次配置、搜索开关和输出内容；上游来源切换也会使缓存失效。旧产物先备份再重建。
- **停止条件**：没有融合字幕时返回非零状态。翻译调用返回空结果时先保留空占位供审校兜底，批次抛出异常时改用日文原文占位，审校批次异常时保留初译；因此部分 API 失败可能生成需要后续检查的降级结果，而不会自动使整步失败。输入快照或原子提交校验失败则会抛错终止。

### STEP3.25：台本结构分类与审核对齐

- **执行条件**：正式门控仅在 `ENABLE_SCRIPT=1`、`ENABLE_HUMAN_REVIEW=1` 和 `ENABLE_SCRIPT_REVIEW_FILTER=1` 同时成立时运行。`ENABLE_SCRIPT_UNITS_SHADOW=1` 则在正式流水线完成后另跑非阻塞影子分析；两者不是同一种失败策略。
- **输入**：当前已确认台本、活动音频与 `*_fusion_timeline.json`。
- **处理与外部服务**：LLM 将原台本分类为 dialogue/action/psychology/separator/unknown；本地代码保留原始跨度，并把可说话单元确定性对齐到 ASR 时间窗。
- **输出**：`*_script_units.json` 与 `*_script_alignment.json`。
- **缓存与失效**：产物绑定台本原文、分类配置、音频来源、时间轴和上游 sidecar 指纹。任一来源变化后不得复用。
- **停止条件**：正式门控要求每个活动音频都有当前有效的分类和对齐产物，缺失、失败或超时会终止流水线；末尾影子分析的失败或超时只记录警告，不影响已完成字幕。

### STEP3.5：本地人工复核门控

- **执行条件**：`ENABLE_HUMAN_REVIEW=1` 且 `STEP35_HUMAN_REVIEW=1`。默认关闭。
- **输入**：每个活动音频对应的 `*_zh.srt`、源音频，以及可用的 ASR/时间轴/台本 sidecar。
- **处理与外部服务**：启动仅监听 `127.0.0.1` 的浏览器页面，供人工听音、查看上下文和候选差异，并编辑文本、起止时间、拆分或合并字幕。该阶段自身不调用 LLM 或搜索服务。
- **输出**：`*_human_reviewed.srt` 与 `*_human_review.json`。
- **缓存与失效**：完成记录绑定审核 bundle、当前双语输入和审核后输出。任一内容变化都会使旧审核失效；重新提交时旧产物会先备份。
- **暂停与停止**：页面未提交或用户中断时返回退出码 75，`run_all.py` 将其视为可恢复暂停而不是失败。缺少 STEP3 产物、提交内容非法或完成记录不匹配时失败关闭。

### STEP4：全篇终审

- **执行条件**：`STEP4_FINAL=1`，默认开启。
- **输入**：人工复核关闭时读取 `*_zh.srt`；开启时只接受与当前输入匹配的 `*_human_reviewed.srt` 和完成记录，不允许静默绕过人工修改。
- **处理与外部服务**：LLM 结合全篇上下文检查中日语义、称呼、语气和一致性，并提交需要修正的条目。启用搜索时可调用 Tavily/Exa。
- **输出**：`*_final.srt` 与 `*_final_manifest.json`。
- **缓存与失效**：manifest 绑定实际上游文件名和内容、LLM 配置、搜索开关及最终输出。来源或配置变化后原子替换并保留旧版备份。人工复核关闭时，旧版无 manifest 的 final 为避免误覆盖会暂时保留，而不是声明其已验证有效。
- **停止条件**：缺少双语字幕、人工复核结果缺失/过期，或任一终审任务失败时返回非零状态，并保留已有 final。

### STEP5：规则验证

- **执行条件**：`STEP5_VALIDATE=1`，默认开启。
- **输入**：全部 `*_final.srt`。
- **处理与外部服务**：纯本地按字幕块扫描 API/裸序号残留、纯标点、空译文、日语缺失、审校注释、内容省略及低置信度标记，不调用 LLM 或搜索。当前实现只对可解析的字幕块做内容规则检查，不负责完整的时间轴合法性和连续性验证。
- **输出**：终端和 `pipeline.log` 中的验证报告；不生成新的字幕文件，也没有独立缓存。
- **停止条件**：严重问题返回非零状态并阻止 STEP6；只有警告时报告后继续。找不到 final 也会失败。

### STEP6：导出纯中文字幕

- **执行条件**：`STEP6_STRIP=1`，默认开启。
- **输入**：全部 `*_final.srt`。
- **处理与外部服务**：纯本地移除每条字幕的日文部分、保留原时间轴和中文并重新编号，不调用外部服务。
- **输出**：`*_cn_only.srt`。
- **缓存与失效**：当前没有 manifest；每次执行都会按当前 final 重新生成并覆盖对应纯中文字幕。它是派生文件，权威终稿仍是 `*_final.srt`。
- **停止条件**：找不到 final 时失败；单个文件无法提取中文时会报告失败并继续处理其余文件，但当前实现不会仅因该文件失败而返回非零状态。

## 模块职责

### 编排与通用服务

- `run_all.py`：加载 `.env`、组合阶段开关、传递子进程环境并处理暂停。
- `common.py`：LLM 客户端、重试、Function Calling、搜索和公共 SRT/日志工具。
- `pipeline_inputs.py`：解析输入、输出目录及支持的音频扩展名。
- `pipeline_cache.py`：SHA-256 指纹、manifest 验证、原子写入和旧产物备份。

### ASR、证据与时间轴

- `ensemble_transcribe.py`：STEP1 主编排、模型生命周期、缓存及融合提交。
- `asr_evidence.py`：规范化候选、窗口、来源、指标和审计 manifest。
- `asr_rescue.py`：生成有边界的救援计划并合并救援证据。
- `asr_timeline.py`：确定性候选对齐、决策和时间轴解析。
- `long_cue_alignment.py`：对异常长字幕生成并验证局部边界方案。

### 台本

- `match_scripts.py`：同名/模糊匹配、总台本分割和人工或自动确认。
- `script_text_io.py`：UTF-8/BOM、CP932/Shift-JIS 与受校验混合编码读取。
- `script_mapping_state.py`：将确认标记绑定到音频和台本内容指纹。
- `script_units.py`：分类 dialogue/action/psychology/separator/unknown，并保留原始跨度。
- `script_alignment.py`：把可说话台本单元确定性对齐到 ASR 时间窗。
- `script_units_shadow_stage.py`：正式 STEP3.25 或流水线末尾影子分析入口。

### 审校与人工复核

- `review_japanese.py`：分批日文二审与全篇上下文审校。
- `translate.py`：日译简中、逐段审校及长句安全拆分。
- `human_review.py`：生成审核项、候选差异、罗马音和输入指纹。
- `review_app.py`：仅绑定 `127.0.0.1` 的浏览器 UI 与提交接口。
- `human_review_gate.py`：验证复核输入/输出是否当前有效，决定继续或暂停。
- `review_final.py`：全篇终审及输入边界检查。
- `validate_final.py`、`strip_japanese.py`：规则验证与纯中文导出。

## STEP1 关键路径

### 双 ASR 与台本行为

只要缓存需要重建，无论是否存在台本，STEP1 都会运行：

- `Systran/faster-whisper-large-v3`
- `mobiuslabsgmbh/faster-whisper-large-v3-turbo`

无台本模式以双模型证据进行融合。台本完全匹配时，正式结果使用 large-v3 时间轴与台本文本，Turbo 仍作为独立证据；台本 mismatch 时回退到普通融合和后续日文二审。不要再实现“台本模式跳过 Turbo”的旧逻辑。

### 幻觉隔离与救援

`ENABLE_HALLUCINATION_FILTER=1` 仅处理命中高精度固定模板且缺少独立支持的候选。原始候选仍保存在证据文件中，以便人工追溯。

`ENABLE_ASR_RESCUE=1` 只扫描可疑的有界窗口：先以 large-v3 无 VAD 转写；仅当 V3 给出可能语音时，才追加 Turbo 对照。禁止对整段音频执行 Turbo 无 VAD 全局扫描，因为轻语/噪声场景容易产生大量固定幻觉。

### 时间轴所有权

`ENABLE_DETERMINISTIC_TIMELINE=1` 时，LLM 返回文本选择和 evidence ID；`asr_timeline.py` 根据候选证据确定顺序、覆盖关系与边界。缺少有效证据的文本不得直接进入正式时间轴。

`ENABLE_LONG_CUE_ALIGNMENT=1` 处理超过 `LONG_CUE_MAX_SECONDS` 的异常长字幕：

- `LONG_CUE_ALIGNMENT_MODE=apply`：只提交通过本地约束的边界方案。
- `LONG_CUE_ALIGNMENT_MODE=shadow`：只输出审计报告，不改 SRT。
- 正常匹配的台本模式跳过该旁路；无台本或台本 mismatch 回退结果才会进入。

## 人工复核契约

人工复核默认关闭。启用后，页面必须：

- 展示上一条、当前条、下一条的中日双语上下文；
- 展示并高亮当前日文、候选日文及其罗马音差异；
- 提供手动播放/暂停，不自动循环异常片段；
- 支持修改起止时间、拆分字幕和与下一条合并；
- 默认折叠已知固定幻觉候选，减少无效审核量。

提交后生成 `*_human_reviewed.srt` 和 `*_human_review.json`。完成标记绑定当前输入和输出；内容变化后旧审核不能复用。启用人工复核时，STEP4 不得绕过缺失、过期或不匹配的审核结果。

## 台本处理契约

台本模式当前不是自动探测：只有 `ENABLE_SCRIPT=1` 才会读取 `scripts/`。未来可考虑 `SCRIPT_MODE=auto|off|required`，但当前代码没有该接口。

STEP0 支持一个发行商总台本对应多个音频。分割与后续结构化必须保留所有原始内容，包括台词、人物动作、心理描写、章节标题和装饰分隔符。LLM 只负责分类/文本判断；分段边界、原始跨度和时间轴由确定性代码维护。

`script_mapping.json` 的确认状态绑定音频与台本 SHA-256。交互终端可人工确认；非交互环境默认 fail-closed，只有明确设置 `SCRIPT_AUTO_VERIFY=1` 才能自动写入确认标记。

## 配置来源

`.env` 由 `run_all.py` 加载，现有进程环境变量优先。布尔值支持 `0/1`、`false/true`、`no/yes`、`off/on`。

主要开关：

```dotenv
ENABLE_SEARCH=0
ENABLE_SCRIPT=0
STEP0_MATCH_SCRIPTS=0
ENABLE_AUDIO_PREPROCESS=1
ENABLE_ASR_EVIDENCE=1
ENABLE_HALLUCINATION_FILTER=1
ENABLE_ASR_RESCUE=1
ENABLE_DETERMINISTIC_TIMELINE=0
ENABLE_LONG_CUE_ALIGNMENT=1
LONG_CUE_ALIGNMENT_MODE=apply
LONG_CUE_MAX_SECONDS=15
ENABLE_HUMAN_REVIEW=0
HUMAN_REVIEW_PORT=8765
STEP1_ENSEMBLE=1
STEP2_REVIEW_JP=1
STEP3_TRANSLATE=1
STEP35_HUMAN_REVIEW=1
STEP4_FINAL=1
STEP5_VALIDATE=1
STEP6_STRIP=1
```

`RESCUE_WINDOW_SECONDS`、`RESCUE_OVERLAP_SECONDS`、`INITIAL_PROMPT`、`HOTWORDS` 和 `VAD_*` 目前在 `run_all.py` 中硬编码并注入子进程。仅在 `.env` 中设置同名变量不会覆盖它们；若要开放配置，必须先修改 `run_all.py` 的读取逻辑和缓存 generation fingerprint，并补测试。

`STEP35_HUMAN_REVIEW` 只是子开关；只有同时设置 `ENABLE_HUMAN_REVIEW=1` 才会执行。类似地，STEP0 还受 `ENABLE_SCRIPT` 约束，STEP3.25 同时受台本、人工复核和 `ENABLE_SCRIPT_REVIEW_FILTER` 约束。

## 产物与缓存

主要正式产物：

| 类别 | 典型文件 |
| --- | --- |
| 原始 ASR | `*_v3.srt`、`*_turbo.srt` 及对应 `*_manifest.json` |
| ASR 审计 | `*_asr_candidates.json`、`*_asr_alignment.json`、`*_asr_windows.json`、`*_asr_decisions.json`、`*_asr_rescue_windows.json`、`*_asr_manifest.json` |
| 融合 | `*_fusion_timeline.json`、`*_ensemble.srt`、`*_fusion_manifest.json`、`*_script_fusion_manifest.json` |
| 长字幕 | `*_long_cue_alignment.json` |
| 下游字幕 | `*_reviewed.srt`、`*_zh.srt`、`*_human_reviewed.srt`、`*_final.srt`、`*_cn_only.srt` |
| 人工复核 | `*_human_review.json` |
| 台本结构 | `*_script_units.json`、`*_script_alignment.json` |
| 日志 | `pipeline.log` |

缓存有效性由输入/输出 SHA-256、配置指纹、generation fingerprint、artifact type、schema version 和完成状态共同决定。音频、模型、VAD、prompt、上游 SRT、LLM 配置或证据变化时，依赖产物必须失效。

写入规则：

- 大部分文本/JSON 通过临时文件与原子替换提交。
- 下游 SRT 先提交 manifest，再原子提交规范输出，并在提交前复核输入未变化。
- 缺少 manifest 的旧产物不会被默认为有效，也不会静默覆盖。
- 可安全重建的过期产物会先移入 `output/_review_backups/`；来源无法验证或覆盖风险高时停止下游。
- `_cache/` 仅保存测试、诊断和临时产物，已被 Git 忽略。

## 隐私与外部服务

- ASR 和音频预处理在本地执行，音频不会上传给 LLM 或搜索服务商。
- 日文/中文字幕文本会发送给配置的 OpenAI 兼容 LLM，用于融合、审校或翻译。
- `ENABLE_SEARCH=0` 时不调用 Tavily/Exa；启用后，只有实际触发搜索的审校调用才会把生成的查询词发给搜索服务商。
- 浏览器复核服务器只监听 `127.0.0.1`。
- `.env` 含密钥且被 Git 忽略；日志、文档和测试样本不得复制真实密钥。

## 开发与验证

当前直接依赖共七项：faster-whisper、openai、librosa、noisereduce、scipy、soundfile、pykakasi。项目不依赖 PyTorch；GPU 可用性以 CTranslate2 为准。

提交前至少运行：

```powershell
$files = Get-ChildItem -File *.py | ForEach-Object { $_.FullName }
venv\Scripts\python.exe -m py_compile $files
git diff --check
```

当前工作区保留内部回归测试时，可运行：

```powershell
venv\Scripts\python.exe -m unittest discover -s _cache\tests -p 'test_*.py' -q
```

这些内部测试和临时验证文件遵循项目规则留在 `_cache/`，不随 Git 发布；公开仓库中的必跑检查仍是语法检查和 `git diff --check`。

运行时验证必须通过 `AUDIO_DIR` 与 `OUTPUT_DIR` 指向隔离目录，所有 mock、诊断脚本和中间产物放入 `_cache/`。搜索相关修改需要使用测试密钥做真实 API 冒烟，但不得在命令输出、日志或提交中暴露密钥。

当前本地开发环境已验证：Windows venv Python 3.14.5、七个直接依赖可导入、`pip check` 通过、FFmpeg/ffprobe 可用、CTranslate2 检测到一张 CUDA GPU。README 仍以 Python 3.10/3.11 为推荐版本，3.14.5 只是当前环境实测结果。

## 发布前检查清单

- 中英文 README 标题层级、事实、命令和配置块保持对应。
- `.env.example` 只包含空密钥和真实可用的配置项。
- `requirements.txt`、README 与开发者文档中的依赖一致。
- 新增源码均在根目录，测试和调试产物均在 `_cache/`。
- `py_compile` 和 `git diff --check` 通过；若提交中包含正式测试，则对应测试套件也通过。
- `git status --short` 中不包含 `.env`、音频、模型、字幕、日志或复核备份。
- 未经明确要求不创建提交、不推送。

## 更新记录

### Unreleased

- 引入双 ASR 结构化证据、固定幻觉隔离、定向救援和确定性时间轴。
- 引入浏览器双语人工复核、罗马音提示和时间轴编辑。
- 引入异常长字幕局部定时与安全应用模式。
- 重做台本无损读取、映射验证、结构分类和时间轴对齐。
- 为 ASR、融合及下游字幕增加内容寻址 manifest 和原子提交边界。

完整版本历史见 [CHANGELOG.zh-CN.md](../CHANGELOG.zh-CN.md)。

## AI 参与说明

代码和文档由人类与 AI 迭代协作完成。架构、参数、听辨结论、验收标准和发布决定由人类作者确认；AI 协助实现、审查、测试和文档整理。

## 许可证

GNU 通用公共许可证 v3.0 或更高版本（`GPL-3.0-or-later`）。详见 [LICENSE](../LICENSE)。Copyright (c) 2025–2026 Kochiya3309。
