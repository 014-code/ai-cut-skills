#!/usr/bin/env python3
"""Run video-level visual/dialogue moderation and optional preview masking.

This script builds on run_visual_moderation.py. It samples video frames, attaches
nearby transcript/dialogue segments, runs the frame-level moderation pipeline,
adds transcript-only checks, aggregates a video decision, and can render a
visual-only masked preview with OpenCV.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
from bisect import bisect_right
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_visual_moderation as frame_mod


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".txt", ".json"}
ACTION_RANK = {"PASS": 0, "REVIEW": 1, "BLOCK": 2}

NUDENET_EXPOSED_CLASSES = {
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
}

NUDENET_SUGGESTIVE_CLASSES = {
    "FEMALE_BREAST_COVERED",
}
CHEST_SUGGESTIVE_LABELS = {"FEMALE_BREAST_COVERED"}

NSFW_CLASS_GROUPS = {
    "ANUS_EXPOSED": "pelvis",
    "BUTTOCKS_EXPOSED": "pelvis",
    "FEMALE_BREAST_EXPOSED": "chest",
    "FEMALE_BREAST_COVERED": "chest",
    "FEMALE_GENITALIA_EXPOSED": "pelvis",
    "MALE_GENITALIA_EXPOSED": "pelvis",
}
AUTO_NSFW_INCLUDE_SUGGESTIVE = True


def load_cv2():
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError("opencv-python/cv2 is required for video sampling and masking.") from exc
    return cv2


def load_nudenet_detector(model_path: Optional[str] = None) -> Tuple[Any, Optional[str]]:
    try:
        from nudenet import NudeDetector  # type: ignore
    except Exception as exc:
        return None, f"nudenet is not available: {exc}"

    try:
        if model_path:
            try:
                return NudeDetector(model_path=model_path), None
            except TypeError:
                return NudeDetector(model_path), None
        return NudeDetector(), None
    except Exception as exc:
        return None, f"failed to initialize NudeDetector: {exc}"


def load_mediapipe_pose() -> Tuple[Any, Optional[str]]:
    try:
        import mediapipe as mp  # type: ignore
    except Exception as exc:
        return None, f"mediapipe is not available: {exc}"

    if not hasattr(mp, "solutions") or not hasattr(mp.solutions, "pose"):
        return None, "mediapipe does not expose mp.solutions.pose; provide a compatible package or use NudeNet-only mode."

    try:
        return mp.solutions.pose.Pose(
            static_image_mode=True,
            model_complexity=1,
            enable_segmentation=False,
            min_detection_confidence=0.5,
        ), None
    except Exception as exc:
        return None, f"failed to initialize mediapipe pose: {exc}"


def parse_time_to_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", ".")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        nums = [float(part) for part in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        minutes, seconds = nums
        return minutes * 60 + seconds
    hours, minutes, seconds = nums
    return hours * 3600 + minutes * 60 + seconds


def segment_from_dict(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = item.get("text") or item.get("content") or item.get("line") or item.get("sentence")
    if not text:
        return None
    start = (
        item.get("start_time")
        if item.get("start_time") is not None
        else item.get("start")
    )
    end = item.get("end_time") if item.get("end_time") is not None else item.get("end")
    segment = {
        "text": str(text).strip(),
        "start_time": parse_time_to_seconds(start),
        "end_time": parse_time_to_seconds(end),
    }
    if segment["end_time"] is None and segment["start_time"] is not None:
        segment["end_time"] = segment["start_time"] + 2.0
    return segment if segment["text"] else None


def parse_json_transcript(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("segments", "dialogue", "subtitles", "transcript", "asr", "lines"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError(f"JSON transcript must be a list or object with segments: {path}")
    segments = []
    for item in data:
        if isinstance(item, dict):
            segment = segment_from_dict(item)
            if segment:
                segments.append(segment)
        elif isinstance(item, str) and item.strip():
            segments.append({"text": item.strip(), "start_time": None, "end_time": None})
    return segments


def parse_srt_or_vtt(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^WEBVTT.*?\n\n", "", text, flags=re.DOTALL | re.IGNORECASE)
    blocks = re.split(r"\n{2,}", text.strip())
    segments = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_line_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_line_index is None:
            if len(lines) == 1:
                segments.append({"text": lines[0], "start_time": None, "end_time": None})
            continue
        start_raw, end_raw = [part.strip().split(" ")[0] for part in lines[time_line_index].split("-->", 1)]
        text_lines = lines[time_line_index + 1 :]
        if not text_lines:
            continue
        segments.append(
            {
                "start_time": parse_time_to_seconds(start_raw),
                "end_time": parse_time_to_seconds(end_raw),
                "text": " ".join(text_lines),
            }
        )
    return segments


def parse_inline_dialogue(values: Iterable[str]) -> List[Dict[str, Any]]:
    segments = []
    for value in values:
        parts = value.split(",", 2)
        if len(parts) == 3:
            start, end, text = parts
            segments.append(
                {
                    "start_time": parse_time_to_seconds(start),
                    "end_time": parse_time_to_seconds(end),
                    "text": text.strip(),
                }
            )
        else:
            segments.append({"start_time": None, "end_time": None, "text": value.strip()})
    return [segment for segment in segments if segment.get("text")]


def load_transcript_segments(paths: Iterable[str], inline_values: Iterable[str]) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(raw_path)
        suffix = path.suffix.lower()
        if suffix == ".json":
            segments.extend(parse_json_transcript(path))
        elif suffix in {".srt", ".vtt", ".txt"}:
            segments.extend(parse_srt_or_vtt(path))
        else:
            raise ValueError(f"Unsupported transcript extension: {path}")
    segments.extend(parse_inline_dialogue(inline_values))
    return sorted(segments, key=lambda item: (item.get("start_time") is None, item.get("start_time") or 0.0))


def video_metadata(video_path: Path) -> Dict[str, Any]:
    cv2 = load_cv2()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open video: {video_path}")
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    duration = frame_count / fps if frame_count and fps else 0.0
    return {
        "frame_count": frame_count,
        "fps": fps,
        "width": width,
        "height": height,
        "duration": duration,
    }


def sample_indices(frame_count: int, fps: float, sample_interval: Optional[float], sample_count: int) -> List[int]:
    if frame_count <= 0:
        return [0]
    if sample_interval and fps:
        step = max(1, int(round(sample_interval * fps)))
        indices = list(range(0, frame_count, step))
        if indices[-1] != frame_count - 1:
            indices.append(frame_count - 1)
        return indices
    sample_count = max(1, sample_count)
    return sorted(
        set(int(round(i * (frame_count - 1) / max(sample_count - 1, 1))) for i in range(sample_count))
    )


def evenly_spaced_subset(indices: List[int], max_count: int) -> List[int]:
    unique = sorted(set(indices))
    if max_count <= 0:
        return []
    if len(unique) <= max_count:
        return unique
    if max_count == 1:
        return [unique[0]]
    picked = {
        unique[int(round(i * (len(unique) - 1) / max(max_count - 1, 1)))]
        for i in range(max_count)
    }
    if len(picked) < max_count:
        for index in unique:
            picked.add(index)
            if len(picked) >= max_count:
                break
    return sorted(picked)[:max_count]


def detect_shot_starts(
    video_path: Path,
    frame_count: int,
    fps: float,
    threshold: float,
    min_gap_seconds: float,
    scan_fps: float,
) -> Tuple[List[int], Dict[str, Any]]:
    """Lightweight content-diff shot detection used to restart local masking per shot."""
    if frame_count <= 0:
        return [0], {"method": "opencv_histogram", "error": "empty_video"}
    cv2 = load_cv2()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return [0], {"method": "opencv_histogram", "error": "failed_to_open_video"}

    step = 1
    if fps:
        step = max(1, int(round(fps / max(scan_fps, 0.5))))
    min_gap_frames = max(1, int(round(max(min_gap_seconds, 0.0) * fps))) if fps else 1
    shot_starts = [0]
    last_start = 0
    previous_gray = None
    previous_hist = None
    max_score = 0.0
    scored_frames = 0

    for index in range(0, frame_count, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv2.resize(frame, (96, 54), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [32], [0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)

        if previous_gray is not None and previous_hist is not None:
            frame_diff = float(np.mean(cv2.absdiff(gray, previous_gray))) / 255.0
            hist_diff = float(cv2.compareHist(previous_hist, hist, cv2.HISTCMP_BHATTACHARYYA))
            score = hist_diff * 0.72 + frame_diff * 0.28
            max_score = max(max_score, score)
            scored_frames += 1
            if score >= threshold and index - last_start >= min_gap_frames:
                shot_starts.append(index)
                last_start = index

        previous_gray = gray
        previous_hist = hist

    cap.release()
    return sorted(set(shot_starts)), {
        "method": "opencv_histogram",
        "threshold": threshold,
        "min_gap_seconds": min_gap_seconds,
        "scan_fps": scan_fps,
        "scan_step_frames": step,
        "scored_frames": scored_frames,
        "max_content_score": round(max_score, 4),
    }


def build_shot_ranges(shot_starts: List[int], frame_count: int, fps: float) -> List[Dict[str, Any]]:
    starts = sorted({max(0, min(frame_count - 1, int(start))) for start in shot_starts if frame_count > 0})
    if not starts:
        starts = [0]
    if starts[0] != 0:
        starts.insert(0, 0)

    ranges = []
    for shot_id, start in enumerate(starts):
        next_start = starts[shot_id + 1] if shot_id + 1 < len(starts) else frame_count
        end = max(start, min(frame_count - 1, next_start - 1))
        ranges.append(
            {
                "shot_id": shot_id,
                "start_frame": start,
                "end_frame": end,
                "start_time": round(start / fps, 3) if fps else 0.0,
                "end_time": round(end / fps, 3) if fps else 0.0,
            }
        )
    return ranges


def shot_id_for_index(index: int, shot_starts: List[int]) -> int:
    if not shot_starts:
        return 0
    return max(0, min(len(shot_starts) - 1, bisect_right(shot_starts, int(index)) - 1))


def annotate_frames_with_shots(
    frames: List[Dict[str, Any]],
    shot_starts: List[int],
    shot_ranges: List[Dict[str, Any]],
) -> None:
    for frame_info in frames:
        shot_id = shot_id_for_index(int(frame_info["index"]), shot_starts)
        frame_info["shot_id"] = shot_id
        if 0 <= shot_id < len(shot_ranges):
            frame_info["shot_start_time"] = shot_ranges[shot_id]["start_time"]
            frame_info["shot_end_time"] = shot_ranges[shot_id]["end_time"]


def shot_anchor_indices(shot_ranges: List[Dict[str, Any]], fps: float, frame_count: int) -> List[int]:
    anchors = []
    stable_offset = max(1, int(round((fps or 25.0) * 0.25)))
    for shot in shot_ranges:
        start = int(shot["start_frame"])
        end = int(shot["end_frame"])
        if end < start:
            continue
        anchors.append(min(end, start + stable_offset))
        if end - start >= stable_offset * 2:
            anchors.append((start + end) // 2)
    return sorted({max(0, min(frame_count - 1, index)) for index in anchors})


def sample_indices_in_range(start: int, end: int, count: int, fps: float) -> List[int]:
    if count <= 0 or end < start:
        return []
    if count == 1:
        return [max(start, min(end, (start + end) // 2))]
    if count == 2:
        fractions = [0.12, 0.88]
    elif count == 3:
        fractions = [0.06, 0.50, 0.94]
    elif count == 4:
        fractions = [0.04, 0.33, 0.67, 0.96]
    elif count == 5:
        fractions = [0.03, 0.33, 0.50, 0.67, 0.97]
    else:
        fractions = [i / max(count - 1, 1) for i in range(count)]
    return sorted(
        {
            max(start, min(end, int(round(start + (end - start) * fraction))))
            for fraction in fractions
        }
    )


def allocate_shot_sample_counts(shot_ranges: List[Dict[str, Any]], max_frames: int) -> List[int]:
    if not shot_ranges or max_frames <= 0:
        return []
    shot_count = len(shot_ranges)
    if max_frames <= shot_count:
        counts = [0] * shot_count
        for index in evenly_spaced_subset(list(range(shot_count)), max_frames):
            counts[index] = 1
        return counts

    if max_frames >= shot_count * 4:
        baseline = 4
    elif max_frames >= shot_count * 3:
        baseline = 3
    elif max_frames >= shot_count * 2:
        baseline = 2
    else:
        baseline = 1

    counts = [baseline] * shot_count
    remaining = max_frames - baseline * shot_count
    if remaining <= 0:
        return counts

    weights = [max(1, int(shot["end_frame"]) - int(shot["start_frame"]) + 1) for shot in shot_ranges]
    total_weight = sum(weights) or shot_count
    fractional: List[Tuple[float, int, int]] = []
    for shot_id, weight in enumerate(weights):
        share = remaining * weight / total_weight
        extra = int(math.floor(share))
        counts[shot_id] += extra
        fractional.append((share - extra, weight, shot_id))

    leftover = max_frames - sum(counts)
    for _, _, shot_id in sorted(fractional, reverse=True)[:leftover]:
        counts[shot_id] += 1
    return counts


def choose_auto_nsfw_indices(
    frame_count: int,
    fps: float,
    sample_interval: Optional[float],
    sample_count: int,
    max_frames: Optional[int],
    shot_ranges: List[Dict[str, Any]],
) -> List[int]:
    base = sample_indices(frame_count, fps, sample_interval, sample_count)
    anchors = shot_anchor_indices(shot_ranges, fps, frame_count)
    merged = sorted(set(base).union(anchors))
    if not max_frames or len(merged) <= max_frames:
        return merged

    max_frames = max(1, int(max_frames))
    per_shot_counts = allocate_shot_sample_counts(shot_ranges, max_frames)
    if per_shot_counts:
        selected = []
        for shot, count in zip(shot_ranges, per_shot_counts):
            selected.extend(sample_indices_in_range(int(shot["start_frame"]), int(shot["end_frame"]), count, fps))
        selected = sorted(set(selected))
        if len(selected) > max_frames:
            selected = evenly_spaced_subset(selected, max_frames)
        if len(selected) < max_frames:
            extras = [index for index in merged if index not in set(selected)]
            selected = sorted(set(selected).union(evenly_spaced_subset(extras, max_frames - len(selected))))
        return sorted(selected)

    return evenly_spaced_subset(merged, max_frames)


def extract_sample_frames(
    video_path: Path,
    work_dir: Path,
    indices: List[int],
    fps: float,
    subdir: str = "frames",
) -> List[Dict[str, Any]]:
    cv2 = load_cv2()
    cap = cv2.VideoCapture(str(video_path))
    frames = []
    frame_dir = work_dir / subdir
    frame_dir.mkdir(parents=True, exist_ok=True)
    for index in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok:
            continue
        timestamp = index / fps if fps else 0.0
        path = frame_dir / f"frame_{index:06d}_{timestamp:.2f}s.jpg"
        cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        frames.append({"index": index, "timestamp": timestamp, "path": str(path)})
    cap.release()
    return frames


def segments_near_time(segments: List[Dict[str, Any]], timestamp: float, window: float) -> List[Dict[str, Any]]:
    start = max(0.0, timestamp - window)
    end = timestamp + window
    result = []
    for segment in segments:
        seg_start = segment.get("start_time")
        seg_end = segment.get("end_time")
        if seg_start is None and seg_end is None:
            result.append(segment)
            continue
        seg_start = 0.0 if seg_start is None else float(seg_start)
        seg_end = seg_start + 2.0 if seg_end is None else float(seg_end)
        if seg_end >= start and seg_start <= end:
            result.append(segment)
    return result


def analyze_payload(payload: Dict[str, Any], engine: str) -> Dict[str, Any]:
    state = {"payload": payload, "provider": "mock"}
    result_state, used_engine = frame_mod.run_pipeline(state, engine)
    return {"engine": used_engine, "decision": result_state["decision"]}


def frame_sidecar_path(frame_path: Path) -> Path:
    return frame_path.with_suffix(".visual_moderation.json")


def analyze_frame(
    frame_info: Dict[str, Any],
    provider: str,
    model: Optional[str],
    timeout: int,
    engine: str,
    transcript_segments: List[Dict[str, Any]],
    transcript_window: float,
) -> Dict[str, Any]:
    frame_path = Path(frame_info["path"])
    nearby_segments = segments_near_time(transcript_segments, frame_info["timestamp"], transcript_window)
    sidecar = frame_sidecar_path(frame_path)
    sidecar.write_text(
        json.dumps(
            {
                "cv": {},
                "dialogue": nearby_segments,
                "context": {
                    "source": "video_frame",
                    "timestamp": frame_info["timestamp"],
                    "frame_index": frame_info["index"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    state = {
        "input_path": str(frame_path),
        "provider": provider,
        "timeout": timeout,
        "model": model,
    }
    result_state, used_engine = frame_mod.run_pipeline(state, engine)
    return {
        "frame_index": frame_info["index"],
        "timestamp": round(frame_info["timestamp"], 3),
        "path": str(frame_path),
        "engine": used_engine,
        "nearby_dialogue_count": len(nearby_segments),
        "decision": result_state["decision"],
    }


def strongest_action(decisions: Iterable[Dict[str, Any]]) -> str:
    action = "PASS"
    for decision in decisions:
        candidate = decision.get("action", "PASS")
        if ACTION_RANK.get(candidate, 0) > ACTION_RANK[action]:
            action = candidate
    return action


def add_redaction_time(redaction: Dict[str, Any], start_time: Optional[float], end_time: Optional[float]) -> Dict[str, Any]:
    item = dict(redaction)
    if item.get("start_time") is None and start_time is not None:
        item["start_time"] = round(max(0.0, start_time), 3)
    if item.get("end_time") is None and end_time is not None:
        item["end_time"] = round(max(0.0, end_time), 3)
    return item


def redaction_key(item: Dict[str, Any]) -> Tuple[Any, ...]:
    bbox = item.get("bbox")
    if isinstance(bbox, list):
        bbox_key: Any = tuple(round(float(value), 4) for value in bbox)
    else:
        bbox_key = None
    keyframes = []
    for keyframe in item.get("bbox_keyframes") or item.get("keyframes") or []:
        if not isinstance(keyframe, dict):
            continue
        timestamp = keyframe.get("time")
        if timestamp is None:
            timestamp = keyframe.get("timestamp")
        normalized = frame_mod.normalize_bbox(keyframe.get("bbox"))
        if timestamp is not None and normalized:
            keyframes.append((round(float(timestamp), 3), tuple(normalized)))
    start = item.get("start_time")
    end = item.get("end_time")
    return (
        item.get("type"),
        item.get("category"),
        round(float(start), 3) if start is not None else None,
        round(float(end), 3) if end is not None else None,
        item.get("text"),
        item.get("replacement"),
        item.get("region"),
        bbox_key,
        tuple(keyframes),
    )


def dedupe_redactions(redactions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for item in redactions:
        key = redaction_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def normalize_redaction_item(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(item)
    bbox = frame_mod.normalize_bbox(normalized.get("bbox"))
    if bbox:
        normalized["bbox"] = bbox
    keyframes = []
    for raw_keyframe in normalized.get("bbox_keyframes") or normalized.get("keyframes") or []:
        if not isinstance(raw_keyframe, dict):
            continue
        timestamp = (
            raw_keyframe.get("time")
            if raw_keyframe.get("time") is not None
            else raw_keyframe.get("timestamp")
        )
        if timestamp is None:
            timestamp = raw_keyframe.get("start_time")
        keyframe_bbox = frame_mod.normalize_bbox(raw_keyframe.get("bbox"))
        if timestamp is None or not keyframe_bbox:
            continue
        keyframes.append({"time": round(float(timestamp), 3), "bbox": keyframe_bbox})
    if keyframes:
        normalized["bbox_keyframes"] = sorted(keyframes, key=lambda keyframe: keyframe["time"])
        normalized.pop("keyframes", None)
        if normalized.get("start_time") is None:
            normalized["start_time"] = normalized["bbox_keyframes"][0]["time"]
        if normalized.get("end_time") is None:
            normalized["end_time"] = normalized["bbox_keyframes"][-1]["time"]
    return frame_mod.sanitize_visual_redaction(normalized)


def load_extra_redactions(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("redactions") or data.get("items") or [data]
    if not isinstance(data, list):
        raise ValueError("--redactions-json must contain a list or an object with redactions.")
    redactions = []
    for item in data:
        if not isinstance(item, dict):
            continue
        redactions.append(normalize_redaction_item(item))
    return redactions


def merge_extra_redactions(decision: Dict[str, Any], extra_redactions: List[Dict[str, Any]], action: str) -> Dict[str, Any]:
    if not extra_redactions:
        return decision
    merged = dict(decision)
    categories = set(merged.get("categories", []))
    for item in extra_redactions:
        category = item.get("category")
        if category:
            categories.add(category)
    current_action = merged.get("action", "PASS")
    if ACTION_RANK.get(action, 0) > ACTION_RANK.get(current_action, 0):
        merged["action"] = action
    merged["categories"] = sorted(category for category in categories if category)
    merged["redactions"] = dedupe_redactions(list(merged.get("redactions", [])) + extra_redactions)
    reasons = list(merged.get("reasons", []))
    reasons.append("Provided redaction targets were applied.")
    merged["reasons"] = frame_mod.dedupe(reasons)
    evidence = dict(merged.get("evidence", {}))
    hits = list(evidence.get("policy_hits", []))
    hits.append("manual.redaction_targets")
    evidence["policy_hits"] = frame_mod.dedupe(hits)
    merged["evidence"] = evidence
    return merged


def aggregate_results(
    video_path: Path,
    metadata: Dict[str, Any],
    frame_results: List[Dict[str, Any]],
    transcript_results: List[Dict[str, Any]],
    sample_window: float,
) -> Dict[str, Any]:
    decisions = [item["decision"] for item in frame_results] + [item["decision"] for item in transcript_results]
    action = strongest_action(decisions)
    categories = sorted({category for decision in decisions for category in decision.get("categories", [])})
    scores = {category: 0.0 for category in frame_mod.CATEGORIES}
    reasons = []
    policy_hits = []
    redactions = []

    for result in frame_results:
        decision = result["decision"]
        timestamp = float(result["timestamp"])
        start_time = max(0.0, timestamp - sample_window)
        end_time = min(float(metadata.get("duration") or timestamp + sample_window), timestamp + sample_window)
        for category, score in decision.get("evidence", {}).get("scores", {}).items():
            scores[category] = max(scores.get(category, 0.0), float(score))
        reasons.extend(decision.get("reasons", []))
        policy_hits.extend(decision.get("evidence", {}).get("policy_hits", []))
        for redaction in decision.get("redactions", []):
            item = add_redaction_time(redaction, start_time, end_time)
            item["source"] = "frame"
            item["frame_timestamp"] = round(timestamp, 3)
            redactions.append(frame_mod.sanitize_visual_redaction(item))

    for result in transcript_results:
        decision = result["decision"]
        segment = result.get("segment") or {}
        for category, score in decision.get("evidence", {}).get("scores", {}).items():
            scores[category] = max(scores.get(category, 0.0), float(score))
        reasons.extend(decision.get("reasons", []))
        policy_hits.extend(decision.get("evidence", {}).get("policy_hits", []))
        for redaction in decision.get("redactions", []):
            item = add_redaction_time(redaction, segment.get("start_time"), segment.get("end_time"))
            item["source"] = "dialogue"
            redactions.append(frame_mod.sanitize_visual_redaction(item))

    redactions = dedupe_redactions(redactions)

    if action == "PASS":
        reasons = ["No scoped visual, OCR, subtitle, or dialogue safety signals were detected."]

    confidence = max([0.74] + [float(decision.get("confidence", 0.0)) for decision in decisions])
    return {
        "video": str(video_path),
        "action": action,
        "categories": categories,
        "confidence": round(frame_mod.clamp_score(confidence), 4),
        "reasons": frame_mod.dedupe(reasons),
        "evidence": {
            "scores": {key: round(value, 4) for key, value in scores.items()},
            "policy_hits": frame_mod.dedupe(policy_hits),
            "frame_count": len(frame_results),
            "dialogue_segment_count": len(transcript_results),
        },
        "redactions": redactions,
        "policy_version": frame_mod.POLICY_VERSION,
    }


def analyze_transcript_segments(segments: List[Dict[str, Any]], engine: str) -> List[Dict[str, Any]]:
    results = []
    for index, segment in enumerate(segments):
        analyzed = analyze_payload({"dialogue": [segment]}, engine)
        decision = analyzed["decision"]
        if decision["action"] == "PASS":
            continue
        results.append(
            {
                "segment_index": index,
                "segment": segment,
                "engine": analyzed["engine"],
                "decision": decision,
            }
        )
    return results


def normalized_box_to_pixels(bbox: Any, width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
    normalized = frame_mod.normalize_bbox(bbox)
    if not normalized:
        return None
    x1, y1, x2, y2 = normalized
    return (
        int(round(x1 * width)),
        int(round(y1 * height)),
        int(round(x2 * width)),
        int(round(y2 * height)),
    )


def normalize_detector_label(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_").upper()


def nudenet_label_risk(label: str, include_suggestive: bool) -> Optional[Tuple[str, str]]:
    normalized = normalize_detector_label(label)
    if normalized in NUDENET_EXPOSED_CLASSES:
        return "BLOCK", NSFW_CLASS_GROUPS.get(normalized, "body")
    if include_suggestive and normalized in NUDENET_SUGGESTIVE_CLASSES:
        return "REVIEW", NSFW_CLASS_GROUPS.get(normalized, "body")
    return None


def detector_box_to_normalized(value: Any, width: int, height: int) -> Optional[List[float]]:
    if isinstance(value, dict):
        if {"x", "y", "w", "h"}.issubset(value):
            value = [value["x"], value["y"], value["w"], value["h"]]
        elif {"x", "y", "width", "height"}.issubset(value):
            value = [value["x"], value["y"], value["width"], value["height"]]
        elif {"x1", "y1", "x2", "y2"}.issubset(value):
            value = [value["x1"], value["y1"], value["x2"], value["y2"]]
        else:
            return None
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x, y, third, fourth = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if max(x, y, third, fourth) <= 1.0:
        return frame_mod.normalize_bbox([x, y, third, fourth])
    if width <= 0 or height <= 0:
        return None

    # NudeNet returns [x, y, width, height]. If the shape looks impossible as
    # xywh, fall back to xyxy for compatibility with detector sidecars.
    xywh_bbox = [x / width, y / height, (x + third) / width, (y + fourth) / height]
    xyxy_bbox = [x / width, y / height, third / width, fourth / height]
    if x + third <= width * 1.15 and y + fourth <= height * 1.15:
        return frame_mod.normalize_bbox(xywh_bbox)
    return frame_mod.normalize_bbox(xyxy_bbox) or frame_mod.normalize_bbox(xywh_bbox)


def bbox_area(bbox: List[float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def bbox_intersection(first: List[float], second: List[float]) -> Optional[List[float]]:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    if x2 <= x1 or y2 <= y1:
        return None
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def bbox_iou(first: List[float], second: List[float]) -> float:
    intersection = bbox_intersection(first, second)
    if not intersection:
        return 0.0
    inter_area = bbox_area(intersection)
    union = bbox_area(first) + bbox_area(second) - inter_area
    return inter_area / union if union > 0 else 0.0


def bbox_center_distance(first: List[float], second: List[float]) -> float:
    fx = (first[0] + first[2]) / 2.0
    fy = (first[1] + first[3]) / 2.0
    sx = (second[0] + second[2]) / 2.0
    sy = (second[1] + second[3]) / 2.0
    return math.sqrt((fx - sx) ** 2 + (fy - sy) ** 2)


def expand_bbox(bbox: List[float], padding: float) -> List[float]:
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    pad_x = width * max(0.0, padding)
    pad_y = height * max(0.0, padding)
    return [
        round(max(0.0, bbox[0] - pad_x), 4),
        round(max(0.0, bbox[1] - pad_y), 4),
        round(min(1.0, bbox[2] + pad_x), 4),
        round(min(1.0, bbox[3] + pad_y), 4),
    ]


def detector_label_is_face(label: Any) -> bool:
    normalized = normalize_detector_label(label)
    return "FACE" in normalized or normalized == "HEAD" or normalized.endswith("_HEAD")


def union_bboxes(bboxes: Iterable[List[float]]) -> Optional[List[float]]:
    normalized = [bbox for bbox in (frame_mod.normalize_bbox(item) for item in bboxes) if bbox]
    if not normalized:
        return None
    return [
        round(min(bbox[0] for bbox in normalized), 4),
        round(min(bbox[1] for bbox in normalized), 4),
        round(max(bbox[2] for bbox in normalized), 4),
        round(max(bbox[3] for bbox in normalized), 4),
    ]


def cleavage_probe_bbox(bbox: List[float]) -> List[float]:
    width = bbox[2] - bbox[0]
    center_x = (bbox[0] + bbox[2]) / 2.0
    if center_x >= 0.57:
        probe = [bbox[0] - width * 0.85, bbox[1], bbox[2], bbox[3]]
    elif center_x <= 0.43:
        probe = [bbox[0], bbox[1], bbox[2] + width * 0.85, bbox[3]]
    else:
        probe = [bbox[0] - width * 0.18, bbox[1], bbox[2] + width * 0.18, bbox[3]]
    return frame_mod.normalize_bbox(probe) or bbox


def image_region_has_obvious_cleavage(frame_path: Path, bbox: List[float]) -> bool:
    normalized = frame_mod.normalize_bbox(bbox)
    if not normalized:
        return False
    normalized = cleavage_probe_bbox(normalized)
    cv2 = load_cv2()
    frame = cv2.imread(str(frame_path))
    if frame is None:
        return False
    height, width = frame.shape[:2]
    x1 = max(0, min(width - 1, int(round(normalized[0] * width))))
    y1 = max(0, min(height - 1, int(round(normalized[1] * height))))
    x2 = max(x1 + 1, min(width, int(round(normalized[2] * width))))
    y2 = max(y1 + 1, min(height, int(round(normalized[3] * height))))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < 24 or crop.shape[1] < 24:
        return False

    ycrcb = cv2.cvtColor(crop, cv2.COLOR_BGR2YCrCb)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    ycrcb_mask = cv2.inRange(ycrcb, (25, 132, 75), (245, 178, 135))
    hsv_mask = cv2.inRange(hsv, (0, 22, 45), (28, 190, 255))
    skin = cv2.bitwise_and(ycrcb_mask, hsv_mask)

    crop_height, crop_width = skin.shape[:2]
    skin_ratio = cv2.countNonZero(skin) / max(1.0, float(crop_height * crop_width))
    if skin_ratio < 0.22:
        return False

    left = skin[:, int(crop_width * 0.12) : int(crop_width * 0.45)]
    right = skin[:, int(crop_width * 0.55) : int(crop_width * 0.88)]
    if left.size == 0 or right.size == 0:
        return False
    left_skin = cv2.countNonZero(left) / max(1.0, float(left.size))
    right_skin = cv2.countNonZero(right) / max(1.0, float(right.size))
    if left_skin < 0.16 or right_skin < 0.16:
        return False

    value = hsv[:, :, 2]
    saturation = hsv[:, :, 1]
    dark_clothing = np.where((value < 85) & (saturation > 25), 255, 0).astype("uint8")
    lower_half = dark_clothing[int(crop_height * 0.50) :, :]
    lower_central = dark_clothing[
        int(crop_height * 0.55) :,
        int(crop_width * 0.25) : int(crop_width * 0.75),
    ]
    mid_skin = skin[
        int(crop_height * 0.20) : int(crop_height * 0.75),
        int(crop_width * 0.10) : int(crop_width * 0.90),
    ]
    lower_dark_ratio = cv2.countNonZero(lower_half) / max(1.0, float(lower_half.size))
    lower_central_dark_ratio = cv2.countNonZero(lower_central) / max(1.0, float(lower_central.size))
    mid_skin_ratio = cv2.countNonZero(mid_skin) / max(1.0, float(mid_skin.size))
    if lower_dark_ratio >= 0.14 and lower_central_dark_ratio >= 0.035 and mid_skin_ratio >= 0.18:
        return True

    dark_threshold = min(145, max(45, int(np.percentile(value, 32)) + 18))
    cx1 = int(crop_width * 0.39)
    cx2 = max(cx1 + 1, int(crop_width * 0.61))
    central_skin = skin[:, cx1:cx2]
    central_value = value[:, cx1:cx2]
    dark_gap = np.where((central_skin == 0) & (central_value <= dark_threshold), 255, 0).astype("uint8")
    dark_gap[: int(crop_height * 0.12), :] = 0
    dark_gap[int(crop_height * 0.92) :, :] = 0

    components, _, stats, _ = cv2.connectedComponentsWithStats(dark_gap, 8)
    for index in range(1, components):
        _, comp_top, comp_width, comp_height, comp_area = stats[index]
        tall_narrow_gap = comp_height / crop_height >= 0.18 and comp_width / crop_width <= 0.24
        meaningful_gap_area = comp_area / dark_gap.size >= 0.014
        starts_before_lower_chest = comp_top / crop_height <= 0.72
        has_dark_clothing_context = lower_dark_ratio >= 0.10 and lower_central_dark_ratio >= 0.025
        has_skin_context = mid_skin_ratio >= 0.18
        if tall_narrow_gap and meaningful_gap_area and starts_before_lower_chest and has_dark_clothing_context and has_skin_context:
            return True
    return False


def is_reasonable_cleavage_mask_bbox(bbox: List[float]) -> bool:
    width = max(0.0, bbox[2] - bbox[0])
    height = max(0.0, bbox[3] - bbox[1])
    area = width * height
    if width < 0.018 or height < 0.055 or area < 0.0011:
        return False
    return width <= 0.09 and height <= 0.22 and area <= 0.016


def focus_bbox_on_cleavage(bbox: List[float]) -> List[float]:
    # For cleavage-heavy covered-breast candidates, collapse the detector box
    # to a narrow inner-edge strip around the central groove rather than the
    # whole chest/upper-torso region.
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    center_x = (bbox[0] + bbox[2]) / 2.0
    if width >= 0.24 and 0.43 <= center_x <= 0.57:
        strip = max(width * 0.065, 0.032)
        focus_x1 = center_x - strip
        focus_x2 = center_x + strip
    elif center_x >= 0.5:
        focus_x1 = bbox[0] - width * 0.08
        focus_x2 = bbox[0] + width * 0.10
    else:
        focus_x1 = bbox[2] - width * 0.10
        focus_x2 = bbox[2] + width * 0.08
    focused = [
        focus_x1,
        bbox[1] + height * 0.16,
        focus_x2,
        bbox[1] + height * 0.98,
    ]
    return frame_mod.normalize_bbox(focused) or bbox


def cleavage_bbox_context(frame: Any, bbox: List[float]) -> Dict[str, Any]:
    cv2 = load_cv2()
    normalized = frame_mod.normalize_bbox(bbox)
    if not normalized:
        return {"ok": False, "reason": "invalid_bbox"}
    frame_height, frame_width = frame.shape[:2]
    width = normalized[2] - normalized[0]
    height = normalized[3] - normalized[1]
    context_bbox = frame_mod.normalize_bbox(
        [
            normalized[0] - width * 2.2,
            normalized[1] - height * 0.35,
            normalized[2] + width * 2.2,
            normalized[3] + height * 0.25,
        ]
    )
    if not context_bbox:
        return {"ok": False, "reason": "invalid_context_bbox"}
    x1 = max(0, min(frame_width - 1, int(round(context_bbox[0] * frame_width))))
    y1 = max(0, min(frame_height - 1, int(round(context_bbox[1] * frame_height))))
    x2 = max(x1 + 1, min(frame_width, int(round(context_bbox[2] * frame_width))))
    y2 = max(y1 + 1, min(frame_height, int(round(context_bbox[3] * frame_height))))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < 16 or crop.shape[1] < 16:
        return {"ok": False, "reason": "context_crop_too_small"}
    ycrcb = cv2.cvtColor(crop, cv2.COLOR_BGR2YCrCb)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    skin = cv2.bitwise_and(
        cv2.inRange(ycrcb, (25, 132, 75), (245, 180, 140)),
        cv2.inRange(hsv, (0, 18, 35), (30, 210, 255)),
    )
    skin_ratio = cv2.countNonZero(skin) / max(1.0, float(skin.size))
    skin_pixels = hsv[skin > 0]
    skin_value_median = float(np.median(skin_pixels[:, 2])) if skin_pixels.size else 0.0
    ok = skin_ratio >= 0.14 and skin_value_median >= 125.0
    return {
        "ok": ok,
        "skin_ratio": round(float(skin_ratio), 4),
        "skin_value_median": round(float(skin_value_median), 2),
        "context_bbox": context_bbox,
        "reason": "ok" if ok else "low_local_live_skin_context",
    }


def chest_boxes_same_subject(first: List[float], second: List[float]) -> bool:
    y_overlap = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    min_height = max(0.0001, min(first[3] - first[1], second[3] - second[1]))
    x_overlap = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    horizontal_gap = max(0.0, max(first[0], second[0]) - min(first[2], second[2]))
    center_distance = abs(((first[0] + first[2]) / 2.0) - ((second[0] + second[2]) / 2.0))
    return y_overlap / min_height >= 0.38 and (x_overlap > 0.0 or horizontal_gap <= 0.08 or center_distance <= 0.30)


def cluster_chest_detections(detections: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    clusters: List[List[Dict[str, Any]]] = []
    for detection in sorted(detections, key=lambda item: (item["bbox"][0] + item["bbox"][2]) / 2.0):
        placed = False
        for cluster in clusters:
            cluster_union = union_bboxes(item["bbox"] for item in cluster)
            if cluster_union and chest_boxes_same_subject(cluster_union, detection["bbox"]):
                cluster.append(detection)
                placed = True
                break
        if not placed:
            clusters.append([detection])
    return clusters


def cleavage_center_guess(chest_bboxes: List[List[float]]) -> float:
    union = union_bboxes(chest_bboxes)
    if not union:
        return 0.5
    sorted_boxes = sorted(chest_bboxes, key=lambda bbox: (bbox[0] + bbox[2]) / 2.0)
    if len(sorted_boxes) >= 2:
        left = sorted_boxes[0]
        right = sorted_boxes[-1]
        center = (left[2] + right[0]) / 2.0
    else:
        center = (union[0] + union[2]) / 2.0
    width = max(0.0001, union[2] - union[0])
    return max(union[0] + width * 0.20, min(union[2] - width * 0.20, center))


def localize_cleavage_groove_bbox(frame_path: Path, chest_bboxes: List[List[float]]) -> Tuple[Optional[List[float]], Dict[str, Any]]:
    """Find a narrow central groove strip inside NudeNet chest hints.

    NudeNet is useful for saying "look around this chest area", but its covered
    breast boxes are often left/right breast masses or nearby clothing. This
    second pass keeps only a small V-neck / central-groove strip, and skips the
    frame when the local evidence is weak.
    """
    chest_union = union_bboxes(chest_bboxes)
    if not chest_union:
        return None, {"status": "missing_chest_union"}
    union_width = chest_union[2] - chest_union[0]
    union_height = chest_union[3] - chest_union[1]
    if union_width <= 0.0 or union_height <= 0.0 or union_width > 0.78 or union_height > 0.46:
        return None, {"status": "unreasonable_chest_union", "union": chest_union}

    cv2 = load_cv2()
    frame = cv2.imread(str(frame_path))
    if frame is None:
        return None, {"status": "frame_read_failed"}
    frame_height, frame_width = frame.shape[:2]
    center_guess = cleavage_center_guess(chest_bboxes)
    search_width = union_width * (0.48 if len(chest_bboxes) >= 2 else 0.90)
    search_width = min(max(search_width, 0.18), 0.36)
    search_bbox = frame_mod.normalize_bbox(
        [
            center_guess - search_width / 2.0,
            chest_union[1] - union_height * 0.22,
            center_guess + search_width / 2.0,
            chest_union[1] + union_height * 1.08,
        ]
    )
    if not search_bbox:
        return None, {"status": "bad_search_bbox", "union": chest_union}

    x1 = max(0, min(frame_width - 1, int(round(search_bbox[0] * frame_width))))
    y1 = max(0, min(frame_height - 1, int(round(search_bbox[1] * frame_height))))
    x2 = max(x1 + 1, min(frame_width, int(round(search_bbox[2] * frame_width))))
    y2 = max(y1 + 1, min(frame_height, int(round(search_bbox[3] * frame_height))))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < 24 or crop.shape[1] < 24:
        return None, {"status": "search_crop_too_small", "search_bbox": search_bbox}

    ycrcb = cv2.cvtColor(crop, cv2.COLOR_BGR2YCrCb)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    ycrcb_skin = cv2.inRange(ycrcb, (25, 132, 75), (245, 180, 140))
    hsv_skin = cv2.inRange(hsv, (0, 18, 35), (30, 210, 255))
    skin = cv2.bitwise_and(ycrcb_skin, hsv_skin)
    skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    crop_height, crop_width = skin.shape[:2]
    skin_scan = skin[int(crop_height * 0.04) : int(crop_height * 0.78), :]
    skin_ratio = cv2.countNonZero(skin_scan) / max(1.0, float(skin_scan.size))
    if skin_ratio < 0.10:
        return None, {"status": "low_skin_context", "skin_ratio": round(float(skin_ratio), 4), "union": chest_union}

    value = hsv[:, :, 2]
    saturation = hsv[:, :, 1]
    rows = np.indices(value.shape)[0]
    bright_clothing = np.where(
        (value > 178)
        & (saturation < 115)
        & (rows > crop_height * 0.18)
        & (rows < crop_height * 0.85),
        255,
        0,
    ).astype("uint8")
    bright_clothing = cv2.dilate(bright_clothing, np.ones((5, 9), np.uint8), iterations=1)
    dark_threshold = min(112, max(38, int(np.percentile(value, 28)) + 14))
    dark = np.where(
        (value <= dark_threshold)
        & (rows > crop_height * 0.16)
        & (rows < crop_height * 0.96)
        & (bright_clothing == 0),
        255,
        0,
    ).astype("uint8")
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    dark_ratio = cv2.countNonZero(dark) / max(1.0, float(dark.size))

    components, _, stats, centroids = cv2.connectedComponentsWithStats(dark, 8)
    guess_px = (center_guess - search_bbox[0]) / max(0.0001, search_bbox[2] - search_bbox[0]) * crop_width
    best: Optional[Tuple[int, int, int, int, int, float, float, bool]] = None
    best_score = -99.0
    for index in range(1, components):
        comp_x, comp_y, comp_w, comp_h, comp_area = [int(value) for value in stats[index]]
        comp_cx, comp_cy = [float(value) for value in centroids[index]]
        area_ratio = comp_area / max(1.0, float(crop_height * crop_width))
        height_ratio = comp_h / max(1.0, float(crop_height))
        width_ratio = comp_w / max(1.0, float(crop_width))
        if height_ratio < 0.13 or area_ratio < 0.004 or width_ratio > 0.86:
            continue
        component_abs_x = search_bbox[0] + (comp_cx / max(1.0, float(crop_width))) * (search_bbox[2] - search_bbox[0])
        if abs(component_abs_x - center_guess) > max(0.12, search_width * 0.48):
            continue
        touches_edge = comp_x <= 1 or comp_x + comp_w >= crop_width - 1
        edge_penalty = 0.45 if touches_edge and width_ratio > 0.32 else 0.0
        score = area_ratio * 4.0 + height_ratio * 0.85 - abs(comp_cx - guess_px) / max(1.0, float(crop_width)) * 1.30 - edge_penalty
        if score > best_score:
            best_score = float(score)
            best = (comp_x, comp_y, comp_w, comp_h, comp_area, comp_cx, comp_cy, touches_edge)

    strip_width = max(0.026, min(0.055, union_width * 0.07))
    if best is None or best_score < 0.22:
        if skin_ratio < 0.24 or dark_ratio < 0.02:
            return None, {
                "status": "no_reliable_groove_component",
                "skin_ratio": round(float(skin_ratio), 4),
                "dark_ratio": round(float(dark_ratio), 4),
                "score": round(float(best_score), 4),
                "union": chest_union,
            }
        fallback_x = center_guess
        if best is not None:
            comp_x, _, comp_w, _, _, _, _, _ = best
            if comp_x <= 1:
                inner_edge_x = search_bbox[0] + ((comp_x + comp_w) / max(1.0, float(crop_width))) * (search_bbox[2] - search_bbox[0])
                fallback_x = center_guess * 0.20 + inner_edge_x * 0.80
            elif comp_x + comp_w >= crop_width - 1:
                inner_edge_x = search_bbox[0] + (comp_x / max(1.0, float(crop_width))) * (search_bbox[2] - search_bbox[0])
                fallback_x = center_guess * 0.20 + inner_edge_x * 0.80
            fallback_x = max(search_bbox[0] + (search_bbox[2] - search_bbox[0]) * 0.18, min(search_bbox[2] - (search_bbox[2] - search_bbox[0]) * 0.18, fallback_x))
        y_top = chest_union[1] + union_height * 0.14
        y_bottom = min(chest_union[1] + union_height * 0.76, y_top + max(0.075, min(0.13, union_height * 0.58)))
        fallback = frame_mod.normalize_bbox([fallback_x - strip_width / 2.0, y_top, fallback_x + strip_width / 2.0, y_bottom])
        if not fallback or not is_reasonable_cleavage_mask_bbox(fallback):
            return None, {"status": "fallback_bbox_rejected", "union": chest_union}
        context = cleavage_bbox_context(frame, fallback)
        if not context.get("ok"):
            return None, {"status": "fallback_context_rejected", "union": chest_union, "context": context}
        return fallback, {
            "status": "fallback_center_strip",
            "skin_ratio": round(float(skin_ratio), 4),
            "dark_ratio": round(float(dark_ratio), 4),
            "local_skin_ratio": context.get("skin_ratio"),
            "union": chest_union,
        }

    comp_x, comp_y, comp_w, comp_h, _, comp_cx, _, touches_edge = best
    dark_center = search_bbox[0] + (comp_cx / max(1.0, float(crop_width))) * (search_bbox[2] - search_bbox[0])
    dark_blend = 0.15 if touches_edge else 0.45
    target_x = center_guess * (1.0 - dark_blend) + dark_center * dark_blend
    target_x = max(search_bbox[0] + (search_bbox[2] - search_bbox[0]) * 0.18, min(search_bbox[2] - (search_bbox[2] - search_bbox[0]) * 0.18, target_x))
    component_top = search_bbox[1] + (comp_y / max(1.0, float(crop_height))) * (search_bbox[3] - search_bbox[1])
    y_top = max(chest_union[1] + union_height * 0.12, component_top - union_height * 0.18)
    y_bottom = min(chest_union[1] + union_height * 0.76, component_top + union_height * 0.34)
    if y_bottom - y_top < 0.075:
        center_y = (y_top + y_bottom) / 2.0
        y_top = center_y - 0.0375
        y_bottom = center_y + 0.0375
    bbox = frame_mod.normalize_bbox([target_x - strip_width / 2.0, y_top, target_x + strip_width / 2.0, y_bottom])
    if not bbox or not is_reasonable_cleavage_mask_bbox(bbox):
        return None, {"status": "localized_bbox_rejected", "union": chest_union}
    context = cleavage_bbox_context(frame, bbox)
    if not context.get("ok"):
        return None, {"status": "localized_context_rejected", "union": chest_union, "context": context}
    return bbox, {
        "status": "localized",
        "score": round(float(best_score), 4),
        "skin_ratio": round(float(skin_ratio), 4),
        "local_skin_ratio": context.get("skin_ratio"),
        "dark_ratio": round(float(dark_ratio), 4),
        "union": chest_union,
    }


def derive_pose_sensitive_regions(frame_path: Path, pose: Any, min_visibility: float) -> List[Dict[str, Any]]:
    if pose is None:
        return []
    cv2 = load_cv2()
    frame = cv2.imread(str(frame_path))
    if frame is None:
        return []
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    result = pose.process(rgb)
    if not result or not result.pose_landmarks:
        return []
    landmarks = result.pose_landmarks.landmark

    def visible(index: int) -> Optional[Any]:
        if index >= len(landmarks):
            return None
        landmark = landmarks[index]
        if getattr(landmark, "visibility", 1.0) < min_visibility:
            return None
        return landmark

    left_shoulder = visible(11)
    right_shoulder = visible(12)
    left_hip = visible(23)
    right_hip = visible(24)
    if not all((left_shoulder, right_shoulder, left_hip, right_hip)):
        return []

    shoulder_y = (left_shoulder.y + right_shoulder.y) / 2.0
    hip_y = (left_hip.y + right_hip.y) / 2.0
    shoulder_width = max(abs(left_shoulder.x - right_shoulder.x), 0.08)
    hip_width = max(abs(left_hip.x - right_hip.x), shoulder_width * 0.8)
    center_x = (left_shoulder.x + right_shoulder.x + left_hip.x + right_hip.x) / 4.0
    torso_height = max(abs(hip_y - shoulder_y), 0.12)

    chest = frame_mod.normalize_bbox(
        [
            center_x - shoulder_width * 0.72,
            shoulder_y - torso_height * 0.06,
            center_x + shoulder_width * 0.72,
            shoulder_y + torso_height * 0.42,
        ]
    )
    torso = frame_mod.normalize_bbox(
        [
            center_x - max(shoulder_width, hip_width) * 0.78,
            shoulder_y - torso_height * 0.03,
            center_x + max(shoulder_width, hip_width) * 0.78,
            hip_y + torso_height * 0.08,
        ]
    )
    pelvis = frame_mod.normalize_bbox(
        [
            center_x - hip_width * 0.75,
            hip_y - torso_height * 0.24,
            center_x + hip_width * 0.75,
            hip_y + torso_height * 0.22,
        ]
    )
    regions = []
    for name, bbox in (("chest", chest), ("torso", torso), ("pelvis", pelvis)):
        if bbox:
            regions.append({"group": name, "bbox": bbox, "source": "mediapipe_pose"})
    return regions


def adjust_bbox_with_pose(
    bbox: List[float],
    group: str,
    pose_regions: List[Dict[str, Any]],
    min_overlap_ratio: float = 0.15,
) -> Tuple[List[float], bool]:
    if not pose_regions:
        return bbox, False
    preferences = {
        "chest": ("chest", "torso"),
        "torso": ("torso", "chest"),
        "pelvis": ("pelvis", "torso"),
    }.get(group, (group, "torso"))
    candidates = [region for region in pose_regions if region.get("group") in preferences]
    best_intersection: Optional[List[float]] = None
    best_overlap = 0.0
    source_area = max(0.0001, bbox_area(bbox))
    for region in candidates:
        region_bbox = region.get("bbox")
        if not region_bbox:
            continue
        intersection = bbox_intersection(bbox, region_bbox)
        if not intersection:
            continue
        overlap = bbox_area(intersection) / source_area
        if overlap > best_overlap:
            best_intersection = intersection
            best_overlap = overlap
    if best_intersection and best_overlap >= min_overlap_ratio:
        return expand_bbox(best_intersection, 0.08), True
    return bbox, False


def frame_has_nsfw_decision(frame_results: List[Dict[str, Any]], timestamp: float, window: float) -> bool:
    for result in frame_results:
        decision = result.get("decision") or {}
        if "nsfw" not in decision.get("categories", []):
            continue
        if decision.get("action") == "PASS":
            continue
        if abs(float(result.get("timestamp", 0.0)) - timestamp) <= window:
            return True
    return False


def pose_fallback_candidates(
    frame_info: Dict[str, Any],
    pose_regions: List[Dict[str, Any]],
    requested_regions: set[str],
    reason: str,
) -> List[Dict[str, Any]]:
    candidates = []
    for region in pose_regions:
        group = region.get("group")
        if group not in requested_regions:
            continue
        bbox = region.get("bbox")
        if not bbox:
            continue
        if not frame_mod.is_local_visual_bbox(bbox, "nsfw"):
            continue
        candidates.append(
            {
                "time": round(float(frame_info["timestamp"]), 3),
                "frame_index": frame_info["index"],
                "bbox": bbox,
                "score": 0.62,
                "label": f"POSE_{str(group).upper()}",
                "group": group,
                "risk_action": "REVIEW",
                "source": "mediapipe_pose",
                "reason": reason,
                "pose_assisted": True,
                "shot_id": frame_info.get("shot_id"),
            }
        )
    return candidates


def provider_supports_vlm_confirmation(provider: str) -> bool:
    return provider in {"dashscope", "openai-compatible"}


def vision_confirms_obvious_cleavage(vision: Dict[str, Any]) -> bool:
    if not isinstance(vision, dict):
        return False
    categories = {str(item).lower() for item in vision.get("categories") or []}
    scores = vision.get("scores") or {}
    nsfw_score = frame_mod.clamp_score(scores.get("nsfw") if isinstance(scores, dict) else 0.0)
    risk_level = str(vision.get("risk_level") or "").lower()
    compact_text = json.dumps(vision, ensure_ascii=False).lower()
    cleavage_words = (
        "cleavage",
        "deep v",
        "low-cut",
        "low cut",
        "chest gap",
        "central groove",
        "乳沟",
        "胸沟",
        "深v",
        "低胸",
        "胸前沟",
    )
    negated_cleavage_phrases = (
        "no cleavage",
        "without cleavage",
        "no visible cleavage",
        "cleavage is not visible",
        "not showing cleavage",
        "not a cleavage",
        "没有乳沟",
        "未见乳沟",
        "无乳沟",
        "没有胸沟",
        "未见胸沟",
        "无胸沟",
    )
    has_cleavage_text = any(word in compact_text for word in cleavage_words)
    negates_cleavage = any(phrase in compact_text for phrase in negated_cleavage_phrases)
    raw_targets = vision.get("redaction_targets") or vision.get("redactions") or []
    if isinstance(vision.get("evidence"), dict):
        raw_targets = raw_targets or vision["evidence"].get("redaction_targets") or []
    has_local_nsfw_target = False
    for target in raw_targets if isinstance(raw_targets, list) else []:
        if not isinstance(target, dict):
            continue
        if str(target.get("category") or "").lower() != "nsfw":
            continue
        bbox = frame_mod.normalize_bbox(target.get("bbox"))
        if bbox and frame_mod.is_local_visual_bbox(bbox, "nsfw"):
            has_local_nsfw_target = True
            break
    has_nsfw_signal = "nsfw" in categories or nsfw_score >= 0.55 or risk_level in {"review", "block", "high", "medium"}
    if has_local_nsfw_target:
        return True
    if has_cleavage_text and not negates_cleavage:
        return True
    return bool(has_nsfw_signal and has_cleavage_text)


def confirm_covered_breast_candidates_with_vlm(
    candidates: List[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    pending = [
        item
        for item in candidates
        if item.get("needs_vlm_confirmation") and item.get("frame_path") and item.get("shot_id") is not None
    ]
    report = {
        "enabled": bool(pending),
        "provider": args.provider,
        "checked_frame_count": 0,
        "confirmed_frame_count": 0,
        "confirmed_candidate_count": 0,
        "skipped_reason": None,
        "error": None,
    }
    if not pending:
        report["skipped_reason"] = "no_covered_breast_candidates"
        return report
    if not provider_supports_vlm_confirmation(args.provider):
        report["skipped_reason"] = "provider_does_not_support_vlm_confirmation"
        return report

    min_gap = max(0.4, float(args.auto_nsfw_vlm_confirm_min_gap))
    buckets: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for item in pending:
        shot_id = int(item["shot_id"])
        bucket = int(float(item["time"]) / min_gap)
        key = (shot_id, bucket)
        existing = buckets.get(key)
        if existing is None or float(item.get("score", 0.0)) > float(existing.get("score", 0.0)):
            buckets[key] = item
    selected = sorted(buckets.values(), key=lambda item: (int(item["shot_id"]), float(item["time"])))
    max_checks = max(0, int(args.auto_nsfw_vlm_confirm_max_frames))
    if max_checks <= 0:
        report["skipped_reason"] = "max_confirmation_frames_is_zero"
        return report
    if len(selected) > max_checks:
        shot_representatives: Dict[int, Dict[str, Any]] = {}
        for item in selected:
            shot_id = int(item["shot_id"])
            existing = shot_representatives.get(shot_id)
            if existing is None or float(item.get("score", 0.0)) > float(existing.get("score", 0.0)):
                shot_representatives[shot_id] = item
        representative_items = sorted(shot_representatives.values(), key=lambda item: (int(item["shot_id"]), float(item["time"])))
        if len(representative_items) >= max_checks:
            selected_indexes = evenly_spaced_subset(list(range(len(representative_items))), max_checks)
            selected = [representative_items[index] for index in selected_indexes]
        else:
            chosen_ids = {id(item) for item in representative_items}
            remaining = [item for item in selected if id(item) not in chosen_ids]
            remaining_slots = max_checks - len(representative_items)
            if len(remaining) > remaining_slots:
                remaining_indexes = evenly_spaced_subset(list(range(len(remaining))), remaining_slots)
                remaining = [remaining[index] for index in remaining_indexes]
            selected = sorted(representative_items + remaining, key=lambda item: (int(item["shot_id"]), float(item["time"])))

    confirmed_windows: List[Tuple[int, float]] = []
    for item in selected:
        frame_path = Path(str(item["frame_path"]))
        try:
            vision = frame_mod.call_openai_compatible_vision(frame_path, args.timeout, args.provider, args.model)
        except Exception as exc:
            report["error"] = str(exc)
            continue
        report["checked_frame_count"] += 1
        if vision_confirms_obvious_cleavage(vision):
            report["confirmed_frame_count"] += 1
            confirmed_windows.append((int(item["shot_id"]), float(item["time"])))

    if not confirmed_windows and report["checked_frame_count"] == 0 and report["error"]:
        return report

    half_window = max(0.25, min_gap * 0.75)
    for item in pending:
        item_shot = int(item["shot_id"])
        item_time = float(item["time"])
        if any(item_shot == shot_id and abs(item_time - time) <= half_window for shot_id, time in confirmed_windows):
            item["confirmed_violation"] = True
            item["continuation_only"] = False
            item["vlm_confirmed"] = True
            report["confirmed_candidate_count"] += 1
    return report


def build_auto_nsfw_tracks(
    candidates: List[Dict[str, Any]],
    duration: float,
    sample_span: float,
    iou_threshold: float,
    max_gap: float,
    chest_max_gap: float,
    chest_continuation_hold: float,
    smooth_alpha: float,
    min_hits: int,
    shot_ranges: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], str, float]:
    tracks: List[Dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: (float(item["time"]), item.get("group", ""))):
        best_track: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for track in tracks:
            last = track["items"][-1]
            if candidate.get("shot_id") is not None and last.get("shot_id") is not None and candidate.get("shot_id") != last.get("shot_id"):
                continue
            gap = float(candidate["time"]) - float(last["time"])
            allowed_gap = max_gap
            if candidate.get("group") == "chest" and last.get("group") == "chest":
                allowed_gap = max(max_gap, chest_max_gap)
            if gap < -0.001 or gap > allowed_gap:
                continue
            if candidate.get("group") != last.get("group"):
                continue
            iou = bbox_iou(candidate["bbox"], last["bbox"])
            distance = bbox_center_distance(candidate["bbox"], last["bbox"])
            match_score = iou + max(0.0, 0.22 - distance)
            if (iou >= iou_threshold or distance <= 0.12) and match_score > best_score:
                best_track = track
                best_score = match_score
        if best_track is None:
            tracks.append({"items": [candidate]})
        else:
            best_track["items"].append(candidate)

    redactions = []
    strongest = "PASS"
    max_score = 0.0
    half_span = max(0.05, sample_span / 2.0)
    for index, track in enumerate(tracks, start=1):
        items = sorted(track["items"], key=lambda item: float(item["time"]))
        if len(items) < min_hits:
            continue
        labels = sorted({str(item.get("label", "")) for item in items if item.get("label")})
        requires_confirmed_cleavage = any(normalize_detector_label(label) in CHEST_SUGGESTIVE_LABELS for label in labels)
        confirmed_hits = [item for item in items if item.get("confirmed_violation")]
        if requires_confirmed_cleavage and not confirmed_hits:
            continue
        if requires_confirmed_cleavage:
            # VLM confirms the semantic risk, while the per-frame localizer
            # decides whether the groove is still visible. Keep the whole local
            # same-shot track once it has at least one confirmed hit, instead of
            # clipping it to sparse VLM confirmation windows.
            if len(items) < min_hits:
                continue
            labels = sorted({str(item.get("label", "")) for item in items if item.get("label")})
            confirmed_hits = [item for item in items if item.get("confirmed_violation")]
        sources = sorted({str(item.get("source", "")) for item in items if item.get("source")})
        track_action = strongest_action({"action": item.get("risk_action", "REVIEW")} for item in items)
        strongest = strongest_action(({"action": strongest}, {"action": track_action}))
        track_score = max(float(item.get("score", 0.0)) for item in items)
        max_score = max(max_score, track_score)
        shot_ids = sorted({int(item["shot_id"]) for item in items if item.get("shot_id") is not None})

        smoothed_keyframes = []
        smoothed_bbox = items[0]["bbox"]
        effective_smooth_alpha = max(smooth_alpha, 0.88) if requires_confirmed_cleavage else smooth_alpha
        for item in items:
            if item is items[0]:
                smoothed_bbox = item["bbox"]
            else:
                smoothed_bbox = interpolate_bbox(smoothed_bbox, item["bbox"], effective_smooth_alpha)
            smoothed_keyframes.append({"time": round(float(item["time"]), 3), "bbox": smoothed_bbox})

        start_time = max(0.0, float(items[0]["time"]) - half_span)
        end_time = min(duration, float(items[-1]["time"]) + half_span)
        if end_time <= start_time:
            end_time = min(duration, start_time + max(0.2, sample_span))
        redactions.append(
            frame_mod.sanitize_visual_redaction(
                {
                "type": "visual_mosaic",
                "category": "nsfw",
                "reason": "Auto-localized NSFW-sensitive body region using "
                + ", ".join(source for source in sources if source)
                + f"; labels={', '.join(labels)}.",
                "start_time": round(start_time, 3),
                "end_time": round(end_time, 3),
                "bbox_keyframes": smoothed_keyframes,
                "source": "auto_nsfw",
                "track_id": f"nsfw_{index:03d}",
                "shot_id": shot_ids[0] if len(shot_ids) == 1 else None,
                "shot_ids": shot_ids if len(shot_ids) > 1 else [],
                "detector_labels": labels,
                "detector_score": round(track_score, 4),
                "risk_action": track_action,
                "confirmed_hit_count": len(confirmed_hits),
                "continued_hit_count": len(items) - len(confirmed_hits),
                }
            )
        )
    return redactions, strongest, max_score


def run_auto_nsfw_redactions(
    video_path: Path,
    metadata: Dict[str, Any],
    work_dir: Path,
    frame_results: List[Dict[str, Any]],
    args: argparse.Namespace,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    fps = float(metadata.get("fps") or 0.0)
    frame_count = int(metadata.get("frame_count") or 0)
    duration = float(metadata.get("duration") or 0.0)
    sample_interval = args.auto_nsfw_sample_interval
    shot_starts, shot_diagnostics = detect_shot_starts(
        video_path,
        frame_count,
        fps,
        args.auto_nsfw_shot_threshold,
        args.auto_nsfw_shot_min_gap,
        args.auto_nsfw_shot_scan_fps,
    )
    shot_ranges = build_shot_ranges(shot_starts, frame_count, fps)
    indices = choose_auto_nsfw_indices(
        frame_count,
        fps,
        sample_interval,
        args.auto_nsfw_sample_count,
        args.auto_nsfw_max_frames,
        shot_ranges,
    )
    auto_frames = extract_sample_frames(video_path, work_dir, indices, fps, subdir="auto_nsfw_frames")
    annotate_frames_with_shots(auto_frames, shot_starts, shot_ranges)

    detector, detector_error = load_nudenet_detector(args.nudenet_model_path)
    pose = None
    pose_error = None
    use_pose = True
    if use_pose:
        pose, pose_error = load_mediapipe_pose()

    candidates: List[Dict[str, Any]] = []
    raw_detection_count = 0
    chest_raw_candidate_count = 0
    chest_cluster_count = 0
    chest_localized_count = 0
    chest_localization_skipped_count = 0
    pose_region_count = 0
    requested_pose_regions = {
        item.strip().lower()
        for item in str(args.auto_nsfw_pose_regions or "").split(",")
        if item.strip()
    }
    fallback_window = max(1.0, sample_interval or (duration / max(len(auto_frames), 1) if duration else 1.0))

    for frame_info in auto_frames:
        frame_path = Path(frame_info["path"])
        pose_regions = derive_pose_sensitive_regions(frame_path, pose, args.auto_nsfw_pose_min_visibility) if pose else []
        pose_region_count += len(pose_regions)
        if detector is not None:
            try:
                detections = detector.detect(str(frame_path)) or []
            except Exception as exc:
                detector_error = f"NudeDetector.detect failed: {exc}"
                detections = []
            covered_chest_detections: List[Dict[str, Any]] = []
            for detection in detections:
                if not isinstance(detection, dict):
                    continue
                raw_detection_count += 1
                label = detection.get("class") or detection.get("label") or detection.get("name")
                normalized_label = normalize_detector_label(label)
                if detector_label_is_face(normalized_label):
                    continue
                risk = nudenet_label_risk(str(label), AUTO_NSFW_INCLUDE_SUGGESTIVE)
                if not risk:
                    continue
                score = frame_mod.clamp_score(
                    detection.get("score")
                    if detection.get("score") is not None
                    else detection.get("confidence")
                )
                min_score = args.auto_nsfw_min_score
                if normalized_label in CHEST_SUGGESTIVE_LABELS:
                    min_score = min(min_score, args.auto_nsfw_chest_suggestive_min_score)
                if score < min_score:
                    continue
                bbox = detector_box_to_normalized(
                    detection.get("box") if detection.get("box") is not None else detection.get("bbox"),
                    int(metadata["width"]),
                    int(metadata["height"]),
                )
                if not bbox:
                    continue
                risk_action, group = risk
                confirmed_violation = True
                needs_vlm_confirmation = False
                if group == "chest" and normalized_label in CHEST_SUGGESTIVE_LABELS:
                    covered_chest_detections.append(
                        {
                            "bbox": bbox,
                            "score": score,
                            "label": normalized_label,
                            "risk_action": risk_action,
                        }
                    )
                    chest_raw_candidate_count += 1
                    continue
                if risk_action == "BLOCK" and score < args.auto_nsfw_block_score:
                    risk_action = "REVIEW"
                bbox = expand_bbox(bbox, args.auto_nsfw_padding)
                pose_assisted = False
                if use_pose:
                    bbox, pose_assisted = adjust_bbox_with_pose(bbox, group, pose_regions)
                if not frame_mod.is_local_visual_bbox(bbox, "nsfw"):
                    continue
                candidates.append(
                    {
                        "time": round(float(frame_info["timestamp"]), 3),
                        "frame_index": frame_info["index"],
                        "bbox": bbox,
                        "score": score,
                        "label": normalized_label,
                        "group": group,
                        "risk_action": risk_action,
                        "source": "nudenet",
                        "pose_assisted": pose_assisted,
                        "confirmed_violation": confirmed_violation,
                        "continuation_only": False,
                        "needs_vlm_confirmation": needs_vlm_confirmation,
                        "frame_path": str(frame_path),
                        "shot_id": frame_info.get("shot_id"),
                    }
                )

            for cluster in cluster_chest_detections(covered_chest_detections):
                chest_cluster_count += 1
                cluster_bboxes = [item["bbox"] for item in cluster]
                bbox, localization = localize_cleavage_groove_bbox(frame_path, cluster_bboxes)
                if not bbox:
                    chest_localization_skipped_count += 1
                    continue
                bbox = expand_bbox(bbox, min(args.auto_nsfw_padding, args.auto_nsfw_cleavage_padding))
                if not is_reasonable_cleavage_mask_bbox(bbox):
                    chest_localization_skipped_count += 1
                    continue
                if not frame_mod.is_local_visual_bbox(bbox, "nsfw"):
                    chest_localization_skipped_count += 1
                    continue
                chest_localized_count += 1
                score = max(float(item.get("score", 0.0)) for item in cluster)
                candidates.append(
                    {
                        "time": round(float(frame_info["timestamp"]), 3),
                        "frame_index": frame_info["index"],
                        "bbox": bbox,
                        "score": score,
                        "label": "FEMALE_BREAST_COVERED",
                        "group": "chest",
                        "risk_action": "REVIEW",
                        "source": "nudenet+opencv_cleavage_localizer",
                        "pose_assisted": False,
                        "confirmed_violation": False,
                        "continuation_only": True,
                        "needs_vlm_confirmation": True,
                        "frame_path": str(frame_path),
                        "shot_id": frame_info.get("shot_id"),
                        "localization_status": localization.get("status"),
                    }
                )

        fallback_allowed = False
        if args.auto_nsfw_pose_fallback == "always":
            fallback_allowed = True
        elif args.auto_nsfw_pose_fallback == "when-nsfw":
            fallback_allowed = frame_has_nsfw_decision(frame_results, float(frame_info["timestamp"]), fallback_window)
        if fallback_allowed and pose_regions:
            candidates.extend(
                pose_fallback_candidates(
                    frame_info,
                    pose_regions,
                    requested_pose_regions or {"chest", "torso", "pelvis"},
                    "Pose fallback region was enabled for NSFW-sensitive masking.",
                )
            )

    vlm_confirmation = confirm_covered_breast_candidates_with_vlm(candidates, args)

    actual_sample_span = sample_interval
    if actual_sample_span is None:
        actual_sample_span = duration / max(len(auto_frames), 1) if duration else 1.0
    redactions, action, max_score = build_auto_nsfw_tracks(
        candidates,
        duration,
        actual_sample_span,
        args.auto_nsfw_track_iou,
        args.auto_nsfw_track_max_gap,
        args.auto_nsfw_chest_track_max_gap,
        args.auto_nsfw_chest_continuation_hold,
        args.auto_nsfw_smooth_alpha,
        args.auto_nsfw_min_track_hits,
        shot_ranges,
    )
    labels = sorted({str(candidate.get("label")) for candidate in candidates if candidate.get("label")})
    diagnostics = {
        "enabled": True,
        "frames_analyzed": len(auto_frames),
        "sample_interval": sample_interval,
        "sample_count": len(auto_frames),
        "detector": {
            "name": "nudenet",
            "available": detector is not None,
            "error": detector_error,
            "raw_detection_count": raw_detection_count,
            "candidate_count": len([item for item in candidates if str(item.get("source", "")).startswith("nudenet")]),
            "covered_chest_raw_candidate_count": chest_raw_candidate_count,
            "covered_chest_cluster_count": chest_cluster_count,
            "covered_chest_localized_count": chest_localized_count,
            "covered_chest_localization_skipped_count": chest_localization_skipped_count,
            "min_score": args.auto_nsfw_min_score,
            "chest_suggestive_min_score": args.auto_nsfw_chest_suggestive_min_score,
            "cleavage_padding": args.auto_nsfw_cleavage_padding,
            "chest_track_max_gap": args.auto_nsfw_chest_track_max_gap,
            "chest_continuation_hold": args.auto_nsfw_chest_continuation_hold,
            "include_suggestive": AUTO_NSFW_INCLUDE_SUGGESTIVE,
        },
        "shot_detection": {
            "enabled": True,
            **shot_diagnostics,
            "shot_count": len(shot_ranges),
            "shot_starts": [
                {
                    "shot_id": shot["shot_id"],
                    "frame": shot["start_frame"],
                    "time": shot["start_time"],
                }
                for shot in shot_ranges
            ],
        },
        "pose": {
            "enabled": use_pose,
            "available": pose is not None,
            "error": pose_error,
            "fallback": args.auto_nsfw_pose_fallback,
            "region_count": pose_region_count,
        },
        "candidate_count": len(candidates),
        "vlm_confirmation": vlm_confirmation,
        "track_count": len(redactions),
        "redaction_count": len(redactions),
        "labels": labels,
        "action": action,
        "max_score": round(max_score, 4),
    }
    if pose is not None:
        pose.close()
    return redactions, diagnostics


def merge_auto_nsfw_redactions(decision: Dict[str, Any], auto_report: Dict[str, Any]) -> Dict[str, Any]:
    redactions = auto_report.get("redactions") or []
    if not redactions:
        return decision
    merged = dict(decision)
    categories = set(merged.get("categories", []))
    categories.add("nsfw")
    merged["categories"] = sorted(categories)
    auto_action = auto_report.get("action") or "REVIEW"
    if ACTION_RANK.get(auto_action, 0) > ACTION_RANK.get(merged.get("action", "PASS"), 0):
        merged["action"] = auto_action
    merged["confidence"] = round(
        max(float(merged.get("confidence", 0.0)), float(auto_report.get("max_score", 0.0)), 0.68),
        4,
    )
    reasons = list(merged.get("reasons", []))
    reasons.append("Auto-localized NSFW-sensitive body regions were added for dynamic masking.")
    merged["reasons"] = frame_mod.dedupe(reasons)
    evidence = dict(merged.get("evidence", {}))
    scores = dict(evidence.get("scores", {}))
    scores["nsfw"] = round(max(float(scores.get("nsfw", 0.0)), float(auto_report.get("max_score", 0.0)), 0.68), 4)
    evidence["scores"] = scores
    labels = list(evidence.get("labels", []))
    labels.extend(auto_report.get("labels", []))
    if labels:
        evidence["labels"] = frame_mod.dedupe(labels)
    hits = list(evidence.get("policy_hits", []))
    hits.append("nsfw.auto_redaction.localized_body_region")
    evidence["policy_hits"] = frame_mod.dedupe(hits)
    merged["evidence"] = evidence
    merged["redactions"] = dedupe_redactions(list(merged.get("redactions", [])) + redactions)
    return merged


def interpolate_bbox(first: List[float], second: List[float], ratio: float) -> List[float]:
    ratio = max(0.0, min(1.0, ratio))
    return [round(first[index] + (second[index] - first[index]) * ratio, 4) for index in range(4)]


def bbox_at_time(redaction: Dict[str, Any], timestamp: float) -> Optional[List[float]]:
    keyframes = redaction.get("bbox_keyframes") or redaction.get("keyframes")
    if not keyframes:
        return frame_mod.normalize_bbox(redaction.get("bbox"))

    normalized_keyframes = []
    for keyframe in keyframes:
        if not isinstance(keyframe, dict):
            continue
        keyframe_time = keyframe.get("time")
        if keyframe_time is None:
            keyframe_time = keyframe.get("timestamp")
        bbox = frame_mod.normalize_bbox(keyframe.get("bbox"))
        if keyframe_time is None or not bbox:
            continue
        normalized_keyframes.append((float(keyframe_time), bbox))

    if not normalized_keyframes:
        return frame_mod.normalize_bbox(redaction.get("bbox"))

    normalized_keyframes.sort(key=lambda item: item[0])
    if timestamp <= normalized_keyframes[0][0]:
        return normalized_keyframes[0][1]
    if timestamp >= normalized_keyframes[-1][0]:
        return normalized_keyframes[-1][1]

    for index in range(len(normalized_keyframes) - 1):
        left_time, left_bbox = normalized_keyframes[index]
        right_time, right_bbox = normalized_keyframes[index + 1]
        if left_time <= timestamp <= right_time:
            span = max(0.001, right_time - left_time)
            return interpolate_bbox(left_bbox, right_bbox, (timestamp - left_time) / span)
    return normalized_keyframes[-1][1]


def redaction_box_to_pixels(
    redaction: Dict[str, Any],
    timestamp: float,
    width: int,
    height: int,
) -> Optional[Tuple[int, int, int, int]]:
    bbox = bbox_at_time(redaction, timestamp)
    if redaction.get("type") in frame_mod.VISUAL_MASK_TYPES and not frame_mod.is_local_visual_bbox(
        bbox,
        redaction.get("category"),
    ):
        return None
    return normalized_box_to_pixels(bbox, width, height)


def pixelate_cv2_region(frame: Any, box: Tuple[int, int, int, int], blocks: int = 14) -> None:
    cv2 = load_cv2()
    x1, y1, x2, y2 = box
    h, w = frame.shape[:2]
    x1, x2 = sorted((max(0, min(w, x1)), max(0, min(w, x2))))
    y1, y2 = sorted((max(0, min(h, y1)), max(0, min(h, y2))))
    if x2 <= x1 or y2 <= y1:
        return
    roi = frame[y1:y2, x1:x2]
    block_size = max(4, int(blocks))
    small_w = max(1, min(x2 - x1, (x2 - x1) // block_size))
    small_h = max(1, min(y2 - y1, (y2 - y1) // block_size))
    small = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
    frame[y1:y2, x1:x2] = cv2.resize(small, (x2 - x1, y2 - y1), interpolation=cv2.INTER_NEAREST)


def redaction_applies(redaction: Dict[str, Any], timestamp: float, mask_actions: set[str], video_action: str) -> bool:
    if video_action not in mask_actions:
        return False
    start = redaction.get("start_time")
    end = redaction.get("end_time")
    if start is None and end is None:
        return True
    start = 0.0 if start is None else float(start)
    end = start + 2.0 if end is None else float(end)
    return start <= timestamp <= end


def render_masked_video(
    video_path: Path,
    output_path: Path,
    redactions: List[Dict[str, Any]],
    video_action: str,
    mask_actions: set[str],
    max_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    cv2 = load_cv2()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open video for masking: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Failed to open video writer: {output_path}")

    processed = 0
    masked_frames = 0
    subtitle_band = (0, int(height * 0.68), width, int(height * 0.92))
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        timestamp = processed / fps if fps else 0.0
        if max_seconds is not None and timestamp > max_seconds:
            break
        applied = False
        for redaction in redactions:
            if not redaction_applies(redaction, timestamp, mask_actions, video_action):
                continue
            rtype = redaction.get("type")
            if rtype in {"visual_mosaic", "visual_blur"}:
                if redaction.get("region") == "full_frame":
                    continue
                box = redaction_box_to_pixels(redaction, timestamp, width, height)
                if box:
                    pixelate_cv2_region(frame, box)
                    applied = True
            elif rtype in {"text_mosaic", "subtitle_replace"}:
                box = redaction_box_to_pixels(redaction, timestamp, width, height) or subtitle_band
                pixelate_cv2_region(frame, box)
                applied = True
        if applied:
            masked_frames += 1
        writer.write(frame)
        processed += 1

    cap.release()
    writer.release()
    return {
        "path": str(output_path),
        "processed_frames": processed,
        "masked_frames": masked_frames,
        "audio_preserved": False,
        "warning": "OpenCV preview export is visual-only. Use ffmpeg integration in backend for audio mute and audio preservation.",
        "source_frame_count": frame_count,
    }


def renderable_visual_redaction(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if item.get("region") == "full_frame":
        return None
    rtype = item.get("type")
    if rtype in frame_mod.VISUAL_MASK_TYPES:
        normalized = normalize_redaction_item(item)
        return normalized if normalized.get("type") in frame_mod.VISUAL_MASK_TYPES else None
    if rtype in {"text_mosaic", "subtitle_replace"}:
        return dict(item)
    return None


def renderable_visual_redactions(redactions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items = []
    for item in redactions:
        if not isinstance(item, dict):
            continue
        normalized = renderable_visual_redaction(item)
        if normalized:
            items.append(normalized)
    return items


def build_ffmpeg_plan(redactions: List[Dict[str, Any]]) -> Dict[str, Any]:
    audio_mutes = [
        {
            "start_time": item.get("start_time"),
            "end_time": item.get("end_time"),
            "category": item.get("category"),
            "reason": item.get("reason"),
        }
        for item in redactions
        if item.get("type") == "audio_mute"
    ]
    visual_masks = renderable_visual_redactions(redactions)
    return {
        "audio_mutes": audio_mutes,
        "visual_masks": visual_masks,
        "note": "Production should translate these targets into ffmpeg enable='between(t,start,end)' filters and volume=0 intervals.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run video visual/dialogue moderation and optional masking.")
    parser.add_argument("video", help="Video file path.")
    parser.add_argument("--transcript", action="append", default=[], help="SRT/VTT/TXT/JSON transcript file. Repeatable.")
    parser.add_argument(
        "--dialogue",
        action="append",
        default=[],
        help="Inline dialogue segment as 'start,end,text' or plain text. Repeatable.",
    )
    parser.add_argument("--provider", choices=("mock", "sidecar", "openai-compatible", "dashscope"), default="sidecar")
    parser.add_argument("--model", help="Vision model override, for example qwen3-vl-flash or qwen3-vl-plus.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--engine", choices=("auto", "sequential", "langgraph"), default="auto")
    parser.add_argument("--sample-interval", type=float, help="Sample every N seconds.")
    parser.add_argument("--sample-count", type=int, default=20, help="Uniform frame sample count when interval is omitted.")
    parser.add_argument("--transcript-window", type=float, default=1.5, help="Attach transcript segments within +/- seconds of each frame.")
    parser.add_argument("--redaction-window", type=float, help="Seconds around sampled frame for visual redactions.")
    parser.add_argument("--work-dir", help="Directory for sampled frames and sidecars.")
    parser.add_argument("--output", help="Write JSON report to this path.")
    parser.add_argument("--redactions-json", help="Extra redaction targets, including dynamic bbox_keyframes.")
    parser.add_argument(
        "--extra-redaction-action",
        choices=("PASS", "REVIEW", "BLOCK"),
        default="BLOCK",
        help="Video action to apply when --redactions-json is provided.",
    )
    parser.add_argument("--masked-output", help="Write visual-only masked preview video to this path when redactions exist.")
    parser.add_argument(
        "--mask-actions",
        default="REVIEW,BLOCK",
        help="Comma-separated video actions that should trigger masked preview rendering.",
    )
    parser.add_argument("--mask-render-max-seconds", type=float, help="Limit masked preview rendering for smoke tests.")
    parser.add_argument("--nudenet-model-path", help="Optional NudeNet ONNX model path override.")
    parser.add_argument(
        "--auto-nsfw-sample-interval",
        type=float,
        default=0.25,
        help="Sample interval in seconds for default NSFW localization.",
    )
    parser.add_argument(
        "--auto-nsfw-sample-count",
        type=int,
        default=16,
        help="Fallback uniform sample count when auto interval is disabled.",
    )
    parser.add_argument("--auto-nsfw-max-frames", type=int, help="Limit auto NSFW localization frames for smoke tests.")
    parser.add_argument("--auto-nsfw-min-score", type=float, default=0.35, help="Minimum NudeNet score to create a candidate.")
    parser.add_argument(
        "--auto-nsfw-chest-suggestive-min-score",
        type=float,
        default=0.25,
        help="Lower NudeNet score floor for covered-breast candidates, still gated by obvious-cleavage detection.",
    )
    parser.add_argument(
        "--auto-nsfw-cleavage-padding",
        type=float,
        default=0.025,
        help="Small relative padding for covered-breast cleavage groove masks.",
    )
    parser.add_argument(
        "--auto-nsfw-chest-track-max-gap",
        type=float,
        default=1.0,
        help="Max seconds to keep linking covered-breast cleavage tracks across brief detector gaps.",
    )
    parser.add_argument(
        "--auto-nsfw-chest-continuation-hold",
        type=float,
        default=0.45,
        help="Seconds to keep masking a covered-breast groove after the last confirmed cleavage hit.",
    )
    parser.add_argument("--auto-nsfw-block-score", type=float, default=0.75, help="Exposed class score required for BLOCK.")
    parser.add_argument(
        "--auto-nsfw-pose-fallback",
        choices=("off", "when-nsfw", "always"),
        default="when-nsfw",
        help="Create pose-derived mask boxes when NudeNet misses. Use when-nsfw for production-style gating.",
    )
    parser.add_argument(
        "--auto-nsfw-pose-regions",
        default="pelvis",
        help="Comma-separated pose fallback regions. Default avoids face/chest/torso broad masking; chest is detector-gated.",
    )
    parser.add_argument("--auto-nsfw-pose-min-visibility", type=float, default=0.5)
    parser.add_argument("--auto-nsfw-padding", type=float, default=0.08, help="Relative padding around detector boxes.")
    parser.add_argument("--auto-nsfw-track-iou", type=float, default=0.2, help="IoU threshold for auto NSFW track matching.")
    parser.add_argument("--auto-nsfw-track-max-gap", type=float, default=0.8, help="Max seconds between matched track boxes.")
    parser.add_argument("--auto-nsfw-smooth-alpha", type=float, default=0.65, help="EMA smoothing weight toward current box.")
    parser.add_argument("--auto-nsfw-min-track-hits", type=int, default=1, help="Minimum detections before emitting a track.")
    parser.add_argument(
        "--auto-nsfw-shot-threshold",
        type=float,
        default=0.36,
        help="Content-difference threshold for shot-aware auto NSFW re-detection.",
    )
    parser.add_argument(
        "--auto-nsfw-shot-min-gap",
        type=float,
        default=0.45,
        help="Minimum seconds between auto NSFW shot boundaries.",
    )
    parser.add_argument(
        "--auto-nsfw-shot-scan-fps",
        type=float,
        default=6.0,
        help="Frames per second to scan for auto NSFW shot boundaries.",
    )
    parser.add_argument(
        "--auto-nsfw-vlm-confirm-max-frames",
        type=int,
        default=24,
        help="Max covered-breast candidate frames to confirm with VLM before rendering masks.",
    )
    parser.add_argument(
        "--auto-nsfw-vlm-confirm-min-gap",
        type=float,
        default=1.0,
        help="Minimum seconds between VLM-covered-breast confirmation buckets.",
    )
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if video_path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported video extension: {video_path.suffix}")

    metadata = video_metadata(video_path)
    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="visual_moderation_video_"))
    work_dir.mkdir(parents=True, exist_ok=True)
    transcript_segments = load_transcript_segments(args.transcript, args.dialogue)
    extra_redactions = load_extra_redactions(args.redactions_json)
    indices = sample_indices(
        int(metadata["frame_count"]),
        float(metadata["fps"]),
        args.sample_interval,
        args.sample_count,
    )
    sampled_frames = extract_sample_frames(video_path, work_dir, indices, float(metadata["fps"]))

    frame_results = []
    for frame_info in sampled_frames:
        frame_results.append(
            analyze_frame(
                frame_info,
                args.provider,
                args.model,
                args.timeout,
                args.engine,
                transcript_segments,
                args.transcript_window,
            )
        )

    transcript_results = analyze_transcript_segments(transcript_segments, args.engine)
    auto_redactions, auto_nsfw_diagnostics = run_auto_nsfw_redactions(
        video_path,
        metadata,
        work_dir,
        frame_results,
        args,
    )
    auto_nsfw_report = dict(auto_nsfw_diagnostics)
    auto_nsfw_report["redactions"] = auto_redactions
    sample_window = args.redaction_window
    if sample_window is None:
        if args.sample_interval:
            sample_window = max(0.5, args.sample_interval / 2.0)
        elif sampled_frames:
            sample_window = max(0.5, float(metadata["duration"]) / max(len(sampled_frames), 1) / 2.0)
        else:
            sample_window = 1.0

    decision = aggregate_results(video_path, metadata, frame_results, transcript_results, sample_window)
    decision = merge_auto_nsfw_redactions(decision, auto_nsfw_report)
    decision = merge_extra_redactions(decision, extra_redactions, args.extra_redaction_action)
    report: Dict[str, Any] = {
        "video": str(video_path),
        "metadata": {
            "frame_count": int(metadata["frame_count"]),
            "fps": round(float(metadata["fps"]), 4),
            "width": int(metadata["width"]),
            "height": int(metadata["height"]),
            "duration": round(float(metadata["duration"]), 3),
        },
        "sampling": {
            "sample_count": len(sampled_frames),
            "sample_interval": args.sample_interval,
            "work_dir": str(work_dir),
        },
        "decision": decision,
        "frame_results": frame_results,
        "transcript_results": transcript_results,
        "auto_nsfw": auto_nsfw_report,
        "ffmpeg_plan": build_ffmpeg_plan(decision["redactions"]),
    }

    if args.masked_output and decision["redactions"]:
        mask_actions = {item.strip().upper() for item in args.mask_actions.split(",") if item.strip()}
        report["masked_output"] = render_masked_video(
            video_path,
            Path(args.masked_output),
            decision["redactions"],
            decision["action"],
            mask_actions,
            args.mask_render_max_seconds,
        )
    else:
        report["masked_output"] = None

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
