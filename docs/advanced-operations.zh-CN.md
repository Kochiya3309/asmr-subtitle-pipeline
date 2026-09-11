# 高级操作

[English](advanced-operations.md) | 简体中文

本指南面向首次运行之后的用户任务。安装和首次生成字幕见[纯新手使用指南](getting-started.zh-CN.md)；配置含义和默认值见[配置参考](configuration.zh-CN.md)。

## 使用视频输入

当来源是视频而不是单独音频时，按以下步骤操作：

1. 将 `.mp4`、`.mkv`、`.mov`、`.webm` 或 `.avi` 文件放入 `video/`。
2. 在 `.env` 中设置 `ENABLE_VIDEO_PREP=1`。
3. 使用 `start.bat` 或 `venv\Scripts\python.exe run_all.py` 运行正常流水线。

流水线会提取第一条音轨，在 `output/<音频名>/input/` 下生成受管理 FLAC，并与普通音频一起加入活动输入集合。不要把该 FLAC 复制到 `audio/`。如果来源目录不是 `video/`，请设置 `VIDEO_DIR`。

### 成功后与首查位置

视频准备成功后，会在 `output/<音频名>/input/` 下生成受管理的 FLAC 和来源 manifest，然后继续正常阶段。若在 STEP1 前停止，先检查源视频的第一条音轨，以及是否有另一活动输入使用相同基本名；首个失败信息见 `output/_shared/pipeline.log`。

## 将已审校字幕压制进视频

只应在完成最终 SRT 审校后使用本操作：

1. 运行 `start_burn_subtitles.bat`。
2. 按提示拖入源视频和已审校 SRT。
3. 可选填写试压开始时间和时长。
4. 选择“标准”或“快速”，检查显示的方案后输入 `y` 开始编码。

工具会把字幕压入第一条视频流、复制全部音轨、不覆盖源文件，并在源文件旁写入 `_subtitled` 或 `_preview_subtitled` 文件。它可直接读取 UTF-8 SRT；对可识别的 CP932 或 GB18030 输入，会在确认后提供 UTF-8 工作副本转换。

### 成功后与首查位置

成功时会在源视频旁生成不覆盖原文件的输出。若没有开始编码，先查看辅助窗口显示的媒体与编码方案，再从项目根目录重新运行 `start_burn_subtitles.bat`；不要用未审校或无法读取的 SRT 替代。

## 检查或迁移旧版平铺输出

先预览拟迁移结果：

```powershell
venv\Scripts\python.exe migrate_output.py --output-dir output
```

若要备份并执行迁移、但不启动 ASR 或 LLM 阶段：

```powershell
venv\Scripts\python.exe migrate_output.py --output-dir output --apply
```

如需回退尚未变化的已迁移产物，请使用实际记录路径：

```powershell
venv\Scripts\python.exe migrate_output.py --rollback "output/_shared/layout_backups/<迁移编号>/migration.json"
```

迁移会保留未知用户文件，遇到冲突会停止而非覆盖。启动当前流水线入口会再次分类旧文件；只有回到旧布局/旧代码上下文时才应使用回退。

### 成功后与首查位置

预览只打印计划，不修改文件。成功执行 `--apply` 后，实际记录路径会显示为 `output/_shared/layout_backups/<迁移编号>/migration.json`。迁移停止时，应先解决报告的冲突或未知文件，再重试 `--apply`；不要通过删除输出文件强行继续。

## 按需导出批量转移副本

上游阶段启用时，正常流水线会运行 STEP7 和 STEP8。只有需要单独导出时才使用：

```powershell
venv\Scripts\python.exe export_final_srt.py
venv\Scripts\python.exe export_cn_only_srt.py
```

两个脚本都接受 `--output-dir` 和 `--transfer-dir`。它们会保留每个音频任务下的规范字幕，并在 `_transfer/final/` 或 `_transfer/cn_only/` 中建立音频同名副本。

### 成功后与首查位置

成功后，规范字幕仍留在 `output/<音频名>/final/`，并在所选转移目录下增加副本。若导出缺失或失败，先确认规范终稿存在，且其上游 STEP4 或 STEP6 已启用；这些辅助脚本不会重建缺失的终稿。

## 安全恢复或重跑

运行 `start.bat`，或执行：

```powershell
venv\Scripts\python.exe run_all.py
```

### 成功后与首查位置

中断后的重跑只会复用通过缓存检查的产物。重跑停止时，查看 `output/_shared/pipeline.log`，确认首个失败阶段和活动输入，解决原因后再重试；不要一开始就删除缓存或无关输出任务。

流水线会先检查缓存有效性再复用。隔离实验应将 `OUTPUT_DIR` 设为一个空目录；除非了解产物依赖关系，不要批量删除 `output/`。
