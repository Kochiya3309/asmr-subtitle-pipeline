# SPDX-License-Identifier: GPL-3.0-or-later
"""Small input-bound manifests for downstream subtitle stages."""

from __future__ import annotations

from output_layout import artifact_name

import hashlib
import json
import os
import shutil
import tempfile
import time

from asr_evidence import write_json_atomic


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_cache_is_current(
    manifest_path: str,
    artifact_type: str,
    input_path: str,
    output_path: str,
    generation_fingerprint: str | None,
) -> bool:
    if not os.path.isfile(manifest_path) or not os.path.isfile(output_path):
        return False
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as handle:
            manifest = json.load(handle)
        required = {
            "schema_version", "artifact_type", "status", "generation_fingerprint",
            "input_name", "input_sha256", "output_name", "output_sha256",
        }
        return (
            isinstance(manifest, dict)
            and set(manifest) == required
            and manifest["schema_version"] == 1
            and manifest["artifact_type"] == artifact_type
            and manifest["status"] == "complete"
            and (
                generation_fingerprint is None
                or manifest["generation_fingerprint"] == generation_fingerprint
            )
            and manifest["input_name"] == artifact_name(input_path)
            and manifest["input_sha256"] == file_sha256(input_path)
            and manifest["output_name"] == artifact_name(output_path)
            and manifest["output_sha256"] == file_sha256(output_path)
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


def write_artifact_manifest(
    manifest_path: str,
    artifact_type: str,
    input_path: str,
    output_path: str,
    generation_fingerprint: str,
) -> None:
    write_json_atomic(manifest_path, {
        "schema_version": 1,
        "artifact_type": artifact_type,
        "status": "complete",
        "generation_fingerprint": generation_fingerprint,
        "input_name": artifact_name(input_path),
        "input_sha256": file_sha256(input_path),
        "output_name": artifact_name(output_path),
        "output_sha256": file_sha256(output_path),
    })


def parse_file_snapshot(path: str, parser):
    """Parse one immutable byte snapshot and return (parsed, sha256)."""
    with open(path, "rb") as handle:
        raw = handle.read()
    input_sha256 = hashlib.sha256(raw).hexdigest()
    directory = os.path.dirname(os.path.abspath(path))
    fd, snapshot_path = tempfile.mkstemp(
        prefix=".pipeline-input-", suffix=os.path.splitext(path)[1], dir=directory,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        parsed = parser(snapshot_path)
    finally:
        try:
            os.unlink(snapshot_path)
        except OSError:
            pass
    return parsed, input_sha256


def commit_text_artifact(
    manifest_path: str,
    artifact_type: str,
    input_path: str,
    expected_input_sha256: str,
    output_path: str,
    text: str,
    generation_fingerprint: str,
) -> None:
    """Commit manifest first and canonical text last, rejecting changed input."""
    if file_sha256(input_path) != expected_input_sha256:
        raise ValueError("pipeline input changed during processing")
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    fd, staged_path = tempfile.mkstemp(
        prefix=".pipeline-output-", suffix=".srt", dir=output_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        staged_sha256 = file_sha256(staged_path)
        if file_sha256(input_path) != expected_input_sha256:
            raise ValueError("pipeline input changed during processing")
        write_json_atomic(manifest_path, {
            "schema_version": 1,
            "artifact_type": artifact_type,
            "status": "complete",
            "generation_fingerprint": generation_fingerprint,
            "input_name": artifact_name(input_path),
            "input_sha256": expected_input_sha256,
            "output_name": artifact_name(output_path),
            "output_sha256": staged_sha256,
        })
        os.replace(staged_path, output_path)
    except Exception:
        try:
            os.unlink(staged_path)
        except OSError:
            pass
        raise


def backup_stale_artifacts(output_path: str, manifest_path: str) -> None:
    existing = [path for path in (output_path, manifest_path) if os.path.isfile(path)]
    if not existing:
        return
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(output_path)), "_review_backups")
    os.makedirs(backup_dir, exist_ok=True)
    suffix = time.time_ns()
    for path in existing:
        shutil.copy2(
            path,
            os.path.join(backup_dir, f"{artifact_name(path)}.{suffix}.bak"),
        )
