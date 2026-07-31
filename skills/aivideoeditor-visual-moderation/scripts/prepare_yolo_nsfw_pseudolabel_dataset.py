#!/usr/bin/env python3
"""Prepare a YOLO pseudo-label dataset from NudeNet detections.

COCO-pretrained YOLO weights do not contain intimate/sensitive body-part
classes. This helper follows the documented AIVideoEditor moderation approach:
use NudeNet as an open-source candidate detector, export reviewed pseudo-labels,
then fine-tune YOLO as a local candidate proposer.

For covered-breast / suggestive-cleavage content, the exported positive label
is the visible central cleavage groove only. A NudeNet chest hit without a
clear local groove becomes a negative/empty-label frame so the model learns
"do not mask normal chest, V-neck clothing, collar edges, faces, or bodies."
The trained YOLO boxes are still evidence proposals; the moderation pipeline
must run policy/VLM checks before rendering mosaic.
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


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
DEFAULT_EXPOSED_LABELS = {
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
}
DEFAULT_COVERED_CHEST_LABELS = {"FEMALE_BREAST_COVERED"}


def load_cv2():
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError("opencv-python/cv2 is required to extract YOLO training frames.") from exc
    return cv2


def load_nudenet(model_path: Optional[str] = None):
    detector, error = video_mod.load_nudenet_detector(model_path)
    if detector is None:
        raise RuntimeError(error or "failed to initialize NudeNet")
    return detector


def parse_csv_set(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def class_name_from_label(label: str) -> str:
    return label.lower()


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def pixel_box_to_normalized(box: Sequence[float], frame_width: int, frame_height: int) -> Optional[List[float]]:
    if len(box) != 4:
        return None
    x, y, width, height = [float(value) for value in box]
    if width <= 0 or height <= 0:
        return None
    normalized = [
        clamp01(x / max(1.0, float(frame_width))),
        clamp01(y / max(1.0, float(frame_height))),
        clamp01((x + width) / max(1.0, float(frame_width))),
        clamp01((y + height) / max(1.0, float(frame_height))),
    ]
    return frame_mod.normalize_bbox(normalized)


def bbox_to_yolo_line(class_id: int, bbox: Sequence[float]) -> Optional[str]:
    normalized = frame_mod.normalize_bbox(list(bbox))
    if not normalized:
        return None
    x1, y1, x2, y2 = normalized
    width = x2 - x1
    height = y2 - y1
    if width <= 0.0 or height <= 0.0:
        return None
    return f"{class_id} {(x1 + x2) / 2.0:.6f} {(y1 + y2) / 2.0:.6f} {width:.6f} {height:.6f}"


def bbox_area(bbox: Sequence[float]) -> float:
    normalized = frame_mod.normalize_bbox(list(bbox))
    if not normalized:
        return 0.0
    return max(0.0, normalized[2] - normalized[0]) * max(0.0, normalized[3] - normalized[1])


def bbox_center(bbox: Sequence[float]) -> Tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def is_reasonable_candidate_bbox(bbox: Sequence[float], label: str) -> bool:
    normalized = frame_mod.normalize_bbox(list(bbox))
    if not normalized:
        return False
    width = normalized[2] - normalized[0]
    height = normalized[3] - normalized[1]
    area = width * height
    if area <= 0.0005:
        return False
    if width >= 0.62 or height >= 0.55 or area >= 0.18:
        return False
    if label == "covered_chest_candidate":
        if width > 0.48 or height > 0.46 or area > 0.12:
            return False
    if label == "cleavage_groove":
        if width > 0.07 or height > 0.18 or area > 0.012:
            return False
    return True


def tighten_cleavage_groove_bbox(bbox: Sequence[float]) -> Optional[List[float]]:
    groove = frame_mod.normalize_bbox(list(bbox))
    if not groove:
        return None
    x1, y1, x2, y2 = groove
    width = x2 - x1
    height = y2 - y1
    center_x = (x1 + x2) / 2.0
    # Bias upward because the lower part often overlaps a black undershirt/V-neck
    # while the risky visual signal is the upper central groove/shadow.
    center_y = y1 + height * 0.43
    target_width = max(0.022, min(0.040, width * 0.76))
    target_height = max(0.065, min(0.115, height * 0.72))
    return frame_mod.normalize_bbox(
        [
            center_x - target_width / 2.0,
            center_y - target_height / 2.0,
            center_x + target_width / 2.0,
            center_y + target_height / 2.0,
        ]
    )


def groove_matches_chest_seed(groove_bbox: Sequence[float], chest_union: Sequence[float]) -> bool:
    groove = frame_mod.normalize_bbox(list(groove_bbox))
    chest = frame_mod.normalize_bbox(list(chest_union))
    if not groove or not chest:
        return False
    width = groove[2] - groove[0]
    height = groove[3] - groove[1]
    if width > 0.085 or height > 0.22:
        return False
    groove_cx, groove_cy = bbox_center(groove)
    chest_cx, _ = bbox_center(chest)
    chest_width = max(0.0001, chest[2] - chest[0])
    chest_height = max(0.0001, chest[3] - chest[1])
    if abs(groove_cx - chest_cx) > max(0.16, chest_width * 0.48):
        return False
    if groove_cy < chest[1] - chest_height * 0.12:
        return False
    if groove_cy > chest[3] + chest_height * 0.12:
        return False
    return True


def collect_input_videos(inputs: Iterable[Path]) -> List[Path]:
    videos: List[Path] = []
    for input_path in inputs:
        if input_path.is_dir():
            for path in sorted(input_path.rglob("*")):
                if path.suffix.lower() in VIDEO_EXTENSIONS:
                    videos.append(path)
        elif input_path.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(input_path)
    deduped: List[Path] = []
    seen: set[str] = set()
    for path in videos:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def sample_times(duration: float, interval: float, max_frames: int) -> List[float]:
    if duration <= 0:
        return []
    interval = max(0.05, interval)
    times = [round(value, 3) for value in frange(0.0, max(0.001, duration - 0.001), interval)]
    if not times:
        times = [0.0]
    if max_frames > 0 and len(times) > max_frames:
        if max_frames == 1:
            return [times[len(times) // 2]]
        indexes = [round(index * (len(times) - 1) / float(max_frames - 1)) for index in range(max_frames)]
        return [times[index] for index in indexes]
    return times


def frange(start: float, stop: float, step: float) -> Iterable[float]:
    value = start
    while value <= stop:
        yield value
        value += step


def split_name(index: int, total: int, val_ratio: float) -> str:
    if total <= 1:
        return "train"
    stride = max(2, round(1.0 / max(0.01, min(0.5, val_ratio))))
    return "val" if index % stride == stride - 1 else "train"


def draw_preview(frame: Any, labels: List[Dict[str, Any]]) -> Any:
    cv2 = load_cv2()
    preview = frame.copy()
    frame_height, frame_width = preview.shape[:2]
    colors = {
        "cleavage_groove": (0, 0, 255),
        "covered_chest_candidate": (0, 0, 255),
        "female_breast_covered": (0, 0, 255),
        "female_breast_exposed": (0, 80, 255),
        "buttocks_exposed": (255, 0, 255),
        "female_genitalia_exposed": (255, 0, 0),
        "male_genitalia_exposed": (255, 0, 0),
        "anus_exposed": (255, 0, 255),
    }
    for item in labels:
        bbox = frame_mod.normalize_bbox(item.get("bbox"))
        if not bbox:
            continue
        x1 = int(round(bbox[0] * frame_width))
        y1 = int(round(bbox[1] * frame_height))
        x2 = int(round(bbox[2] * frame_width))
        y2 = int(round(bbox[3] * frame_height))
        name = str(item.get("name") or "candidate")
        color = colors.get(name, (0, 0, 255))
        cv2.rectangle(preview, (x1, y1), (x2, y2), color, 2)
        cv2.putText(preview, name, (x1, max(16, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
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
        cv2.putText(thumb, path.stem[:34], (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), np.vstack(rows))


def write_dataset_yaml(output_dir: Path, class_names: List[str]) -> None:
    lines = [
        f"path: {output_dir.as_posix()}",
        "train: images/train",
        "val: images/val",
        "names:",
    ]
    for index, name in enumerate(class_names):
        lines.append(f"  {index}: {name}")
    (output_dir / "dataset.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def detections_to_training_labels(
    detections: List[Dict[str, Any]],
    frame_path: Path,
    frame_width: int,
    frame_height: int,
    class_mode: str,
    include_labels: set[str],
    min_score: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    raw_items: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for detection in detections:
        label = str(detection.get("class") or detection.get("label") or "").upper()
        score = float(detection.get("score") or 0.0)
        if not label or label not in include_labels or score < min_score:
            continue
        bbox = pixel_box_to_normalized(detection.get("box") or detection.get("bbox") or [], frame_width, frame_height)
        if not bbox:
            rejected.append({"label": label, "score": score, "reason": "bad_bbox", "detection": detection})
            continue
        raw_items.append({"label": label, "score": score, "bbox": bbox})

    if class_mode in {"cleavage-groove", "covered-chest-candidate"}:
        chest_items = [item for item in raw_items if item["label"] in DEFAULT_COVERED_CHEST_LABELS]
        clusters = video_mod.cluster_chest_detections(chest_items)
        labels: List[Dict[str, Any]] = []
        for cluster in clusters:
            union = video_mod.union_bboxes(item["bbox"] for item in cluster)
            if not union:
                continue
            if class_mode == "cleavage-groove":
                groove_bbox, localization = video_mod.localize_cleavage_groove_bbox(frame_path, [item["bbox"] for item in cluster])
                if not groove_bbox:
                    rejected.append(
                        {
                            "label": "cleavage_groove",
                            "bbox": union,
                            "reason": "no_visible_cleavage_groove",
                            "localization": localization,
                        }
                    )
                    continue
                groove_bbox = tighten_cleavage_groove_bbox(groove_bbox)
                if not groove_bbox:
                    rejected.append(
                        {
                            "label": "cleavage_groove",
                            "bbox": union,
                            "reason": "groove_tighten_failed",
                            "localization": localization,
                        }
                    )
                    continue
                if not is_reasonable_candidate_bbox(groove_bbox, "cleavage_groove") or not groove_matches_chest_seed(groove_bbox, union):
                    rejected.append(
                        {
                            "label": "cleavage_groove",
                            "bbox": groove_bbox,
                            "chest_union": union,
                            "reason": "groove_bbox_rejected",
                            "localization": localization,
                        }
                    )
                    continue
                labels.append(
                    {
                        "name": "cleavage_groove",
                        "class_id": 0,
                        "bbox": groove_bbox,
                        "chest_union": union,
                        "source_labels": [item["label"] for item in cluster],
                        "score": round(max(item["score"] for item in cluster), 4),
                        "localization": localization,
                    }
                )
                continue
            if not is_reasonable_candidate_bbox(union, "covered_chest_candidate"):
                rejected.append({"label": "covered_chest_candidate", "bbox": union, "reason": "candidate_too_broad"})
                continue
            labels.append(
                {
                    "name": "covered_chest_candidate",
                    "class_id": 0,
                    "bbox": union,
                    "source_labels": [item["label"] for item in cluster],
                    "score": round(max(item["score"] for item in cluster), 4),
                }
            )
        return labels, rejected

    class_names = sorted(class_name_from_label(label) for label in include_labels)
    class_id_by_name = {name: index for index, name in enumerate(class_names)}
    labels = []
    for item in raw_items:
        name = class_name_from_label(item["label"])
        if not is_reasonable_candidate_bbox(item["bbox"], name):
            rejected.append({"label": item["label"], "bbox": item["bbox"], "reason": "candidate_too_broad"})
            continue
        labels.append(
            {
                "name": name,
                "class_id": class_id_by_name[name],
                "bbox": item["bbox"],
                "source_labels": [item["label"]],
                "score": round(item["score"], 4),
            }
        )
    return labels, rejected


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a YOLO pseudo-label dataset from NudeNet detections.")
    parser.add_argument("inputs", nargs="+", type=Path, help="Input video file(s) or folders.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--class-mode",
        choices=["cleavage-groove", "covered-chest-candidate", "nudenet-labels"],
        default="cleavage-groove",
        help="cleavage-groove exports only the visible central groove; no visible groove means an empty-label negative frame.",
    )
    parser.add_argument(
        "--include-labels",
        help="Comma-separated NudeNet labels. Defaults to FEMALE_BREAST_COVERED for cleavage-groove/covered-chest modes.",
    )
    parser.add_argument("--min-score", type=float, default=0.42)
    parser.add_argument("--sample-interval", type=float, default=0.6)
    parser.add_argument("--max-frames-per-video", type=int, default=220)
    parser.add_argument("--negative-keep-ratio", type=float, default=0.65)
    parser.add_argument("--val-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260730)
    parser.add_argument("--nudenet-model-path")
    parser.add_argument("--preview-limit", type=int, default=120)
    args = parser.parse_args()

    videos = collect_input_videos(args.inputs)
    if not videos:
        raise FileNotFoundError("no input videos found")

    if args.include_labels:
        include_labels = parse_csv_set(args.include_labels)
    elif args.class_mode in {"cleavage-groove", "covered-chest-candidate"}:
        include_labels = set(DEFAULT_COVERED_CHEST_LABELS)
    else:
        include_labels = set(DEFAULT_EXPOSED_LABELS | DEFAULT_COVERED_CHEST_LABELS)

    if args.class_mode == "cleavage-groove":
        class_names = ["cleavage_groove"]
    elif args.class_mode == "covered-chest-candidate":
        class_names = ["covered_chest_candidate"]
    else:
        class_names = sorted(class_name_from_label(label) for label in include_labels)

    cv2 = load_cv2()
    detector = load_nudenet(args.nudenet_model_path)
    rng = random.Random(args.seed)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for split in ("train", "val"):
        (args.output_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (args.output_dir / "labels" / split).mkdir(parents=True, exist_ok=True)
    preview_dir = args.output_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = args.output_dir / "_tmp_frames"
    temp_dir.mkdir(parents=True, exist_ok=True)

    pending_items: List[Dict[str, Any]] = []
    rejected_items: List[Dict[str, Any]] = []
    for video_index, video_path in enumerate(videos):
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            rejected_items.append({"video": str(video_path), "reason": "video_open_failed"})
            continue
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
        times = sample_times(duration, args.sample_interval, args.max_frames_per_video)

        for sample_index, timestamp in enumerate(times):
            frame_index = max(0, int(round(timestamp * fps)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frame_height, frame_width = frame.shape[:2]
            temp_frame_path = temp_dir / f"v{video_index:03d}_{sample_index:05d}.jpg"
            cv2.imwrite(str(temp_frame_path), frame)
            try:
                detections = detector.detect(str(temp_frame_path)) or []
            except Exception as exc:
                rejected_items.append({"video": str(video_path), "time": timestamp, "reason": f"nudenet_failed: {exc}"})
                continue

            labels, rejected = detections_to_training_labels(
                detections,
                frame_path=temp_frame_path,
                frame_width=frame_width,
                frame_height=frame_height,
                class_mode=args.class_mode,
                include_labels=include_labels,
                min_score=args.min_score,
            )
            for rejected_item in rejected:
                rejected_items.append({"video": str(video_path), "time": timestamp, **rejected_item})
            if not labels and rng.random() > max(0.0, min(1.0, args.negative_keep_ratio)):
                continue
            pending_items.append(
                {
                    "video": str(video_path),
                    "video_index": video_index,
                    "time": round(timestamp, 3),
                    "frame_path": str(temp_frame_path),
                    "labels": labels,
                    "width": frame_width,
                    "height": frame_height,
                }
            )
        cap.release()

    pending_items.sort(key=lambda item: (item["video"], item["time"]))
    written: List[Dict[str, Any]] = []
    preview_paths: List[Path] = []
    for index, item in enumerate(pending_items):
        split = split_name(index, len(pending_items), args.val_ratio)
        source_frame = Path(item["frame_path"])
        frame = cv2.imread(str(source_frame))
        if frame is None:
            continue
        has_labels = bool(item["labels"])
        prefix = "pos" if has_labels else "neg"
        stem = f"{prefix}_{index:05d}_v{item['video_index']:03d}_{item['time']:08.3f}".replace(".", "_")
        image_path = args.output_dir / "images" / split / f"{stem}.jpg"
        label_path = args.output_dir / "labels" / split / f"{stem}.txt"
        cv2.imwrite(str(image_path), frame)
        label_lines = []
        for label in item["labels"]:
            line = bbox_to_yolo_line(int(label["class_id"]), label["bbox"])
            if line:
                label_lines.append(line)
        label_path.write_text("\n".join(label_lines) + ("\n" if label_lines else ""), encoding="utf-8")
        preview_path = preview_dir / f"{stem}.jpg"
        cv2.imwrite(str(preview_path), draw_preview(frame, item["labels"]))
        if len(preview_paths) < args.preview_limit:
            preview_paths.append(preview_path)
        written.append(
            {
                "split": split,
                "image": str(image_path),
                "label": str(label_path),
                "video": item["video"],
                "time": item["time"],
                "positive": bool(label_lines),
                "labels": item["labels"],
            }
        )

    for temp_file in temp_dir.glob("*.jpg"):
        try:
            temp_file.unlink()
        except OSError:
            pass
    try:
        temp_dir.rmdir()
    except OSError:
        pass

    write_dataset_yaml(args.output_dir, class_names)
    contact_sheet = args.output_dir / "previews_contact_sheet.jpg"
    make_contact_sheet(preview_paths, contact_sheet)
    summary = {
        "videos": [str(path) for path in videos],
        "class_mode": args.class_mode,
        "include_labels": sorted(include_labels),
        "class_names": class_names,
        "min_score": args.min_score,
        "sample_interval": args.sample_interval,
        "max_frames_per_video": args.max_frames_per_video,
        "positive_count": len([item for item in written if item["positive"]]),
        "negative_count": len([item for item in written if not item["positive"]]),
        "total_count": len(written),
        "rejected_count": len(rejected_items),
        "dataset_yaml": str(args.output_dir / "dataset.yaml"),
        "contact_sheet": str(contact_sheet),
        "items": written,
        "rejected": rejected_items[:500],
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("positive_count", "negative_count", "total_count", "rejected_count", "dataset_yaml", "contact_sheet")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
