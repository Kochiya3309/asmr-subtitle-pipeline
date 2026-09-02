# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
import os
import sys
import time
import re
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from faster_whisper import WhisperModel
from common import (
    get_llm_client, call_deepseek, extract_srt_from_response,
    format_timestamp, FUSION_TOOL, read_text_file,
    reset_usage, get_usage_report, get_llm_config
)
from asr_evidence import (
    align_candidates,
    build_audit_manifest,
    build_alignment_artifact,
    build_windows_artifact,
    candidates_from_segments,
    canonical_fingerprint,
    classify_windows,
    ensure_legacy_model_evidence,
    file_fingerprint,
    filtered_srt_for_role,
    load_evidence,
    new_evidence_document,
    upsert_model_evidence,
    write_json_atomic,
)
from asr_rescue import generate_rescue_windows, run_rescue_channel
from asr_timeline import (
    EVIDENCE_FUSION_TOOL,
    EVIDENCE_FUSION_SYSTEM_PROMPT,
    FUSION_ALGORITHM_VERSION,
    build_fusion_payload,
    build_fusion_timeline,
    parse_fusion_selections,
    render_timeline_srt,
    validate_fusion_manifest,
    write_text_atomic,
)
from script_mapping_state import resolve_mapping_path, validate_mapping_verification
from script_units import (
    generate_script_units_shadow,
    load_current_script_units,
    read_script_text_exact,
    script_units_generation_fingerprint,
)
from long_cue_alignment import (
    build_alignment_report,
    parse_srt as parse_alignment_srt,
    rewrite_srt_with_proposals,
)
from pipeline_cache import backup_stale_artifacts

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _project_path(value):
    return value if os.path.isabs(value) else os.path.join(PROJECT_DIR, value)


# ====== 配置 ======
AUDIO_DIR  = _project_path(os.environ.get("AUDIO_DIR", "./audio"))
OUTPUT_DIR = _project_path(os.environ.get("OUTPUT_DIR", "./output"))
ENABLE_SEARCH = os.environ.get("ENABLE_SEARCH", "1") == "1"
AUDIO_EXTS = ['.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus']
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
# ====== 台本功能（V3.2 新增）======
ENABLE_SCRIPT = os.environ.get("ENABLE_SCRIPT", "0") == "1"
ENABLE_SCRIPT_UNITS_SHADOW = os.environ.get("ENABLE_SCRIPT_UNITS_SHADOW", "0") == "1"
SCRIPT_UNITS_API_TIMEOUT_SECONDS = 20.0
SCRIPT_MAPPING_FILE = os.path.join(OUTPUT_DIR, "script_mapping.json")
SCRIPT_VERIFIED_FILE = os.path.join(OUTPUT_DIR, "script_mapping.verified")
MISMATCH_FILE = os.path.join(OUTPUT_DIR, "script_mismatch.json")
# =================================
# ====== 转写优化（V3.4 新增）======
ENABLE_AUDIO_PREPROCESS = os.environ.get("ENABLE_AUDIO_PREPROCESS", "1") == "1"
ENABLE_ASR_EVIDENCE = os.environ.get("ENABLE_ASR_EVIDENCE", "1") == "1"
ENABLE_HALLUCINATION_FILTER = os.environ.get("ENABLE_HALLUCINATION_FILTER", "1") == "1"
ENABLE_ASR_RESCUE = os.environ.get("ENABLE_ASR_RESCUE", "1") == "1"
ENABLE_DETERMINISTIC_TIMELINE = os.environ.get("ENABLE_DETERMINISTIC_TIMELINE", "0") == "1"
ENABLE_LONG_CUE_ALIGNMENT = os.environ.get("ENABLE_LONG_CUE_ALIGNMENT", "1") == "1"
LONG_CUE_ALIGNMENT_MODE = os.environ.get("LONG_CUE_ALIGNMENT_MODE", "apply").strip().lower()
LONG_CUE_MAX_SECONDS = float(os.environ.get("LONG_CUE_MAX_SECONDS", "15"))
RESCUE_WINDOW_SECONDS = float(os.environ.get("RESCUE_WINDOW_SECONDS", "12"))
RESCUE_OVERLAP_SECONDS = float(os.environ.get("RESCUE_OVERLAP_SECONDS", "2"))
INITIAL_PROMPT = os.environ.get("INITIAL_PROMPT", "")
HOTWORDS = os.environ.get("HOTWORDS", "")
VAD_THRESHOLD = float(os.environ.get("VAD_THRESHOLD", "0.2"))
VAD_MIN_SPEECH_MS = int(os.environ.get("VAD_MIN_SPEECH_MS", "50"))
VAD_SPEECH_PAD_MS = int(os.environ.get("VAD_SPEECH_PAD_MS", "800"))
VAD_MIN_SILENCE_MS = int(os.environ.get("VAD_MIN_SILENCE_MS", "500"))
# =================================

# ====== mismatch 跟踪（线程安全集合）======
_mismatch_files = set()
_matched_script_files = set()
_script_contexts = {}
_script_exact_texts = {}
_script_exact_paths = {}
# ====== Turbo 补跑队列（不在并行 worker 里跑 GPU，交由阶段2.5 串行处理）======
_needs_turbo = set()
# ==========================================================================

# ====== Whisper 模型缓存 ======
_model_cache = {}

SCRIPT_FUSION_CONTRACT_VERSION = "script-fusion-legacy-timecodes-v1"
SCRIPT_FUSION_PROMPT_VERSION = "2026-08-29.1"
PREPROCESS_CACHE_CONTRACT_VERSION = "preprocessed-audio-manifest-v1"
ASR_CACHE_CONTRACT_VERSION = "asr-srt-manifest-v1"
LONG_CUE_ALIGNMENT_CONTRACT_VERSION = "long-cue-alignment-manifest-v1"
LEGACY_FUSION_CONTRACT_VERSION = "legacy-llm-fusion-manifest-v1"
LEGACY_FUSION_PROMPT_VERSION = "2026-09-02.1"

def _detect_device():
    """检测计算设备（基于 ctranslate2，faster-whisper 的推理后端）。
    回归修复：此前用 torch 判 CUDA 是错的——本项目不依赖 torch，
    faster-whisper 的 GPU 加速由 ctranslate2 + NVIDIA 运行库提供，
    torch 缺失会被误判为无 GPU 而回退 CPU。
    规则：有 CUDA 设备 → cuda/int8_float16（老 GPU 不支持时由
    get_whisper_model 的加载降级兜底）；无 CUDA 设备 → cpu/int8。"""
    import ctranslate2
    try:
        cuda_count = ctranslate2.get_cuda_device_count()
    except Exception:
        cuda_count = 0
    if cuda_count > 0:
        return "cuda", "int8_float16"
    print("    ⚠ 未检测到可用 GPU，回退 CPU 模式（速度慢 5~10 倍）")
    return "cpu", "int8"

_device, _compute_type = _detect_device()

def get_whisper_model(model_name):
    if model_name not in _model_cache:
        print(f"    ⏳ 首次加载 {model_name}（{_device}/{_compute_type}）...")
        try:
            _model_cache[model_name] = WhisperModel(
                model_name, device=_device, compute_type=_compute_type
            )
        except Exception as e:
            # 老 GPU（计算能力 < 7.5）不支持 int8_float16，降级 float16 重试
            if _device == "cuda" and _compute_type == "int8_float16":
                print(f"    ⚠ int8_float16 加载失败（{e}），降级 float16 重试...")
                _model_cache[model_name] = WhisperModel(
                    model_name, device="cuda", compute_type="float16"
                )
            else:
                raise
    else:
        print(f"    ♻ 复用已加载的 {model_name}")
    return _model_cache[model_name]
# ===============================


# ====== 音频预处理（V3.4 新增）======
def preprocess_audio(input_path, output_path):
    """音频预处理：降噪 + 高通滤波 + RMS归一化，输出 16kHz mono wav。
    依赖 librosa / noisereduce / scipy / soundfile（惰性导入）。"""
    import numpy as np
    import librosa
    import noisereduce as nr
    from scipy.signal import butter, sosfilt
    import soundfile as sf

    print(f"    🎚 预处理中 ...")
    y, sr = librosa.load(input_path, sr=16000, mono=True)
    sr = int(sr)

    # 高通滤波 80Hz（去除低频隆隆声）
    sos = butter(4, 80, btype='highpass', fs=sr, output='sos')
    y = sosfilt(sos, y)

    # 稳态噪声抑制（保守强度，避免误伤 ASMR 轻声）
    y = nr.reduce_noise(y=y, sr=sr, stationary=True, prop_decrease=0.7)

    # RMS 归一化（-20dBFS 目标，gain 限幅避免峰值削波）
    rms = np.sqrt(np.mean(y ** 2)) + 1e-8
    target_rms = 0.1
    gain = target_rms / rms
    peak = np.max(np.abs(y))
    gain = min(gain, 0.95 / peak)   # 不让峰值超 0.95
    y = y * gain

    sf.write(output_path, y, sr, subtype='PCM_16')
    print(f"    ✅ 预处理 → {output_path}")
# ===================================


def _audio_preprocess_config():
    return {
        "contract_version": PREPROCESS_CACHE_CONTRACT_VERSION,
        "sample_rate": 16000,
        "mono": True,
        "highpass_hz": 80,
        "noise_reduction": {"stationary": True, "prop_decrease": 0.7},
        "target_rms": 0.1,
        "peak_limit": 0.95,
        "subtype": "PCM_16",
    }


