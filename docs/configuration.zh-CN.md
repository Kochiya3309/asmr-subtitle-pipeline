# 配置参考

[English](configuration.md) | 简体中文

将 `.env.example` 复制为 `.env` 后，只设置本次运行需要的值。`.env`
可能包含密钥：不要提交、分享、截图或附在问题报告中。`run_all.py` 会加载
`.env`；已存在的同名进程环境变量优先。所有开关请使用 `0` 或 `1`。首次运行请看
[Getting Started](getting-started.zh-CN.md)。

## API、模型与搜索

| 配置项 | 默认值 | 含义、依赖与风险 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 留空 | 当前 OpenAI 兼容 LLM 所需的密钥。仅当此项为空时，才会把 `DEEPSEEK_API_KEY` 作为旧版回退读取。 |
| `OPENAI_BASE_URL` | 留空时为 `https://api.deepseek.com` | OpenAI 兼容服务商的基础地址。切换服务商时应同时设置 `OPENAI_MODEL`。 |
| `OPENAI_MODEL` | 留空时为 `deepseek-v4-pro` | 服务商模型标识，必须受所选 endpoint 支持。 |
| `OPENAI_ENABLE_THINKING` | `0` | 只有所选服务商支持其非标准 thinking 参数时才设为 `1`；否则请求可能失败。 |
| `OPENAI_MAX_TOKENS` | 留空或无效时为 `131072` | 单次 LLM 响应的最大输出 token 数，必须在服务商与模型限制内。 |
| `TAVILY_API_KEY` | 留空 | 可选 Tavily 凭证；只在启用搜索时有用。 |
| `EXA_API_KEY` | 留空 | 可选 Exa 凭证；只在启用搜索时有用。 |
| `ENABLE_SEARCH` | `0` | 允许符合条件的审校调用已配置的搜索服务。生成的查询词可能离开本机；处理私密或 NSFW 内容时，除非确有查阅需要，应保持关闭。 |

音频预处理和 ASR 在本地运行。融合、日文审校、翻译和终审会把所需的字幕文本（而非音频）发送给配置的 LLM。`ENABLE_SEARCH=0` 时不会调用搜索服务商。

## 输入、输出与交付路径

`run_all.py` 会以项目根目录解析相对路径。

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `AUDIO_DIR` | `./audio` | 主音频输入目录。 |
| `VIDEO_DIR` | `./video` | 只有 `ENABLE_VIDEO_PREP=1` 时才扫描的视频输入目录。 |
| `OUTPUT_DIR` | `./output` | 输出和流水线缓存根目录。隔离测试请指向空目录。 |
| `SRT_TRANSFER_DIR` | `./output/_transfer` | `export_final_srt.py` 和 `export_cn_only_srt.py` 使用的交付副本根目录；副本会写入 `final/` 或 `cn_only/`。 |
| `ENABLE_VIDEO_PREP` | `0` | 扫描 `VIDEO_DIR` 顶层的 `.mp4`、`.mkv`、`.mov`、`.webm`、`.avi`，把每个可接受视频的第一条音轨作为受管理的 FLAC 输入。 |

视频派生的受管理输入写在 `output/<audio>/input/`。不要把该 FLAC 复制到 `AUDIO_DIR`：它由流水线的输入记录管理。

## 台本模式

| 配置项 | 默认值 | 含义与依赖 |
| --- | --- | --- |
| `ENABLE_SCRIPT` | `0` | 启用从 `SCRIPT_DIR` 读取台本。为 `0` 时不会自动检测台本。 |
| `STEP0_MATCH_SCRIPTS` | `0` | 执行台本匹配与合并台本拆分；仅 `ENABLE_SCRIPT=1` 时有效。 |
| `SCRIPT_DIR` | `./scripts` | 日文源台本目录。 |
| `SCRIPT_FALLBACK_FULL` | `1` | 匹配或拆分无法解析音频时，允许未使用的完整台本作为回退候选。它会增加误匹配风险，应复核映射。 |
| `SCRIPT_FORCE_RESPLIT` | `0` | 重建已验证的拆分台本缓存。仅在明确需要重新拆分时使用。 |
| `SCRIPT_AUTO_VERIFY` | `0` | 允许非交互任务确认 `script_mapping.json`，即不经交互复核就接受映射风险。 |

## ASR、证据与定时

