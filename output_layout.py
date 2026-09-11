# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical per-audio output paths and stable identities across relocation.

Manifests retain logical names (the original <base>_<suffix> names). Physical
paths are deliberately not part of these names, so moving an artifact does not
invalidate its content-bound cache or completed human review.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path


ARTIFACTS = {
    "source_audio.flac": ("input", "asr_source.flac"),
    "source_manifest.json": ("input", "source_manifest.json"),
    "preprocessed.wav": ("asr", "preprocessed.wav"),
    "preprocessed_manifest.json": ("asr", "preprocessed_manifest.json"),
    "v3.srt": ("asr", "v3.srt"),
    "v3_manifest.json": ("asr", "v3_manifest.json"),
    "turbo.srt": ("asr", "turbo.srt"),
    "turbo_manifest.json": ("asr", "turbo_manifest.json"),
    "asr_candidates.json": ("evidence", "candidates.json"),
    "asr_alignment.json": ("evidence", "alignment.json"),
    "asr_windows.json": ("evidence", "windows.json"),
    "asr_decisions.json": ("evidence", "decisions.json"),
    "asr_rescue_windows.json": ("evidence", "rescue_windows.json"),
    "asr_manifest.json": ("evidence", "manifest.json"),
    "ensemble.srt": ("review", "ensemble.srt"),
    "fusion_manifest.json": ("review", "fusion_manifest.json"),
    "script_fusion_manifest.json": ("review", "script_fusion_manifest.json"),
    "fusion_timeline.json": ("review", "fusion_timeline.json"),
    "long_cue_alignment.json": ("review", "long_cue_alignment.json"),
    "reviewed.srt": ("review", "reviewed.srt"),
    "reviewed_manifest.json": ("review", "reviewed_manifest.json"),
    "zh.srt": ("review", "zh.srt"),
    "zh_manifest.json": ("review", "zh_manifest.json"),
    "human_reviewed.srt": ("review", "human_reviewed.srt"),
    "human_review.json": ("review", "human_review.json"),
    "raw.txt": ("review", "raw.txt"),
    "script_units.json": ("script", "units.json"),
    "script_alignment.json": ("script", "alignment.json"),
    "final.srt": ("final", "final.srt"),
    "final_manifest.json": ("final", "final_manifest.json"),
    "cn_only.srt": ("final", "cn_only.srt"),
}
# Artifacts whose physical filename repeats <base> inside the stage folder,
# e.g. <base>_final.srt and <base>_cn_only.srt, so delivered subtitle files
# keep their audio identity next to the associated audio task.
BASE_EMBEDDED = {"cn_only.srt", "final.srt"}
SHARED = {"pipeline.log", "script_mapping.json", "script_mapping.verified",
          "script_mismatch.json", "scripts_split", "_review_backups"}
_REVERSE = {value: key for key, value in ARTIFACTS.items()}


def validate_base(base: str) -> str:
    if (not base or base in {".", ".."} or base.casefold() in {"_shared", "scripts_split", "_review_backups"}
            or any(char in base for char in '/\\:*?"<>|')
            or base.endswith((".", " "))):
        raise ValueError(f"无效或保留的音频任务名：{base!r}")
    return base


def split_artifact_name(name: str) -> tuple[str, str]:
    for suffix in sorted(ARTIFACTS, key=len, reverse=True):
        tail = "_" + suffix
        if name.endswith(tail):
            return validate_base(name[:-len(tail)]), suffix
    raise ValueError(f"未知的流水线产物：{name}")


