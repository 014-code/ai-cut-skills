#!/usr/bin/env python3
"""Package a short-drama video with notices and an orientation-matched tail board."""

from __future__ import annotations

import argparse
import json
import math
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
) -> str:
    options = [
        f"fontfile='{escape_filter_value(str(font))}'",
        f"text='{escape_filter_value(text)}'",
        f"fontcolor=0x{color.lstrip('#')}",
        f"fontsize={size}",
        f"borderw={border}",
        "bordercolor=black",
        "shadowx=0",
        "shadowy=0",
        f"x={x}",
        f"y={y}",
    ]
    return "drawtext=" + ":".join(options)


def estimated_text_units(text: str) -> float:
    return sum(1.0 if ord(character) > 127 else 0.62 for character in text)


def fit_font_size(text: str, requested_size: int, width: int) -> int:
    if not text:
        return requested_size
    horizontal_margin = max(12, round(width * 0.04))
    usable_width = max(1, width - horizontal_margin * 2)
    units = max(1.0, estimated_text_units(text))
    fitted_maximum = int(usable_width / (units * 1.06))
    if fitted_maximum < 10:
        raise SystemExit(
            "Overlay text is too long to remain readable inside the horizontal safe margin"
        )
    return min(requested_size, fitted_maximum)


def parse_box(value: str) -> dict[str, int]:
    """Parse an audited source-pixel rectangle expressed as x,y,w,h."""
    try:
        parts = [int(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid box {value!r}; expected integer x,y,w,h"
        ) from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"Invalid box {value!r}; expected exactly x,y,w,h"
        )
    x, y, box_width, box_height = parts
    if x < 0 or y < 0 or box_width <= 0 or box_height <= 0:
        raise argparse.ArgumentTypeError(
            f"Invalid box {value!r}; x/y must be non-negative and w/h must be positive"
        )
    return {"x": x, "y": y, "width": box_width, "height": box_height}


def audited_boxes(
    boxes: list[dict[str, int]],
    *,
    kind: str,
    frame_width: int,
    frame_height: int,
) -> list[dict[str, Any]]:
    audited: list[dict[str, Any]] = []
    for index, box in enumerate(boxes, start=1):
        right = box["x"] + box["width"]
        bottom = box["y"] + box["height"]
        if right > frame_width or bottom > frame_height:
            raise SystemExit(
                f"{kind} box #{index} exceeds the {frame_width}x{frame_height} frame: "
                f"{box['x']},{box['y']},{box['width']},{box['height']}"
            )
        audited.append({**box, "kind": kind, "index": index})
    return audited


def boxes_conflict(
    first: dict[str, Any],
    second: dict[str, Any],
    *,
    gap: int,
) -> bool:
    """Return True when rectangles overlap or have less than the required gap."""
    first_right = first["x"] + first["width"]
    first_bottom = first["y"] + first["height"]
    second_right = second["x"] + second["width"]
    second_bottom = second["y"] + second["height"]
    return not (
        first_right + gap <= second["x"]
        or second_right + gap <= first["x"]
        or first_bottom + gap <= second["y"]
        or second_bottom + gap <= first["y"]
    )


