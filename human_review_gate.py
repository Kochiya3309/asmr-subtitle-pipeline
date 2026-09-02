# SPDX-License-Identifier: GPL-3.0-or-later
"""Sequential STEP3.5 gate for optional local human review."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import webbrowser

from human_review import validate_completed_review_outputs
from review_app import build_bundle_for_base, create_review_server
from pipeline_inputs import discover_active_audio


PIPELINE_PAUSE_EXIT_CODE = 75
DEFAULT_REVIEW_PORT = 8765


def _review_port_env() -> int:
    try:
        port = int(os.environ.get("HUMAN_REVIEW_PORT", str(DEFAULT_REVIEW_PORT)))
    except (TypeError, ValueError):
        return DEFAULT_REVIEW_PORT
    return port if 0 <= port <= 65535 else DEFAULT_REVIEW_PORT


def discover_review_bases(output_dir: Path, audio_dir: Path) -> list[str]:
    active = discover_active_audio(audio_dir)
    for base in active:
        expected = output_dir / f"{base}_zh.srt"
        if not expected.is_file():
            raise FileNotFoundError(f"当前音频缺少 STEP3 产物：{expected}")
    return sorted(active)


def _is_current_review(job: dict) -> bool:
    try:
        _validate_review_against_current_sources(job)
    except (FileNotFoundError, ValueError, OSError):
        return False
    return True


def _validate_review_against_current_sources(job: dict) -> None:
    current_job = build_bundle_for_base(
        job["base"], job["output_dir"], job["audio_dir"]
    )
    validate_completed_review_outputs(
        job["output_srt"],
        job["result_json"],
        expected_bundle_id=current_job["bundle"]["bundle_id"],
        expected_bundle_fingerprint=current_job["bundle"]["bundle_fingerprint"],
        current_input_srt=current_job["input_srt"],
    )


def review_one(job: dict, *, port: int = DEFAULT_REVIEW_PORT, open_browser: bool = True) -> bool:
    server = create_review_server(
        bundle=job["bundle"],
        audio_path=job["audio_path"],
        output_srt=job["output_srt"],
        result_json=job["result_json"],
        port=port,
    )
    server.exit_after_submit = True
    url = server.expected_origin + "/"
    print(f"  人工复核页面：{url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  人工复核已正常暂停；审核产物未提交，后续步骤不会运行。")
        return False
    finally:
        server.server_close()
    _validate_review_against_current_sources(job)
    return True


def main() -> int:
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="逐个完成 STEP3 后的本机人工复核")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(os.environ.get("OUTPUT_DIR", project / "output")),
    )
    parser.add_argument(
        "--audio-dir", type=Path,
        default=Path(os.environ.get("AUDIO_DIR", project / "audio")),
    )
    parser.add_argument("--port", type=int, default=_review_port_env())
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    output_dir = args.output_dir.resolve()
    audio_dir = args.audio_dir.resolve()
    bases = discover_review_bases(output_dir, audio_dir)
    if not bases:
        raise FileNotFoundError(f"未找到 {output_dir / '*_zh.srt'}")
    print(f"检测到 {len(bases)} 个双语字幕，开始人工复核门控。")
    for position, base in enumerate(bases, start=1):
        print(f"\n[{position}/{len(bases)}] {base}")
        job = build_bundle_for_base(base, output_dir, audio_dir)
        if _is_current_review(job):
            print("  已有与当前字幕匹配的完整人工审核，跳过。")
            continue
        if job["output_srt"].exists() or job["result_json"].exists():
            print("  检测到旧版或不完整审核产物；重新提交时会自动备份旧文件。")
        if not review_one(
            job, port=args.port, open_browser=not args.no_open,
        ):
            return PIPELINE_PAUSE_EXIT_CODE
        print("  人工复核已提交并校验通过。")
    print("\n全部人工复核已完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
