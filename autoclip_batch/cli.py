from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .core import FFmpegBackend, Pipeline, SimulationBackend, discover_jobs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch-edit videos from a reusable JSON plan.")
    parser.add_argument("--plan", type=Path, default=Path("edit_plan.json"))
    parser.add_argument("--simulate", action="store_true", help="Run offline demo without FFmpeg")
    parser.add_argument("--demo-failure", action="store_true", help="Fail attempt 1 to demonstrate retry")
    parser.add_argument("--retries", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    plan_path = args.plan.resolve()
    _, jobs = discover_jobs(plan_path)
    if not jobs:
        print(f"No videos found. Put files in: {plan_path.parent / 'input'}")
        return 1
    tools_dir = plan_path.parent / "tools"
    local_ffmpeg = tools_dir / "ffmpeg"
    local_ffprobe = tools_dir / "ffprobe"
    full_bin = Path("/opt/homebrew/opt/ffmpeg-full/bin")
    preferred_ffmpeg = (
        local_ffmpeg if local_ffmpeg.exists()
        else full_bin / "ffmpeg" if (full_bin / "ffmpeg").exists()
        else Path("ffmpeg")
    )
    preferred_ffprobe = (
        local_ffprobe if local_ffprobe.exists()
        else full_bin / "ffprobe" if (full_bin / "ffprobe").exists()
        else Path("ffprobe")
    )
    try:
        backend = (
            SimulationBackend(fail_first_attempt=args.demo_failure)
            if args.simulate else FFmpegBackend(
                ffmpeg=str(preferred_ffmpeg),
                ffprobe=str(preferred_ffprobe),
            )
        )
    except RuntimeError as exc:
        print(f"Setup error: {exc}")
        print("You can still verify the automation loop with: python3 -m autoclip_batch --simulate --demo-failure")
        return 2
    summary = Pipeline(plan_path.parent, backend, retries=args.retries).run(jobs)
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0