def estimated_text_box(
    text: str,
    *,
    size: int,
    frame_width: int,
    y: int,
) -> dict[str, int]:
    # Reserve a little more than drawtext's nominal glyph bounds for the 1–2px outline.
    box_width = min(
        frame_width,
        math.ceil(estimated_text_units(text) * size * 1.06) + 6,
    )
    box_height = math.ceil(size * 1.25) + 4
    return {
        "x": max(0, (frame_width - box_width) // 2),
        "y": y,
        "width": box_width,
        "height": box_height,
    }


def resolve_notice_placement(
    *,
    text: str,
    size: int,
    frame_width: int,
    frame_height: int,
    requested_y: int | None,
    occupied_boxes: list[dict[str, Any]],
    gap: int,
    minimum_y: int,
) -> tuple[int | None, dict[str, int] | None, str, bool]:
    if not text:
        return None, None, "not_added", True

    provisional = estimated_text_box(
        text,
        size=size,
        frame_width=frame_width,
        y=0,
    )
    box_height = provisional["height"]
    bottom_margin = max(10, round(frame_height * 0.015))

    def candidate(y: int) -> dict[str, int]:
        return {**provisional, "y": y}

    def collisions(box: dict[str, int]) -> list[dict[str, Any]]:
        return [
            occupied
            for occupied in occupied_boxes
            if boxes_conflict(box, occupied, gap=gap)
        ]

    if requested_y is not None:
        box = candidate(requested_y)
        if requested_y < minimum_y or requested_y + box_height > frame_height:
            raise SystemExit(
                f"--notice-y must keep the added notice within y={minimum_y}–"
                f"{frame_height - box_height}; got {requested_y}"
            )
        conflicts = collisions(box)
        if conflicts:
            labels = ", ".join(
                f"{item['kind']}#{item['index']}" for item in conflicts
            )
            raise SystemExit(
                f"--notice-y collides with audited source overlays ({labels}); "
                "move it or correct the audited boxes"
            )
        return requested_y, box, "explicit", True

    source_notice_boxes = [
        box for box in occupied_boxes if box.get("kind") == "source_notice"
    ]
    if source_notice_boxes:
        notice_y = min(
            box["y"] - gap - box_height for box in source_notice_boxes
        )
        mode = "auto_above_source_notice"
    else:
        notice_y = frame_height - box_height - bottom_margin
        mode = "default_bottom"
    for _ in range(len(occupied_boxes) + 1):
        box = candidate(notice_y)
        conflicts = collisions(box)
        if not conflicts:
            if notice_y < minimum_y:
                break
            return notice_y, box, mode, True
        notice_y = min(item["y"] - gap - box_height for item in conflicts)
        mode = "auto_above_source_overlays"

    raise SystemExit(
        "No collision-free position remains for the added notice below the title/benefit. "
        "Correct --source-notice-box/--source-subtitle-box, reduce --notice-font-size, "
        "or provide a verified --notice-y."
    )


def resolve_overlay_plan(args: argparse.Namespace, width: int, height: int) -> dict[str, Any]:
    requested_title_size = args.title_font_size or max(28, round(height * 0.03))
    requested_benefit_size = args.benefit_font_size or max(24, round(height * 0.025))
    requested_notice_size = args.notice_font_size or max(14, round(height * 0.0125))
    title_gap = max(10, round(height * 0.01))
    if min(requested_title_size, requested_benefit_size, requested_notice_size) <= 0:
        raise SystemExit("Overlay font sizes must be positive")

    if args.source_title_present:
        if args.title_bottom is None:
            raise SystemExit(
                "--source-title-present requires --title-bottom from the visual title audit"
            )
        if args.title_y is not None:
            raise SystemExit("--title-y is only valid when adding --title-text")
        title_text = ""
        title_y = None
        title_bottom = args.title_bottom
        title_size = requested_title_size
    else:
        title_text = (args.title_text or "").strip()
        if not title_text:
            raise SystemExit("A source without a title requires non-empty --title-text")
        if args.title_bottom is not None:
            raise SystemExit("--title-bottom is only valid with --source-title-present")
        title_y = args.title_y if args.title_y is not None else max(20, round(height * 0.02))
        if title_y < 0:
            raise SystemExit("--title-y must be within the frame")
        title_size = fit_font_size(title_text, requested_title_size, width)
        title_bottom = title_y + title_size

    if title_bottom <= 0 or title_bottom >= height:
        raise SystemExit(f"Title bottom must be within the frame; got {title_bottom}")

    benefit_size = fit_font_size(args.benefit_text, requested_benefit_size, width)
    minimum_benefit_y = title_bottom + title_gap
    benefit_y = args.benefit_y if args.benefit_y is not None else minimum_benefit_y
    if args.benefit_text and benefit_y < minimum_benefit_y:
        raise SystemExit(
            f"Benefit text must start at or below y={minimum_benefit_y} to clear the title"
        )
    if args.benefit_text and benefit_y + benefit_size >= height:
        raise SystemExit("Benefit text does not fit inside the frame")

    risk_text = "" if args.source_risk_present else (args.risk_text or "").strip()
    ai_text = "" if args.source_ai_present else (args.ai_text or "").strip()
    if not args.source_risk_present and not risk_text:
        raise SystemExit(
            "Declare --source-risk-present or provide non-empty --risk-text"
        )
    if not args.source_ai_present and not ai_text:
        raise SystemExit(
            "Declare --source-ai-present or provide non-empty --ai-text"
        )

    notice_text = " · ".join(text for text in (risk_text, ai_text) if text)
    notice_size = fit_font_size(notice_text, requested_notice_size, width)
    source_notice_boxes = audited_boxes(
        args.source_notice_box,
        kind="source_notice",
        frame_width=width,
        frame_height=height,
    )
    source_subtitle_boxes = audited_boxes(
        args.source_subtitle_box,
        kind="source_subtitle",
        frame_width=width,
        frame_height=height,
    )
    if (args.source_risk_present or args.source_ai_present) and not source_notice_boxes:
        raise SystemExit(
            "Every existing source notice requires at least one "
            "--source-notice-box x,y,w,h from the visual audit"
        )
    if args.source_subtitles_present and not source_subtitle_boxes:
        raise SystemExit(
            "--source-subtitles-present requires at least one "
            "--source-subtitle-box x,y,w,h union region from the visual audit"
        )
    if not args.source_subtitles_present and source_subtitle_boxes:
        raise SystemExit(
            "--source-subtitle-box is only valid with --source-subtitles-present"
        )
    notice_gap = (
        args.notice_gap
        if args.notice_gap is not None
        else max(8, round(height * (16 / 1920)))
    )
    if notice_gap < 0:
        raise SystemExit("--notice-gap must be non-negative")
    minimum_notice_y = (
        benefit_y + benefit_size + notice_gap
        if args.benefit_text
        else title_bottom + notice_gap
    )
    occupied_boxes = [*source_notice_boxes, *source_subtitle_boxes]
    notice_y, notice_box, notice_position_mode, notice_clear = resolve_notice_placement(
        text=notice_text,
        size=notice_size,
        frame_width=width,
        frame_height=height,
        requested_y=args.notice_y,
        occupied_boxes=occupied_boxes,
        gap=notice_gap,
        minimum_y=minimum_notice_y,
    )

    return {
        "source_title_present": bool(args.source_title_present),
        "added_title_text": title_text,
        "title_y": title_y,
        "title_bottom": title_bottom,
        "title_size": title_size,
        "title_gap": title_gap,
        "benefit_y": benefit_y,
        "benefit_size": benefit_size,
        "source_risk_present": bool(args.source_risk_present),
        "source_ai_present": bool(args.source_ai_present),
        "source_subtitles_present": bool(args.source_subtitles_present),
        "added_risk_text": risk_text,
        "added_ai_text": ai_text,
        "notice_text": notice_text,
        "notice_y": notice_y,
        "notice_box": notice_box,
        "notice_size": notice_size,
        "notice_gap": notice_gap,
        "notice_position_mode": notice_position_mode,
        "source_notice_boxes": source_notice_boxes,
        "source_subtitle_boxes": source_subtitle_boxes,
        "notice_clear_of_source_overlays": notice_clear,
        "notice_background": False,
        "notice_shadow": 0,
    }


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
    title_group = parser.add_mutually_exclusive_group(required=True)
    title_group.add_argument("--source-title-present", action="store_true")
    title_group.add_argument("--title-text")
    risk_group = parser.add_mutually_exclusive_group(required=True)
    risk_group.add_argument("--source-risk-present", action="store_true")
    risk_group.add_argument("--risk-text")
    ai_group = parser.add_mutually_exclusive_group(required=True)
    ai_group.add_argument("--source-ai-present", action="store_true")
    ai_group.add_argument("--ai-text")
    subtitle_group = parser.add_mutually_exclusive_group(required=True)
    subtitle_group.add_argument("--source-subtitles-present", action="store_true")
    subtitle_group.add_argument("--source-subtitles-absent", action="store_true")
    parser.add_argument("--font-file", type=Path)
    parser.add_argument("--title-font-size", type=int)
    parser.add_argument("--benefit-font-size", type=int)
    parser.add_argument("--notice-font-size", type=int)
    parser.add_argument("--title-bottom", type=int)
    parser.add_argument("--title-y", type=int)
    parser.add_argument("--benefit-y", type=int)
    parser.add_argument("--notice-y", type=int)
    parser.add_argument(
        "--source-notice-box",
        action="append",
        type=parse_box,
        default=[],
        metavar="X,Y,W,H",
        help="Repeatable source-pixel rectangle for an existing warning/AI notice",
    )
    parser.add_argument(
        "--source-subtitle-box",
        action="append",
        type=parse_box,
        default=[],
        metavar="X,Y,W,H",
        help="Repeatable source-pixel rectangle or union band for burned-in dialogue subtitles",
    )
    parser.add_argument(
        "--notice-gap",
        type=int,
        help="Minimum pixel gap from audited source boxes (default: 16px at 1080x1920)",
    )
    parser.add_argument("--title-color", default="FFF4C2")
    parser.add_argument("--benefit-color", default="FFE12B")
    parser.add_argument("--notice-color", default="FFFFFF")
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--qa-dir",
        type=Path,
        help="Directory for mandatory opening/middle/late full frames and bottom crops",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser


def qa_sample_times(body_duration: float) -> list[tuple[str, float]]:
    inset = min(0.5, max(0.04, body_duration * 0.2))
    candidates = [
        ("opening", min(inset, body_duration * 0.25)),
        ("middle", body_duration * 0.5),
        ("late_body", max(0.0, body_duration - inset)),
    ]
    unique: list[tuple[str, float]] = []
    seen: set[float] = set()
    for label, timestamp in candidates:
        normalized = round(max(0.0, timestamp), 3)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append((label, normalized))
    return unique


def extract_qa_frames(
    *,
    video: Path,
    body_duration: float,
    qa_dir: Path,
    ffmpeg: str,
) -> list[dict[str, Any]]:
    qa_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for label, timestamp in qa_sample_times(body_duration):
        full_frame = qa_dir / f"{label}_{timestamp:.3f}s.png"
        bottom_crop = qa_dir / f"{label}_{timestamp:.3f}s_bottom.png"
        common = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
        ]
        run([*common, str(full_frame)])
        run(
            [
                *common,
                "-vf",
                "crop=iw:floor(ih*0.35/2)*2:0:ih-oh",
                str(bottom_crop),
            ]
        )
        if not full_frame.is_file() or not full_frame.stat().st_size:
            raise SystemExit(f"Failed to create QA frame: {full_frame}")
        if not bottom_crop.is_file() or not bottom_crop.stat().st_size:
            raise SystemExit(f"Failed to create bottom-region QA crop: {bottom_crop}")
        frames.append(
            {
                "label": label,
                "timestamp_seconds": timestamp,
                "full_frame": str(full_frame.resolve()),
                "bottom_35_percent": str(bottom_crop.resolve()),
            }
        )
    return frames


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
    qa_dir = (
        args.qa_dir.expanduser().resolve()
        if args.qa_dir
        else output.with_name(output.name + ".qa")
    )

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

    overlay_plan = resolve_overlay_plan(args, width, height)
    border = max(2, round(height * 0.0015))
    notice_border = max(1, min(2, round(height * 0.001)))
    body_video_filters = [
        f"trim=start=0:end={cut_at:.6f}",
        "setpts=PTS-STARTPTS",
        f"scale={width}:{height}:flags=lanczos",
    ]
    if overlay_plan["added_title_text"]:
        body_video_filters.append(
            drawtext(
                font=font,
                text=overlay_plan["added_title_text"],
                size=overlay_plan["title_size"],
                color=args.title_color,
                x="(w-text_w)/2",
                y=str(overlay_plan["title_y"]),
                border=border,
            )
        )
    if args.benefit_text:
        body_video_filters.append(
            drawtext(
                font=font,
                text=args.benefit_text,
                size=overlay_plan["benefit_size"],
                color=args.benefit_color,
                x="(w-text_w)/2",
                y=str(overlay_plan["benefit_y"]),
                border=border,
            )
        )
    if overlay_plan["notice_text"]:
        body_video_filters.append(
            drawtext(
                font=font,
                text=overlay_plan["notice_text"],
                size=overlay_plan["notice_size"],
                color=args.notice_color,
                x="(w-text_w)/2",
                y=str(overlay_plan["notice_y"]),
                border=notice_border,
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
        qa_frames = extract_qa_frames(
            video=output,
            body_duration=cut_at,
            qa_dir=qa_dir,
            ffmpeg=ffmpeg,
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
        "title_present_exactly_once": (
            overlay_plan["source_title_present"] != bool(overlay_plan["added_title_text"])
        ),
        "title_benefit_separation": (
            not args.benefit_text
            or overlay_plan["benefit_y"]
            >= overlay_plan["title_bottom"] + overlay_plan["title_gap"]
        ),
        "fiction_notice_not_duplicated": (
            overlay_plan["source_risk_present"] != bool(overlay_plan["added_risk_text"])
        ),
        "ai_notice_not_duplicated": (
            overlay_plan["source_ai_present"] != bool(overlay_plan["added_ai_text"])
        ),
        "notice_background_disabled": not overlay_plan["notice_background"],
        "notice_clear_of_source_overlays": overlay_plan[
            "notice_clear_of_source_overlays"
        ],
        "notice_qa_frames_created": len(qa_frames) == len(qa_sample_times(cut_at)),
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
            "title_added": overlay_plan["added_title_text"],
            "fiction_risk_added": overlay_plan["added_risk_text"],
            "ai_notice_added": overlay_plan["added_ai_text"],
        },
        "source_overlay_audit": {
            "title_present": overlay_plan["source_title_present"],
            "risk_notice_present": overlay_plan["source_risk_present"],
            "ai_notice_present": overlay_plan["source_ai_present"],
            "subtitles_present": overlay_plan["source_subtitles_present"],
            "notice_boxes": overlay_plan["source_notice_boxes"],
            "subtitle_boxes": overlay_plan["source_subtitle_boxes"],
        },
        "overlay_layout": {
            "title_y": overlay_plan["title_y"],
            "title_bottom": overlay_plan["title_bottom"],
            "title_benefit_gap": overlay_plan["title_gap"],
            "benefit_y": overlay_plan["benefit_y"],
            "notice_y": overlay_plan["notice_y"],
            "notice_box": overlay_plan["notice_box"],
            "notice_gap": overlay_plan["notice_gap"],
            "notice_position_mode": overlay_plan["notice_position_mode"],
            "notice_background": False,
            "notice_border": notice_border,
            "notice_shadow": 0,
        },
        "font_file": str(font),
        "font_sizes": {
            "title": overlay_plan["title_size"],
            "benefit": overlay_plan["benefit_size"],
            "notice": overlay_plan["notice_size"],
        },
        "expected_duration_seconds": expected_duration,
        "output": str(output),
        "qa_review": {
            "status": "visual_review_required",
            "instruction": (
                "Inspect every full frame and bottom_35_percent crop; confirm the added notice "
                "does not cover source notices, dialogue subtitles, or important faces."
            ),
            "frames": qa_frames,
        },
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
