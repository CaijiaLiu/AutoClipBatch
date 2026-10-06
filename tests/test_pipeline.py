from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoclip_batch.core import (
    Job,
    Pipeline,
    SimulationBackend,
    discover_jobs,
    select_highlight_windows,
)


class PipelineTests(unittest.TestCase):
    def make_job(self, root: Path) -> Job:
        source = root / "input" / "clip.mp4"
        source.parent.mkdir()
        source.write_bytes(b"demo-video-content")
        return Job(source, root / "output" / "clip_vertical.mp4", 0, 5, "Demo", None)

    def test_retries_then_succeeds_and_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self.make_job(root)
            job.output.parent.mkdir()
            result = Pipeline(root, SimulationBackend(fail_first_attempt=True)).run([job])
            self.assertEqual(result, {"success": 1, "cached": 0, "failed": 0})
            events = [json.loads(line)["event"] for line in (root / ".autoclip/events.jsonl").read_text().splitlines()]
            self.assertEqual(events, ["attempt", "attempt_failed", "attempt", "success"])

    def test_second_run_uses_hash_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self.make_job(root)
            job.output.parent.mkdir()
            pipeline = Pipeline(root, SimulationBackend())
            self.assertEqual(pipeline.run([job])["success"], 1)
            self.assertEqual(Pipeline(root, SimulationBackend()).run([job])["cached"], 1)

    def test_changed_source_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self.make_job(root)
            job.output.parent.mkdir()
            self.assertEqual(Pipeline(root, SimulationBackend()).run([job])["success"], 1)
            job.source.write_bytes(b"changed-video-content")
            self.assertEqual(Pipeline(root, SimulationBackend()).run([job])["success"], 1)

    def test_discovers_multi_clip_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "one.mp4").write_bytes(b"one")
            (input_dir / "two.mp4").write_bytes(b"two")
            plan = {
                "batch_enabled": False,
                "compilations": [{
                    "output": "combined.mp4",
                    "title": "Demo",
                    "clips": [
                        {"file": "one.mp4", "start": 1, "duration": 2},
                        {"file": "two.mp4", "start": 3, "duration": 4},
                    ],
                }],
            }
            plan_path = root / "edit_plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            _, jobs = discover_jobs(plan_path)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].output.name, "combined.mp4")
            self.assertEqual(jobs[0].duration, 6)
            self.assertEqual([clip.source.name for clip in jobs[0].segments], ["one.mp4", "two.mp4"])

    def test_highlight_selection_prefers_motion_and_keeps_clips_apart(self) -> None:
        samples = [
            {"time": 2, "motion": 2, "brightness": 128, "saturation": 5},
            {"time": 8, "motion": 30, "brightness": 120, "saturation": 20},
            {"time": 9, "motion": 29, "brightness": 120, "saturation": 20},
            {"time": 20, "motion": 25, "brightness": 130, "saturation": 18},
            {"time": 32, "motion": 22, "brightness": 125, "saturation": 15},
        ]
        windows = select_highlight_windows(samples, 40, 9, 3)
        self.assertEqual(len(windows), 3)
        starts = [window[0] for window in windows]
        self.assertTrue(any(6 <= start <= 8 for start in starts))
        self.assertTrue(all(b - a >= 2.4 for a, b in zip(starts, starts[1:])))


if __name__ == "__main__":
    unittest.main()
