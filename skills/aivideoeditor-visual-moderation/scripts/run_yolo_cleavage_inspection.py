#!/usr/bin/env python3
"""Inspect YOLO cleavage-groove predictions on curated frame samples.

This script is a small analysis companion for prepare_yolo_cleavage_dataset.py.
It reads the dataset summary, runs a YOLO model on the saved frame images, and
exports annotated previews plus a compact metrics JSON.
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

import run_visual_moderation as frame_mod


def load_cv2():
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError("opencv-python/cv2 is required for YOLO inspection.") from exc
    return cv2


def load_yolo(weights: Path):
    try:
        from ultralytics import YOLO
    except Exception as exc:
        raise RuntimeError("ultralytics is required for YOLO inspection.") from exc
    return YOLO(str(weights))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def normalize_bbox(bbox: Sequence[float]) -> Optional[List[float]]:
    normalized = frame_mod.normalize_bbox(list(bbox))
    return normalized if normalized else None


def bbox_iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    return inter / max(1e-9, area_a + area_b - inter)


def read_summary(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"summary JSON must contain an items list: {path}")
    normalized: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        image = item.get("image")
        if not image:
            continue
        normalized.append(item)
    normalized.sort(key=lambda item: float(item.get("time") or 0.0))
    return normalized


def evenly_sample(items: List[Dict[str, Any]], sample_count: int, seed: int) -> List[Dict[str, Any]]:
    if sample_count <= 0 or sample_count >= len(items):
        return items

    positives = [item for item in items if item.get("positive")]
    negatives = [item for item in items if not item.get("positive")]

    def take_evenly(seq: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
        if count <= 0:
            return []
        if count >= len(seq):
            return list(seq)
        if count == 1:
            return [seq[len(seq) // 2]]
        chosen: List[int] = []
        for index in range(count):
            pos = round(index * (len(seq) - 1) / float(count - 1))
            if pos not in chosen:
                chosen.append(pos)
        if len(chosen) < count:
            for index in range(len(seq)):
                if len(chosen) >= count:
                    break
                if index not in chosen:
                    chosen.append(index)
        return [seq[index] for index in sorted(chosen[:count])]

    if len(positives) >= sample_count:
        return take_evenly(positives, sample_count)

    selected = list(positives)
    remaining = sample_count - len(selected)
    selected.extend(take_evenly(negatives, remaining))
    selected.sort(key=lambda item: float(item.get("time") or 0.0))
    return selected


def draw_normalized_box(image: Any, bbox: Sequence[float], color: Tuple[int, int, int], label: str) -> None:
    cv2 = load_cv2()
    height, width = image.shape[:2]
    normalized = normalize_bbox(bbox)
    if not normalized:
        return
    x1, y1, x2, y2 = normalized
    px1 = int(round(x1 * width))
    py1 = int(round(y1 * height))
    px2 = int(round(x2 * width))
    py2 = int(round(y2 * height))
    cv2.rectangle(image, (px1, py1), (px2, py2), color, 2)
    cv2.putText(
        image,
        label,
        (px1, max(18, py1 - 6)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        color,
        1,
        cv2.LINE_AA,
    )


def draw_pixel_box(image: Any, bbox: Sequence[float], color: Tuple[int, int, int], label: str) -> None:
    cv2 = load_cv2()
    height, width = image.shape[:2]
    if len(bbox) != 4:
        return
    x1, y1, x2, y2 = [float(value) for value in bbox]
    px1 = int(round(clamp(x1, 0.0, width - 1)))
    py1 = int(round(clamp(y1, 0.0, height - 1)))
    px2 = int(round(clamp(x2, 0.0, width - 1)))
    py2 = int(round(clamp(y2, 0.0, height - 1)))
    if px2 <= px1 or py2 <= py1:
        return
    cv2.rectangle(image, (px1, py1), (px2, py2), color, 2)
    cv2.putText(
        image,
        label,
        (px1, min(height - 8, py2 + 18)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        color,
        1,
        cv2.LINE_AA,
    )


def resize_to_width(image: Any, thumb_width: int) -> Any:
    cv2 = load_cv2()
    height, width = image.shape[:2]
    if width <= 0:
        return image
    scaled_height = max(1, int(round(height * (thumb_width / float(width)))))
    return cv2.resize(image, (thumb_width, scaled_height), interpolation=cv2.INTER_AREA)


def make_contact_sheet(image_paths: Iterable[Path], output_path: Path, columns: int = 4, thumb_width: int = 240) -> None:
    cv2 = load_cv2()
    import numpy as np

    thumbs: List[Any] = []
    for path in image_paths:
        image = cv2.imread(str(path))
        if image is None:
            continue
        thumb = resize_to_width(image, thumb_width)
        cv2.putText(
            thumb,
            path.stem,
            (6, 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )
        thumbs.append(thumb)

    if not thumbs:
        return

    max_height = max(item.shape[0] for item in thumbs)
    rows: List[Any] = []
    for index in range(0, len(thumbs), columns):
        row = thumbs[index : index + columns]
        padded: List[Any] = []
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


def predict_frame(
    model: Any,
    image: Any,
    conf: float,
    imgsz: int,
    broad_width_ratio: float,
    broad_height_ratio: float,
) -> List[Dict[str, Any]]:
    result = model.predict(image, conf=conf, imgsz=imgsz, device="cpu", verbose=False)[0]
    height, width = image.shape[:2]
    predictions: List[Dict[str, Any]] = []
    if result.boxes is None:
        return predictions
    xyxy = result.boxes.xyxy.cpu().numpy()
    scores = result.boxes.conf.cpu().numpy()
    for box, score in zip(xyxy, scores):
        x1, y1, x2, y2 = [float(value) for value in box.tolist()]
        bw = max(0.0, x2 - x1)
        bh = max(0.0, y2 - y1)
        normalized_bbox = [
            round(clamp(x1 / max(1.0, float(width)), 0.0, 1.0), 4),
            round(clamp(y1 / max(1.0, float(height)), 0.0, 1.0), 4),
            round(clamp(x2 / max(1.0, float(width)), 0.0, 1.0), 4),
            round(clamp(y2 / max(1.0, float(height)), 0.0, 1.0), 4),
        ]
        predictions.append(
            {
                "bbox": normalized_bbox,
                "pixel_bbox": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                "conf": float(score),
                "broad": bool(
                    (bw / max(1.0, float(width)) > broad_width_ratio)
                    or (bh / max(1.0, float(height)) > broad_height_ratio)
                ),
            }
        )
    return predictions


def select_records(items: List[Dict[str, Any]], sample_count: int, seed: int) -> List[Dict[str, Any]]:
    if sample_count <= 0 or sample_count >= len(items):
        return items
    random.Random(seed).shuffle(items)
    return evenly_sample(items, sample_count, seed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run YOLO inspection on curated cleavage-groove frames.")
    parser.add_argument("--summary-json", type=Path, required=True, help="Dataset summary from prepare_yolo_cleavage_dataset.py")
    parser.add_argument("--weights", type=Path, required=True, help="Trained YOLO weights, typically best.pt")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output directory for annotated previews")
    parser.add_argument("--sample-count", type=int, default=0, help="Optional frame limit; 0 keeps all frames")
    parser.add_argument("--seed", type=int, default=20260730)
    parser.add_argument("--conf", type=float, default=0.08)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--match-iou", type=float, default=0.2)
    parser.add_argument("--broad-width-ratio", type=float, default=0.18)
    parser.add_argument("--broad-height-ratio", type=float, default=0.25)
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--thumb-width", type=int, default=240)
    args = parser.parse_args()

    if not args.summary_json.exists():
        raise FileNotFoundError(args.summary_json)
    if not args.weights.exists():
        raise FileNotFoundError(args.weights)

    cv2 = load_cv2()
    model = load_yolo(args.weights)
    items = select_records(read_summary(args.summary_json), args.sample_count, args.seed)

    frames_dir = args.output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    records: List[Dict[str, Any]] = []
    preview_paths: List[Path] = []
    for index, item in enumerate(items):
        image_path = Path(str(item["image"]))
        image = cv2.imread(str(image_path))
        if image is None:
            continue

        positive = bool(item.get("positive"))
        gt_boxes = [item["bbox"]] if positive and item.get("bbox") else []
        preds = predict_frame(
            model,
            image,
            conf=args.conf,
            imgsz=args.imgsz,
            broad_width_ratio=args.broad_width_ratio,
            broad_height_ratio=args.broad_height_ratio,
        )

        best_iou = 0.0
        if gt_boxes:
            for gt_box in gt_boxes:
                for pred in preds:
                    best_iou = max(best_iou, bbox_iou(gt_box, pred["bbox"]))

        annotated = image.copy()
        for gt_box in gt_boxes:
            draw_normalized_box(annotated, gt_box, (0, 0, 255), "GT")
        for pred in preds:
            color = (0, 165, 255) if pred["broad"] else (255, 255, 0)
            label = f'YOLO {pred["conf"]:.2f}' + (" broad" if pred["broad"] else "")
            draw_pixel_box(annotated, pred["pixel_bbox"], color, label)

        header = f'{float(item.get("time") or 0.0):.3f}s'
        if positive:
            header += f"  GT IoU={best_iou:.2f}"
        else:
            header += f"  NEG preds={len(preds)}"
        cv2.putText(
            annotated,
            header,
            (16, 34),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (30, 220, 255),
            2,
            cv2.LINE_AA,
        )

        out_path = frames_dir / f"{index:04d}_{image_path.stem}.jpg"
        cv2.imwrite(str(out_path), annotated)
        preview_paths.append(out_path)
        records.append(
            {
                "time": float(item.get("time") or 0.0),
                "image": str(image_path),
                "output": str(out_path),
                "positive": positive,
                "gt_boxes": gt_boxes,
                "predictions": preds,
                "best_iou": best_iou,
                "hit": bool(positive and best_iou >= args.match_iou),
            }
        )

    positive_records = [record for record in records if record["positive"]]
    negative_records = [record for record in records if not record["positive"]]
    metrics = {
        "sample_count": len(records),
        "positive_frames": len(positive_records),
        "positive_hit_frames_iou_ge_%.2f" % args.match_iou: len([record for record in positive_records if record["hit"]]),
        "positive_hit_rate": round(
            len([record for record in positive_records if record["hit"]]) / len(positive_records), 3
        )
        if positive_records
        else 0.0,
        "negative_frames": len(negative_records),
        "negative_frames_with_any_prediction": len([record for record in negative_records if record["predictions"]]),
        "negative_fp_rate": round(
            len([record for record in negative_records if record["predictions"]]) / len(negative_records), 3
        )
        if negative_records
        else 0.0,
        "avg_predictions_per_frame": round(
            sum(len(record["predictions"]) for record in records) / len(records), 3
        )
        if records
        else 0.0,
        "avg_best_iou_positive": round(
            sum(record["best_iou"] for record in positive_records) / len(positive_records), 3
        )
        if positive_records
        else 0.0,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    make_contact_sheet(preview_paths, args.output_dir / "prediction_contact_sheet.jpg", columns=args.columns, thumb_width=args.thumb_width)
    (args.output_dir / "predictions.json").write_text(
        json.dumps(
            {
                "summary_json": str(args.summary_json),
                "weights": str(args.weights),
                "records": records,
                "metrics": metrics,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({**metrics, "contact_sheet": str(args.output_dir / "prediction_contact_sheet.jpg")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