| 配置项 | 默认值 | 含义与依赖 |
| --- | --- | --- |
| `ENABLE_AUDIO_PREPROCESS` | `1` | 在 ASR 前启用项目的 16 kHz 单声道预处理。修改会使受影响的预处理和 ASR 缓存失效。 |
| `ENABLE_ASR_EVIDENCE` | `1` | 保留模型候选、指标、来源、对齐和审计 manifest，供基于证据的处理使用。 |
| `ENABLE_HALLUCINATION_FILTER` | `1` | 将高精度固定幻觉模板从融合输入中隔离，不删除原始候选。依赖 `ENABLE_ASR_EVIDENCE=1`；关闭证据时无效。 |
| `ENABLE_ASR_RESCUE` | `1` | 仅对可疑窗口执行有边界的救援转写。依赖 `ENABLE_ASR_EVIDENCE=1`；不会启用整段音频的 Turbo 无 VAD 扫描。 |
| `ENABLE_DETERMINISTIC_TIMELINE` | `0` | 启用实验性的确定性时间轴解析，以选定文本和 evidence IDs 为依据。修改会使融合缓存失效。 |
| `ENABLE_LONG_CUE_ALIGNMENT` | `1` | 检查异常长字幕。STEP1 直接读取此项，因此只能使用 `0` 或 `1`。 |
| `LONG_CUE_ALIGNMENT_MODE` | `apply` | `apply` 仅提交通过本地校验的边界方案；`shadow` 只写审计报告，不修改 SRT。除此之外的值不受支持。 |
| `LONG_CUE_MAX_SECONDS` | `15` | 长字幕处理的正数时长阈值，单位为秒。 |

## 流水线阶段与人工复核

| 配置项 | 默认值 | 含义与依赖 |
| --- | --- | --- |
| `STEP1_ENSEMBLE` | `1` | 运行预处理、双 ASR、证据、救援、融合和适用的长字幕处理。 |
| `STEP2_REVIEW_JP` | `1` | 运行日文二审。在有效的台本模式中只审校 mismatch。 |
| `STEP3_TRANSLATE` | `1` | 运行日译简中和逐段审校。 |
| `ENABLE_HUMAN_REVIEW` | `0` | 启用本地浏览器人工复核门控。服务只绑定 `127.0.0.1`；未提交复核时流程暂停。 |
| `HUMAN_REVIEW_PORT` | `8765` | 本地复核服务端口。设为 `0` 时由系统选择可用端口。 |
| `STEP35_HUMAN_REVIEW` | `1` | 浏览器复核门控的子开关；仅 `ENABLE_HUMAN_REVIEW=1` 时运行。 |
| `ENABLE_SCRIPT_REVIEW_FILTER` | `0` | 仅在 `ENABLE_SCRIPT=1` 且 `ENABLE_HUMAN_REVIEW=1` 时启用正式台本结构复核过滤，并会调用 LLM。 |
| `ENABLE_SCRIPT_UNITS_SHADOW` | `0` | 在 `ENABLE_SCRIPT=1` 时，于正式阶段后运行非阻塞台本结构影子分析。它只产出审计数据，不改变正式字幕或时间轴。 |
| `SCRIPT_UNITS_TOTAL_TIMEOUT_SECONDS` | `120` | 台本结构分类和对齐的正数超时秒数。末尾影子分析超时不会使已完成的正式流水线失败。 |
| `STEP4_FINAL` | `1` | 运行中日双语全文终审。 |
| `STEP5_VALIDATE` | `1` | 运行本地终稿字幕规则校验。 |
| `STEP6_STRIP` | `1` | 生成纯中文字幕。启用时，STEP5 失败会在 STEP6 后报告，而非阻断导出；流水线整体仍以失败状态结束。 |
| `STEP7_EXPORT_FINAL` | `1` | 把双语终稿复制到 `SRT_TRANSFER_DIR/final/`；仅 `STEP4_FINAL=1` 时有效。 |
| `STEP8_EXPORT_CN_ONLY` | `1` | 把纯中文字幕复制到 `SRT_TRANSFER_DIR/cn_only/`；仅 `STEP6_STRIP=1` 时有效。 |

## 不支持通过 `.env` 覆盖的名称

`RESCUE_WINDOW_SECONDS`、`RESCUE_OVERLAP_SECONDS`、`INITIAL_PROMPT`、
`HOTWORDS` 和 `VAD_*` 由 `run_all.py` 设置并注入子进程。只在 `.env` 中添加它们不会替换实际值。`ACTIVE_AUDIO_PATHS`、`ACTIVE_AUDIO_BASES` 和 `LOG_FILE` 是本轮运行的内部状态，不是普通用户配置。变更这些名称需要修改实现，而不是编辑 `.env`。
