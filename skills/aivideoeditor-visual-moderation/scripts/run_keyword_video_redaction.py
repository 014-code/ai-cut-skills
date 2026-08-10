#!/usr/bin/env python3
"""Apply business-keyword subtitle masks and synchronized audio mutes to a source video."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


HERE = Path(__file__).resolve().parent


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


KEYWORD_MOD = load_module(HERE / "simulate_keyword_text_audio_redaction.py", "keyword_redaction_impl")
MASK_MOD = load_module(HERE / "run_aliyun_review_mask_rereview.py", "keyword_video_mask_impl")
ASR_MOD = load_module(HERE / "run_high_accuracy_asr.py", "keyword_high_accuracy_asr")

SUBTITLE_SEARCH_BBOX = [0.04, 0.54, 0.96, 0.94]
FALLBACK_SUBTITLE_LINE_BBOX = [0.22, 0.80, 0.78, 0.91]
KEYWORD_BOX_MIN_WIDTH = 0.018
KEYWORD_BOX_MIN_HEIGHT = 0.035
SUBTITLE_TIME_PAD_SECONDS = 0.24
KEYWORD_AUDIO_PAD_SECONDS = 0.04
FULL_VIDEO_OCR_INTERVAL_SECONDS = 0.30
FULL_VIDEO_OCR_MAX_SECONDS = 6 * 60 * 60


def parse_bbox(value: str) -> List[float]:
    parts = [item.strip() for item in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be four comma-separated normalized values: x1,y1,x2,y2")
    try:
        bbox = [float(item) for item in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from exc
    x1, y1, x2, y2 = bbox
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise argparse.ArgumentTypeError("bbox must satisfy 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1")
    return bbox


def normalize_bbox(bbox: Sequence[float] | None) -> Optional[List[float]]:
    if bbox is None or len(bbox) != 4:
        return None
    x1, y1, x2, y2 = [float(value) for value in bbox]
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def expand_bbox(bbox: Sequence[float], pad_x: float, pad_y: float) -> List[float]:
    x1, y1, x2, y2 = [float(value) for value in bbox]
    expanded = normalize_bbox([x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y])
    return expanded or [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def load_cv2() -> Any:
    try:
        import cv2  # type: ignore

        return cv2
    except Exception as exc:
        raise RuntimeError("opencv-python/cv2 is required for OCR-style subtitle localization.") from exc


def load_numpy() -> Any:
    try:
        import numpy as np  # type: ignore

        return np
    except Exception as exc:
        raise RuntimeError("numpy is required for OCR-style subtitle localization.") from exc


@lru_cache(maxsize=1)
def load_rapidocr() -> Any:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception as exc:
        raise RuntimeError("rapidocr_onnxruntime is required for subtitle OCR localization.") from exc
    return RapidOCR()


def pixel_bbox_to_normalized(box: Tuple[int, int, int, int], width: int, height: int) -> Optional[List[float]]:
    x1, y1, x2, y2 = box
    return normalize_bbox([x1 / width, y1 / height, x2 / width, y2 / height])


def sample_times(start_time: float, end_time: float, max_samples: int = 4) -> List[float]:
    if end_time <= start_time:
        return [start_time]
    duration = end_time - start_time
    edge_pad = min(0.06, max(0.02, duration * 0.08))
    if duration < 0.7:
        return [round(start_time, 3), round(start_time + min(0.03, duration * 0.35), 3)]
    early = min(0.03, max(0.015, duration * 0.04))
    candidates = [
        start_time,
        start_time + early,
        start_time + duration * 0.5,
        end_time - edge_pad,
    ]
    result: List[float] = []
    for item in candidates:
        if not result or abs(item - result[-1]) >= 0.025:
            result.append(round(float(item), 3))
        if len(result) >= max_samples:
            break
    return result


def frame_at_time(capture: Any, timestamp: float, fps: float) -> Optional[Any]:
    cv2 = load_cv2()
    if fps > 0:
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(round(timestamp * fps))))
    else:
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp * 1000.0))
    ok, frame = capture.read()
    return frame if ok else None


def component_rows(components: List[Dict[str, Any]], band_height: int) -> List[List[Dict[str, Any]]]:
    rows: List[List[Dict[str, Any]]] = []
    for component in sorted(components, key=lambda item: item["cy"]):
        placed = False
        for row in rows:
            row_cy = sum(item["cy"] for item in row) / len(row)
            if abs(component["cy"] - row_cy) <= max(14.0, band_height * 0.075):
                row.append(component)
                placed = True
                break
        if not placed:
            rows.append([component])
    return rows


def ocr_box_to_normalized(box: Sequence[Sequence[float]], origin_x: int, origin_y: int, width: int, height: int) -> Optional[List[float]]:
    xs = [float(point[0]) for point in box]
    ys = [float(point[1]) for point in box]
    return normalize_bbox(
        [
            (origin_x + min(xs)) / width,
            (origin_y + min(ys)) / height,
            (origin_x + max(xs)) / width,
            (origin_y + max(ys)) / height,
        ]
    )


def text_match_score(candidate_text: str, preferred_text: str = "", preferred_keywords: Sequence[str] = ()) -> float:
    candidate_norm = normalize_keyword_token(candidate_text)
    preferred_norm = normalize_keyword_token(preferred_text)
    if not candidate_norm and not preferred_norm and not preferred_keywords:
        return 0.0

    score = 0.0
    if candidate_norm and preferred_norm:
        score += SequenceMatcher(None, candidate_norm, preferred_norm).ratio()
    if preferred_norm and candidate_norm and (candidate_norm in preferred_norm or preferred_norm in candidate_norm):
        score += 0.35
    for keyword in preferred_keywords:
        normalized_keyword = normalize_keyword_token(keyword)
        if normalized_keyword and normalized_keyword in candidate_norm:
            score += 0.22 + min(0.18, len(normalized_keyword) / 18.0)
    return score


def choose_ocr_candidate(
    candidates: List[Dict[str, Any]],
    preferred_text: str = "",
    preferred_keywords: Sequence[str] = (),
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None

    def score(item: Dict[str, Any]) -> float:
        bbox = item["line_bbox"]
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        center_y = (bbox[1] + bbox[3]) / 2.0
        return (
            float(item.get("score", 0.0)) * 2.0
            + width * 1.5
            + height * 0.5
            + center_y * 0.4
            + text_match_score(str(item.get("text") or ""), preferred_text, preferred_keywords) * 1.8
        )

    return max(candidates, key=score)


def detect_subtitle_line_bbox_opencv(frame: Any, search_bbox: Sequence[float] = SUBTITLE_SEARCH_BBOX) -> Tuple[Optional[List[float]], Dict[str, Any]]:
    cv2 = load_cv2()
    np = load_numpy()
    height, width = frame.shape[:2]
    search = normalize_bbox(search_bbox) or SUBTITLE_SEARCH_BBOX
    x1 = max(0, min(width - 1, int(round(search[0] * width))))
    y1 = max(0, min(height - 1, int(round(search[1] * height))))
    x2 = max(x1 + 1, min(width, int(round(search[2] * width))))
    y2 = max(y1 + 1, min(height, int(round(search[3] * height))))
    band = frame[y1:y2, x1:x2]
    if band.size == 0:
        return None, {"method": "subtitle_text_region", "status": "empty_search_band"}

    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    dynamic_threshold = int(np.percentile(gray, 89))
    threshold = max(168, min(218, dynamic_threshold))
    _, bright = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, close_kernel, iterations=1)

    count, _, stats, centroids = cv2.connectedComponentsWithStats(bright, 8)
    band_height, band_width = bright.shape[:2]
    components: List[Dict[str, Any]] = []
    for index in range(1, count):
        comp_x, comp_y, comp_w, comp_h, comp_area = [int(value) for value in stats[index]]
        if comp_area < 45 or comp_w < 7 or comp_h < 9:
            continue
        if comp_h > band_height * 0.26 or comp_w > band_width * 0.42:
            continue
        comp_cx, comp_cy = [float(value) for value in centroids[index]]
        if comp_cy < band_height * 0.32:
            continue
        components.append(
            {
                "x": comp_x,
                "y": comp_y,
                "w": comp_w,
                "h": comp_h,
                "area": comp_area,
                "cx": comp_cx,
                "cy": comp_cy,
            }
        )

    best: Optional[Tuple[float, Tuple[int, int, int, int], Dict[str, Any]]] = None
    for row in component_rows(components, band_height):
        if len(row) < 3:
            continue
        row_x1 = min(item["x"] for item in row)
        row_y1 = min(item["y"] for item in row)
        row_x2 = max(item["x"] + item["w"] for item in row)
        row_y2 = max(item["y"] + item["h"] for item in row)
        row_w = row_x2 - row_x1
        row_h = row_y2 - row_y1
        row_center_y = (row_y1 + row_y2) / 2.0
        row_span = row_w / max(1.0, float(band_width))
        area_ratio = sum(item["area"] for item in row) / max(1.0, float(band_width * band_height))
        if row_span < 0.09 or row_h < 12 or row_h > band_height * 0.22:
            continue
        score = len(row) * 0.9 + row_span * 7.0 + area_ratio * 28.0 + (row_center_y / band_height) * 1.2
        meta = {
            "component_count": len(row),
            "row_span": round(float(row_span), 4),
            "row_height": int(row_h),
            "threshold": threshold,
            "search_bbox": list(search),
        }
        if best is None or score > best[0]:
            best = (score, (row_x1, row_y1, row_x2, row_y2), meta)

    if best is None:
        return None, {
            "method": "subtitle_text_region",
            "status": "not_found",
            "threshold": threshold,
            "component_count": len(components),
            "search_bbox": list(search),
        }

    _, (row_x1, row_y1, row_x2, row_y2), meta = best
    pad_x = max(5, int((row_x2 - row_x1) * 0.012))
    pad_y = max(4, int((row_y2 - row_y1) * 0.16))
    normalized = pixel_bbox_to_normalized(
        (
            x1 + row_x1 - pad_x,
            y1 + row_y1 - pad_y,
            x1 + row_x2 + pad_x,
            y1 + row_y2 + pad_y,
        ),
        width,
        height,
    )
    if not normalized:
        return None, {"method": "subtitle_text_region", "status": "invalid_bbox", **meta}
    return normalized, {"method": "subtitle_text_region", "status": "localized", **meta, "line_bbox": normalized}


def detect_subtitle_observation(
    frame: Any,
    search_bbox: Sequence[float] = SUBTITLE_SEARCH_BBOX,
    *,
    preferred_text: str = "",
    preferred_keywords: Sequence[str] = (),
) -> Dict[str, Any]:
    cv2 = load_cv2()
    height, width = frame.shape[:2]
    search = normalize_bbox(search_bbox) or SUBTITLE_SEARCH_BBOX
    x1 = max(0, min(width - 1, int(round(search[0] * width))))
    y1 = max(0, min(height - 1, int(round(search[1] * height))))
    x2 = max(x1 + 1, min(width, int(round(search[2] * width))))
    y2 = max(y1 + 1, min(height, int(round(search[3] * height))))
    band = frame[y1:y2, x1:x2]
    if band.size == 0:
        return {"method": "rapidocr", "status": "empty_search_band", "search_bbox": list(search)}

    candidates: List[Dict[str, Any]] = []
    try:
        ocr = load_rapidocr()
        result = ocr(band)
        ocr_items = result[0] if isinstance(result, tuple) and result else result
        for item in ocr_items or []:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            box, text, score = item[:3]
            text = str(text or "").strip()
            if not text:
                continue
            bbox = ocr_box_to_normalized(box, x1, y1, width, height)
            if not bbox:
                continue
            box_height = bbox[3] - bbox[1]
            box_width = bbox[2] - bbox[0]
            center_y = (bbox[1] + bbox[3]) / 2.0
            if box_height < 0.018 or box_width < 0.08:
                continue
            if center_y < search[1] + (search[3] - search[1]) * 0.25:
                continue
            candidates.append(
                {
                    "method": "rapidocr",
                    "status": "localized",
                    "line_bbox": bbox,
                    "text": text,
                    "score": round(float(score or 0.0), 4),
                    "search_bbox": list(search),
                }
            )
    except Exception as exc:
        return {
            "method": "rapidocr",
            "status": "error",
            "error": str(exc),
            "search_bbox": list(search),
        }

    chosen = choose_ocr_candidate(candidates, preferred_text, preferred_keywords)
    if chosen:
        return chosen

    fallback_bbox, fallback_meta = detect_subtitle_line_bbox_opencv(frame, search_bbox)
    if fallback_bbox:
        return {
            "method": fallback_meta.get("method", "subtitle_text_region"),
            "status": fallback_meta.get("status", "localized"),
            "line_bbox": fallback_bbox,
            "text": "",
            "score": 0.0,
            "search_bbox": list(search),
            "fallback": True,
        }
    return {
        "method": fallback_meta.get("method", "subtitle_text_region"),
        "status": "not_found",
        "text": "",
        "score": 0.0,
        "search_bbox": list(search),
        "fallback": True,
    }


def detect_subtitle_line_bbox(frame: Any, search_bbox: Sequence[float] = SUBTITLE_SEARCH_BBOX) -> Tuple[Optional[List[float]], Dict[str, Any]]:
    observation = detect_subtitle_observation(frame, search_bbox)
    return observation.get("line_bbox"), observation


def normalize_keyword_token(value: str) -> str:
    return re.sub(r"[\s:：,，;；、.。!！?？\"'“”‘’`·_\-—()（）\[\]【】]+", "", str(value or "")).casefold()


def normalized_text_with_mapping(value: str) -> Tuple[str, List[int]]:
    normalized_chars: List[str] = []
    mapping: List[int] = []
    for index, char in enumerate(str(value or "")):
        normalized_char = normalize_keyword_token(char)
        if not normalized_char:
            continue
        for item in normalized_char:
            normalized_chars.append(item)
            mapping.append(index)
    return "".join(normalized_chars), mapping


def find_keyword_span(
    text: str,
    keyword: str,
    preferred_start: Optional[int] = None,
    preferred_end: Optional[int] = None,
    preferred_text: str = "",
) -> Tuple[int, int, bool]:
    text = str(text or "")
    keyword = str(keyword or "")
    if not text:
        return 0, 1, False
    normalized_text, mapping = normalized_text_with_mapping(text)
    normalized_keyword = normalize_keyword_token(keyword)
    matches = list(re.finditer(re.escape(normalized_keyword), normalized_text)) if normalized_keyword and mapping else []
    if matches:
        if preferred_start is None:
            chosen = matches[0]
        else:
            preferred_denominator = max(1, len(preferred_text or text))
            target_ratio = preferred_start / preferred_denominator
            text_denominator = max(1, len(text))
            chosen = min(matches, key=lambda match: abs((match.start() / text_denominator) - target_ratio))
        start = mapping[chosen.start()]
        end = mapping[chosen.end() - 1] + 1
        return int(start), int(end), True

    preferred_denominator = max(1, len(preferred_text or text))
    if preferred_start is None:
        start = 0
    else:
        start = int(round((preferred_start / preferred_denominator) * len(text)))
    if preferred_start is not None and preferred_end is not None:
        length = max(1, int(round(((preferred_end - preferred_start) / preferred_denominator) * len(text))))
    else:
        length = max(1, min(len(keyword), len(text)))
    start = max(0, min(len(text) - 1, start))
    end = min(len(text), start + length)
    if end <= start:
        end = min(len(text), start + 1)
    return start, end, False


def keyword_bbox_from_line(line_bbox: Sequence[float], text: str, char_start: int, char_end: int) -> List[float]:
    bbox = normalize_bbox(line_bbox) or FALLBACK_SUBTITLE_LINE_BBOX
    char_count = max(1, len(text))
    char_start = max(0, min(char_count, int(char_start)))
    char_end = max(char_start + 1, min(char_count, int(char_end)))
    x1, y1, x2, y2 = bbox
    line_width = x2 - x1
    line_height = y2 - y1
    per_char = line_width / char_count
    keyword_x1 = x1 + per_char * char_start
    keyword_x2 = x1 + per_char * char_end
    pad_x = max(per_char * 0.32, 0.006)
    pad_y = max(line_height * 0.16, 0.005)
    if keyword_x2 - keyword_x1 < KEYWORD_BOX_MIN_WIDTH:
        center_x = (keyword_x1 + keyword_x2) / 2.0
        keyword_x1 = center_x - KEYWORD_BOX_MIN_WIDTH / 2.0
        keyword_x2 = center_x + KEYWORD_BOX_MIN_WIDTH / 2.0
    if line_height < KEYWORD_BOX_MIN_HEIGHT:
        center_y = (y1 + y2) / 2.0
        y1 = center_y - KEYWORD_BOX_MIN_HEIGHT / 2.0
        y2 = center_y + KEYWORD_BOX_MIN_HEIGHT / 2.0
    return expand_bbox([keyword_x1, y1, keyword_x2, y2], pad_x, pad_y)


def collect_segment_observations(
    start_time: float,
    end_time: float,
    capture: Any,
    fps: float,
    fallback_line_bbox: Sequence[float],
    previous_line_bbox: Optional[Sequence[float]] = None,
    *,
    preferred_text: str = "",
    preferred_keywords: Sequence[str] = (),
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    times = sample_times(start_time, end_time)
    observations: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    fallback_count = 0
    localized_count = 0
    last_line_bbox = normalize_bbox(previous_line_bbox) or normalize_bbox(fallback_line_bbox)
    for timestamp in times:
        frame = frame_at_time(capture, timestamp, fps)
        if frame is not None:
            observation = detect_subtitle_observation(
                frame,
                preferred_text=preferred_text,
                preferred_keywords=preferred_keywords,
            )
        else:
            observation = {"method": "rapidocr", "status": "frame_read_failed", "search_bbox": list(SUBTITLE_SEARCH_BBOX)}
        line_bbox = normalize_bbox(observation.get("line_bbox")) or last_line_bbox or normalize_bbox(fallback_line_bbox)
        if line_bbox is None:
            line_bbox = normalize_bbox(fallback_line_bbox)
        if line_bbox is None:
            line_bbox = list(FALLBACK_SUBTITLE_LINE_BBOX)
        if observation.get("status") == "localized" and observation.get("line_bbox"):
            last_line_bbox = list(observation["line_bbox"])
            localized_count += 1
        else:
            fallback_count += 1
            observation["fallback_line_bbox"] = list(line_bbox)
        observation["time"] = round(float(timestamp), 3)
        observation["line_bbox"] = line_bbox
        observations.append(observation)
        diagnostics.append(
            {
                "time": round(float(timestamp), 3),
                "status": observation.get("status"),
                "method": observation.get("method"),
                "text": observation.get("text", ""),
                "line_bbox": line_bbox,
                "fallback": observation.get("status") != "localized",
            }
        )
    return observations, {
        "samples": diagnostics,
        "fallback_count": fallback_count,
        "localized_count": localized_count,
    }


def video_duration_seconds(capture: Any, fps: float) -> float:
    frame_count = float(capture.get(load_cv2().CAP_PROP_FRAME_COUNT) or 0.0)
    if fps > 0 and frame_count > 0:
        return frame_count / fps
    duration_ms = float(capture.get(load_cv2().CAP_PROP_POS_MSEC) or 0.0)
    return max(0.0, duration_ms / 1000.0)


def scan_full_video_ocr(
    capture: Any,
    fps: float,
    duration: float,
    *,
    interval_seconds: float = FULL_VIDEO_OCR_INTERVAL_SECONDS,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Scan every part of the video so ASR omissions cannot suppress OCR hits."""
    if duration <= 0:
        return [], {"enabled": True, "scan_interval_seconds": interval_seconds, "scan_frame_count": 0}
    if duration > FULL_VIDEO_OCR_MAX_SECONDS:
        raise RuntimeError(
            f"Full-video subtitle OCR is limited to {FULL_VIDEO_OCR_MAX_SECONDS / 3600:.0f} hours; "
            f"got {duration:.1f} seconds."
        )

    observations: List[Dict[str, Any]] = []
    localized_count = 0
    fallback_count = 0
    frame_count = max(1, int(duration / max(0.08, interval_seconds)) + 1)
    scanned_frame_count = 0
    for index in range(frame_count):
        timestamp = min(duration - 0.01, index * interval_seconds)
        if timestamp < 0:
            break
        frame = frame_at_time(capture, timestamp, fps)
        if frame is None:
            continue
        scanned_frame_count += 1
        observation = detect_subtitle_observation(frame)
        observation["time"] = round(float(timestamp), 3)
        observation["line_bbox"] = normalize_bbox(observation.get("line_bbox"))
        observations.append(observation)
        if observation.get("status") == "localized" and observation.get("line_bbox"):
            localized_count += 1
        else:
            fallback_count += 1

    return observations, {
        "enabled": True,
        "method": "full_video_fixed_interval_ocr",
        "ocr_backend": "rapidocr_onnxruntime",
        "scan_interval_seconds": interval_seconds,
        "scan_duration_seconds": round(duration, 3),
        "scan_frame_count": scanned_frame_count,
        "localized_sample_count": localized_count,
        "fallback_sample_count": fallback_count,
    }


