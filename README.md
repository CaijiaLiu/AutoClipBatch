# AutoClip Batch

A deterministic Python + FFmpeg batch-editing tool that reads one reusable JSON plan, processes every video in a folder, validates the outputs, retries failures twice, and skips unchanged work with SHA-256 caching.

## Automation loop

`read task -> run FFmpeg -> validate with ffprobe -> retry on failure -> cache success -> exit`

The production path makes **zero AI/API calls**. Reusing `edit_plan.json` and `.autoclip/manifest.json` avoids repeated prompts and repeated video processing.

## Run the offline demonstration now

This exercises the full state machine and deliberately fails the first attempt so the retry is visible:

```bash
cd AutoClipBatch
python3 -m autoclip_batch --simulate --demo-failure
```

Simulation mode writes a small metadata placeholder instead of a playable video; it exists only to test the workflow when FFmpeg is unavailable. Run the same command a second time. It returns `cached: 1` without processing the unchanged file again.

## Process real videos

1. Install FFmpeg with title support on macOS: `brew install ffmpeg-full`, or place executable files at `tools/ffmpeg` and `tools/ffprobe`.
2. Put videos in `input/`.
3. Edit `edit_plan.json` if needed.
4. Run:

```bash
python3 -m autoclip_batch
```

The repository includes a small generated `input/demo.mp4`, so a fresh clone can run immediately. Replace it or add your own footage when you are ready.

Outputs go to `output/`. Machine-readable events are saved in `.autoclip/events.jsonl`; successful hashes and probe results are saved in `.autoclip/manifest.json`.

### Local binary installation (older macOS)

If Homebrew cannot install FFmpeg, download the two ZIP builds linked by FFmpeg's official download page and unzip them locally:

```bash
mkdir -p tools
curl -L "https://evermeet.cx/ffmpeg/ffmpeg-9.0.2.zip" -o tools/ffmpeg.zip
curl -L "https://evermeet.cx/ffmpeg/ffprobe-9.0.2.zip" -o tools/ffprobe.zip
unzip -jo tools/ffmpeg.zip -d tools
unzip -jo tools/ffprobe.zip -d tools
chmod +x tools/ffmpeg tools/ffprobe
```

The application automatically prefers these project-local executables when present.

On Homebrew systems, the application automatically prefers the keg-only `ffmpeg-full` binaries so title rendering works without changing the global shell PATH.

## JSON configuration

`defaults` apply to every discovered video. `overrides` can customize a file by its exact filename. `watermark` accepts a PNG path relative to this folder.

Set `batch_enabled` to `false` and add a `compilations` entry to join selected sections from multiple videos:

```json
{
  "input_dir": "input",
  "output_dir": "output",
  "batch_enabled": false,
  "compilations": [
    {
      "output": "combined_vlog.mp4",
      "title": "My Short Vlog",
      "clips": [
        {"file": "video1.mp4", "start": 5, "duration": 8},
        {"file": "video2.mp4", "start": 20, "duration": 10}
      ]
    }
  ]
}
```

Clips are normalized to 1080x1920, given compatible audio tracks when necessary, and concatenated in the listed order. Set `batch_enabled` to `true` to also create an individual vertical export for every input video.

## Automatic long-vlog highlights

Add `auto_highlights` to let the local pipeline score sampled frames by visual motion, saturation, and exposure, choose non-overlapping moments from across a long video, and compile them automatically:

```json
"auto_highlights": [
  {
    "file": "long_vlog.mp4",
    "output": "auto_highlight_vlog.mp4",
    "target_duration": 15,
    "clip_duration": 3,
    "sample_interval": 2,
    "title": "Auto Highlights"
  }
]
```

The selection report is written beside the video as `auto_highlight_vlog.highlights.json`. This deterministic first-stage selector makes no AI/API calls. It identifies visually active candidate moments; semantic understanding of dialogue or events would require an optional transcription or vision-model stage.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Portfolio evidence

- **Agent automation:** reusable plan-driven tool that discovers work, executes it, validates results, retries failures, and records a terminal state.
- **Automatic editing:** real mode performs trimming, 9:16 conversion, title/watermark composition, audio encoding, and batch export through FFmpeg.
- **Token efficiency:** the production workflow uses no AI calls; stable JSON plans and content-hash caching prevent repeated prompts and duplicate processing.
