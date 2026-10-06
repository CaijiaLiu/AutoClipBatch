from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


@dataclass(frozen=True)
class ClipSegment:
    source: Path
    trim_start: float
    duration: float


@dataclass(frozen=True)
class Job:
    source: Path
    output: Path
    trim_start: float
    duration: float | None
    title: str
    watermark: Path | None
    segments: tuple[ClipSegment, ...] = ()
    auto_highlight_source: Path | None = None
    highlight_clip_duration: float = 3.0
    highlight_sample_interval: float = 1.0

    @property
    def label(self) -> str:
        return self.output.name if self.segments or self.auto_highlight_source else self.source.name


class Backend(Protocol):
    name: str

    def process(self, job: Job, attempt: int) -> None: ...

    def probe(self, path: Path) -> dict[str, Any]: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def job_key(job: Job) -> str:
    sources = (
        [
            {
                "source_hash": file_sha256(segment.source),
                "trim_start": segment.trim_start,
                "duration": segment.duration,
            }
            for segment in job.segments
        ]
        if job.segments
        else [{"source_hash": file_sha256(job.source)}]
    )
    payload = {
        "sources": sources,
        "trim_start": job.trim_start,
        "duration": job.duration,
        "title": job.title,
        "watermark": file_sha256(job.watermark) if job.watermark else None,
        "format_version": 2,
        "auto_highlight": (
            {
                "target_duration": job.duration,
                "clip_duration": job.highlight_clip_duration,
                "sample_interval": job.highlight_sample_interval,
            }
            if job.auto_highlight_source else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def discover_jobs(plan_path: Path) -> tuple[dict[str, Any], list[Job]]:
    plan = load_json(plan_path, {})
    root = plan_path.parent
    input_dir = (root / plan.get("input_dir", "input")).resolve()
    output_dir = (root / plan.get("output_dir", "output")).resolve()
    defaults = plan.get("defaults", {})
    overrides = plan.get("overrides", {})
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = []
    if plan.get("batch_enabled", True):
        sources = sorted(
            path for path in input_dir.glob(plan.get("pattern", "*"))
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        )
    jobs: list[Job] = []
    for source in sources:
        settings = {**defaults, **overrides.get(source.name, {})}
        watermark_value = settings.get("watermark")
        watermark = (root / watermark_value).resolve() if watermark_value else None
        jobs.append(
            Job(
                source=source.resolve(),
                output=output_dir / f"{source.stem}_vertical.mp4",
                trim_start=float(settings.get("trim_start", 0)),
                duration=(
                    float(settings["duration"])
                    if settings.get("duration") is not None else None
                ),
                title=str(settings.get("title", "")),
                watermark=watermark,
            )
        )

    for compilation in plan.get("compilations", []):
        segments: list[ClipSegment] = []
        for item in compilation.get("clips", []):
            source = (input_dir / item["file"]).resolve()
            if not source.exists():
                raise FileNotFoundError(f"Compilation source not found: {source}")
            duration = float(item["duration"])
            if duration <= 0:
                raise ValueError(f"Clip duration must be positive: {item}")
            segments.append(
                ClipSegment(source, float(item.get("start", 0)), duration)
            )
        if not segments:
            raise ValueError("Each compilation must contain at least one clip")
        watermark_value = compilation.get("watermark")
        watermark = (root / watermark_value).resolve() if watermark_value else None
        output_name = compilation.get("output", "combined_vertical.mp4")
        jobs.append(
            Job(
                source=segments[0].source,
                output=output_dir / output_name,
                trim_start=0,
                duration=sum(segment.duration for segment in segments),
                title=str(compilation.get("title", "")),
                watermark=watermark,
                segments=tuple(segments),
            )
        )

    for highlight in plan.get("auto_highlights", []):
        source = (input_dir / highlight["file"]).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Auto-highlight source not found: {source}")
        target_duration = float(highlight.get("target_duration", 15))
        clip_duration = float(highlight.get("clip_duration", 3))
        sample_interval = float(highlight.get("sample_interval", 1))
        if min(target_duration, clip_duration, sample_interval) <= 0:
            raise ValueError("Auto-highlight durations must be positive")
        jobs.append(
            Job(
                source=source,
                output=output_dir / highlight.get("output", f"{source.stem}_highlights.mp4"),
                trim_start=0,
                duration=target_duration,
                title=str(highlight.get("title", "")),
                watermark=None,
                auto_highlight_source=source,
                highlight_clip_duration=clip_duration,
                highlight_sample_interval=sample_interval,
            )
        )
    return plan, jobs


def select_highlight_windows(
    samples: list[dict[str, float]],
    video_duration: float,
    target_duration: float,
    clip_duration: float,
) -> list[tuple[float, float, float]]:
    """Rank visually active, well-exposed samples and return spaced clip windows."""
    if video_duration <= 0:
        return []
    count = max(1, math.ceil(min(target_duration, video_duration) / clip_duration))
    candidates: list[tuple[float, float]] = []
    for sample in samples:
        time = float(sample.get("time", 0))
        motion = float(sample.get("motion", 0))
        saturation = float(sample.get("saturation", 0))
        brightness = float(sample.get("brightness", 128))
        exposure_quality = max(0.0, 128.0 - abs(brightness - 128.0))
        score = motion * 2.0 + saturation * 0.35 + exposure_quality * 0.08
        candidates.append((score, time))
    if not candidates:
        step = video_duration / (count + 1)
        candidates = [(0.0, step * (index + 1)) for index in range(count)]

    selected: list[tuple[float, float, float]] = []
    minimum_gap = clip_duration * 1.1
    for score, center in sorted(candidates, reverse=True):
        start = max(0.0, min(center - clip_duration / 2, video_duration - clip_duration))
        if any(abs(start - previous[0]) < minimum_gap for previous in selected):
            continue
        selected.append((start, min(clip_duration, video_duration - start), score))
        if len(selected) == count:
            break

    # Fill sparse results with evenly distributed windows.
    for index in range(count):
        if len(selected) == count:
            break
        start = max(0.0, min((video_duration - clip_duration) * index / max(count - 1, 1), video_duration - clip_duration))
        if not any(abs(start - previous[0]) < clip_duration * 0.8 for previous in selected):
            selected.append((start, min(clip_duration, video_duration - start), 0.0))
    selected.sort(key=lambda item: item[0])
    remaining = min(target_duration, video_duration)
    trimmed: list[tuple[float, float, float]] = []
    for start, duration, score in selected:
        if remaining <= 0:
            break
        actual = min(duration, remaining)
        trimmed.append((start, actual, score))
        remaining -= actual
    return trimmed


class FFmpegBackend:
    name = "ffmpeg"

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self.ffmpeg = shutil.which(ffmpeg)
        self.ffprobe = shutil.which(ffprobe)
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError(
                "FFmpeg/ffprobe not found. On macOS run: brew install ffmpeg"
            )

    @staticmethod
    def _escape_drawtext(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    def process(self, job: Job, attempt: int) -> None:
        if job.auto_highlight_source:
            analyzed = self._build_highlight_job(job)
            self._process_compilation(analyzed, attempt)
            return
        if job.segments:
            self._process_compilation(job, attempt)
            return
        job.output.parent.mkdir(parents=True, exist_ok=True)
        temp = job.output.with_name(job.output.stem + ".partial.mp4")
        temp.unlink(missing_ok=True)
        filters = [
            "scale=1080:1920:force_original_aspect_ratio=decrease",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
        ]
        if job.title:
            title = self._escape_drawtext(job.title)
            filters.append(
                "drawtext=text='{}':fontcolor=white:fontsize=64:box=1:"
                "boxcolor=black@0.55:boxborderw=24:x=(w-text_w)/2:y=120".format(title)
            )
        command = [self.ffmpeg, "-hide_banner", "-y", "-ss", str(job.trim_start), "-i", str(job.source)]
        if job.watermark:
            command.extend(["-i", str(job.watermark)])
            complex_filter = ",".join(filters) + "[base];[1:v]scale=180:-1[wm];[base][wm]overlay=W-w-40:H-h-40[v]"
            command.extend(["-filter_complex", complex_filter, "-map", "[v]", "-map", "0:a?"])
        else:
            command.extend(["-vf", ",".join(filters)])
        if job.duration is not None:
            command.extend(["-t", str(job.duration)])

        # Each retry uses a safer, faster encoding profile.
        profiles = [
            ["-c:v", "libx264", "-preset", "medium", "-crf", "21"],
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"],
            ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "25"],
        ]
        command.extend(profiles[min(attempt, len(profiles) - 1)])
        command.extend(["-c:a", "aac", "-movflags", "+faststart", str(temp)])
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr[-1200:])
        temp.replace(job.output)

    def _video_duration(self, source: Path) -> float:
        command = [
            self.ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:])
        return float(result.stdout.strip())

    def _visual_samples(self, source: Path, interval: float) -> list[dict[str, float]]:
        command = [
            self.ffmpeg, "-hide_banner", "-i", str(source),
            "-vf", f"fps=1/{interval},scale=160:-1,signalstats,metadata=print",
            "-an", "-f", "null", "-",
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr[-1200:])
        samples: list[dict[str, float]] = []
        current: dict[str, float] | None = None
        patterns = {
            "motion": re.compile(r"lavfi\.signalstats\.YDIF=([0-9.]+)"),
            "brightness": re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)"),
            "saturation": re.compile(r"lavfi\.signalstats\.SATAVG=([0-9.]+)"),
        }
        for line in result.stderr.splitlines():
            match = re.search(r"pts_time:([0-9.]+)", line)
            if match:
                if current is not None:
                    samples.append(current)
                current = {"time": float(match.group(1))}
                continue
            if current is None:
                continue
            for name, pattern in patterns.items():
                match = pattern.search(line)
                if match:
                    current[name] = float(match.group(1))
        if current is not None:
            samples.append(current)
        return samples

    def _build_highlight_job(self, job: Job) -> Job:
        source = job.auto_highlight_source
        if source is None:
            return job
        video_duration = self._video_duration(source)
        samples = self._visual_samples(source, job.highlight_sample_interval)
        windows = select_highlight_windows(
            samples, video_duration, job.duration or video_duration,
            job.highlight_clip_duration,
        )
        if not windows:
            raise RuntimeError("No usable highlight windows were found")
        segments = tuple(ClipSegment(source, start, duration) for start, duration, _ in windows)
        analysis_path = job.output.with_suffix(".highlights.json")
        save_json(
            analysis_path,
            {
                "source": source.name,
                "video_duration": video_duration,
                "target_duration": job.duration,
                "method": "visual motion + saturation + exposure heuristic",
                "selected_clips": [
                    {"start": round(start, 3), "duration": round(duration, 3), "score": round(score, 3)}
                    for start, duration, score in windows
                ],
            },
        )
        return replace(job, segments=segments, auto_highlight_source=None)

    def _has_audio(self, source: Path) -> bool:
        command = [
            self.ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(source),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        return result.returncode == 0 and bool(result.stdout.strip())

    def _process_compilation(self, job: Job, attempt: int) -> None:
        """Normalize requested clips, then concatenate them into one vertical video."""
        job.output.parent.mkdir(parents=True, exist_ok=True)
        final_temp = job.output.with_name(job.output.stem + ".partial.mp4")
        final_temp.unlink(missing_ok=True)
        profiles = [
            ["-preset", "medium", "-crf", "21"],
            ["-preset", "veryfast", "-crf", "23"],
            ["-preset", "ultrafast", "-crf", "25"],
        ]
        profile = profiles[min(attempt, len(profiles) - 1)]

        with tempfile.TemporaryDirectory(prefix="autoclip-", dir=job.output.parent) as directory:
            work = Path(directory)
            clip_paths: list[Path] = []
            for index, segment in enumerate(job.segments):
                clip_path = work / f"clip-{index:03d}.mp4"
                filters = [
                    "scale=1080:1920:force_original_aspect_ratio=decrease",
                    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
                    "fps=30",
                    "setsar=1",
                ]
                if job.title:
                    title = self._escape_drawtext(job.title)
                    filters.append(
                        "drawtext=text='{}':fontcolor=white:fontsize=64:box=1:"
                        "boxcolor=black@0.55:boxborderw=24:x=(w-text_w)/2:y=120".format(title)
                    )
                command = [
                    self.ffmpeg, "-hide_banner", "-y", "-ss", str(segment.trim_start),
                    "-t", str(segment.duration), "-i", str(segment.source),
                ]
                if self._has_audio(segment.source):
                    command.extend(["-map", "0:v:0", "-map", "0:a:0"])
                else:
                    command.extend([
                        "-f", "lavfi", "-t", str(segment.duration),
                        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-map", "0:v:0", "-map", "1:a:0",
                    ])
                command.extend([
                    "-vf", ",".join(filters), "-c:v", "libx264", *profile,
                    "-c:a", "aac", "-ar", "48000", "-ac", "2",
                    "-t", str(segment.duration), str(clip_path),
                ])
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(result.stderr[-1200:])
                clip_paths.append(clip_path)

            concat_file = work / "concat.txt"
            concat_file.write_text(
                "".join(f"file '{path.as_posix()}'\n" for path in clip_paths),
                encoding="utf-8",
            )
            command = [
                self.ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_file), "-c", "copy", "-movflags", "+faststart",
                str(final_temp),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr[-1200:])
        final_temp.replace(job.output)

    def probe(self, path: Path) -> dict[str, Any]:
        command = [
            self.ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(path),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:])
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return {
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "duration": float(data["format"]["duration"]),
        }