def _preprocessed_paths(audio_path):
    base = os.path.splitext(os.path.basename(audio_path))[0]
    output_path = os.path.join(OUTPUT_DIR, f"{base}_preprocessed.wav")
    manifest_path = os.path.join(OUTPUT_DIR, f"{base}_preprocessed_manifest.json")
    return output_path, manifest_path


def _preprocessed_cache_is_current(audio_path, output_path, manifest_path):
    if not os.path.isfile(output_path) or not os.path.isfile(manifest_path):
        return False
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as handle:
            manifest = json.load(handle)
        return (
            isinstance(manifest, dict)
            and set(manifest) == {
                "schema_version", "artifact_type", "status",
                "generation_fingerprint", "source_audio", "output_audio",
            }
            and manifest["schema_version"] == 1
            and manifest["artifact_type"] == "preprocessed_audio_manifest"
            and manifest["status"] == "complete"
            and manifest["generation_fingerprint"] == canonical_fingerprint(
                _audio_preprocess_config()
            )
            and manifest["source_audio"] == file_fingerprint(audio_path)
            and manifest["output_audio"] == file_fingerprint(output_path)
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


def _ensure_preprocessed_audio(audio_path):
    output_path, manifest_path = _preprocessed_paths(audio_path)
    if _preprocessed_cache_is_current(audio_path, output_path, manifest_path):
        print("    ⏭ 预处理缓存指纹有效，跳过")
        return output_path
    expected_source = file_fingerprint(audio_path)
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    fd, staged_path = tempfile.mkstemp(
        prefix=".preprocessed-", suffix=".wav", dir=output_dir,
    )
    os.close(fd)
    try:
        preprocess_audio(audio_path, staged_path)
        if file_fingerprint(audio_path) != expected_source:
            raise ValueError("preprocess input changed during generation")
        staged_output = file_fingerprint(staged_path)
        staged_output["name"] = os.path.basename(output_path)
        write_json_atomic(manifest_path, {
            "schema_version": 1,
            "artifact_type": "preprocessed_audio_manifest",
            "status": "complete",
            "generation_fingerprint": canonical_fingerprint(_audio_preprocess_config()),
            "source_audio": expected_source,
            "output_audio": staged_output,
        })
        if file_fingerprint(audio_path) != expected_source:
            raise ValueError("preprocess input changed before commit")
        os.replace(staged_path, output_path)
    except Exception:
        try:
            os.unlink(staged_path)
        except OSError:
            pass
        raise
    return output_path


def _actual_audio_path(audio_path):
    return _ensure_preprocessed_audio(audio_path) if ENABLE_AUDIO_PREPROCESS else audio_path


def _main_transcribe_kwargs():
    kwargs = dict(
        language="ja",
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=VAD_MIN_SILENCE_MS,
            threshold=VAD_THRESHOLD,
            min_speech_duration_ms=VAD_MIN_SPEECH_MS,
            speech_pad_ms=VAD_SPEECH_PAD_MS,
        ),
        condition_on_previous_text=False,
        no_repeat_ngram_size=5,
        repetition_penalty=1.5,
        temperature=0.0,
        compression_ratio_threshold=2.0,
        log_prob_threshold=-1.0,
    )
    if INITIAL_PROMPT:
        kwargs["initial_prompt"] = INITIAL_PROMPT
    if HOTWORDS:
        kwargs["hotwords"] = HOTWORDS
    return kwargs


def _asr_cache_manifest_path(output_srt):
    stem, _ = os.path.splitext(output_srt)
    return f"{stem}_manifest.json"


def _asr_generation_fingerprint(model_name, transcribe_kwargs):
    return canonical_fingerprint({
        "contract_version": ASR_CACHE_CONTRACT_VERSION,
        "model": model_name,
        "transcribe_kwargs": transcribe_kwargs,
        "audio_preprocess": (
            _audio_preprocess_config() if ENABLE_AUDIO_PREPROCESS else {"enabled": False}
        ),
        "word_timestamps": False,
    })


