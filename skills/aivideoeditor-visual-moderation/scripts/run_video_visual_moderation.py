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
    "BELLY_EXPOSED",
    "BUTTOCKS_COVERED",
    "FEMALE_BREAST_COVERED",
}
CHEST_SUGGESTIVE_LABELS = {"FEMALE_BREAST_COVERED"}

NSFW_CLASS_GROUPS = {
    "ANUS_EXPOSED": "pelvis",
    "BUTTOCKS_EXPOSED": "pelvis",
    "BUTTOCKS_COVERED": "pelvis",
    "FEMALE_BREAST_EXPOSED": "chest",
    "FEMALE_BREAST_COVERED": "chest",
    "FEMALE_GENITALIA_EXPOSED": "pelvis",
    "MALE_GENITALIA_EXPOSED": "pelvis",
    "BELLY_EXPOSED": "torso",
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
    return normalized


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
            redactions.append(item)

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
            redactions.append(item)

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


def image_region_has_obvious_cleavage(frame_path: Path, bbox: List[float]) -> bool:
    normalized = frame_mod.normalize_bbox(bbox)
    if not normalized:
        return False
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
        _, _, comp_width, comp_height, comp_area = stats[index]
        if comp_height / crop_height >= 0.18 and comp_width / crop_width <= 0.22 and comp_area / dark_gap.size >= 0.018:
            return True
    return False


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
            }
        )
    return candidates


def build_auto_nsfw_tracks(
    candidates: List[Dict[str, Any]],
    duration: float,
    sample_span: float,
    iou_threshold: float,
    max_gap: float,
    smooth_alpha: float,
    min_hits: int,
) -> Tuple[List[Dict[str, Any]], str, float]:
    tracks: List[Dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: (float(item["time"]), item.get("group", ""))):
        best_track: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for track in tracks:
            last = track["items"][-1]
            gap = float(candidate["time"]) - float(last["time"])
            if gap < -0.001 or gap > max_gap:
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
        sources = sorted({str(item.get("source", "")) for item in items if item.get("source")})
        track_action = strongest_action({"action": item.get("risk_action", "REVIEW")} for item in items)
        strongest = strongest_action(({"action": strongest}, {"action": track_action}))
        track_score = max(float(item.get("score", 0.0)) for item in items)
        max_score = max(max_score, track_score)

        smoothed_keyframes = []
        smoothed_bbox = items[0]["bbox"]
        for item in items:
            if item is items[0]:
                smoothed_bbox = item["bbox"]
            else:
                smoothed_bbox = interpolate_bbox(smoothed_bbox, item["bbox"], smooth_alpha)
            smoothed_keyframes.append({"time": round(float(item["time"]), 3), "bbox": smoothed_bbox})

        start_time = max(0.0, float(items[0]["time"]) - half_span)
        end_time = min(duration, float(items[-1]["time"]) + half_span)
        if end_time <= start_time:
            end_time = min(duration, start_time + max(0.2, sample_span))
        redactions.append(
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
                "detector_labels": labels,
                "detector_score": round(track_score, 4),
                "risk_action": track_action,
            }
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
    indices = sample_indices(frame_count, fps, sample_interval, args.auto_nsfw_sample_count)
    if args.auto_nsfw_max_frames:
        indices = indices[: max(1, args.auto_nsfw_max_frames)]
    auto_frames = extract_sample_frames(video_path, work_dir, indices, fps, subdir="auto_nsfw_frames")

    detector, detector_error = load_nudenet_detector(args.nudenet_model_path)
    pose = None
    pose_error = None
    use_pose = True
    if use_pose:
        pose, pose_error = load_mediapipe_pose()

    candidates: List[Dict[str, Any]] = []
    raw_detection_count = 0
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
                if score < args.auto_nsfw_min_score:
                    continue
                bbox = detector_box_to_normalized(
                    detection.get("box") if detection.get("box") is not None else detection.get("bbox"),
                    int(metadata["width"]),
                    int(metadata["height"]),
                )
                if not bbox:
                    continue
                risk_action, group = risk
                if group == "chest" and normalized_label in CHEST_SUGGESTIVE_LABELS and not image_region_has_obvious_cleavage(frame_path, bbox):
                    continue
                if risk_action == "BLOCK" and score < args.auto_nsfw_block_score:
                    risk_action = "REVIEW"
                bbox = expand_bbox(bbox, args.auto_nsfw_padding)
                pose_assisted = False
                if use_pose:
                    bbox, pose_assisted = adjust_bbox_with_pose(bbox, group, pose_regions)
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

    actual_sample_span = sample_interval
    if actual_sample_span is None:
        actual_sample_span = duration / max(len(auto_frames), 1) if duration else 1.0
    redactions, action, max_score = build_auto_nsfw_tracks(
        candidates,
        duration,
        actual_sample_span,
        args.auto_nsfw_track_iou,
        args.auto_nsfw_track_max_gap,
        args.auto_nsfw_smooth_alpha,
        args.auto_nsfw_min_track_hits,
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
            "candidate_count": len([item for item in candidates if item.get("source") == "nudenet"]),
            "min_score": args.auto_nsfw_min_score,
            "include_suggestive": AUTO_NSFW_INCLUDE_SUGGESTIVE,
        },
        "pose": {
            "enabled": use_pose,
            "available": pose is not None,
            "error": pose_error,
            "fallback": args.auto_nsfw_pose_fallback,
            "region_count": pose_region_count,
        },
        "candidate_count": len(candidates),
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
    return normalized_box_to_pixels(bbox_at_time(redaction, timestamp), width, height)


def pixelate_cv2_region(frame: Any, box: Tuple[int, int, int, int], blocks: int = 18) -> None:
    cv2 = load_cv2()
    x1, y1, x2, y2 = box
    h, w = frame.shape[:2]
    x1, x2 = sorted((max(0, min(w, x1)), max(0, min(w, x2))))
    y1, y2 = sorted((max(0, min(h, y1)), max(0, min(h, y2))))
    if x2 <= x1 or y2 <= y1:
        return
    roi = frame[y1:y2, x1:x2]
    small_w = max(1, min(blocks, x2 - x1))
    small_h = max(1, min(blocks, y2 - y1))
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
                    box = (0, 0, width, height)
                else:
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
    visual_masks = [
        item
        for item in redactions
        if item.get("type") in {"visual_mosaic", "visual_blur", "text_mosaic", "subtitle_replace"}
    ]
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
