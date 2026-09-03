#!/usr/bin/env python3
"""Standalone quality-first video compressor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


class CompressionError(RuntimeError):
    pass


ProgressCallback = Callable[[float], None]


def resolve_binary(name: str, override: str | None = None) -> str:
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise CompressionError(f"Binary not found: {candidate}")
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        found = shutil.which(f"{name}.exe")
        if found:
            return found
    raise CompressionError(f"Missing dependency: {name}")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def ratio(value: Any) -> float:
    try:
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        return float(value or 0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def numeric(value: Any, default: float = 0.0) -> float:
    """容错解析 ffprobe 数值字段，兼容 ``N/A`` 和空值。"""
    try:
        if value is None or str(value).strip().upper() in {"", "N/A", "NA", "NONE"}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    return int(numeric(value, float(default)))


def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    result = run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise CompressionError((result.stderr or "ffprobe failed").strip()[-2000:])
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise CompressionError(f"Invalid ffprobe output: {exc}") from exc
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = numeric(fmt.get("duration"))
    width = integer(video.get("width"))
    height = integer(video.get("height"))
    if duration <= 0 or width <= 0 or height <= 0:
        raise CompressionError("Input has no valid video duration or dimensions")
    audio_bitrate = integer(audio.get("bit_rate"))
    video_bitrate = integer(video.get("bit_rate"))
    if video_bitrate <= 0:
        # Some UserGrowth sources omit stream-level bit_rate. Estimate the
        # visual stream from the container bitrate, then fall back to file
        # size/duration when the container also has no bitrate metadata.
        container_bitrate = integer(fmt.get("bit_rate"))
        video_bitrate = max(container_bitrate - audio_bitrate, 0)
    if video_bitrate <= 0 and duration > 0:
        video_bitrate = max(
            int(path.stat().st_size * 8 / duration) - audio_bitrate,
            0,
        )
    return {
        "path": str(path),
        "sizeBytes": path.stat().st_size,
        "duration": duration,
        "width": width,
        "height": height,
        "codec": str(video.get("codec_name") or "").lower(),
        "pixelFormat": str(video.get("pix_fmt") or ""),
        "fps": ratio(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "bitrate": integer(fmt.get("bit_rate")),
        "videoBitrate": video_bitrate,
        "audioBitrate": audio_bitrate,
        "audioCodec": str(audio.get("codec_name") or "").lower(),
        "audioSampleRate": integer(audio.get("sample_rate")),
        "audioChannels": integer(audio.get("channels")),
        "hasAudio": bool(audio),
    }


def even(value: int) -> int:
    return max(2, value if value % 2 == 0 else value - 1)


def scaled_dimensions(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(width, height) <= max_long_edge:
        return width, height
    scale = max_long_edge / max(width, height)
    return even(round(width * scale)), even(round(height * scale))


def target_codec(profile: str) -> str:
    return {
        "h264-quality": "h264",
        "hevc-quality": "hevc",
        "av1-quality": "av1",
        "strict-size": "h264",
    }[profile]


def compatible_for_copy(info: dict[str, Any], profile: str, max_size_mb: float | None) -> bool:
    if max_size_mb is not None and info["sizeBytes"] > max_size_mb * 1024 * 1024:
        return False
    codec = target_codec(profile)
    if codec == "h264":
        return info["codec"] == "h264" and info["pixelFormat"] in {"yuv420p", "yuvj420p"}
    if codec == "hevc":
        return info["codec"] in {"hevc", "h265"}
    return info["codec"] in {"av1", "av01"}


def build_filter(info: dict[str, Any], max_long_edge: int | None, target_fps: float | None) -> str | None:
    filters: list[str] = []
    width, height = scaled_dimensions(info["width"], info["height"], max_long_edge)
    if (width, height) != (info["width"], info["height"]):
        filters.append(f"scale={width}:{height}:flags=lanczos")
    if target_fps and info["fps"] > target_fps + 0.05:
        filters.append(f"fps={target_fps:g}")
    return ",".join(filters) if filters else None


def build_encode_command(
    ffmpeg: str,
    source: Path,
    destination: Path,
    info: dict[str, Any],
    profile: str,
    crf: int,
    preset: str,
    audio_mode: str,
    audio_bitrate: str,
    max_long_edge: int | None = None,
    target_fps: float | None = None,
    emit_progress: bool = False,
) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?"]
    video_filter = build_filter(info, max_long_edge, target_fps)
    if video_filter:
        command.extend(["-vf", video_filter])
    if profile in {"h264-quality", "strict-size"}:
        command.extend(["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"])
    elif profile == "hevc-quality":
        # hvc1 is better recognized than the libx265 default hev1 tag by
        # common MP4 players and browser implementations.
        command.extend(["-c:v", "libx265", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-tag:v", "hvc1"])
    else:
        command.extend(["-c:v", "libsvtav1", "-preset", "6", "-crf", str(crf), "-pix_fmt", "yuv420p"])
    if not info["hasAudio"] or audio_mode == "none":
        command.append("-an")
    elif audio_mode == "copy":
        command.extend(["-c:a", "copy"])
    else:
        command.extend(["-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000"])
    command.extend(["-map_metadata", "-1", "-movflags", "+faststart"])
    if emit_progress:
        command.extend(["-progress", "pipe:1", "-nostats"])
    command.append(str(destination))
    return command


def remux(ffmpeg: str, source: Path, destination: Path) -> None:
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map_metadata",
            "-1",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    if result.returncode != 0:
        raise CompressionError((result.stderr or "remux failed").strip()[-2000:])


def _emit_progress(callback: ProgressCallback | None, percent: float) -> None:
    if callback is None:
        return
    try:
        callback(max(0.0, min(100.0, percent)))
    except Exception:
        # Progress reporting is observability only; it must never fail a valid
        # compression because a consumer disconnected or rejected an update.
        return


def _progress_seconds(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed / 1_000_000


def encode_attempt(
    command: list[str],
    *,
    duration: float,
    progress_callback: ProgressCallback | None = None,
    progress_start: float = 0.0,
    progress_span: float = 100.0,
) -> None:
    if progress_callback is None:
        result = run(command)
        if result.returncode != 0:
            raise CompressionError((result.stderr or "ffmpeg encode failed").strip()[-3000:])
        return

    process = subprocess.Popen(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    output_lines: list[str] = []
    if process.stdout is not None:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            output_lines.append(line)
            if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                elapsed = _progress_seconds(line.split("=", 1)[1])
                if elapsed is not None and duration > 0:
                    _emit_progress(
                        progress_callback,
                        progress_start + min(1.0, elapsed / duration) * progress_span,
                    )
            elif line == "progress=end":
                _emit_progress(progress_callback, progress_start + progress_span)
    return_code = process.wait()
    if return_code != 0:
        detail = "\n".join(output_lines)
        raise CompressionError((detail or "ffmpeg encode failed").strip()[-3000:])


def attempts_for(profile: str, max_size_mb: float | None) -> list[dict[str, Any]]:
    if profile != "strict-size":
        return [{"crf": 22 if profile == "h264-quality" else 28, "audioMode": "copy", "audioBitrate": "128k"}]
    if max_size_mb is None:
        raise CompressionError("strict-size requires --max-size-mb")
    return [
        {"crf": 20, "audioMode": "copy", "audioBitrate": "128k"},
        {"crf": 22, "audioMode": "aac", "audioBitrate": "128k"},
        {"crf": 24, "audioMode": "aac", "audioBitrate": "96k"},
        {"crf": 25, "audioMode": "aac", "audioBitrate": "96k", "maxLongEdge": 1280},
        {"crf": 27, "audioMode": "aac", "audioBitrate": "96k", "maxLongEdge": 960, "targetFps": 25},
    ]


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def compress(
    args: argparse.Namespace,
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    source = Path(args.input).expanduser().resolve()
    destination = Path(args.output).expanduser().resolve()
    if not source.is_file():
        raise CompressionError(f"Input file does not exist: {source}")
    if source == destination:
        raise CompressionError("Output must differ from input")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not args.overwrite:
        raise CompressionError(f"Output already exists: {destination}; use --overwrite")

    if progress_callback is not None:
        downstream_callback = progress_callback
        last_progress = -1.0

        def monotonic_progress(percent: float) -> None:
            nonlocal last_progress
            bounded = max(0.0, min(100.0, float(percent)))
            if bounded < last_progress:
                return
            last_progress = bounded
            downstream_callback(bounded)

        progress_callback = monotonic_progress

    ffprobe = resolve_binary("ffprobe", args.ffprobe)
    info = probe(source, ffprobe)
    profile = args.profile
    max_size_mb = args.max_size_mb
    decision = "encode"
    used_attempt: dict[str, Any] | None = None
    min_video_bitrate_kbps = max(float(args.min_video_bitrate_kbps or 0), 0.0)

    _emit_progress(progress_callback, 0.0)

    with tempfile.TemporaryDirectory(prefix="video-compress-", dir=str(destination.parent)) as temp_dir:
        temp_root = Path(temp_dir)
        copy_destination = temp_root / "copy-output"
        if (
                min_video_bitrate_kbps > 0
                and info["videoBitrate"] > 0
                and info["videoBitrate"] <= min_video_bitrate_kbps * 1000
        ):
            # A low-bitrate source has little visual redundancy left to remove;
            # preserve it byte-for-byte instead of introducing a second lossy
            # generation. The output copy still goes through atomic replace.
            shutil.copy2(source, copy_destination)
            os.replace(copy_destination, destination)
            decision = "skip_low_bitrate"
            _emit_progress(progress_callback, 100.0)
        elif args.mode == "auto" and compatible_for_copy(info, profile, max_size_mb):
            shutil.copy2(source, copy_destination)
            os.replace(copy_destination, destination)
            decision = "copy"
            _emit_progress(progress_callback, 100.0)
        elif args.mode == "remux":
            ffmpeg = resolve_binary("ffmpeg", args.ffmpeg)
            remux_path = temp_root / "remux.mp4"
            remux(ffmpeg, source, remux_path)
            os.replace(remux_path, destination)
            decision = "remux"
            _emit_progress(progress_callback, 100.0)
        else:
            ffmpeg = resolve_binary("ffmpeg", args.ffmpeg)
            successful = False
            attempts = attempts_for(profile, max_size_mb)
            attempt_span = 100.0 / max(len(attempts), 1)
            for index, attempt in enumerate(attempts, start=1):
                candidate = temp_root / f"attempt-{index}.mp4"
                command = build_encode_command(
                    ffmpeg,
                    source,
                    candidate,
                    info,
                    profile,
                    int(attempt["crf"]),
                    args.preset,
                    str(attempt["audioMode"]),
                    str(attempt["audioBitrate"]),
                    attempt.get("maxLongEdge"),
                    attempt.get("targetFps"),
                    progress_callback is not None,
                )
                encode_attempt(
                    command,
                    duration=info["duration"],
                    progress_callback=progress_callback,
                    progress_start=(index - 1) * attempt_span,
                    progress_span=attempt_span,
                )
                candidate_size = candidate.stat().st_size
                used_attempt = {**attempt, "attempt": index, "sizeBytes": candidate_size}
                if max_size_mb is None or candidate_size <= max_size_mb * 1024 * 1024:
                    if max_size_mb is None and candidate_size >= info["sizeBytes"]:
                        # Quality-first encoding is not useful when it makes
                        # the file larger. Keep the original bytes intact.
                        shutil.copy2(source, copy_destination)
                        os.replace(copy_destination, destination)
                        decision = "keep_original_no_savings"
                    else:
                        os.replace(candidate, destination)
                    _emit_progress(progress_callback, 100.0)
                    successful = True
                    break
            if not successful:
                raise CompressionError(
                    f"Unable to meet {max_size_mb} MiB without more aggressive degradation"
                )

    output_info = probe(destination, ffprobe)
    report = {
        "input": info,
        "output": output_info,
        "profile": profile,
        "mode": args.mode,
        "decision": decision,
        "minVideoBitrateKbps": min_video_bitrate_kbps or None,
        "attempt": used_attempt,
        "sizeRatio": round(output_info["sizeBytes"] / max(info["sizeBytes"], 1), 5),
        "savedBytes": info["sizeBytes"] - output_info["sizeBytes"],
        "elapsedSeconds": round(time.perf_counter() - started, 3),
    }
    write_report(Path(args.report).expanduser().resolve() if args.report else None, report)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Quality-first standalone video compressor")
    result.add_argument("input")
    result.add_argument("output")
    result.add_argument("--profile", choices=["h264-quality", "hevc-quality", "av1-quality", "strict-size"], default="h264-quality")
    result.add_argument("--mode", choices=["auto", "encode", "remux"], default="auto")
    result.add_argument("--max-size-mb", type=float)
    result.add_argument("--preset", default="medium")
    result.add_argument("--ffmpeg")
    result.add_argument("--ffprobe")
    result.add_argument("--report")
    result.add_argument(
        "--min-video-bitrate-kbps",
        type=float,
        default=0,
        help="Skip visual re-encoding when the source video bitrate is at or below this threshold",
    )
    result.add_argument(
        "--progress",
        action="store_true",
        help="Emit newline-delimited JSON progress events while encoding",
    )
    result.add_argument("--overwrite", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        progress_callback = None
        if args.progress:
            def progress_callback(percent: float) -> None:
                print(
                    json.dumps(
                        {"type": "progress", "percent": round(percent, 2)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )

        report = compress(args, progress_callback=progress_callback)
    except CompressionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
