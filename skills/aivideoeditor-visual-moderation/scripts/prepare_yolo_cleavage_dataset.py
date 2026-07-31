#!/usr/bin/env python3
"""Prepare a small YOLO dataset for cleavage-groove localization.

The goal is to turn the current video moderation report into trainable YOLO
labels without teaching the model to mask clothing, faces, whole people, or
whole chests. Positive labels should be narrow central-groove boxes. Negative
frames should still have empty label files so YOLO learns hard negatives.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_video_visual_moderation as video_mod
import run_visual_moderation as frame_mod


def load_cv2():
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError("opencv-python/cv2 is required to extract YOLO training frames.") from exc
    return cv2


def parse_csv_set(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def bbox_to_yolo(bbox: Sequence[float]) -> Optional[Tuple[float, float, float, float]]:
    normalized = frame_mod.normalize_bbox(list(bbox))
    if not normalized:
        return None
    x1, y1, x2, y2 = normalized
    width = x2 - x1
    height = y2 - y1
    if width <= 0.0 or height <= 0.0:
        return None
    return (
        clamp01((x1 + x2) / 2.0),
        clamp01((y1 + y2) / 2.0),
        clamp01(width),
        clamp01(height),
    )


def yolo_line(class_id: int, bbox: Sequence[float]) -> Optional[str]:
    converted = bbox_to_yolo(bbox)
    if not converted:
        return None
    return f"{class_id} " + " ".join(f"{value:.6f}" for value in converted)


def bbox_area(bbox: Sequence[float]) -> float:
    normalized = frame_mod.normalize_bbox(list(bbox))
    if not normalized:
        return 0.0
    return max(0.0, normalized[2] - normalized[0]) * max(0.0, normalized[3] - normalized[1])


def relocalization_seed_bbox(bbox: Sequence[float]) -> Optional[List[float]]:
    normalized = frame_mod.normalize_bbox(list(bbox))
    if not normalized:
        return None
    x1, y1, x2, y2 = normalized
    width = x2 - x1
    height = y2 - y1
    center_x = (x1 + x2) / 2.0
    seed_half_width = max(0.20, min(0.28, width * 5.0))
    seed_top_padding = max(0.16, min(0.32, height * 2.6))
    seed_bottom_padding = max(0.03, min(0.10, height * 0.85))
    seed = frame_mod.normalize_bbox(
        [
            center_x - seed_half_width,
            y1 - seed_top_padding,
            center_x + seed_half_width,
            y2 + seed_bottom_padding,
        ]
    )
    if not seed:
        return None
    seed_height = seed[3] - seed[1]
    if seed_height > 0.44:
        center_y = (seed[1] + seed[3]) / 2.0
        seed = frame_mod.normalize_bbox([seed[0], center_y - 0.22, seed[2], center_y + 0.22])
    return seed


def relocalize_positive_bbox(frame: Any, temp_dir: Path, stem: str, bbox: Sequence[float]) -> Tuple[Optional[List[float]], Dict[str, Any]]:
    cv2 = load_cv2()
    seed = relocalization_seed_bbox(bbox)
    if not seed:
        return None, {"status": "bad_relocalization_seed"}
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{stem}.jpg"
    cv2.imwrite(str(temp_path), frame)
    try:
        refined, localization = video_mod.localize_cleavage_groove_bbox(temp_path, [seed])
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    if not refined:
        localization = dict(localization)
        localization["seed"] = seed
        return None, localization
    return refined, {**localization, "seed": seed, "original_bbox": frame_mod.normalize_bbox(list(bbox))}


def bbox_at(redaction: Dict[str, Any], timestamp: float) -> Optional[List[float]]:
    bbox = video_mod.bbox_at_time(redaction, timestamp)
    if not bbox:
        return None
    # YOLO seed labels for cleavage should stay narrow and local.
    if bbox_area(bbox) > 0.018:
        return None
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    if width > 0.085 or height > 0.24:
        return None
    return bbox


def evenly_spaced_times(start: float, end: float, count: int) -> List[float]:
    if count <= 0 or end < start:
        return []
    if count == 1 or math.isclose(start, end):
        return [round((start + end) / 2.0, 3)]
    step = (end - start) / float(count - 1)
    return [round(start + index * step, 3) for index in range(count)]


def collect_report_positive_items(
    report: Dict[str, Any],
    include_track_ids: set[str],
    exclude_track_ids: set[str],
    samples_per_track: int,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for redaction in report.get("decision", {}).get("redactions", []):
        if not isinstance(redaction, dict):
            continue
        if redaction.get("type") not in frame_mod.VISUAL_MASK_TYPES:
            continue
        if str(redaction.get("category") or "").lower() != "nsfw":
            continue
        track_id = str(redaction.get("track_id") or "")
        if include_track_ids and track_id not in include_track_ids:
            continue
        if track_id in exclude_track_ids:
            continue
        start = redaction.get("start_time")
        end = redaction.get("end_time")
        if start is None or end is None:
            continue
        times = evenly_spaced_times(float(start), float(end), max(1, samples_per_track))
        for timestamp in times:
            bbox = bbox_at(redaction, timestamp)
            if not bbox:
                continue
            items.append(
                {
                    "time": timestamp,
                    "bbox": bbox,
                    "track_id": track_id,
                    "source": "report",
                }
            )
    return items


def load_curated_items(path: Optional[Path]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not path:
        return [], []
    data = json.loads(path.read_text(encoding="utf-8"))
    positives = data.get("positives") or []
    negatives = data.get("negatives") or []
    if not isinstance(positives, list) or not isinstance(negatives, list):
        raise ValueError("curated JSON must contain list fields: positives and negatives")
    return positives, negatives


def read_frame(cap: Any, fps: float, timestamp: float) -> Optional[Any]:
    cv2 = load_cv2()
    frame_index = max(0, int(round(timestamp * fps)))
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ok, frame = cap.read()
    return frame if ok else None


def draw_preview(frame: Any, bboxes: Iterable[Sequence[float]], label: str) -> Any:
    cv2 = load_cv2()
    preview = frame.copy()
    height, width = preview.shape[:2]
    for bbox in bboxes:
        normalized = frame_mod.normalize_bbox(list(bbox))
        if not normalized:
            continue
        x1 = int(round(normalized[0] * width))
        y1 = int(round(normalized[1] * height))
        x2 = int(round(normalized[2] * width))
        y2 = int(round(normalized[3] * height))
        cv2.rectangle(preview, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(preview, label, (x1, max(16, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
    return preview


def make_contact_sheet(image_paths: List[Path], output_path: Path, columns: int = 5, thumb_width: int = 220) -> None:
    if not image_paths:
        return
    cv2 = load_cv2()
    import numpy as np

    thumbs = []
    for path in image_paths:
        image = cv2.imread(str(path))
        if image is None:
            continue
        height, width = image.shape[:2]
        thumb_height = max(1, int(height * thumb_width / max(1, width)))
        thumb = cv2.resize(image, (thumb_width, thumb_height))
        cv2.putText(thumb, path.stem, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
        thumbs.append(thumb)
    if not thumbs:
        return
    max_height = max(item.shape[0] for item in thumbs)
    rows = []
    for index in range(0, len(thumbs), columns):
        row = thumbs[index : index + columns]
        padded = []
        for thumb in row:
            if thumb.shape[0] < max_height:
                pad = np.full((max_height - thumb.shape[0], thumb.shape[1], 3), 255, dtype=np.uint8)
                thumb = np.vstack([thumb, pad])
            padded.append(thumb)
        while len(padded) < columns:
            padded.append(np.full((max_height, thumb_width, 3), 255, dtype=np.uint8))
        rows.append(np.hstack(padded))
    sheet = np.vstack(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), sheet)


def split_name(index: int, total: int, val_ratio: float) -> str:
    if total <= 1:
        return "train"
    stride = max(2, round(1.0 / max(0.01, min(0.5, val_ratio))))
    return "val" if index % stride == stride - 1 else "train"


def write_dataset_yaml(output_dir: Path, class_name: str) -> None:
    yaml_text = (
        f"path: {output_dir.as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "names:\n"
        f"  0: {class_name}\n"
    )
    (output_dir / "dataset.yaml").write_text(yaml_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare YOLO labels for central cleavage-groove localization.")
    parser.add_argument("video", type=Path, help="Input video path.")
    parser.add_argument("--report", type=Path, help="Visual moderation report with bbox_keyframes.")
    parser.add_argument("--curated-json", type=Path, help="Optional JSON with positives/negatives overrides.")
    parser.add_argument("--output-dir", type=Path, required=True, help="YOLO dataset output directory.")
    parser.add_argument("--class-name", default="cleavage_groove")
    parser.add_argument("--include-track-ids", help="Comma-separated report track IDs to use as positives.")
    parser.add_argument("--exclude-track-ids", help="Comma-separated report track IDs to ignore.")
    parser.add_argument("--samples-per-track", type=int, default=4)
    parser.add_argument("--val-ratio", type=float, default=0.2)
    parser.add_argument("--negative-time", action="append", type=float, default=[], help="Hard-negative frame timestamp.")
    parser.add_argument(
        "--allow-low-context-positive",
        action="store_true",
        help="Keep positive boxes even when local skin/context validation says the box is likely off target.",
    )
    parser.add_argument(
        "--disable-positive-relocalization",
        action="store_true",
        help="Use report boxes directly instead of relocalizing each positive frame around the central groove.",
    )
    parser.add_argument("--seed", type=int, default=20260730)
    args = parser.parse_args()

    if not args.video.exists():
        raise FileNotFoundError(args.video)

    cv2 = load_cv2()
    report: Dict[str, Any] = {}
    if args.report:
        report = json.loads(args.report.read_text(encoding="utf-8"))

    curated_positives, curated_negatives = load_curated_items(args.curated_json)
    positives = collect_report_positive_items(
        report,
        parse_csv_set(args.include_track_ids),
        parse_csv_set(args.exclude_track_ids),
        args.samples_per_track,
    )
    for item in curated_positives:
        if "bbox" not in item:
            continue
        positives.append(
            {
                "time": round(float(item["time"]), 3),
                "bbox": frame_mod.normalize_bbox(item["bbox"]),
                "track_id": item.get("track_id") or "curated",
                "source": "curated",
            }
        )

    negatives: List[Dict[str, Any]] = [{"time": round(float(value), 3), "source": "cli"} for value in args.negative_time]
    for item in curated_negatives:
        negatives.append({"time": round(float(item["time"]), 3), "source": item.get("source") or "curated"})

    # De-duplicate near-identical timestamps.
    seen_pos: set[Tuple[int, int, int, int, int]] = set()
    deduped_pos: List[Dict[str, Any]] = []
    for item in positives:
        bbox = frame_mod.normalize_bbox(item.get("bbox"))
        if not bbox:
            continue
        key = (round(float(item["time"]) * 10), *(round(value * 1000) for value in bbox))
        if key in seen_pos:
            continue
        seen_pos.add(key)
        item["bbox"] = bbox
        deduped_pos.append(item)
    positives = deduped_pos

    seen_neg: set[int] = set()
    deduped_neg: List[Dict[str, Any]] = []
    for item in negatives:
        key = round(float(item["time"]) * 10)
        if key in seen_neg:
            continue
        seen_neg.add(key)
        deduped_neg.append(item)
    negatives = deduped_neg

    random.Random(args.seed).shuffle(positives)
    random.Random(args.seed + 1).shuffle(negatives)
    all_items = positives + negatives

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for split in ("train", "val"):
        (args.output_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (args.output_dir / "labels" / split).mkdir(parents=True, exist_ok=True)
    preview_dir = args.output_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise RuntimeError(f"failed to open video: {args.video}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)

    preview_paths: List[Path] = []
    written = []
    skipped = []
    temp_relocalize_dir = args.output_dir / "_tmp_relocalize"
    for index, item in enumerate(all_items):
        split = split_name(index, len(all_items), args.val_ratio)
        timestamp = float(item["time"])
        frame = read_frame(cap, fps, timestamp)
        if frame is None:
            continue
        is_positive = "bbox" in item and item.get("bbox")
        positive_context: Optional[Dict[str, Any]] = None
        relocalization: Optional[Dict[str, Any]] = None
        original_bbox = item.get("bbox")
        if is_positive and not args.disable_positive_relocalization:
            safe_time = f"{timestamp:08.3f}".replace(".", "_")
            refined_bbox, relocalization = relocalize_positive_bbox(frame, temp_relocalize_dir, f"relocalize_{index:04d}_{safe_time}", item["bbox"])
            if refined_bbox:
                item["bbox"] = refined_bbox
        if is_positive:
            positive_context = video_mod.cleavage_bbox_context(frame, item["bbox"])
            if not args.allow_low_context_positive and not positive_context.get("ok"):
                skipped.append(
                    {
                        "time": timestamp,
                        "bbox": item.get("bbox"),
                        "track_id": item.get("track_id"),
                        "source": item.get("source"),
                        "reason": "positive_context_rejected",
                        "context": positive_context,
                        "relocalization": relocalization,
                    }
                )
                continue
        prefix = "pos" if is_positive else "neg"
        safe_time = f"{timestamp:08.3f}".replace(".", "_")
        stem = f"{prefix}_{index:04d}_{safe_time}"
        image_path = args.output_dir / "images" / split / f"{stem}.jpg"
        label_path = args.output_dir / "labels" / split / f"{stem}.txt"
        cv2.imwrite(str(image_path), frame)
        lines: List[str] = []
        if is_positive:
            line = yolo_line(0, item["bbox"])
            if line:
                lines.append(line)
        label_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        preview = draw_preview(frame, [item["bbox"]] if is_positive else [], args.class_name)
        preview_path = preview_dir / f"{stem}.jpg"
        cv2.imwrite(str(preview_path), preview)
        preview_paths.append(preview_path)
        written.append(
            {
                "split": split,
                "image": str(image_path),
                "label": str(label_path),
                "time": timestamp,
                "positive": bool(lines),
                "bbox": item.get("bbox"),
                "original_bbox": original_bbox if is_positive else None,
                "track_id": item.get("track_id"),
                "source": item.get("source"),
                "context": positive_context if is_positive else None,
                "relocalization": relocalization if is_positive else None,
            }
        )

    cap.release()
    try:
        temp_relocalize_dir.rmdir()
    except OSError:
        pass
    write_dataset_yaml(args.output_dir, args.class_name)
    make_contact_sheet(preview_paths, args.output_dir / "previews_contact_sheet.jpg")
    summary = {
        "video": str(args.video),
        "report": str(args.report) if args.report else None,
        "class_name": args.class_name,
        "positive_count": len([item for item in written if item["positive"]]),
        "negative_count": len([item for item in written if not item["positive"]]),
        "skipped_positive_count": len(skipped),
        "total_count": len(written),
        "dataset_yaml": str(args.output_dir / "dataset.yaml"),
        "contact_sheet": str(args.output_dir / "previews_contact_sheet.jpg"),
        "items": written,
        "skipped": skipped,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("positive_count", "negative_count", "total_count", "dataset_yaml", "contact_sheet")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