def full_ocr_hit_clusters(
    observations: Sequence[Dict[str, Any]],
    *,
    interval_seconds: float = FULL_VIDEO_OCR_INTERVAL_SECONDS,
) -> List[Dict[str, Any]]:
    """Group adjacent OCR detections of the same keyword into continuous windows."""
    detections: List[Dict[str, Any]] = []
    for observation in observations:
        text = str(observation.get("text") or "")
        if not text:
            continue
        for hit in KEYWORD_MOD.find_hits(text):
            detections.append(
                {
                    "time": float(observation.get("time", 0.0)),
                    "keyword": hit.keyword,
                    "category": hit.category,
                    "actions": list(hit.actions),
                    "char_start": hit.start,
                    "char_end": hit.end,
                    "source_text": text,
                    "observation": observation,
                }
            )
    detections.sort(key=lambda item: (item["keyword"], item["category"], item["time"]))
    clusters: List[Dict[str, Any]] = []
    max_gap = max(interval_seconds * 2.25, interval_seconds + 0.18)
    for detection in detections:
        previous = clusters[-1] if clusters else None
        same_key = previous and previous["keyword"] == detection["keyword"] and previous["category"] == detection["category"]
        close_enough = previous and detection["time"] - previous["last_time"] <= max_gap
        same_line = previous and text_match_score(
            str(previous["observations"][-1].get("text") or ""),
            str(detection["source_text"] or ""),
        ) >= 0.35
        if same_key and close_enough and same_line:
            previous["last_time"] = detection["time"]
            previous["observations"].append(detection["observation"])
            previous["char_start"] = detection["char_start"]
            previous["char_end"] = detection["char_end"]
            previous["source_text"] = detection["source_text"]
            continue
        clusters.append(
            {
                "keyword": detection["keyword"],
                "category": detection["category"],
                "actions": detection["actions"],
                "char_start": detection["char_start"],
                "char_end": detection["char_end"],
                "source_text": detection["source_text"],
                "first_time": detection["time"],
                "last_time": detection["time"],
                "observations": [detection["observation"]],
            }
        )

    for cluster in clusters:
        cluster["start_time"] = round(max(0.0, cluster["first_time"] - interval_seconds * 1.05), 3)
        cluster["end_time"] = round(cluster["last_time"] + interval_seconds * 1.05, 3)
        cluster["timing_source"] = "full_video_ocr_frames"
    return clusters