def _contained(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError(f"产物路径越出输出目录：{path}")
    return path


def _base_embedded_name(path) -> str | None:
    """Return the logical name when a physical file repeats <base> in its name."""
    path = Path(path)
    for suffix in BASE_EMBEDDED:
        stage = ARTIFACTS[suffix][0]
        if path.parent.name != stage or not path.name.endswith(f"_{suffix}"):
            continue
        base = path.name[: -len(f"_{suffix}")]
        if base == path.parent.parent.name:
            return f"{base}_{suffix}"
    return None


def canonical_output_file(root, logical_name: str) -> str:
    root = Path(root).resolve()
    if logical_name in SHARED:
        return str(_contained(root, root / "_shared" / logical_name))
    base, suffix = split_artifact_name(logical_name)
    stage, name = ARTIFACTS[suffix]
    if suffix in BASE_EMBEDDED:
        name = f"{base}_{suffix}"
    return str(_contained(root, root / base / stage / name))


def output_file(root, logical_name: str) -> str:
    """Return the canonical path; stage entry points migrate legacy files first."""
    return canonical_output_file(root, logical_name)


def artifact_name(path) -> str:
    """Stable logical filename for fingerprints and audio-base extraction."""
    path = Path(path)
    suffix = _REVERSE.get((path.parent.name, path.name))
    if suffix:
        return f"{validate_base(path.parent.parent.name)}_{suffix}"
    embedded = _base_embedded_name(path)
    if embedded:
        return embedded
    return path.name


def output_root(path) -> Path:
    path = Path(path).resolve()
    if (path.parent.name, path.name) in _REVERSE or _base_embedded_name(path):
        return path.parent.parent.parent
    return path.parent


def logical_path(path) -> Path:
    """Location-independent legacy path for existing path-bound identities."""
    path = Path(path).resolve()
    if (path.parent.name, path.name) in _REVERSE or _base_embedded_name(path):
        return output_root(path) / artifact_name(path)
    for parent in path.parents:
        if parent.name == "scripts_split" and parent.parent.name == "_shared":
            return parent.parent.parent / "scripts_split" / path.relative_to(parent)
    return path


def resolve_relocated_script(path) -> Path:
    """Resolve old split-script references without rewriting signed mappings."""
    path = Path(path).resolve()
    if path.exists():
        return path
    for parent in path.parents:
        if parent.name == "scripts_split" and parent.parent.name != "_shared":
            target = parent.parent / "_shared" / "scripts_split" / path.relative_to(parent)
            if target.is_file():
                return _contained(parent.parent.resolve(), target)
    return path


def active_audio_bases_from_env() -> frozenset[str] | None:
    """Return the invocation-scoped audio bases, if a pipeline run supplied them."""
    raw = os.environ.get("ACTIVE_AUDIO_BASES")
    if raw is None:
        return None
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("ACTIVE_AUDIO_BASES must be a JSON array") from exc
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("ACTIVE_AUDIO_BASES must be a JSON array of audio bases")
    return frozenset(validate_base(value) for value in values)


def discover_outputs(root, pattern: str, *, active_bases=None) -> list[str]:
    """Scan known stages only, optionally limited to this pipeline invocation."""
    root = Path(root).resolve()
    if not root.is_dir():
        return []
    if active_bases is None:
        active_bases = active_audio_bases_from_env()
    else:
        active_bases = frozenset(validate_base(base) for base in active_bases)
    found = {}
    for path in root.iterdir():
        if path.is_file() and fnmatch.fnmatchcase(path.name, pattern):
            try:
                base, _ = split_artifact_name(path.name)
            except ValueError:
                base = None
            if active_bases is not None and base not in active_bases:
                continue
            found[path.name] = str(path)
        elif path.is_dir() and path.name != "_shared":
            if active_bases is not None and path.name not in active_bases:
                continue
            for suffix, (stage, name) in ARTIFACTS.items():
                logical_name = f"{path.name}_{suffix}"
                if not fnmatch.fnmatchcase(logical_name, pattern):
                    continue
                candidate = Path(canonical_output_file(root, logical_name))
                if candidate.is_file():
                    if logical_name in found:
                        raise FileExistsError(f"重复产物：{logical_name}")
                    found[logical_name] = str(candidate)
    # The order of iterdir must not decide which duplicate wins.
    for name in found:
        if (root / name).is_file():
            target = Path(canonical_output_file(root, name))
            if target.is_file():
                raise FileExistsError(f"新旧产物同时存在：{name}")
    return [found[name] for name in sorted(found)]


def prepare_output(root) -> None:
    # Lazy import keeps path helpers pure and avoids dependency cycles.
    from migrate_output import migrate_output
    migrate_output(root)
