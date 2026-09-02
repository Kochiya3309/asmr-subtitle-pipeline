# SPDX-License-Identifier: GPL-3.0-or-later
"""Content-bound verification state for script/audio mappings."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from asr_evidence import canonical_fingerprint, write_json_atomic


SCHEMA_VERSION = 1
ARTIFACT_TYPE = "script_mapping_verification"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_path(value, project_dir: Path) -> Path | None:
    if not value or not isinstance(value, (str, os.PathLike)):
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (project_dir / path).resolve()


def resolve_mapping_path(value, project_dir) -> Path | None:
    return _resolve_path(value, Path(project_dir).resolve())


def _small_file_identity(value, project_dir: Path):
    path = _resolve_path(value, project_dir)
    if path is None:
        return None
    if not path.is_file():
        raise FileNotFoundError(path)
    stat = path.stat()
    return {
        "path_fingerprint": hashlib.sha256(
            os.path.normcase(str(path)).encode("utf-8")
        ).hexdigest(),
        "size_bytes": stat.st_size,
        "sha256": _sha256(path),
    }


def _audio_identity(path: Path):
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    stat = path.stat()
    return {
        "name": path.name,
        "path_fingerprint": hashlib.sha256(
            os.path.normcase(str(path)).encode("utf-8")
        ).hexdigest(),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def build_mapping_verification(mapping_path, audio_paths, project_dir) -> dict:
    mapping_file = Path(mapping_path).resolve()
    project = Path(project_dir).resolve()
    with mapping_file.open("r", encoding="utf-8-sig") as handle:
        mapping = json.load(handle)
    if not isinstance(mapping, dict):
        raise ValueError("script mapping must be an object")

    audio_by_base = {}
    for value in audio_paths:
        path = _resolve_path(value, project)
        if path is None:
            raise ValueError("invalid audio path")
        base = path.stem
        if base in audio_by_base:
            raise ValueError(f"duplicate audio base: {base}")
        audio_by_base[base] = path
    if set(mapping) != set(audio_by_base):
        raise ValueError("script mapping audio set does not match current inputs")

    entries = {}
    for base in sorted(mapping):
        info = mapping[base]
        if not isinstance(info, dict):
            raise ValueError(f"invalid mapping entry: {base}")
        mapped_audio = _resolve_path(info.get("audio"), project)
        if mapped_audio is None or mapped_audio != audio_by_base[base]:
            raise ValueError(f"mapping audio path mismatch: {base}")
        entries[base] = {
            "mapping_entry_fingerprint": canonical_fingerprint(info),
            "audio": _audio_identity(audio_by_base[base]),
            "script_source": _small_file_identity(info.get("script_source"), project),
            "script_path": _small_file_identity(info.get("script_path"), project),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "status": "verified",
        "mapping_name": mapping_file.name,
        "mapping_sha256": _sha256(mapping_file),
        "entries": entries,
    }


def write_mapping_verification(path, mapping_path, audio_paths, project_dir) -> dict:
    document = build_mapping_verification(mapping_path, audio_paths, project_dir)
    write_json_atomic(str(path), document)
    return document


def validate_mapping_verification(
    verification_path, mapping_path, audio_paths, project_dir,
) -> dict:
    with Path(verification_path).open("r", encoding="utf-8-sig") as handle:
        recorded = json.load(handle)
    expected = build_mapping_verification(mapping_path, audio_paths, project_dir)
    if recorded != expected:
        raise ValueError("script mapping verification is stale")
    return recorded
