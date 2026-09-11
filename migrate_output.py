# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview/apply a reversible flat-output migration; no API or ASR calls."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from output_layout import ARTIFACTS, BASE_EMBEDDED, SHARED, canonical_output_file


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_embedded_artifacts(root) -> list[tuple[Path, Path]]:
    """Old hierarchical runs kept embedded-named files as <base>/<stage>/<suffix>."""
    root = Path(root).resolve()
    rows = []
    for audio in sorted(root.iterdir()):
        if not audio.is_dir() or audio.name.startswith(".") or audio.name in SHARED:
            continue
        for suffix in BASE_EMBEDDED:
            stage = ARTIFACTS[suffix][0]
            legacy = audio / stage / suffix
            if not legacy.is_file():
                continue
            logical = f"{audio.name}_{suffix}"
            target = Path(canonical_output_file(root, logical))
            if target.exists():
                raise FileExistsError(f"迁移目标已存在，需人工确认：{target}")
            if legacy.is_symlink() or legacy.resolve() != legacy:
                raise ValueError(f"不能自动迁移链接：{legacy}")
            rows.append((legacy, target))
    return rows


def migration_plan(root) -> list[tuple[Path, Path]]:
    root = Path(root).resolve()
    if not root.is_dir():
        return []
    plan = []
    for source in sorted(root.iterdir()):
        if source.name.startswith("."):
            continue
        if source.is_dir() and source.name not in SHARED:
            continue
        try:
            target = Path(canonical_output_file(root, source.name))
        except ValueError:
            # Unknown user files stay in place, never guessed or deleted.
            continue
        if source.is_symlink() or source.resolve() != source:
            raise ValueError(f"不能自动迁移链接：{source}")
        if target.exists():
            raise FileExistsError(f"迁移目标已存在，未覆盖：{target}")
        if source.is_dir():
            for item in source.rglob("*"):
                if item.is_symlink() or item.resolve() != item:
                    raise ValueError(f"不能自动迁移含链接的目录：{source}")
        plan.append((source, target))
    plan.extend(_legacy_embedded_artifacts(root))
    return plan


def _identity(path):
    if path.is_file():
        return {"kind": "file", "sha256": _sha256(path)}
    return {
        "kind": "directory",
        "files": {str(p.relative_to(path)): _sha256(p)
                  for p in sorted(path.rglob("*")) if p.is_file()},
        "directories": [str(p.relative_to(path)) for p in sorted(path.rglob("*"))
                        if p.is_dir()],
    }


def _write_journal(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def migrate_output(root) -> Path | None:
    root = Path(root).resolve()
    lock = root / ".output-layout.lock"
    if lock.exists():
        raise FileExistsError(f"输出目录正在迁移，请等待迁移完成：{lock}")
    plan = migration_plan(root)
    if not plan:
        return None
    # A second stage/process must not race an in-progress migration.
    with lock.open("x", encoding="ascii") as handle:
        handle.write(str(os.getpid()))
    try:
        plan = migration_plan(root)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        archive = root / "_shared" / "layout_backups" / stamp
        if not archive.resolve().is_relative_to(root):
            raise ValueError("迁移备份目录越出输出目录")
        archive.mkdir(parents=True)
        journal_path = archive / "migration.json"
        rows = []
        for source, target in plan:
            backup = archive / "original" / source.relative_to(root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            before = _identity(source)
            if source.is_dir():
                shutil.copytree(source, backup, copy_function=shutil.copy2)
            else:
                shutil.copy2(source, backup)
            if _identity(backup) != before or _identity(source) != before:
                raise ValueError(f"备份验证失败或源文件发生变化：{source}")
            rows.append({"source": str(source.relative_to(root)),
                         "target": str(target.relative_to(root)), "identity": before})
        journal = {"schema_version": 1, "root": str(root), "status": "pending", "moves": rows}
        _write_journal(journal_path, journal)
        for (source, target), row in zip(plan, rows):
            if _identity(source) != row["identity"]:
                raise ValueError(f"迁移期间源文件发生变化：{source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            # rename refuses overwrite on Windows; precheck also covers POSIX.
            if target.exists():
                raise FileExistsError(target)
            source.rename(target)
        journal["status"] = "complete"
        _write_journal(journal_path, journal)
        print(f"输出目录已分类：{len(plan)} 项；原件备份及回退记录：{journal_path}")
        return journal_path
    finally:
        lock.unlink()


def rollback_migration(journal_path):
    journal_path = Path(journal_path).resolve()
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    root = Path(journal["root"]).resolve()
    if not journal_path.is_relative_to(root / "_shared" / "layout_backups"):
        raise ValueError("回退记录不在对应输出目录中")
    pairs = []
    for row in journal["moves"]:
        source, target = (root / row[key] for key in ("source", "target"))
        if any(not p.resolve().is_relative_to(root) or p.resolve() == root
               for p in (source, target)):
            raise ValueError("回退路径越出输出目录")
        if source.exists() and not target.exists():
            if _identity(source) != row["identity"]:
                raise ValueError(f"原路径内容已改变：{source}")
            continue
        if source.exists() or not target.exists() or _identity(target) != row["identity"]:
            raise ValueError(f"迁移后产物已改变或路径冲突，不能自动回退：{target}")
        pairs.append((source, target))
    for source, target in reversed(pairs):
        source.parent.mkdir(parents=True, exist_ok=True)
        target.rename(source)
    journal["status"] = "rolled_back"
    _write_journal(journal_path, journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=os.environ.get("OUTPUT_DIR", "./output"))
    parser.add_argument("--apply", action="store_true", help="备份并执行迁移；默认只预览")
    parser.add_argument("--rollback", help="按 migration.json 回退尚未改变的产物")
    args = parser.parse_args()
    if args.rollback:
        if args.apply:
            parser.error("--apply 与 --rollback 不能同时使用")
        rollback_migration(args.rollback)
    elif args.apply:
        migrate_output(args.output_dir)
    else:
        for source, target in migration_plan(args.output_dir):
            print(f"{source} -> {target}")


if __name__ == "__main__":
    main()
