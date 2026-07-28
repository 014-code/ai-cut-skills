#!/usr/bin/env python3
"""Build a resumable Read-review queue from an existing visual asset Manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from extract_video_frames import FrameExtractionError, extract_frames
from validate_manifest import has_valid_media_dimensions, validate_region


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read visual asset Manifest: {resolved}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("assets"), list):
        raise ValueError("Visual asset Manifest must be an object with an assets array")
    return value


def queue_key(relative_path: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(relative_path).stem).strip("._")
    digest = hashlib.sha1(relative_path.encode("utf-8")).hexdigest()[:10]
    return f"{readable or 'asset'}-{digest}"


def source_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def load_reusable_frame_report(report_path: Path, source: Path) -> dict[str, Any] | None:
    if not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(report, dict)
        or report.get("source_signature") != source_signature(source)
        or not isinstance(report.get("frames"), list)
    ):
        return None
    if not all(Path(str(frame.get("path", ""))).is_file() for frame in report["frames"]):
        return None
    report["reused"] = True
    return report


def prepare_video_frames(source: Path, frames_root: Path, relative_path: str) -> dict[str, Any]:
    output_dir = frames_root / queue_key(relative_path)
    report_path = output_dir / "frames.json"
    reusable = load_reusable_frame_report(report_path, source)
    if reusable is not None:
        return reusable
    report = extract_frames(source, output_dir)
    report["relative_path"] = relative_path
    report["source_signature"] = source_signature(source)
    report["reused"] = False
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def understanding_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not str(record.get("description", "")).strip():
        reasons.append("missing_description")
    if not isinstance(record.get("effective_region"), dict):
        reasons.append("missing_effective_region")
    elif not validate_region(record):
        reasons.append("invalid_effective_region")
    if not has_valid_media_dimensions(record):
        reasons.append("invalid_media_dimensions")
    return reasons


def build_queue(
    manifest_path: Path,
    asset_root: Path,
    frames_root: Path,
    *,
    extract_pending_video_frames: bool = True,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    asset_root = asset_root.expanduser().resolve()
    frames_root = frames_root.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    manifest_root = Path(str(manifest.get("asset_root", ""))).expanduser().resolve()
    if manifest_root != asset_root:
        raise ValueError(f"Manifest asset_root does not match: {manifest_root} != {asset_root}")

    records = [
        record
        for record in manifest["assets"]
        if isinstance(record, dict)
        and str(record.get("kind", "")).lower() in {"image", "video"}
    ]
    pending: list[dict[str, Any]] = []
    extraction_errors: list[str] = []
    for record in records:
        relative_path = str(record.get("relative_path", "")).strip()
        reasons = understanding_reasons(record)
        if not reasons:
            continue
        source = (asset_root / relative_path).resolve()
        item: dict[str, Any] = {
            "relative_path": relative_path,
            "kind": str(record.get("kind", "")).lower(),
            "reasons": reasons,
            "source_path": str(source),
            "read_targets": [str(source)] if source.is_file() else [],
            "frame_report": None,
        }
        if item["kind"] == "video" and extract_pending_video_frames and source.is_file():
            try:
                frame_report = prepare_video_frames(source, frames_root, relative_path)
                item["read_targets"] = [
                    str(frame["path"])
                    for frame in frame_report.get("frames", [])
                    if frame.get("path")
                ]
                item["frame_report"] = str(
                    frames_root / queue_key(relative_path) / "frames.json"
                )
                item["representative_frames_reused"] = bool(frame_report.get("reused"))
            except (FrameExtractionError, OSError, ValueError) as exc:
                message = f"{relative_path}: {exc}"
                extraction_errors.append(message)
                item["frame_error"] = str(exc)
        pending.append(item)

    total = len(records)
    pending_count = len(pending)
    complete_count = total - pending_count
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "manifest": str(manifest_path),
        "asset_root": str(asset_root),
        "frames_root": str(frames_root),
        "status": "complete" if not pending else "needs_understanding",
        "summary": {
            "total": total,
            "complete": complete_count,
            "pending": pending_count,
            "progress_percent": round(complete_count * 100.0 / total, 2) if total else 100.0,
        },
        "items": pending,
        "frame_extraction_errors": extraction_errors,
        "resume_policy": (
            "Read every item.read_targets, update description/effective_region in the Manifest, "
            "then rerun this command; completed records disappear from items."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--frames-root", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--no-extract-video-frames", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.expanduser().resolve()
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else manifest_path.parent / "understanding_queue.json"
    )
    frames_root = (
        args.frames_root.expanduser().resolve()
        if args.frames_root
        else manifest_path.parent / "asset_frames"
    )
    try:
        report = build_queue(
            manifest_path,
            args.asset_root,
            frames_root,
            extract_pending_video_frames=not args.no_extract_video_frames,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
