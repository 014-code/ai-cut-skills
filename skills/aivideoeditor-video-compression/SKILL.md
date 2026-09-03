---
name: aivideoeditor-video-compression
description: Compress standalone video files with a quality-first policy, compatibility-aware encoding, size limits, caching-friendly reports, and post-encode validation. Use for video compression only; do not invoke for fission, editing, packaging, or platform upload.
---

# Video Compression

Use this skill for a standalone input-video to output-video compression job.
It must not perform frame fission, overlays, subtitles, front/tail boards,
uploads, platform automation, or UserGrowth workflows.

## Required behavior

- Preserve the original file. Never overwrite the source.
- Probe the source once with `ffprobe` and report codec, duration, size,
  resolution, FPS, pixel format, and audio streams.
- Prefer `copy` or `remux` when the source already satisfies the selected
  profile. Copy/remux is the only genuinely lossless size optimization.
- For visual compression, use a quality-first CRF encode. Do not lower
  resolution, FPS, duration, or remove audio unless the user explicitly asks
  for a strict size cap and the fallback ladder requires it.
- Keep output creation atomic and validate the completed file with `ffprobe`.
- Emit a JSON report containing the decision, profile, input/output metadata,
  size ratio, elapsed time, and any fallback used.

## Profiles

The bundled CLI supports:

- `h264-quality`: broadly compatible H.264 delivery, default CRF 22.
- `hevc-quality`: H.265/HEVC CRF 28 output for storage where the consumer
  supports it. MP4 is tagged `hvc1` for broader player/browser recognition.
- `av1-quality`: AV1 output for storage where slower encoding is acceptable.
- `strict-size`: quality-first attempts followed by audio, resolution, and
  FPS reductions until `--max-size-mb` is met.

Use `--mode auto` to reuse a compatible source, `--mode encode` to force an
encode, or `--mode remux` to rewrite the MP4 container without re-encoding.
Use `--min-video-bitrate-kbps N` to skip a second lossy encode for sources at
or below the configured visual bitrate threshold. The output is still copied
atomically and the JSON report records `decision=skip_low_bitrate`.

## Execution

Run `scripts/compress_video.py --help` first. A typical quality-first run is:

```text
python scripts/compress_video.py input.mp4 output.mp4 --profile h264-quality --mode auto --report report.json
```

For a hard limit:

```text
python scripts/compress_video.py input.mp4 output.mp4 --profile strict-size --max-size-mb 100 --report report.json
```

For a smaller HEVC copy of a low-bitrate H.264 source:

```text
python scripts/compress_video.py input.mp4 output.mp4 --profile hevc-quality --mode encode --report report.json
```

Do not call the existing LLM/COS 48 MiB helper for ordinary video delivery;
that helper has a different purpose and may trim, lower FPS, or remove audio.

## Quality and safety

- H.264/x264 is the default compatibility target.
- H.265 and AV1 are opt-in because platform/browser support varies. H.265 is
  useful when a low-bitrate H.264 source still needs smaller storage, but it
  encodes more slowly and should not be used where H.264 is required.
- Copy AAC audio when compatible; otherwise encode AAC at 128 kbps.
- Use temporary files beside the destination and atomically replace only the
  destination, never the input.
- If a requested size cap cannot be met without severe degradation, fail with
  an explicit report instead of silently deleting audio or shortening video.
- A low-bitrate source should normally be left untouched. When a threshold is
  supplied, compare the probed video-stream bitrate (with a size/duration
  estimate when stream metadata is absent) before starting FFmpeg.
- If a quality-first encode is not smaller than the source, keep the original
  bytes and report `decision=keep_original_no_savings`.