class SimulationBackend:
    """Offline demo backend that exercises retry, validation, and cache logic."""

    name = "simulation"

    def __init__(self, fail_first_attempt: bool = False) -> None:
        self.fail_first_attempt = fail_first_attempt

    def process(self, job: Job, attempt: int) -> None:
        if self.fail_first_attempt and attempt == 0:
            raise RuntimeError("Injected first-attempt failure for retry demonstration")
        job.output.write_text(
            json.dumps({"width": 1080, "height": 1920, "duration": job.duration or 5.0}),
            encoding="utf-8",
        )

    def probe(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))


class Pipeline:
    def __init__(self, project_root: Path, backend: Backend, retries: int = 2) -> None:
        self.root = project_root
        self.backend = backend
        self.retries = retries
        self.manifest_path = project_root / ".autoclip" / "manifest.json"
        self.log_path = project_root / ".autoclip" / "events.jsonl"
        self.manifest: dict[str, Any] = load_json(self.manifest_path, {"jobs": {}})
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _event(self, event: str, **fields: Any) -> None:
        record = {"time": utc_now(), "event": event, "backend": self.backend.name, **fields}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        logging.info("%s %s", event.upper(), fields.get("source", ""))

    @staticmethod
    def _validate(metadata: dict[str, Any]) -> None:
        if metadata.get("width") != 1080 or metadata.get("height") != 1920:
            raise ValueError(f"Expected 1080x1920, got {metadata}")
        if float(metadata.get("duration", 0)) <= 0:
            raise ValueError(f"Invalid duration: {metadata}")

    def run_job(self, job: Job) -> str:
        key = job_key(job)
        previous = self.manifest["jobs"].get(key)
        if previous and previous.get("status") == "success" and job.output.exists():
            try:
                self._validate(self.backend.probe(job.output))
                self._event("cache_hit", source=job.label, output=job.output.name)
                return "cached"
            except Exception:
                self._event("cache_invalid", source=job.label)

        last_error = ""
        for attempt in range(self.retries + 1):
            self._event("attempt", source=job.label, attempt=attempt + 1)
            try:
                self.backend.process(job, attempt)
                metadata = self.backend.probe(job.output)
                self._validate(metadata)
                self.manifest["jobs"][key] = {
                    "status": "success", "source": job.label,
                    "output": str(job.output), "metadata": metadata,
                    "completed_at": utc_now(),
                }
                save_json(self.manifest_path, self.manifest)
                self._event("success", source=job.label, attempt=attempt + 1, metadata=metadata)
                return "success"
            except Exception as exc:
                last_error = str(exc)
                job.output.unlink(missing_ok=True)
                self._event("attempt_failed", source=job.label, attempt=attempt + 1, error=last_error)

        self.manifest["jobs"][key] = {
            "status": "failed", "source": job.label, "error": last_error,
            "failed_at": utc_now(),
        }
        save_json(self.manifest_path, self.manifest)
        self._event("failed", source=job.label, error=last_error)
        return "failed"

    def run(self, jobs: list[Job]) -> dict[str, int]:
        summary = {"success": 0, "cached": 0, "failed": 0}
        for job in jobs:
            summary[self.run_job(job)] += 1
        return summary