def _asr_cache_is_current(model_name, audio_path, actual_audio, output_srt):
    manifest_path = _asr_cache_manifest_path(output_srt)
    if not os.path.isfile(output_srt) or not os.path.isfile(manifest_path):
        return False
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as handle:
            manifest = json.load(handle)
        transcribe_kwargs = _main_transcribe_kwargs()
        return (
            isinstance(manifest, dict)
            and set(manifest) == {
                "schema_version", "artifact_type", "status", "model",
                "generation_fingerprint", "source_audio", "actual_audio",
                "output_srt",
            }
            and manifest["schema_version"] == 1
            and manifest["artifact_type"] == "asr_srt_manifest"
            and manifest["status"] == "complete"
            and manifest["model"] == model_name
            and manifest["generation_fingerprint"]
            == _asr_generation_fingerprint(model_name, transcribe_kwargs)
            and manifest["source_audio"] == file_fingerprint(audio_path)
            and manifest["actual_audio"] == file_fingerprint(actual_audio)
            and manifest["output_srt"] == file_fingerprint(output_srt)
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


def _write_asr_cache_manifest(model_name, audio_path, actual_audio, output_srt,
                              transcribe_kwargs, *, source_fingerprint=None,
                              actual_fingerprint=None, output_fingerprint=None):
    write_json_atomic(_asr_cache_manifest_path(output_srt), {
        "schema_version": 1,
        "artifact_type": "asr_srt_manifest",
        "status": "complete",
        "model": model_name,
        "generation_fingerprint": _asr_generation_fingerprint(
            model_name, transcribe_kwargs
        ),
        "source_audio": source_fingerprint or file_fingerprint(audio_path),
        "actual_audio": actual_fingerprint or file_fingerprint(actual_audio),
        "output_srt": output_fingerprint or file_fingerprint(output_srt),
    })


def _asr_evidence_path(audio_path):
    base = os.path.splitext(os.path.basename(audio_path))[0]
    return os.path.join(OUTPUT_DIR, f"{base}_asr_candidates.json")


def _backfill_legacy_evidence(audio_path, model_name, srt_path):
    """Best-effort sidecar creation; legacy caches must remain runnable."""
    if not ENABLE_ASR_EVIDENCE:
        return
    try:
        ensure_legacy_model_evidence(
            _asr_evidence_path(audio_path), audio_path, model_name, srt_path
        )
    except Exception as e:
        print(f"    ⚠ ASR 证据回填失败，继续使用旧 SRT：{e}")


def _probe_audio_duration(audio_path):
    """Read duration from metadata; fall back to librosa for codec coverage."""
    try:
        import soundfile as sf
        info = sf.info(audio_path)
        if info.samplerate > 0 and info.frames > 0:
            return info.frames / info.samplerate
    except Exception:
        pass
    import librosa
    return float(librosa.get_duration(path=audio_path))


def _build_rescue_artifact(candidates, audio_duration):
    """Plan rescue solely from main evidence so repeated runs stay stable."""
    main_candidates = [
        item for item in candidates if item.get("source_channel") == "main"
    ]
    main_windows = align_candidates(main_candidates)
    main_decisions = classify_windows(main_candidates, main_windows)
    return generate_rescue_windows(
        main_candidates,
        main_decisions,
        audio_duration,
        window_seconds=RESCUE_WINDOW_SECONDS,
        overlap_seconds=RESCUE_OVERLAP_SECONDS,
    )


def _write_shadow_decisions(audio_path, plan_rescue=True):
    """Write deterministic audit artifacts without changing formal subtitles."""
    if not ENABLE_ASR_EVIDENCE:
        return
    base = os.path.splitext(os.path.basename(audio_path))[0]
    try:
        document = load_evidence(_asr_evidence_path(audio_path), audio_path)
        stale_runs = [
            key for key, run in document["runs"].items()
            if run.get("source_relation") != "matched"
        ]
        if stale_runs:
            raise ValueError(f"证据来源关系未确认：{', '.join(stale_runs)}")
        candidates = document["candidates"]
        windows = align_candidates(candidates)
        evidence_fingerprint = canonical_fingerprint(document)
        generation_id = f"asr-{evidence_fingerprint[:16]}"
        artifacts = {
            f"{base}_asr_alignment.json": build_alignment_artifact(candidates, windows),
            f"{base}_asr_windows.json": build_windows_artifact(candidates, windows),
            f"{base}_asr_decisions.json": classify_windows(candidates, windows),
        }
        timeline_filename = f"{base}_fusion_timeline.json"
        artifacts[timeline_filename] = build_fusion_timeline(
            document, artifacts[f"{base}_asr_decisions.json"]
        )
        rescue_count = 0
        rescue_artifact = None
        if ENABLE_ASR_RESCUE and plan_rescue:
            rescue_artifact = _build_rescue_artifact(
                candidates, _probe_audio_duration(audio_path)
            )
            rescue_filename = f"{base}_asr_rescue_windows.json"
            artifacts[rescue_filename] = rescue_artifact
            rescue_count = len(rescue_artifact["windows"])
        for filename, payload in artifacts.items():
            payload["source"] = document["source"]
            payload["generation_id"] = generation_id
            payload["evidence_fingerprint"] = evidence_fingerprint
            write_json_atomic(os.path.join(OUTPUT_DIR, filename), payload)
        manifest_name = f"{base}_asr_manifest.json"
        manifest = build_audit_manifest(
            generation_id,
            evidence_fingerprint,
            {payload["artifact_type"]: filename for filename, payload in artifacts.items()},
        )
        manifest["source"] = document["source"]
        write_json_atomic(os.path.join(OUTPUT_DIR, manifest_name), manifest)
        quarantine_count = len(artifacts[f"{base}_asr_decisions.json"]["quarantined_evidence_ids"])
        print(
            f"  🧭 ASR 影子审计：{len(windows)} 个对齐窗口，"
            f"{quarantine_count} 条隔离候选，{rescue_count} 个救援窗口"
        )
        return rescue_artifact
    except Exception as e:
        print(f"  ⚠ ASR 影子审计失败，不影响现有字幕流程：{e}")
        return None


def _rescue_audio_path(audio_path):
    if ENABLE_AUDIO_PREPROCESS:
        base = os.path.splitext(os.path.basename(audio_path))[0]
        preprocessed = os.path.join(OUTPUT_DIR, f"{base}_preprocessed.wav")
        if os.path.exists(preprocessed):
            return preprocessed
    return audio_path


def _execute_rescue_if_enabled(audio_path, rescue_artifact):
    if not ENABLE_ASR_RESCUE or not rescue_artifact:
        return
    try:
        if run_rescue_channel(
            audio_path=audio_path,
            actual_audio=_rescue_audio_path(audio_path),
            evidence_path=_asr_evidence_path(audio_path),
            rescue_artifact=rescue_artifact,
            get_model=get_whisper_model,
        ):
            _write_shadow_decisions(audio_path, plan_rescue=True)
    except Exception as e:
        print(f"  ⚠ 救援通道失败，保留主通道结果继续：{e}")


def _long_cue_alignment_path(audio_path):
    base = os.path.splitext(os.path.basename(audio_path))[0]
    return os.path.join(OUTPUT_DIR, f"{base}_long_cue_alignment.json")


def _long_cue_generation_fingerprint(max_duration_ms):
    return canonical_fingerprint({
        "contract_version": LONG_CUE_ALIGNMENT_CONTRACT_VERSION,
        "model": "large-v3",
        "max_duration_ms": max_duration_ms,
    })


def _current_long_cue_alignment(audio_path, ensemble_srt, mode, max_duration_ms):
    path = _long_cue_alignment_path(audio_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            report = json.load(handle)
        if (
            isinstance(report, dict)
            and report.get("schema_version") == 1
            and report.get("artifact_type") == "long_cue_alignment"
            and report.get("status") == "complete"
            and report.get("mode") == mode
            and report.get("generation_fingerprint")
            == _long_cue_generation_fingerprint(max_duration_ms)
            and report.get("source_audio") == file_fingerprint(audio_path)
            and report.get("output_srt") == file_fingerprint(ensemble_srt)
        ):
            return report
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return None


def _run_long_cue_alignment(audio_path, ensemble_srt, *, mode=None):
    """Run a serial timing-only V3 pass without changing baseline ASR evidence."""
    if not ENABLE_LONG_CUE_ALIGNMENT:
        return None
    effective_mode = (mode or LONG_CUE_ALIGNMENT_MODE).strip().lower()
    if effective_mode not in {"shadow", "apply"}:
        raise ValueError("LONG_CUE_ALIGNMENT_MODE must be shadow or apply")
    if LONG_CUE_MAX_SECONDS <= 0:
        raise ValueError("LONG_CUE_MAX_SECONDS must be positive")
    with open(ensemble_srt, "r", encoding="utf-8-sig") as handle:
        source_srt = handle.read()
    source_cues = parse_alignment_srt(source_srt)
    max_duration_ms = round(LONG_CUE_MAX_SECONDS * 1000)
    if not any(
        cue["end_ms"] - cue["start_ms"] > max_duration_ms
        for cue in source_cues
    ):
        if _current_long_cue_alignment(
            audio_path, ensemble_srt, effective_mode, max_duration_ms
        ):
            return None
        report = {
            "schema_version": 1,
            "artifact_type": "long_cue_alignment",
            "status": "complete",
            "max_duration_ms": max_duration_ms,
            "source_cue_count": len(source_cues),
            "items": [],
            "summary": {
                "detected": 0, "aligned": 0, "split": 0, "unresolved": 0,
            },
            "mode": effective_mode,
            "model": "large-v3",
            "generation_fingerprint": _long_cue_generation_fingerprint(
                max_duration_ms
            ),
            "source_audio": file_fingerprint(audio_path),
            "alignment_audio": file_fingerprint(_rescue_audio_path(audio_path)),
            "input_srt": file_fingerprint(ensemble_srt),
            "output_srt": file_fingerprint(ensemble_srt),
        }
        write_json_atomic(_long_cue_alignment_path(audio_path), report)
        return None

    from faster_whisper.audio import decode_audio

    actual_audio = _rescue_audio_path(audio_path)
    audio_samples = decode_audio(actual_audio, sampling_rate=16_000)
    report = build_alignment_report(
        source_srt,
        get_whisper_model("large-v3"),
        audio_samples,
        sampling_rate=16_000,
        max_duration_ms=max_duration_ms,
    )
    report.update({
        "mode": effective_mode,
        "model": "large-v3",
        "generation_fingerprint": _long_cue_generation_fingerprint(max_duration_ms),
        "source_audio": file_fingerprint(audio_path),
        "alignment_audio": file_fingerprint(actual_audio),
        "input_srt": file_fingerprint(ensemble_srt),
    })
    if effective_mode == "apply":
        proposals = {
            item["source_index"]: item
            for item in report["items"]
            if item["status"] in {"aligned", "split"}
        }
        rewritten = rewrite_srt_with_proposals(source_srt, proposals)
        write_text_atomic(ensemble_srt, rewritten)
    report["output_srt"] = file_fingerprint(ensemble_srt)
    write_json_atomic(_long_cue_alignment_path(audio_path), report)
    return report


def _filter_fusion_inputs(audio_path, v3_text, turbo_text, log_prefix=""):
    """Apply high-precision quarantine; fall back safely on audit errors."""
    if not ENABLE_ASR_EVIDENCE or not ENABLE_HALLUCINATION_FILTER:
        return v3_text, turbo_text, []
    try:
        document = load_evidence(_asr_evidence_path(audio_path), audio_path)
        if any(run.get("source_relation") != "matched" for run in document["runs"].values()):
            raise ValueError("存在来源关系未确认的 ASR run")
        main_candidates = [
            item for item in document["candidates"]
            if item.get("source_channel") == "main"
        ]
        decisions = classify_windows(main_candidates, align_candidates(main_candidates))
        filtered_v3, removed_v3 = filtered_srt_for_role(
            document, decisions, "v3", v3_text
        )
        filtered_turbo, removed_turbo = filtered_srt_for_role(
            document, decisions, "turbo", turbo_text
        )
        removed = removed_v3 + removed_turbo
        if removed:
            ids = ", ".join(item["evidence_id"] for item in removed)
            print(f"{log_prefix}🛡 已隔离 {len(removed)} 条高置信幻觉候选：{ids}")
        return filtered_v3, filtered_turbo, removed
    except Exception as e:
        print(f"{log_prefix}⚠ 幻觉隔离不可用，回退原始 SRT 融合：{e}")
        return v3_text, turbo_text, []


def _build_current_fusion_timeline(audio_path):
    document = load_evidence(_asr_evidence_path(audio_path), audio_path)
    if document.get("source") != new_evidence_document(audio_path)["source"]:
        raise ValueError("ASR 证据对应的音频版本已过期")
    if any(run.get("source_relation") != "matched" for run in document["runs"].values()):
        raise ValueError("存在来源关系未确认的 ASR run")
    candidates = document["candidates"]
    decisions = classify_windows(candidates, align_candidates(candidates))
    return build_fusion_timeline(document, decisions)


def _fusion_generation_fingerprint():
    _, base_url, model, enable_thinking, max_tokens = get_llm_config()
    return canonical_fingerprint({
        "algorithm_version": FUSION_ALGORITHM_VERSION,
        "system_prompt": EVIDENCE_FUSION_SYSTEM_PROMPT,
        "tool_schema": EVIDENCE_FUSION_TOOL,
        "llm": {
            "base_url": base_url,
            "model": model,
            "enable_thinking": enable_thinking,
            "max_tokens": max_tokens,
            "temperature": 0.25,
            "enable_search": ENABLE_SEARCH,
        },
    })


def _legacy_fusion_inputs(audio_path, v3_srt, turbo_srt):
    evidence_path = _asr_evidence_path(audio_path)
    evidence_sha256 = None
    if (
        ENABLE_ASR_EVIDENCE
        and ENABLE_HALLUCINATION_FILTER
        and os.path.isfile(evidence_path)
    ):
        evidence_sha256 = file_fingerprint(evidence_path)["sha256"]
    return {
        "source_audio": new_evidence_document(audio_path)["source"],
        "v3_srt": file_fingerprint(v3_srt),
        "turbo_srt": file_fingerprint(turbo_srt),
        "evidence_sha256": evidence_sha256,
    }


def _legacy_fusion_generation_fingerprint():
    _, base_url, model, enable_thinking, max_tokens = get_llm_config()
    return canonical_fingerprint({
        "contract_version": LEGACY_FUSION_CONTRACT_VERSION,
        "prompt_version": LEGACY_FUSION_PROMPT_VERSION,
        "tool_schema": FUSION_TOOL,
        "llm": {
            "base_url": base_url,
            "model": model,
            "enable_thinking": enable_thinking,
            "max_tokens": max_tokens,
            "temperature": 0.25,
            "enable_search": ENABLE_SEARCH,
        },
        "hallucination_filter": {
            "evidence_enabled": ENABLE_ASR_EVIDENCE,
            "filter_enabled": ENABLE_HALLUCINATION_FILTER,
        },
    })


def _script_fusion_generation_fingerprint():
    _, base_url, model, enable_thinking, max_tokens = get_llm_config()
    return canonical_fingerprint({
        "contract_version": SCRIPT_FUSION_CONTRACT_VERSION,
        "prompt_version": SCRIPT_FUSION_PROMPT_VERSION,
        "tool_schema": FUSION_TOOL,
        "llm": {
            "base_url": base_url,
            "model": model,
            "enable_thinking": enable_thinking,
            "max_tokens": max_tokens,
            "temperature": 0.25,
            "enable_search": ENABLE_SEARCH,
        },
    })


def _optional_file_fingerprint(path):
    return file_fingerprint(path) if path and os.path.isfile(path) else None


def _script_fusion_inputs(audio_path, v3_srt, script_text):
    base = os.path.splitext(os.path.basename(audio_path))[0]
    return {
        "audio_source": new_evidence_document(audio_path)["source"],
        "v3_srt": file_fingerprint(v3_srt),
        "script_text_fingerprint": canonical_fingerprint({"text": script_text}),
        "mapping": _script_contexts.get(base, {
            "mapping_entry_fingerprint": canonical_fingerprint({"legacy_text_only": True}),
            "script_source": None,
            "script_path": None,
        }),
        "generation_fingerprint": _script_fusion_generation_fingerprint(),
    }


def _validate_script_fusion_manifest(manifest):
    required = {
        "schema_version", "artifact_type", "status", "inputs", "output_srt",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError("invalid script fusion manifest fields")
    if manifest["schema_version"] != 1 or manifest["artifact_type"] != "script_fusion_manifest":
        raise ValueError("invalid script fusion manifest identity")
    if manifest["status"] not in {"pending", "matched", "mismatch_fallback"}:
        raise ValueError("invalid script fusion status")
    if not isinstance(manifest["inputs"], dict):
        raise ValueError("invalid script fusion inputs")
    output = manifest["output_srt"]
    if manifest["status"] == "pending":
        if output is not None:
            raise ValueError("pending script fusion cannot have output fingerprint")
        return
    if not isinstance(output, dict) or not isinstance(output.get("sha256"), str):
        raise ValueError("invalid script fusion output fingerprint")


def _write_script_fusion_manifest(
    audio_path, script_text, status, *, expected_inputs=None, output_path=None
):
    base = os.path.splitext(os.path.basename(audio_path))[0]
    v3_srt = os.path.join(OUTPUT_DIR, f"{base}_v3.srt")
    ensemble_srt = os.path.join(OUTPUT_DIR, f"{base}_ensemble.srt")
    manifest_path = os.path.join(OUTPUT_DIR, f"{base}_script_fusion_manifest.json")
    current_inputs = _script_fusion_inputs(audio_path, v3_srt, script_text)
    if expected_inputs is not None and current_inputs != expected_inputs:
        raise ValueError("台本融合处理期间输入发生变化")
    output_fingerprint = file_fingerprint(output_path or ensemble_srt)
    output_fingerprint["name"] = os.path.basename(ensemble_srt)
    manifest = {
        "schema_version": 1,
        "artifact_type": "script_fusion_manifest",
        "status": status,
        "inputs": expected_inputs or current_inputs,
        "output_srt": output_fingerprint,
    }
    write_json_atomic(manifest_path, manifest)
    return manifest


def _write_pending_script_fusion_manifest(audio_path, script_text, expected_inputs):
    base = os.path.splitext(os.path.basename(audio_path))[0]
    v3_srt = os.path.join(OUTPUT_DIR, f"{base}_v3.srt")
    current_inputs = _script_fusion_inputs(audio_path, v3_srt, script_text)
    if current_inputs != expected_inputs:
        raise ValueError("台本融合处理期间输入发生变化")
    write_json_atomic(
        os.path.join(OUTPUT_DIR, f"{base}_script_fusion_manifest.json"),
        {
            "schema_version": 1,
            "artifact_type": "script_fusion_manifest",
            "status": "pending",
            "inputs": expected_inputs,
            "output_srt": None,
        },
    )


def _load_current_script_fusion(audio_path, v3_srt, ensemble_srt, script_text):
    base = os.path.splitext(os.path.basename(audio_path))[0]
    manifest_path = os.path.join(OUTPUT_DIR, f"{base}_script_fusion_manifest.json")
    if not os.path.isfile(manifest_path):
        return None
    with open(manifest_path, "r", encoding="utf-8-sig") as handle:
        manifest = json.load(handle)
    _validate_script_fusion_manifest(manifest)
    expected_inputs = _script_fusion_inputs(audio_path, v3_srt, script_text)
    if manifest["inputs"] != expected_inputs:
        raise ValueError("台本融合输入已变化")
    if manifest["status"] == "pending":
        raise ValueError("台本 mismatch fallback 事务尚未提交")
    if manifest["output_srt"] != file_fingerprint(ensemble_srt):
        raise ValueError("台本融合输出指纹不匹配")
    return manifest


def _archive_incomplete_fusion_manifest(path):
    archived = f"{path}.abandoned-{time.time_ns()}"
    os.replace(path, archived)
    return archived


def _srt_block_count(text):
    return len(re.split(r'\n\s*\n', text.strip())) if text.strip() else 0


def _write_asr_srt_atomic(output_srt, segments, input_is_current):
    output_dir = os.path.dirname(os.path.abspath(output_srt))
    os.makedirs(output_dir, exist_ok=True)
    fd, staged_path = tempfile.mkstemp(
        prefix=".asr-output-", suffix=".srt", dir=output_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for index, segment in enumerate(segments, start=1):
                handle.write(
                    f"{index}\n"
                    f"{format_timestamp(segment.start)} --> "
                    f"{format_timestamp(segment.end)}\n"
                    f"{segment.text.strip()}\n\n"
                )
        if not input_is_current():
            raise ValueError("ASR input changed before commit")
        staged_output = file_fingerprint(staged_path)
        staged_output["name"] = os.path.basename(output_srt)
        os.replace(staged_path, output_srt)
        return staged_output
    except Exception:
        try:
            os.unlink(staged_path)
        except OSError:
            pass
        raise


def transcribe_with_model(model_name, audio_path, output_srt):
    model = get_whisper_model(model_name)
    actual_audio = _actual_audio_path(audio_path)
    transcribe_kwargs = _main_transcribe_kwargs()
    expected_source = file_fingerprint(audio_path)
    same_audio_input = (
        os.path.normcase(os.path.abspath(actual_audio))
        == os.path.normcase(os.path.abspath(audio_path))
    )
    expected_actual = (
        expected_source if same_audio_input else file_fingerprint(actual_audio)
    )

    def input_is_current():
        current_source = file_fingerprint(audio_path)
        if current_source != expected_source:
            return False
        current_actual = (
            current_source if same_audio_input else file_fingerprint(actual_audio)
        )
        return current_actual == expected_actual

    print(f"    🎙 转写中 ...")
    segments, _ = model.transcribe(actual_audio, **transcribe_kwargs)
    # Segment 是惰性生成器；一次物化后同时写兼容 SRT 与影子证据，
    # 不开启 word_timestamps，避免第一阶段改变当前转写基线。
    segment_list = list(segments)
    if not input_is_current():
        raise ValueError("ASR input changed during generation")
    staged_output = _write_asr_srt_atomic(
        output_srt, segment_list, input_is_current,
    )
    count = len(segment_list)
    print(f"    ✅ {model_name} → {output_srt} ({count} 条)")

    if ENABLE_ASR_EVIDENCE:
        evidence_config = {
            **transcribe_kwargs,
            "audio_preprocess": {
                "enabled": ENABLE_AUDIO_PREPROCESS,
                "sample_rate": 16000 if ENABLE_AUDIO_PREPROCESS else None,
                "mono": True if ENABLE_AUDIO_PREPROCESS else None,
                "highpass_hz": 80 if ENABLE_AUDIO_PREPROCESS else None,
                "noise_reduction_prop_decrease": 0.7 if ENABLE_AUDIO_PREPROCESS else None,
                "target_rms": 0.1 if ENABLE_AUDIO_PREPROCESS else None,
            },
            "word_timestamps": False,
        }
        try:
            source_view = "preprocessed" if actual_audio != audio_path else "raw"
            candidates = candidates_from_segments(
                segment_list,
                model_name,
                source_view=source_view,
                source_channel="main",
            )
            upsert_model_evidence(
                path=_asr_evidence_path(audio_path),
                audio_path=audio_path,
                model_name=model_name,
                candidates=candidates,
                transcribe_config=evidence_config,
                actual_audio=actual_audio,
                source_channel="main",
                source_view=source_view,
                emitted_srt=file_fingerprint(output_srt),
            )
            print(f"    🧾 ASR 证据 → {_asr_evidence_path(audio_path)}")
        except Exception as e:
            # 证据层当前是 shadow 模式，绝不能让旁路产物破坏正式 SRT。
            print(f"    ⚠ ASR 证据写入失败，正式 SRT 已保留：{e}")
    if not input_is_current():
        raise ValueError("ASR input changed before manifest commit")
    _write_asr_cache_manifest(
        model_name, audio_path, actual_audio, output_srt, transcribe_kwargs,
        source_fingerprint=expected_source,
        actual_fingerprint=expected_actual,
        output_fingerprint=staged_output,
    )


def _fix_srt_indexing(srt_text):
    """修复 SRT 文件的序号：确保第一个字幕块有序号 '1'，并顺次编号。"""
    blocks = re.split(r'\n\n+', srt_text.strip())
    if not blocks:
        return srt_text

    fixed_blocks = []
    for i, block in enumerate(blocks, start=1):
        lines = block.strip().split('\n')
        if lines and re.match(r'^\d+$', lines[0]):
            lines[0] = str(i)
        else:
            lines.insert(0, str(i))
        fixed_blocks.append('\n'.join(lines))

    return '\n\n'.join(fixed_blocks) + '\n'

def _fusion_parser(args):
    """FUSION_TOOL 输出解析器。优先检测 error 字段返回特殊标记。"""
    error = args.get("error")
    if error:
        return f"__SCRIPT_MISMATCH__:{error}"
    subtitles = args.get("subtitles", [])
    if not subtitles:
        return None
    blocks = []
    for i, sub in enumerate(subtitles, start=1):
        timecode = sub.get("timecode", "")
        text = sub.get("text", "")
        if timecode and text:
            blocks.append(f"{i}\n{timecode}\n{text}")
    if not blocks:
        return None
    return "\n\n".join(blocks)


def _ensure_model_transcription(model_name, audio_path, output_srt):
    actual_audio = _actual_audio_path(audio_path)
    manifest_path = _asr_cache_manifest_path(output_srt)
    if _asr_cache_is_current(model_name, audio_path, actual_audio, output_srt):
        print(f"  ⏭ {model_name} SRT 缓存指纹有效，跳过")
        _backfill_legacy_evidence(audio_path, model_name, output_srt)
        return
    if os.path.isfile(output_srt) or os.path.isfile(manifest_path):
        print(f"  ⚠ {model_name} SRT 缓存未绑定或已过期，重新转写")
        backup_stale_artifacts(output_srt, manifest_path)
    transcribe_with_model(model_name, audio_path, output_srt)

def transcribe_one_audio(audio_path, has_script=False):
    """阶段1：V3 + Turbo 双模型转写（GPU 独占，串行执行）。

    台本模式仍运行 Turbo 作为时间轴交叉证据，但正式台本文本融合保持
    V3 + 官方台本，不把 Turbo 文本直接加入台本融合 prompt。
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(audio_path))[0]
    v3_srt    = os.path.join(OUTPUT_DIR, f"{base}_v3.srt")
    turbo_srt = os.path.join(OUTPUT_DIR, f"{base}_turbo.srt")

    print(f"\n{'='*60}")
    print(f"📁 {audio_path}")
    print(f"{'='*60}")

    # Both model caches are bound to exact audio/config/output fingerprints.
    _ensure_model_transcription("large-v3", audio_path, v3_srt)
    _ensure_model_transcription("large-v3-turbo", audio_path, turbo_srt)

    rescue_artifact = _write_shadow_decisions(audio_path, plan_rescue=not has_script)
    if not has_script:
        _execute_rescue_if_enabled(audio_path, rescue_artifact)

    return base, v3_srt, turbo_srt


def fuse_one_audio(audio_path, log_prefix="", script_text="", _is_retry=False):
    """阶段2：LLM 融合（网络 I/O，可并行）
    有台本时走 V3+台本模式；Turbo 仅作为旁路 ASR 证据；
    无台本或 mismatch 回退时走 V3+Turbo 模式（原 prompt）。"""
    base = os.path.splitext(os.path.basename(audio_path))[0]
    v3_srt    = os.path.join(OUTPUT_DIR, f"{base}_v3.srt")
    turbo_srt = os.path.join(OUTPUT_DIR, f"{base}_turbo.srt")
    ensemble_srt = os.path.join(OUTPUT_DIR, f"{base}_ensemble.srt")
    fusion_manifest = os.path.join(OUTPUT_DIR, f"{base}_fusion_manifest.json")
    use_script = bool(script_text) and not _is_retry
    script_inputs_snapshot = None

    manifest = None
    if os.path.exists(fusion_manifest):
        try:
            with open(fusion_manifest, "r", encoding="utf-8-sig") as handle:
                manifest = json.load(handle)
            validate_fusion_manifest(manifest, require_complete=False)
        except Exception as e:
            if os.path.exists(ensemble_srt):
                print(f"{log_prefix}⚠ fusion manifest 非法且字幕已存在，拒绝使用：{e}")
                return ("stale", None)
            archived = _archive_incomplete_fusion_manifest(fusion_manifest)
            print(f"{log_prefix}⚠ 已归档非法 fusion manifest：{archived}")
            manifest = None
        if manifest and manifest["status"] == "pending":
            if os.path.exists(ensemble_srt):
                print(f"{log_prefix}⚠ 检测到未提交的确定性字幕事务，拒绝使用现有 SRT")
                return ("stale", None)
            if not ENABLE_DETERMINISTIC_TIMELINE or use_script:
                archived = _archive_incomplete_fusion_manifest(fusion_manifest)
                print(f"{log_prefix}⚠ 已归档未完成的 fusion manifest：{archived}")
                manifest = None
        elif manifest and not os.path.exists(ensemble_srt):
            archived = _archive_incomplete_fusion_manifest(fusion_manifest)
            print(f"{log_prefix}⚠ complete manifest 缺少 SRT，已归档：{archived}")
            manifest = None

    if use_script and os.path.exists(ensemble_srt):
        script_manifest = os.path.join(
            OUTPUT_DIR, f"{base}_script_fusion_manifest.json"
        )
        if not os.path.exists(script_manifest):
            print(f"{log_prefix}⚠ 保留无台本 manifest 的旧版 {ensemble_srt}；来源无法验证，停止下游")
            return ("stale", None)
        try:
            cached = _load_current_script_fusion(
                audio_path, v3_srt, ensemble_srt, script_text
            )
        except Exception as e:
            print(f"{log_prefix}⚠ 台本融合缓存已过期；为避免覆盖旧结果，本次不重建：{e}")
            return ("stale", None)
        if cached["status"] == "mismatch_fallback":
            _mismatch_files.add(base)
        else:
            _matched_script_files.add(base)
        print(f"{log_prefix}⏭ 台本融合缓存有效，跳过")
        return ("ok", ensemble_srt)

    if os.path.exists(ensemble_srt):
        if manifest is None:
            print(f"{log_prefix}⏭ 保留无 manifest 的旧版 {ensemble_srt}，不自动覆盖")
            return ("ok", ensemble_srt)
        if manifest["contract_version"] == LEGACY_FUSION_CONTRACT_VERSION:
            try:
                current_inputs = _legacy_fusion_inputs(
                    audio_path, v3_srt, turbo_srt,
                )
                if (
                    manifest["timeline_fingerprint"]
                    == canonical_fingerprint(current_inputs)
                    and manifest["fusion_generation_fingerprint"]
                    == _legacy_fusion_generation_fingerprint()
                    and manifest["output_srt"] == file_fingerprint(ensemble_srt)
                ):
                    print(f"{log_prefix}LLM fusion cache fingerprint is current; skipped")
                    return ("ok", ensemble_srt)
                print(f"{log_prefix}LLM fusion cache is stale; refusing old output")
                return ("stale", None)
            except Exception as e:
                print(f"{log_prefix}cannot validate LLM fusion cache: {e}")
                return ("stale", None)
        if not ENABLE_DETERMINISTIC_TIMELINE or use_script:
            if manifest["output_srt"] != file_fingerprint(ensemble_srt):
                print(f"{log_prefix}⚠ 确定性字幕输出指纹不匹配，拒绝使用")
                return ("stale", None)
            print(f"{log_prefix}⏭ 已提交的确定性字幕缓存有效，跳过")
            return ("ok", ensemble_srt)
        try:
            timeline = _build_current_fusion_timeline(audio_path)
            if (
                manifest.get("contract_version") == timeline["contract_version"]
                and manifest.get("timeline_fingerprint") == timeline["timeline_fingerprint"]
                and manifest.get("fusion_generation_fingerprint") == _fusion_generation_fingerprint()
                and manifest.get("output_srt") == file_fingerprint(ensemble_srt)
            ):
                print(f"{log_prefix}⏭ 确定性融合缓存有效，跳过")
                return ("ok", ensemble_srt)
            print(f"{log_prefix}⚠ 确定性融合缓存已过期；为避免覆盖旧结果，本次不重建")
            return ("stale", None)
        except Exception as e:
            print(f"{log_prefix}⚠ 无法验证确定性融合缓存；保留旧结果：{e}")
            return ("stale", None)

    if use_script:
        script_inputs_snapshot = _script_fusion_inputs(
            audio_path, v3_srt, script_text
        )

    # Read V3
    with open(v3_srt, "r", encoding="utf-8") as f:
        v3_text = f.read().strip()
    v3_lines = _srt_block_count(v3_text)

    # Bug 修复：无台本模式且 Turbo 缺失时提前返回 pending——
    # 不在并行 worker 线程里跑 GPU 转写（违犯"阶段1串行转写（GPU 独占）"
    # 设计，多文件并发补跑会 CUDA 争用/线程不安全），也避免无谓的 client 创建。
    # 记录到补跑队列，由 main 的阶段2.5 在主线程串行处理。
    if not use_script and not os.path.exists(turbo_srt):
        _needs_turbo.add(base)
        print(f"{log_prefix}⏳ Turbo 缺失，已加入阶段2.5 串行补跑队列")
        return ("pending_turbo", None)

    client = get_llm_client()
    deterministic_timeline = None
    legacy_inputs_snapshot = None
    output_tool = FUSION_TOOL
    output_parser = _fusion_parser

    if use_script:
        # ===== 台本模式：V3 + 台本（不需要 Turbo）=====
        print(f"{log_prefix}📄 台本模式：V3 {v3_lines} 条 + 台本")
        user_input = (
            "以下はWhisperモデルlarge-v3で書き起こした字幕と、公式台本です。\n\n"
            "【字幕：large-v3】\n```srt\n" + v3_text + "\n```\n\n"
            "【公式台本（時間軸なし、権威テキスト、非台詞説明を含む可能性あり）】\n"
            "```\n" + script_text + "\n```\n\n"
            "【台本の取り扱い】\n"
            "1. 台本中の※注記・シーン説明などの非台詞内容は文脈理解のみに用い、字幕に入れないこと\n"
            "2. テキストは台本を優先し、時間軸はWhisperを使用すること\n"
            "3. 台本にのみ存在する発話も必ず採用すること（Whisperが拾えなかった発話の補完）。\n"
            "   その際、時間軸が不明な場合は前後の発話の時間軸を参考に推定すること\n"
            "4. Whisperのみの発話は台本の抜け漏れの可能性があるため残すこと\n"
            "5. 台本とWhisper出力が著しく不一致する場合\n"
            "   （別作品の台本・内容の交差なし・音声と台本の完全なミスマッチ等）、\n"
            "   subtitlesを空配列にし、errorフィールドに理由を記入すること。\n"
            "   ただし部分的不一致は修正対象であり、error報告の対象外とする\n\n"
            "Whisperの時間軸と台本のテキストを統合して字幕を生成してください。"
        )
        system_prompt = (
            "あなたは日本語音声認識の字幕校正エキスパートです。"
            "Whisperモデル（large-v3）の転写結果と公式台本を統合し、"
            "台本を権威テキストとして字幕を生成してください。\n"
            "1. 時間軸はWhisperの出力を使用し、テキストは台本を優先\n"
            "2. 台本中の非台詞内容（※注記・シーン説明）は字幕に入れない\n"
            "3. 断片化は統合、重複は削除（意図的繰り返しは残す）\n"
            "4. わからない単語があれば web_search で検索して確認\n"
            "5. 【最重要】冒頭の発話の扱い：\n"
            "   音声の冒頭は環境音や囁きのために認識精度が低下することがありますが、\n"
            "   それは実際の台詞が存在しないことを意味しません。\n"
            "   Whisper・台本のどちらか一方でも最初の数秒間に発話があれば、\n"
            "   必ず統合後の字幕の1行目として採用してください。\n"
            "   たとえ「あ…」「えっと…」のような一言でも残してください。\n"
            "6. 「(中略)」「(省略)」などの省略記号を絶対に使用しないでください。\n"
            "【提出方法（絶対厳守）】\n"
            "統合が完了したら、submit_fusion 関数を呼び出して結果を提出してください。\n"
            "subtitles配列の各要素に timecode と text を含めてください。\n"
            "番号は不要です（自動付与されます）。説明や分析は一切不要です。"
        )
    else:
        # ===== 无台本模式：V3 + Turbo（原逻辑）=====
        with open(turbo_srt, "r", encoding="utf-8") as f:
            turbo_text = f.read().strip()
        legacy_inputs_snapshot = _legacy_fusion_inputs(
            audio_path, v3_srt, turbo_srt,
        )
        if ENABLE_DETERMINISTIC_TIMELINE:
            try:
                deterministic_timeline = _build_current_fusion_timeline(audio_path)
                if not deterministic_timeline["items"]:
                    raise ValueError("自动融合时间轴为空")
                payload = build_fusion_payload(deterministic_timeline)
            except Exception as e:
                print(f"{log_prefix}❌ 确定性时间轴不可用，不回退到 LLM 生成时间：{e}")
                return ("failed", None)
            user_input = (
                "以下はローカルで固定済みの日本語ASR証拠タイムラインです。\n"
                "start/end は参照専用です。出力には時刻を絶対に含めないでください。\n"
                "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"
            )
            system_prompt = EVIDENCE_FUSION_SYSTEM_PROMPT
            output_tool = EVIDENCE_FUSION_TOOL
            output_parser = lambda args: parse_fusion_selections(
                args, deterministic_timeline
            )
            v3_lines = _srt_block_count(v3_text)
            turbo_lines = _srt_block_count(turbo_text)
            print(
                f"{log_prefix}🧭 确定性融合：{len(deterministic_timeline['items'])} 个时间轴项，"
                f"V3 {v3_lines} 条 / Turbo {turbo_lines} 条"
            )
        else:
            v3_text, turbo_text, quarantined = _filter_fusion_inputs(
                audio_path, v3_text, turbo_text, log_prefix
            )
            v3_lines = _srt_block_count(v3_text)
            turbo_lines = _srt_block_count(turbo_text)
            print(f"{log_prefix}📄 V3: {v3_lines} 条 / Turbo: {turbo_lines} 条"
                  + ("（mismatch 回退）" if _is_retry else ""))

            user_input = (
                "以下の2つの字幕ファイルは、同じ日本語音声を異なるWhisperモデルで書き起こしたものです。\n\n"
                "【字幕A：large-v3（高精度）】\n```srt\n" + v3_text + "\n```\n\n"
                "【字幕B：large-v3-turbo（高速）】\n```srt\n" + turbo_text + "\n```\n\n"
            )
            if quarantined:
                user_input += (
                    "\n【監査情報】プログラムが音響的な裏付けのない既知の定型幻覚候補を"
                    f"{len(quarantined)}件隔離済みです。隔離された文を推測で復元しないでください。\n"
                )
            user_input += "2つの字幕を比較し、より正確な字幕を1つに統合してください。"
            system_prompt = (
                "あなたは日本語音声認識の字幕校正エキスパートです。"
                "2つのWhisperモデルの字幕を比較・統合してください。\n"
                "1. 時刻が近いもの同士を同じ発話とみなし、より自然な方を採用\n"
                "2. 文法的正しさ・文脈整合性・同音異義語誤りをチェック\n"
                "3. 断片化は統合、重複は削除（意図的繰り返しは残す）\n"
                "4. 時刻は原則 V3 を採用、turbo のみの発話は turbo の時刻を使用\n"
                "5. わからない単語があれば web_search で検索して確認\n"
                "6. 【重要】片方のモデルにしか存在しない発話について：\n"
                "   囁き・擬音・叫び・吃音などの実在発話は、不自然または断片的という理由だけで削除しないこと。\n"
                "   一方、文脈にも他方のモデルにも裏付けられない定型文や無関係な文章を推測で補完しないこと。\n"
                "6.5. 【最重要】冒頭の字幕（最初の数行）の扱い：\n"
                "   音声の冒頭は環境音や囁きのために認識精度が低下することがありますが、\n"
                "   それは実際の台詞が存在しないことを意味しません。\n"
                "   字幕A・字幕Bのどちらか一方にしかない冒頭発話も、文脈と音声認識結果を慎重に評価してください。\n"
                "   冒頭の発話は断片的でも挨拶や状況説明であることが多いため、\n"
                "   それを削除するとストーリーの冒頭が失われます。\n"
                "   たとえ「あ…」「えっと…」のような一言でも残してください。\n"
                "   判断が難しい場合は、実在しうる短い発話を勝手に整文せず原形に近く保ってください。\n"
                "7. 「(中略)」「(省略)」などの省略記号を絶対に使用しないでください。\n"
                "【提出方法（絶対厳守）】\n"
                "統合が完了したら、submit_fusion 関数を呼び出して結果を提出してください。\n"
                "subtitles配列の各要素に timecode と text を含めてください。\n"
                "番号は不要です（自動付与されます）。説明や分析は一切不要です。"
            )

    input_len = len(user_input)
    # 修复：CJK 文本约 1 token/字符（原 //2 低估约一半，长音频会误判
    # 通过 → API 超长 → 重试全部失败）。按 1:1 保守估算。
    estimated_tokens = input_len
    print(f"{log_prefix}📨 发送融合请求：{input_len} 字符（约 {estimated_tokens} tokens）")

    if estimated_tokens > 100000:
        print(f"{log_prefix}⚠ 输入过大，可能超出模型限制。建议将音频切割后再处理。")
        return ("failed", None)

    if ENABLE_SEARCH:
        system_prompt += "   不确定的文化概念请先 web_search 搜索确认。\n\n"

    result = call_deepseek(
        client, system_prompt, user_input,
        verbose=True, enable_search=ENABLE_SEARCH,
        output_tool=output_tool,
        output_tool_parser=output_parser,
        stream=False,
        log_prefix=log_prefix,
        show_reasoning=False
    )

    # 检测台本不匹配（仅台本模式）
    if use_script and result and result.startswith("__SCRIPT_MISMATCH__:"):
        error_msg = result[len("__SCRIPT_MISMATCH__:"):]
        print(f"{log_prefix}⚠ 台本不匹配：{error_msg}，回退到无台本模式（V3+Turbo）")
        _mismatch_files.add(base)
        _write_pending_script_fusion_manifest(
            audio_path, script_text, script_inputs_snapshot,
        )
        retry_result = fuse_one_audio(audio_path, log_prefix, "", _is_retry=True)
        if retry_result[0] == "ok":
            _write_script_fusion_manifest(
                audio_path, script_text, "mismatch_fallback",
                expected_inputs=script_inputs_snapshot,
            )
        return retry_result

    if not result:
        print(f"{log_prefix}❌ 融合失败（可能原因：API 错误 / max_tokens 不足 / 输出格式异常）")
        print(f"{log_prefix}   输入大小：{input_len} 字符，V3：{v3_lines} 条")
        return ("failed", None)

    if deterministic_timeline is not None:
        try:
            fixed = render_timeline_srt(deterministic_timeline, result)
        except Exception as e:
            print(f"{log_prefix}❌ 确定性融合响应无效，未写入字幕：{e}")
            return ("failed", None)
    else:
        fixed = extract_srt_from_response(result)
    if '-->' not in fixed:
        raw = os.path.join(OUTPUT_DIR, f"{base}_raw.txt")
        with open(raw, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"{log_prefix}⚠ 格式异常，原始响应 → {raw}")
        return ("failed", None)

    fixed = _fix_srt_indexing(fixed)
    if deterministic_timeline is not None:
        manifest_base = {
            "schema_version": 1,
            "artifact_type": "fusion_manifest",
            "contract_version": deterministic_timeline["contract_version"],
            "timeline_fingerprint": deterministic_timeline["timeline_fingerprint"],
            "fusion_generation_fingerprint": _fusion_generation_fingerprint(),
        }
        write_json_atomic(fusion_manifest, {
            **manifest_base,
            "status": "pending",
            "output_srt": None,
        })
        write_text_atomic(ensemble_srt, fixed)
        write_json_atomic(fusion_manifest, {
            **manifest_base,
            "status": "complete",
            "output_srt": file_fingerprint(ensemble_srt),
        })
    elif use_script:
        staged_srt = f"{ensemble_srt}.pending-{time.time_ns()}"
        try:
            write_text_atomic(staged_srt, fixed + "\n")
            _write_script_fusion_manifest(
                audio_path,
                script_text,
                "matched",
                expected_inputs=script_inputs_snapshot,
                output_path=staged_srt,
            )
            os.replace(staged_srt, ensemble_srt)
        except Exception:
            try:
                os.unlink(staged_srt)
            except OSError:
                pass
            raise
    else:
        if legacy_inputs_snapshot is None:
            raise ValueError("legacy fusion input snapshot is missing")
        if _legacy_fusion_inputs(audio_path, v3_srt, turbo_srt) != legacy_inputs_snapshot:
            raise ValueError("legacy fusion inputs changed during processing")
        staged_srt = f"{ensemble_srt}.pending-{time.time_ns()}"
        try:
            write_text_atomic(staged_srt, fixed + "\n")
            staged_output = file_fingerprint(staged_srt)
            staged_output["name"] = os.path.basename(ensemble_srt)
            manifest_base = {
                "schema_version": 1,
                "artifact_type": "fusion_manifest",
                "contract_version": LEGACY_FUSION_CONTRACT_VERSION,
                "timeline_fingerprint": canonical_fingerprint(
                    legacy_inputs_snapshot
                ),
                "fusion_generation_fingerprint": (
                    _legacy_fusion_generation_fingerprint()
                ),
            }
            write_json_atomic(fusion_manifest, {
                **manifest_base,
                "status": "complete",
                "output_srt": staged_output,
            })
            if (
                _legacy_fusion_inputs(audio_path, v3_srt, turbo_srt)
                != legacy_inputs_snapshot
            ):
                raise ValueError("legacy fusion inputs changed before commit")
            os.replace(staged_srt, ensemble_srt)
        except Exception:
            try:
                os.unlink(staged_srt)
            except OSError:
                pass
            raise
    if use_script:
        _matched_script_files.add(base)
    el = len(re.findall(r'\n\n+', fixed.strip())) + 1
    print(f"{log_prefix}✅ {ensemble_srt} ({el} 条)")
    return ("ok", ensemble_srt)


def _write_merged_script_mismatches(active_bases):
    """保留未重新判定的历史 mismatch，只更新本轮确实得到结论的音频。"""
    previous = set()
    if os.path.exists(MISMATCH_FILE):
        try:
            with open(MISMATCH_FILE, "r", encoding="utf-8-sig") as handle:
                value = json.load(handle)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                previous = set(value)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

    active = set(active_bases)
    merged = ((previous & active) - _matched_script_files) | (_mismatch_files & active)
    write_json_atomic(MISMATCH_FILE, sorted(merged))
    return merged


def check_script_mapping(*, force_script_units=False):
    """启动时检查台本映射文件与验证标记，返回 {audio_base: script_text} 字典。"""
    _script_contexts.clear()
    _script_exact_texts.clear()
    _script_exact_paths.clear()
    if not ENABLE_SCRIPT:
        return {}

    if not os.path.exists(SCRIPT_MAPPING_FILE):
        print("❌ 台本映射文件不存在，请先运行 match_scripts.py")
        sys.exit(1)
    if not os.path.exists(SCRIPT_VERIFIED_FILE):
        print("❌ 台本映射未通过人工验证，请运行 match_scripts.py 完成验证")
        sys.exit(1)

    try:
        verified_audio_files = collect_audio_files()
        validate_mapping_verification(
            SCRIPT_VERIFIED_FILE,
            SCRIPT_MAPPING_FILE,
            verified_audio_files,
            PROJECT_DIR,
        )
    except Exception as e:
        print(f"❌ 台本映射验证已失效，请重新运行 match_scripts.py：{e}")
        sys.exit(1)

    with open(SCRIPT_MAPPING_FILE, "r", encoding="utf-8") as f:
        mapping = json.load(f)

    # 检查台本文件是否在验证后被修改
    verified_mtime = os.path.getmtime(SCRIPT_VERIFIED_FILE)
    for audio_base, info in mapping.items():
        script_path = resolve_mapping_path(info.get("script_path"), PROJECT_DIR)
        if script_path and script_path.exists():
            if script_path.stat().st_mtime > verified_mtime:
                print(f"❌ 台本 {script_path} 在验证后被修改，请重新运行 match_scripts.py")
                sys.exit(1)

    # 读取所有台本文本
    script_texts = {}
    for audio_base, info in mapping.items():
        raw_script_path = info.get("script_path")
        script_path = resolve_mapping_path(raw_script_path, PROJECT_DIR)
        script_source = resolve_mapping_path(info.get("script_source"), PROJECT_DIR)
        if raw_script_path is None:
            script_texts[audio_base] = ""
            continue
        if script_path is None or not script_path.is_file():
            print(f"❌ 已验证台本路径当前不可用，拒绝静默退化无台本模式：{raw_script_path}")
            sys.exit(1)
        try:
            script_texts[audio_base] = read_text_file(str(script_path)).strip()
            _script_contexts[audio_base] = {
                "mapping_entry_fingerprint": canonical_fingerprint(info),
                "script_source": _optional_file_fingerprint(
                    str(script_source) if script_source else None
                ),
                "script_path": _optional_file_fingerprint(str(script_path)),
            }
        except Exception as e:
            print(f"❌ 已验证台本 {script_path} 读取失败，拒绝静默退化无台本模式：{e}")
            sys.exit(1)
        if ENABLE_SCRIPT_UNITS_SHADOW or force_script_units:
            try:
                _script_exact_texts[audio_base] = read_script_text_exact(str(script_path))
                _script_exact_paths[audio_base] = str(script_path)
            except Exception as e:
                # The optional shadow must not affect the established script flow.
                print(f"  Script-units shadow could not read {script_path}; skipped: {e}")
    try:
        validate_mapping_verification(
            SCRIPT_VERIFIED_FILE,
            SCRIPT_MAPPING_FILE,
            collect_audio_files(),
            PROJECT_DIR,
        )
    except Exception as e:
        print(f"❌ 台本 mapping/脚本在读取期间发生变化，拒绝使用混合快照：{e}")
        sys.exit(1)
    return script_texts


def _script_units_llm_config():
    _, base_url, model, enable_thinking, max_tokens = get_llm_config()
    return {
        "base_url": base_url,
        "model": model,
        "enable_thinking": enable_thinking,
        "max_tokens": max_tokens,
    }


def run_script_units_shadow(audio_files, *, force=False):
    """Build optional script-structure sidecars; never alter subtitle output."""
    if not ENABLE_SCRIPT or (not ENABLE_SCRIPT_UNITS_SHADOW and not force):
        return {}
    scripts = {}
    for audio_path in audio_files:
        base = os.path.splitext(os.path.basename(audio_path))[0]
        script_text = _script_exact_texts.get(base, "")
        if script_text:
            scripts[base] = script_text
    if not scripts:
        print("  Script-units shadow: no non-empty scripts; skipped")
        return {}

    try:
        llm_config = _script_units_llm_config()
    except Exception as exc:
        print(f"  Script-units shadow could not initialize; continuing: {exc}")
        return {}
    results = {}
    client = None
    for base, script_text in scripts.items():
        output_path = os.path.join(OUTPUT_DIR, f"{base}_script_units.json")
        source_path = _script_exact_paths.get(base)
        try:
            cached = load_current_script_units(
                output_path,
                script_text,
                script_units_generation_fingerprint(llm_config),
            )
            if cached is not None:
                if not source_path or read_script_text_exact(source_path) != script_text:
                    raise ValueError("script source changed during cache validation")
                results[base] = cached
                print(f"  Script-units shadow {base}: cached")
                continue
            if client is None:
                raw_client = get_llm_client()
                with_options = getattr(raw_client, "with_options", None)
                if not callable(with_options):
                    raise RuntimeError(
                        "LLM client cannot enforce a bounded request timeout"
                    )
                client = with_options(
                    timeout=SCRIPT_UNITS_API_TIMEOUT_SECONDS,
                    max_retries=0,
                )
            status, artifact = generate_script_units_shadow(
                client,
                call_deepseek,
                script_text,
                output_path,
                llm_config,
                log_prefix=f"[{base}/script-units] ",
                input_is_current=lambda path=source_path, expected=script_text: (
                    bool(path) and read_script_text_exact(path) == expected
                ),
            )
            results[base] = artifact
            print(f"  Script-units shadow {base}: {status}")
        except Exception as exc:
            # Shadow analysis must never change or block production subtitles.
            print(f"  Script-units shadow {base} failed; continuing: {exc}")
            break
    return results


def script_unit_source_is_current(base):
    source_path = _script_exact_paths.get(base)
    expected = _script_exact_texts.get(base)
    return (
        bool(source_path)
        and isinstance(expected, str)
        and read_script_text_exact(source_path) == expected
    )


def collect_audio_files():
    files = []
    if os.path.isfile(AUDIO_DIR):
        return [AUDIO_DIR]
    if os.path.isdir(AUDIO_DIR):
        for f in sorted(os.listdir(AUDIO_DIR)):
            if any(f.lower().endswith(ext) for ext in AUDIO_EXTS):
                files.append(os.path.join(AUDIO_DIR, f))
    return files


def _check_duplicate_bases(audio_files):
    """检测去掉扩展名后重名的音频（如 foo.mp3 + foo.wav）。
    重名会导致输出 SRT/预处理缓存互相覆盖，返回重复对列表 [(旧, 新), ...]。"""
    seen, dups = {}, []
    for af in audio_files:
        b = os.path.splitext(os.path.basename(af))[0]
        if b in seen:
            dups.append((seen[b], af))
        else:
            seen[b] = af
    return dups


def main():
    reset_usage()

    audio_files = collect_audio_files()
    if not audio_files:
        print(f"❌ 在 {AUDIO_DIR} 中未找到音频文件！")
        print(f"   支持的格式：{', '.join(AUDIO_EXTS)}")
        sys.exit(1)

    # 修复：检测重名 base（如 foo.mp3 + foo.wav），重名会导致输出
    # SRT/预处理缓存互相覆盖。明确报错退出，而非静默覆盖。
    dups = _check_duplicate_bases(audio_files)
    if dups:
        print(f"❌ 检测到文件名冲突（去掉扩展名后重名，输出会互相覆盖）：")
        for a, b in dups:
            print(f"   • {os.path.basename(a)} 与 {os.path.basename(b)}")
        print(f"   请重命名其中一个文件后重新运行。")
        sys.exit(1)

    # V3.2 新增：台本映射检查
    script_texts = check_script_mapping()
    if ENABLE_SCRIPT:
        with_open = sum(1 for v in script_texts.values() if v)
        print(f"📜 台本功能已开启：{with_open} / {len(audio_files)} 个音频有台本")
    print("=" * 60)
    print("  Whisper Ensemble Batch — 批量双模型融合转写")
    print(f"  模式：阶段1串行转写 + 阶段2并行融合（并发数 {MAX_WORKERS}）")
    if ENABLE_SCRIPT:
        print(f"  台本辅助：开启")
    print("=" * 60)
    print(f"📋 {len(audio_files)} 个文件")
    for f in audio_files:
        print(f"   • {f}")
    print(f"📂 输出：{OUTPUT_DIR}/")
    print(f"🔍 搜索：{'开' if ENABLE_SEARCH else '关'}")
    print()

    t0 = time.time()

    # ================================================================
    #  阶段1：串行转写所有文件（GPU 独占）
    # ================================================================
    print(f"\n{'#'*60}")
    print("# 阶段1：双模型转写（串行，GPU 独占）")
    print(f"{'#'*60}\n")

    transcribed = []
    for i, af in enumerate(audio_files, 1):
        print(f"\n{'#'*50}")
        print(f"### [转写 {i}/{len(audio_files)}] {os.path.basename(af)}")
        print(f"{'#'*50}")
        base = os.path.splitext(os.path.basename(af))[0]
        has_script = bool(script_texts.get(base, ""))
        try:
            transcribe_one_audio(af, has_script=has_script)
            transcribed.append(af)
        except Exception as e:
            print(f"  ❌ 转写异常：{e}")

    # ================================================================
    #  阶段2：并行融合所有文件（网络 I/O）
    # ================================================================
    print(f"\n{'#'*60}")
    print(f"# 阶段2：LLM 融合（并行，并发数 {MAX_WORKERS}）")
    print(f"{'#'*60}\n")

    # 预初始化 client，避免多线程首次调用时重复创建
    get_llm_client()

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for af in transcribed:
            base = os.path.splitext(os.path.basename(af))[0]
            log_prefix = f"[{base}] "
            script_text = script_texts.get(base, "")  # V3.2 新增
            future = executor.submit(
                fuse_one_audio, af, log_prefix, script_text
            )
            futures[future] = af

        for future in as_completed(futures):
            af = futures[future]
            base = os.path.splitext(os.path.basename(af))[0]
            try:
                status, data = future.result()
                if status == "ok":
                    results.append((af, data))
                elif status == "pending_turbo":
                    # 已加入阶段2.5 补跑队列，结果稍后填充
                    print(f"[{os.path.basename(af)}] ⏳ 等待阶段2.5 补跑 Turbo")
                else:
                    results.append((af, None))
            except Exception as e:
                print(f"[{os.path.basename(af)}] ❌ 融合异常: {e}")
                results.append((af, None))

    # ================================================================
    #  阶段2.5：串行补跑 Turbo + 重新融合（GPU 独占，主线程执行）
    #  说明：mismatch 回退或阶段1 转写失败导致 Turbo 缺失的文件，不能在
    #  线程池 worker 里补跑 GPU 转写（多线程并发 CUDA 会争用/崩溃），
    #  统一在此串行补跑后重新融合。
    # ================================================================
    if _needs_turbo:
        print(f"\n{'#'*60}")
        print(f"# 阶段2.5：串行补跑 Turbo 并重新融合（{len(_needs_turbo)} 个文件）")
        print(f"{'#'*60}\n")
        for af in transcribed:
            base = os.path.splitext(os.path.basename(af))[0]
            if base not in _needs_turbo:
                continue
            log_prefix = f"[{base}] "
            try:
                script_inputs_snapshot = None
                if base in _mismatch_files and script_texts.get(base):
                    script_inputs_snapshot = _script_fusion_inputs(
                        af,
                        os.path.join(OUTPUT_DIR, f"{base}_v3.srt"),
                        script_texts[base],
                    )
                turbo_srt = os.path.join(OUTPUT_DIR, f"{base}_turbo.srt")
                if not os.path.exists(turbo_srt):
                    transcribe_with_model("large-v3-turbo", af, turbo_srt)
                rescue_artifact = _write_shadow_decisions(af)
                _execute_rescue_if_enabled(af, rescue_artifact)
                status, data = fuse_one_audio(af, log_prefix, "", _is_retry=True)
                if status == "ok" and base in _mismatch_files and script_texts.get(base):
                    _write_script_fusion_manifest(
                        af,
                        script_texts[base],
                        "mismatch_fallback",
                        expected_inputs=script_inputs_snapshot,
                    )
                results.append((af, data) if status == "ok" else (af, None))
            except Exception as e:
                print(f"[{base}] ❌ 阶段2.5 补跑异常: {e}")
                results.append((af, None))

    # 只覆盖本轮已重新判定的状态；缓存跳过的音频保留历史 mismatch。
    # Timing-only V3 pass remains serial because it shares STEP1's GPU model cache.
    if ENABLE_LONG_CUE_ALIGNMENT:
        print(f"\n{'#'*60}")
        print("# 阶段2.75：异常长字幕局部定时（串行，GPU 独占）")
        print(f"{'#'*60}\n")
        successful_paths = {path for path, output in results if output}
        for af in transcribed:
            if af not in successful_paths:
                continue
            base = os.path.splitext(os.path.basename(af))[0]
            if script_texts.get(base) and base not in _mismatch_files:
                print(f"[{base}] 台本模式暂不启用长字幕旁路")
                continue
            ensemble_srt = os.path.join(OUTPUT_DIR, f"{base}_ensemble.srt")
            try:
                report = _run_long_cue_alignment(af, ensemble_srt)
                if report:
                    summary = report["summary"]
                    print(
                        f"[{base}] 长字幕 {summary['detected']} 条："
                        f"缩边界 {summary['aligned']} / 拆分 {summary['split']} / "
                        f"未解决 {summary['unresolved']}（{report['mode']}）"
                    )
                else:
                    print(f"[{base}] 没有超过 {LONG_CUE_MAX_SECONDS:g}s 的字幕")
            except Exception as exc:
                # Fail closed: preserve the already valid ensemble SRT.
                print(f"[{base}] 长字幕定时旁路失败，保留原字幕：{exc}")

    active_bases = [os.path.splitext(os.path.basename(path))[0] for path in audio_files]
    effective_mismatches = _write_merged_script_mismatches(active_bases)

    if effective_mismatches:
        print(f"\n⚠ 检测到 {len(effective_mismatches)} 个文件台本不匹配，已回退无台本模式：")
        for name in sorted(effective_mismatches):
            print(f"   • {name}")
        print(f"   已记录至 {MISMATCH_FILE}")
        print(f"   下游 STEP2（日语二审）将仅对这些文件执行")
    else:
        print(f"\n✅ 无台本不匹配（mismatch 列表为空）")
        if ENABLE_SCRIPT:
            print(f"   下游 STEP2（日语二审）可跳过")

    print(f"\n{'='*60}")
    print(f"🏁 完成！总耗时 {(time.time()-t0)/60:.1f} 分钟")
    print(f"{'='*60}")
    for af, out in results:
        print(f"  {'✅' if out else '❌'} {os.path.basename(af)}")
    print(f"\n💡 下一步：output/*_ensemble.srt → review_japanese.py")

    print()
    print(get_usage_report())

    successful_audio = {audio_path for audio_path, output in results if output}
    failed_audio = [
        audio_path for audio_path in audio_files
        if audio_path not in successful_audio
    ]
    if failed_audio:
        print(f"\n❌ STEP1 未完成：{len(failed_audio)} 个音频没有有效融合字幕")
        for audio_path in failed_audio:
            print(f"   - {os.path.basename(audio_path)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