def select_anchor_observation(
    observations: Sequence[Dict[str, Any]],
    target_time: float,
    keyword: str,
    preferred_text: str,
) -> Optional[Dict[str, Any]]:
    localized = [
        observation
        for observation in observations
        if observation.get("status") == "localized" and observation.get("line_bbox")
    ]
    if not localized:
        return None

    keyword_norm = normalize_keyword_token(keyword)
    preferred_norm = normalize_keyword_token(preferred_text)
    time_span = max(
        0.5,
        max((float(observation.get("time", target_time)) for observation in localized), default=target_time)
        - min((float(observation.get("time", target_time)) for observation in localized), default=target_time),
    )

    def score(observation: Dict[str, Any]) -> float:
        bbox = normalize_bbox(observation.get("line_bbox"))
        if not bbox:
            return -1.0
        observation_time = float(observation.get("time", target_time))
        time_score = 1.0 - min(1.0, abs(observation_time - target_time) / time_span)
        observation_text = str(observation.get("text") or "")
        text_score = text_match_score(observation_text, preferred_text, (keyword,))
        if keyword_norm and keyword_norm in normalize_keyword_token(observation_text):
            text_score += 0.35
        if preferred_norm and preferred_norm in normalize_keyword_token(observation_text):
            text_score += 0.15
        ocr_score = float(observation.get("score") or 0.0)
        bbox_span = (bbox[2] - bbox[0]) * 0.7 + (bbox[3] - bbox[1]) * 0.35
        return text_score * 2.2 + time_score * 1.3 + ocr_score * 1.4 + bbox_span

    return max(localized, key=score)


