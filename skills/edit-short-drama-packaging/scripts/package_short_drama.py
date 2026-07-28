#!/usr/bin/env python3
"""Package a short-drama video with notices and an orientation-matched tail board."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


def run(
    command: list[str],
    *,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_executable(value: str) -> str:
    resolved = shutil.which(value)
    if resolved:
        return resolved
    path = Path(value).expanduser()
    if path.is_file():
        return str(path.resolve())
    raise SystemExit(f"Required executable not found: {value}")


def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    result = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size,bit_rate:"
                "stream=index,codec_type,codec_name,width,height,"
                "r_frame_rate,avg_frame_rate,sample_rate,channels,duration"
            ),
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def first_stream(data: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
    return next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == codec_type),
        None,
    )


def duration_of(data: dict[str, Any], stream: dict[str, Any] | None = None) -> float:
    if stream and stream.get("duration") not in (None, "N/A"):
        return float(stream["duration"])
    return float(data["format"]["duration"])


def parse_rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 30.0
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 30.0
    return float(value)


def orientation(width: int, height: int) -> str:
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def choose_tailboard(
    *,
    explicit: Path | None,
    directory: Path | None,
    source_orientation: str,
    ffprobe: str,
) -> tuple[Path, dict[str, Any]]:
    if explicit:
        candidates = [explicit]
    elif directory:
        if not directory.is_dir():
            raise SystemExit(f"Tail-board directory not found: {directory}")
        candidates = sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        )
    else:
        raise SystemExit("Provide --tailboard or --tailboard-dir")

    if not candidates:
        raise SystemExit("No tail-board video candidates found")

    scored: list[tuple[int, Path, dict[str, Any]]] = []
    for candidate in candidates:
        if not candidate.is_file():
            raise SystemExit(f"Tail-board file not found: {candidate}")
        candidate_probe = probe(candidate, ffprobe)
        video = first_stream(candidate_probe, "video")
        if not video:
            continue
        candidate_orientation = orientation(int(video["width"]), int(video["height"]))
        name = candidate.stem.lower()
        label_match = (
            ("竖" in name or "portrait" in name or "vertical" in name)
            if source_orientation == "portrait"
            else ("横" in name or "landscape" in name or "horizontal" in name)
        )
        score = 0
        if candidate_orientation == source_orientation:
            score += 100
        if label_match:
            score += 50
        score += min(int(video["width"]) * int(video["height"]) // 1_000_000, 20)
        scored.append((score, candidate, candidate_probe))

    if not scored:
        raise SystemExit("Tail-board candidates contain no video stream")
    scored.sort(key=lambda item: (-item[0], str(item[1])))
    _, selected, selected_probe = scored[0]
    return selected.resolve(), selected_probe


def discover_font(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.is_file():
            raise SystemExit(f"Font file not found: {explicit}")
        return explicit.resolve()

    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Heavy.otf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        windir / "Fonts" / "msyhbd.ttc",
        windir / "Fonts" / "simhei.ttf",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SystemExit(
        "No suitable Chinese font was found. Pass an installed .ttf/.otf/.ttc with --font-file."
    )


def escape_filter_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in (":", "'", ",", "[", "]", "%"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def drawtext(
    *,
    font: Path,
    text: str,
    size: int,
    color: str,
    x: str,
    y: str,
    border: int,
    box: bool = False,
) -> str:
    options = [
        f"fontfile='{escape_filter_value(str(font))}'",
        f"text='{escape_filter_value(text)}'",
        f"fontcolor=0x{color.lstrip('#')}",
        f"fontsize={size}",
        f"borderw={border}",
        "bordercolor=black",
        f"x={x}",
        f"y={y}",
    ]
    if box:
        options.extend(["box=1", "boxcolor=black@0.45", f"boxborderw={max(4, border * 2)}"])
    return "drawtext=" + ":".join(options)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Add free-viewing/risk/AI copy, optionally remove the source tail, "
            "and append an orientation-matched tail board."
        )
    )
    parser.add_argument("source", type=Path)
    tail_group = parser.add_mutually_exclusive_group(required=True)
    tail_group.add_argument("--tailboard", type=Path)
    tail_group.add_argument("--tailboard-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cut-at", type=float)
    parser.add_argument("--benefit-text", default="0元免费看全集")
    parser.add_argument("--risk-text", default="本故事纯属虚构")
    parser.add_argument("--ai-text", default="视频由AI生成")
    parser.add_argument("--font-file", type=Path)
    parser.add_argument("--benefit-font-size", type=int)
    parser.add_argument("--notice-font-size", type=int)
    parser.add_argument("--benefit-color", default="FFE12B")
    parser.add_argument("--notice-color", default="FFFFFF")
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    ffmpeg = require_executable(args.ffmpeg)
    ffprobe = require_executable(args.ffprobe)

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Source video not found: {source}")
    if output.exists() and not args.overwrite:
        raise SystemExit(f"Output already exists; pass --overwrite to replace it: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    source_probe = probe(source, ffprobe)
    source_video = first_stream(source_probe, "video")
    if not source_video:
        raise SystemExit("Source contains no video stream")
    source_audio = first_stream(source_probe, "audio")
    source_duration = duration_of(source_probe, source_video)
    cut_at = source_duration if args.cut_at is None else args.cut_at
    if cut_at <= 0 or cut_at > source_duration + 0.05:
        raise SystemExit(
            f"--cut-at must be within source duration 0–{source_duration:.3f}s; got {cut_at}"
        )

    width = int(source_video["width"])
    height = int(source_video["height"])
    if width % 2:
        width -= 1
    if height % 2:
        height -= 1
    source_orientation = orientation(width, height)
    fps_value = parse_rate(
        source_video.get("avg_frame_rate") or source_video.get("r_frame_rate")
    )
    fps = f"{fps_value:.6f}".rstrip("0").rstrip(".")

    tailboard, tail_probe = choose_tailboard(
        explicit=args.tailboard.expanduser().resolve() if args.tailboard else None,
        directory=args.tailboard_dir.expanduser().resolve() if args.tailboard_dir else None,
        source_orientation=source_orientation,
        ffprobe=ffprobe,
    )
    tail_video = first_stream(tail_probe, "video")
    if not tail_video:
        raise SystemExit("Selected tail board contains no video stream")
    tail_audio = first_stream(tail_probe, "audio")
    tail_duration = duration_of(tail_probe, tail_video)
    font = discover_font(args.font_file)

    benefit_size = args.benefit_font_size or max(24, round(height * 0.025))
    notice_size = args.notice_font_size or max(14, round(height * 0.0125))
    border = max(2, round(height * 0.0015))
    body_video_filters = [
        f"trim=start=0:end={cut_at:.6f}",
        "setpts=PTS-STARTPTS",
        f"scale={width}:{height}:flags=lanczos",
    ]
    if args.benefit_text:
        body_video_filters.append(
            drawtext(
                font=font,
                text=args.benefit_text,
                size=benefit_size,
                color=args.benefit_color,
                x="(w-text_w)/2",
                y=f"{max(20, round(height * 0.04))}",
                border=border,
            )
        )
    notice_text = " · ".join(text for text in (args.risk_text, args.ai_text) if text)
    if notice_text:
        body_video_filters.append(
            drawtext(
                font=font,
                text=notice_text,
                size=notice_size,
                color=args.notice_color,
                x="(w-text_w)/2",
                y=f"h-text_h-{max(10, round(height * 0.015))}",
                border=max(1, border - 1),
                box=True,
            )
        )
    body_video_filters.extend([f"fps={fps}", "setsar=1", "format=yuv420p"])

    filters = [f"[0:v:0]{','.join(body_video_filters)}[bodyv]"]
    if source_audio:
        filters.append(
            (
                f"[0:a:0]atrim=start=0:end={cut_at:.6f},asetpts=PTS-STARTPTS,"
                "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[bodya]"
            )
        )
    else:
        filters.append(
            (
                f"anullsrc=r=44100:cl=stereo,atrim=duration={cut_at:.6f},"
                "asetpts=PTS-STARTPTS[bodya]"
            )
        )

    filters.append(
        (
            f"[1:v:0]trim=start=0:end={tail_duration:.6f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={fps},setsar=1,format=yuv420p[tailv]"
        )
    )
    if tail_audio:
        filters.append(
            (
                f"[1:a:0]atrim=start=0:end={tail_duration:.6f},asetpts=PTS-STARTPTS,"
                "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[taila]"
            )
        )
    else:
        filters.append(
            (
                f"anullsrc=r=44100:cl=stereo,atrim=duration={tail_duration:.6f},"
                "asetpts=PTS-STARTPTS[taila]"
            )
        )
    filters.append("[bodyv][bodya][tailv][taila]concat=n=2:v=1:a=1[outv][outa]")

    temp_output = output.with_name(f"{output.stem}.part{output.suffix}")
    if temp_output.exists():
        temp_output.unlink()
    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-i",
        str(tailboard),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        args.preset,
        "-crf",
        str(args.crf),
        "-profile:v",
        "high",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]

    try:
        run(command)
        os.replace(temp_output, output)
        run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(output),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ]
        )
    except Exception:
        if temp_output.exists():
            temp_output.unlink()
        raise

    output_probe = probe(output, ffprobe)
    output_video = first_stream(output_probe, "video")
    output_audio = first_stream(output_probe, "audio")
    output_duration = duration_of(output_probe, output_video)
    expected_duration = cut_at + tail_duration
    duration_tolerance = max(0.25, 3.0 / max(fps_value, 1.0))
    checks = {
        "video_stream": output_video is not None,
        "audio_stream": output_audio is not None,
        "dimensions": bool(
            output_video
            and int(output_video.get("width", 0)) == width
            and int(output_video.get("height", 0)) == height
        ),
        "duration": abs(output_duration - expected_duration) <= duration_tolerance,
        "video_codec": bool(output_video and output_video.get("codec_name") == "h264"),
        "audio_codec": bool(output_audio and output_audio.get("codec_name") == "aac"),
    }
    status = "passed" if all(checks.values()) else "failed"
    manifest_path = (
        args.manifest.expanduser().resolve()
        if args.manifest
        else output.with_suffix(output.suffix + ".json")
    )
    manifest = {
        "status": status,
        "source": str(source),
        "source_duration_seconds": source_duration,
        "cut_at_seconds": cut_at,
        "removed_source_tail_seconds": max(0.0, source_duration - cut_at),
        "tailboard": str(tailboard),
        "tailboard_duration_seconds": tail_duration,
        "source_orientation": source_orientation,
        "copy": {
            "benefit": args.benefit_text,
            "fiction_risk": args.risk_text,
            "ai_notice": args.ai_text,
        },
        "font_file": str(font),
        "font_sizes": {"benefit": benefit_size, "notice": notice_size},
        "expected_duration_seconds": expected_duration,
        "output": str(output),
        "output_probe": output_probe,
        "checks": checks,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if status != "passed":
        raise SystemExit(f"Output validation failed; inspect {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