def locate_subtitle_hit_keyframes(
    start_time: float,
    end_time: float,
    hit: Dict[str, Any],
    observations: List[Dict[str, Any]],
    fallback_line_bbox: Sequence[float],
    previous_line_bbox: Optional[Sequence[float]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    keyframes: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    fallback_count = 0
    last_line_bbox = normalize_bbox(previous_line_bbox) or normalize_bbox(fallback_line_bbox)
    preferred_start = int(hit.get("char_start", 0))
    preferred_end = int(hit.get("char_end", preferred_start + 1))
    preferred_text = str(hit.get("source_text") or "")
    keyword = str(hit.get("keyword") or "")
    target_time = (start_time + end_time) / 2.0
    keyword_norm = normalize_keyword_token(keyword)
    localized_observations = [
        observation
        for observation in observations
        if observation.get("status") == "localized" and observation.get("line_bbox")
    ]
    matching_observations = [
        observation
        for observation in localized_observations
        if keyword_norm and keyword_norm in normalize_keyword_token(str(observation.get("text") or ""))
        or (
            preferred_text
            and text_match_score(str(observation.get("text") or ""), preferred_text, (keyword,)) >= 0.60
        )
    ]
    active_observations = matching_observations or localized_observations or list(observations)
    active_start_time = None
    active_end_time = None
    if matching_observations or localized_observations:
        active_start_time = min(float(item.get("time", start_time)) for item in active_observations)
        active_end_time = max(float(item.get("time", end_time)) for item in active_observations)
    anchor_observation = select_anchor_observation(observations, target_time, keyword, preferred_text)
    anchor_line_bbox = normalize_bbox(anchor_observation.get("line_bbox")) if anchor_observation else None
    for observation in active_observations:
        timestamp = float(observation.get("time", start_time))
        line_bbox = anchor_line_bbox or normalize_bbox(observation.get("line_bbox")) or last_line_bbox or normalize_bbox(fallback_line_bbox)
        if line_bbox is None:
            line_bbox = list(FALLBACK_SUBTITLE_LINE_BBOX)
        if anchor_line_bbox:
            last_line_bbox = list(anchor_line_bbox)
        elif observation.get("status") == "localized" and observation.get("line_bbox"):
            last_line_bbox = list(line_bbox)
        else:
            fallback_count += 1
        sample_text = str(observation.get("text") or "")
        start, end, matched = find_keyword_span(sample_text, keyword, preferred_start, preferred_end, preferred_text)
        chosen_text = sample_text if matched else preferred_text or sample_text
        if not chosen_text:
            chosen_text = preferred_text or keyword
        if not matched and preferred_text:
            start, end, _ = find_keyword_span(preferred_text, keyword, preferred_start, preferred_end, preferred_text)
        if end <= start:
            end = min(len(chosen_text), start + max(1, len(keyword)))
        keyword_bbox = keyword_bbox_from_line(line_bbox, chosen_text, start, end)
        keyframes.append({"time": round(float(timestamp), 3), "bbox": keyword_bbox})
        diagnostics.append(
            {
                "time": round(float(timestamp), 3),
                "keyword": keyword,
                "status": observation.get("status"),
                "method": observation.get("method"),
                "line_bbox": line_bbox,
                "anchor_line_bbox": anchor_line_bbox,
                "keyword_bbox": keyword_bbox,
                "text": sample_text,
                "source_text": preferred_text,
                "matched_from_observation": matched,
                "fallback": observation.get("status") != "localized",
            }
        )
    if not keyframes:
        line_bbox = list(fallback_line_bbox)
        keyframes.append(
            {
                "time": round(float(start_time), 3),
                "bbox": keyword_bbox_from_line(line_bbox, preferred_text or keyword, preferred_start, preferred_end),
            }
        )
        fallback_count += 1
    return keyframes, {
        "samples": diagnostics,
        "fallback_count": fallback_count,
        "localized_count": len(diagnostics) - fallback_count,
        "active_start_time": round(active_start_time, 3) if active_start_time is not None else None,
        "active_end_time": round(active_end_time, 3) if active_end_time is not None else None,
        "active_observation_count": len(active_observations),
        "active_observation_mode": "keyword_match" if matching_observations else ("localized_line" if localized_observations else "fallback_segment"),
    }


def build_synchronized_audio_mutes(
    subtitle_redactions: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Mute only the audio span corresponding to each masked keyword character range."""
    candidates: List[Dict[str, Any]] = []
    reason = "rendered subtitle hit requires synchronized audio mute from business policy"
    for item in subtitle_redactions:
        start_time, end_time, timing = keyword_audio_window(item)
        candidates.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "keywords": sorted(item.get("keywords") or []),
                "reason": reason,
                "timing": timing,
            }
        )
    return MASK_MOD.merge_audio_mutes(candidates)


def keyword_audio_window(item: Dict[str, Any]) -> Tuple[float, float, str]:
    """Map the masked characters into a proportional subtitle/audio time span."""
    segment_start = float(item.get("start_time", 0.0) or 0.0)
    segment_end = float(item.get("end_time", segment_start) or segment_start)
    if segment_end < segment_start:
        segment_start, segment_end = segment_end, segment_start
    source_text = str(item.get("source_text") or "")
    char_start = item.get("char_start")
    char_end = item.get("char_end")
    try:
        char_start = int(char_start)
        char_end = int(char_end)
    except (TypeError, ValueError):
        char_start = -1
        char_end = -1
    if source_text and 0 <= char_start < char_end <= len(source_text) and segment_end > segment_start + 0.001:
        duration = segment_end - segment_start
        start = segment_start + duration * (char_start / len(source_text))
        end = segment_start + duration * (char_end / len(source_text))
        start = max(segment_start, start - KEYWORD_AUDIO_PAD_SECONDS)
        end = min(segment_end, max(start + 0.04, end + KEYWORD_AUDIO_PAD_SECONDS))
        return round(start, 3), round(end, 3), "proportional_keyword_character_span"
    return round(segment_start, 3), round(segment_end, 3), "subtitle_window_fallback_missing_character_span"


def subtitle_audio_sync_check(redactions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    subtitles = [item for item in redactions if item.get("type") == "subtitle_replace"]
    audio_mutes = [item for item in redactions if item.get("type") == "audio_mute"]
    uncovered: List[Dict[str, Any]] = []
    for subtitle in subtitles:
        start_time, end_time, timing = keyword_audio_window(subtitle)
        covered = any(
            float(audio.get("start_time", 0.0) or 0.0) <= start_time + 0.001
            and float(audio.get("end_time", 0.0) or 0.0) >= end_time - 0.001
            for audio in audio_mutes
        )
        if not covered:
            uncovered.append(
                {
                    "start_time": round(start_time, 3),
                    "end_time": round(end_time, 3),
                    "keywords": sorted(subtitle.get("keywords") or []),
                    "timing": timing,
                }
            )
    return {
        "required": True,
        "passed": not uncovered,
        "subtitle_redaction_count": len(subtitles),
        "audio_mute_count": len(audio_mutes),
        "uncovered_subtitle_redactions": uncovered,
    }


def build_redactions(video: Path, plan: Dict[str, Any], fallback_subtitle_bbox: Sequence[float]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    redactions: List[Dict[str, Any]] = []
    cv2 = load_cv2()
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError(f"Failed to open video for subtitle OCR localization: {video}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 25.0)
    duration = video_duration_seconds(capture, fps)
    full_ocr_observations, full_ocr_report = scan_full_video_ocr(capture, fps, duration)
    ocr_hit_clusters = full_ocr_hit_clusters(full_ocr_observations)
    localization_items: List[Dict[str, Any]] = []
    localized_count = 0
    fallback_count = 0
    supplemental_ocr_hits: List[Dict[str, Any]] = []
    last_line_bbox: Optional[List[float]] = None
    for segment in plan.get("segments") or []:
        start_time = float(segment.get("start_time", 0.0))
        end_time = float(segment.get("end_time", start_time + 1.5))
        segment_text = str(segment.get("text") or "")
        planned_hits = list(segment.get("hits") or [])
        preferred_keywords = [str(hit.get("keyword") or "") for hit in planned_hits if str(hit.get("keyword") or "").strip()]
        if not preferred_keywords:
            preferred_keywords = [hit.keyword for hit in KEYWORD_MOD.find_hits(segment_text)]
        observations = [
            dict(observation)
            for observation in full_ocr_observations
            if start_time - 0.04 <= float(observation.get("time", 0.0)) <= end_time + 0.04
        ]
        if observations:
            observation_report = {
                "localized_count": len(
                    [item for item in observations if item.get("status") == "localized" and item.get("line_bbox")]
                ),
                "fallback_count": len(
                    [item for item in observations if item.get("status") != "localized" or not item.get("line_bbox")]
                ),
            }
        else:
            observations, observation_report = collect_segment_observations(
                start_time,
                end_time,
                capture,
                fps,
                fallback_subtitle_bbox,
                last_line_bbox,
                preferred_text=segment_text,
                preferred_keywords=preferred_keywords,
            )
        localized_count += int(observation_report.get("localized_count") or 0)
        fallback_count += int(observation_report.get("fallback_count") or 0)
        localized_observations = [
            observation
            for observation in observations
            if observation.get("status") == "localized" and observation.get("line_bbox")
        ]
        if localized_observations:
            last_line_bbox = list(localized_observations[-1]["line_bbox"])

        hit_specs: List[Dict[str, Any]] = []
        seen_specs = set()
        def add_hit_spec(
            keyword: str,
            category: str,
            actions: Sequence[str],
            char_start: int,
            char_end: int,
            source_text: str,
            source_kind: str,
            source_start_time: Optional[float] = None,
            source_end_time: Optional[float] = None,
            timing_source: str = "segment_span",
            word_start_index: Optional[int] = None,
            word_end_index: Optional[int] = None,
        ) -> None:
            key = (
                normalize_keyword_token(keyword),
                str(category),
                int(char_start),
                int(char_end),
            )
            if key in seen_specs:
                return
            seen_specs.add(key)
            hit_specs.append(
                {
                    "keyword": str(keyword),
                    "category": str(category),
                    "actions": list(actions),
                    "char_start": int(char_start),
                    "char_end": int(char_end),
                    "source_text": source_text,
                    "source_kind": source_kind,
                    "start_time": round(float(source_start_time if source_start_time is not None else start_time), 3),
                    "end_time": round(float(source_end_time if source_end_time is not None else end_time), 3),
                    "timing_source": timing_source,
                    "word_start_index": word_start_index,
                    "word_end_index": word_end_index,
                }
            )

        if planned_hits:
            for hit in planned_hits:
                add_hit_spec(
                    str(hit.get("keyword") or ""),
                    str(hit.get("category") or "keyword"),
                    list(hit.get("actions") or []),
                    int(hit.get("char_start", 0)),
                    int(hit.get("char_end", 0)),
                    segment_text,
                    "transcript",
                    float(hit.get("start_time", start_time)),
                    float(hit.get("end_time", end_time)),
                    str(hit.get("timing_source") or "segment_span"),
                    hit.get("word_start_index"),
                    hit.get("word_end_index"),
                )
        else:
            for hit in KEYWORD_MOD.find_hits(segment_text):
                add_hit_spec(hit.keyword, hit.category, hit.actions, hit.start, hit.end, segment_text, "transcript")

        if not hit_specs:
            continue

        for hit in hit_specs:
            keyframes, localization = locate_subtitle_hit_keyframes(
                start_time,
                end_time,
                hit,
                observations,
                fallback_subtitle_bbox,
                last_line_bbox,
            )
            localization_items.append(
                {
                    "start_time": round(float(hit.get("start_time", start_time)), 3),
                    "end_time": round(float(hit.get("end_time", end_time)), 3),
                    "keyword": hit.get("keyword"),
                    "char_start": hit.get("char_start"),
                    "char_end": hit.get("char_end"),
                    "source_kind": hit.get("source_kind"),
                    "timing_source": hit.get("timing_source"),
                    **localization,
                }
            )
            localized_samples = [
                sample
                for sample in localization.get("samples") or []
                if sample.get("status") == "localized" and sample.get("line_bbox")
            ]
            timing_source = str(hit.get("timing_source") or "segment_span")
            active_start = localization.get("active_start_time")
            active_end = localization.get("active_end_time")
            if timing_source == "word_timestamps":
                mask_start_time = max(0.0, float(hit.get("start_time", start_time)) - 0.12)
                mask_end_time = float(hit.get("end_time", end_time)) + 0.08
            elif active_start is not None and active_end is not None:
                mask_start_time = max(0.0, float(active_start) - 0.06)
                mask_end_time = min(duration, float(active_end) + 0.10)
            else:
                mask_start_time = max(0.0, float(hit.get("start_time", start_time)) - SUBTITLE_TIME_PAD_SECONDS)
                mask_end_time = float(hit.get("end_time", end_time)) + 0.04
            redactions.append(
                {
                    "type": "subtitle_replace",
                    "category": str(hit.get("category") or "keyword"),
                    "start_time": round(mask_start_time, 3),
                    "end_time": round(mask_end_time, 3),
                    "bbox_keyframes": keyframes,
                    "bbox_interpolation": "step",
                    "replacement": "[已处理]",
                    "reason": "keyword subtitle masking from business policy; localized to OCR subtitle text and hit character span; subtitle masks use step keyframes with a fixed anchor bbox so they appear immediately instead of sliding",
                    "keywords": [str(hit.get("keyword") or "")],
                    "char_start": hit.get("char_start"),
                    "char_end": hit.get("char_end"),
                    "source": "keyword_policy_ocr" if hit.get("source_kind") == "ocr_subtitle" else "keyword_policy",
                    "source_kind": hit.get("source_kind"),
                    "source_text": hit.get("source_text"),
                    "timing_source": timing_source,
                    "localization": {
                        "method": "rapidocr_subtitle_region",
                        "fallback_used": bool(localization.get("fallback_count")),
                        "line_bbox": localized_samples[-1]["line_bbox"] if localized_samples else (localization.get("samples") or [{}])[-1].get("line_bbox"),
                        "anchor_line_bbox": (localization.get("samples") or [{}])[-1].get("anchor_line_bbox"),
                    },
                }
            )

    for cluster in ocr_hit_clusters:
        hit = {
            "keyword": cluster["keyword"],
            "category": cluster["category"],
            "actions": cluster["actions"],
            "char_start": cluster["char_start"],
            "char_end": cluster["char_end"],
            "source_text": cluster["source_text"],
            "source_kind": "ocr_subtitle",
            "start_time": cluster["start_time"],
            "end_time": cluster["end_time"],
            "timing_source": cluster["timing_source"],
        }
        keyframes, localization = locate_subtitle_hit_keyframes(
            float(cluster["start_time"]),
            float(cluster["end_time"]),
            hit,
            list(cluster["observations"]),
            fallback_subtitle_bbox,
            last_line_bbox,
        )
        active_start = localization.get("active_start_time")
        active_end = localization.get("active_end_time")
        start_time = round(
            max(0.0, float(active_start) - 0.06) if active_start is not None else max(0.0, float(cluster["start_time"])),
            3,
        )
        end_time = round(
            min(duration, float(active_end) + 0.10) if active_end is not None else min(duration, float(cluster["end_time"])),
            3,
        )
        existing = next(
            (
                item
                for item in redactions
                if item.get("type") == "subtitle_replace"
                and normalize_keyword_token(str((item.get("keywords") or [""])[0]))
                == normalize_keyword_token(str(cluster["keyword"]))
                and float(item.get("start_time", 0.0)) <= end_time + FULL_VIDEO_OCR_INTERVAL_SECONDS
                and float(item.get("end_time", 0.0)) >= start_time - FULL_VIDEO_OCR_INTERVAL_SECONDS
            ),
            None,
        )
        if existing:
            existing["start_time"] = round(min(float(existing["start_time"]), start_time), 3)
            existing["end_time"] = round(max(float(existing["end_time"]), end_time), 3)
            merged_keyframes = list(existing.get("bbox_keyframes") or []) + keyframes
            deduped_keyframes: Dict[float, Dict[str, Any]] = {}
            for keyframe in sorted(merged_keyframes, key=lambda item: float(item.get("time", 0.0))):
                deduped_keyframes[round(float(keyframe.get("time", 0.0)), 3)] = keyframe
            existing["bbox_keyframes"] = list(deduped_keyframes.values())
            existing["source"] = "keyword_policy_transcript_plus_full_video_ocr"
            existing["source_kind"] = "transcript+ocr_subtitle"
        else:
            localized_samples = [
                sample
                for sample in localization.get("samples") or []
                if sample.get("status") == "localized" and sample.get("line_bbox")
            ]
            redactions.append(
                {
                    "type": "subtitle_replace",
                    "category": str(cluster["category"]),
                    "start_time": start_time,
                    "end_time": end_time,
                    "bbox_keyframes": keyframes,
                    "bbox_interpolation": "step",
                    "replacement": "[已处理]",
                    "reason": "full-video OCR keyword hit; localized to the detected subtitle line and hit character span",
                    "keywords": [str(cluster["keyword"])],
                    "char_start": cluster["char_start"],
                    "char_end": cluster["char_end"],
                    "source": "keyword_policy_full_video_ocr",
                    "source_kind": "ocr_subtitle",
                    "source_text": cluster["source_text"],
                    "timing_source": cluster["timing_source"],
                    "localization": {
                        "method": "rapidocr_full_video_scan",
                        "fallback_used": bool(localization.get("fallback_count")),
                        "line_bbox": localized_samples[-1]["line_bbox"] if localized_samples else None,
                    },
                }
            )
        localization_items.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "keyword": cluster["keyword"],
                "char_start": cluster["char_start"],
                "char_end": cluster["char_end"],
                "source_kind": "ocr_subtitle",
                "timing_source": cluster["timing_source"],
                **localization,
            }
        )
        supplemental_ocr_hits.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "times": [item.get("time") for item in cluster["observations"]],
                "keyword": cluster["keyword"],
                "category": cluster["category"],
                "ocr_text": cluster["source_text"],
            }
        )
    capture.release()
    subtitle_redactions = [item for item in redactions if item.get("type") == "subtitle_replace"]
    for item in build_synchronized_audio_mutes(subtitle_redactions):
        redactions.append(
            {
                "type": "audio_mute",
                "category": "subtitle_keyword",
                "start_time": item["start_time"],
                "end_time": item["end_time"],
                "reason": item.get("reason") or "rendered subtitle hit requires synchronized audio mute from business policy",
                "keywords": sorted(item.get("keywords") or []),
                "source": "keyword_policy",
            }
        )
    return redactions, {
        "method": "ocr_subtitle_region",
        "ocr_backend": "rapidocr_onnxruntime",
        "search_bbox_normalized": list(SUBTITLE_SEARCH_BBOX),
        "fallback_subtitle_line_bbox_normalized": list(fallback_subtitle_bbox),
        "localized_sample_count": localized_count,
        "fallback_sample_count": fallback_count,
        "supplemental_ocr_hit_count": len(supplemental_ocr_hits),
        "supplemental_ocr_hits": supplemental_ocr_hits,
        "full_video_ocr_scan": full_ocr_report,
        "items": localization_items,
    }


def write_human_report(path: Path, report: Dict[str, Any]) -> None:
    plan = report["plan"]
    lines = [
        "字幕打码与消音处理说明",
        "",
        f"源视频: {report['source']}",
        f"输出视频: {report['output']}",
        f"字幕打码数量: {report['subtitle_redaction_count']}",
        f"音频消音数量: {report['audio_mute_count']}",
        "- 任一字幕打码范围都会被对应音频消音覆盖；OCR 补充打码会同步扩展消音范围。",
    ]
    sync_check = report.get("subtitle_audio_sync") or {}
    lines.append(f"字幕与音频同步校验: {'通过' if sync_check.get('passed') else '未通过'}")
    render_pad = float((report.get("processed") or {}).get("audio_mute_render_pad_seconds") or 0.0)
    if render_pad > 0:
        lines.append(f"音频消音渲染边距: {render_pad:.2f} 秒")
    transcript_generated = report.get("transcript_generated") or {}
    if transcript_generated.get("enabled"):
        lines.append(f"音频转写方式: {transcript_generated.get('engine')}")
        lines.append(f"转写字幕文件: {transcript_generated.get('output') or report.get('transcript')}")
        lines.append(f"词/字级时间戳可用: {bool(transcript_generated.get('word_timestamps_available'))}")
    else:
        lines.append(f"转写字幕文件: {report.get('transcript')}")
    lines.extend(
        [
            f"字幕定位方式: {report['subtitle_localization']['method']}",
            f"OCR/文本区域定位成功样本: {report['subtitle_localization']['localized_sample_count']}",
            f"兜底字幕框样本: {report['subtitle_localization']['fallback_sample_count']}",
            f"OCR补充命中数量: {len(report.get('ocr_supplemental_hits') or [])}",
            "",
            "命中明细:",
        ]
    )
    full_scan = report["subtitle_localization"].get("full_video_ocr_scan") or {}
    if full_scan.get("enabled"):
        lines.insert(
            8,
            f"全片OCR扫描: 每 {float(full_scan.get('scan_interval_seconds', 0.0)):.2f} 秒扫描一次，"
            f"共 {int(full_scan.get('scan_frame_count', 0))} 帧，补漏扫描通过。",
        )
    any_hit = False
    for segment in plan.get("segments") or []:
        hits = segment.get("hits") or []
        if not hits:
            continue
        any_hit = True
        actions = []
        if segment.get("needs_subtitle_mask"):
            actions.append("字幕打码")
        if segment.get("needs_audio_mute"):
            actions.append("音频消音")
        keywords = "、".join(hit.get("keyword", "") for hit in hits)
        hit_windows = "、".join(
            f"{float(hit.get('start_time', segment['start_time'])):.2f}-{float(hit.get('end_time', segment['end_time'])):.2f} 秒"
            for hit in hits
        )
        lines.append(
            f"- {float(segment['start_time']):.1f}-{float(segment['end_time']):.1f} 秒 | {' + '.join(actions)} | 命中: {keywords}"
        )
        lines.append(f"  命中时间: {hit_windows}")
        lines.append(f"  原字幕: {segment.get('text', '')}")
        lines.append(f"  处理后: {segment.get('masked_text', '')}")
    if not any_hit:
        lines.append("- 无命中，输出视频未做字幕/音频修改。")
    ocr_hits = report.get("ocr_supplemental_hits") or []
    if ocr_hits:
        lines.append("")
        lines.append("OCR补充命中:")
        for item in ocr_hits:
            lines.append(
                f"- {float(item['start_time']):.1f}-{float(item['end_time']):.1f} 秒 | 词: {item['keyword']} | 来源: OCR字幕"
            )
            lines.append(f"  OCR文本: {item.get('ocr_text', '')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def resolve_ffmpeg(value: str | None) -> Path:
    if value:
        path = Path(value)
        if path.exists():
            return path
        resolved = shutil.which(value)
        if resolved:
            return Path(resolved)
        raise SystemExit(f"ffmpeg not found: {value}")
    candidate = MASK_MOD.find_ffmpeg()
    if candidate:
        return Path(candidate)
    raise SystemExit("ffmpeg not found. Pass --ffmpeg or run from a repo that has material_remix_desktop_source/bin/ffmpeg.exe.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply keyword subtitle masking and synchronized audio muting to a source video.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--transcript", type=Path, help="Timestamped transcript JSON. If omitted, open-source ASR generates one first.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--subtitle-bbox",
        type=parse_bbox,
        default=FALLBACK_SUBTITLE_LINE_BBOX,
        help="Fallback normalized subtitle line bbox only when OCR/text-region localization fails.",
    )
    parser.add_argument("--ffmpeg")
    parser.add_argument("--asr-backend", choices=["auto", "funasr", "whisper"], default="auto")
    parser.add_argument("--asr-model", help="ASR model. Defaults: funasr=paraformer-zh, whisper=large-v3.")
    parser.add_argument("--asr-language", default="zh")
    parser.add_argument("--asr-device", default="auto")
    parser.add_argument("--asr-keep-audio", action="store_true")
    parser.add_argument("--copy-when-clean", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    video = args.video.resolve()
    if not video.is_file():
        raise SystemExit(f"Source video not found: {video}")

    output = args.output.resolve()
    output_dir = (args.output_dir or output.parent).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    transcript_generated: Dict[str, Any] = {"enabled": False}
    transcript = args.transcript.resolve() if args.transcript else None
    resolved_ffmpeg: Optional[Path] = resolve_ffmpeg(args.ffmpeg)
    if transcript is not None:
        if not transcript.is_file():
            raise SystemExit(f"Transcript JSON not found: {transcript}")
    else:
        asr_dir = output_dir / "asr"
        transcript = asr_dir / f"{video.stem}_transcript.json"
        try:
            generated = ASR_MOD.transcribe_source_to_json(
                video,
                transcript,
                backend=args.asr_backend,
                model=args.asr_model,
                language=args.asr_language,
                device=args.asr_device,
                ffmpeg=resolved_ffmpeg,
                keep_audio=args.asr_keep_audio,
            )
        except Exception as exc:
            raise SystemExit(f"ASR transcript generation failed: {exc}") from exc
        transcript_generated = {
            "enabled": True,
            "output": str(transcript),
            "human_report": generated.get("human_report"),
            "engine": generated.get("engine"),
            "backend": generated.get("backend"),
            "model": generated.get("model"),
            "language": generated.get("language"),
            "word_timestamps_available": generated.get("word_timestamps_available"),
            "segment_count": generated.get("segment_count"),
            "word_count": generated.get("word_count"),
        }

    segments = KEYWORD_MOD.load_segments(transcript)
    plan = KEYWORD_MOD.analyze_segments(segments)
    redactions, subtitle_localization = build_redactions(video, plan, args.subtitle_bbox)
    subtitle_audio_sync = subtitle_audio_sync_check(redactions)
    if not subtitle_audio_sync["passed"]:
        raise RuntimeError(f"Subtitle mask is not covered by audio mute: {subtitle_audio_sync['uncovered_subtitle_redactions']}")

    plan_path = output_dir / "redaction_plan.json"
    report_path = output_dir / "keyword_video_redaction_report.json"
    human_path = output_dir / "字幕消音处理说明.txt"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if redactions:
        processed = MASK_MOD.apply_redactions(video, output, redactions, resolved_ffmpeg)
    elif args.copy_when_clean:
        shutil.copy2(video, output)
        processed = {"status": "copied_clean", "output": str(output), "visual_redaction_count": 0, "audio_mute_count": 0}
    else:
        processed = {"status": "skipped_clean", "output": None, "visual_redaction_count": 0, "audio_mute_count": 0}

    report = {
        "source": str(video),
        "transcript": str(transcript),
        "transcript_generated": transcript_generated,
        "output": str(output),
        "plan_path": str(plan_path),
        "subtitle_bbox_normalized": list(args.subtitle_bbox),
        "subtitle_localization": subtitle_localization,
        "subtitle_redaction_count": len([item for item in redactions if item.get("type") == "subtitle_replace"]),
        "audio_mute_count": len([item for item in redactions if item.get("type") == "audio_mute"]),
        "subtitle_audio_sync": subtitle_audio_sync,
        "redactions": redactions,
        "processed": processed,
        "plan": plan,
        "ocr_supplemental_hits": subtitle_localization.get("supplemental_ocr_hits", []),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_human_report(human_path, report)

    print(
        json.dumps(
            {
                "output": str(output),
                "plan": str(plan_path),
                "report": str(report_path),
                "human_report": str(human_path),
                "transcript": str(transcript),
                "transcript_generated": transcript_generated,
                "subtitle_redactions": report["subtitle_redaction_count"],
                "audio_mutes": report["audio_mute_count"],
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
