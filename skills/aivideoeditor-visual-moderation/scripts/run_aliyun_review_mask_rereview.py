#!/usr/bin/env python3
"""Run the review -> mask/mute -> re-review loop for Aliyun Green CIP videos."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).resolve().parent
GREEN_MOD = load_module(HERE / "run_aliyun_video_moderation.py", "aliyun_green_video")
VISUAL_MOD = load_module(HERE / "run_video_visual_moderation.py", "skill_video_visual_moderation")


def get_nested(data: Dict[str, Any], *keys: str) -> Any:
    return GREEN_MOD.get_nested(data, *keys)


def merge_intervals(items: List[Dict[str, Any]], max_gap: float = 0.25) -> List[Dict[str, Any]]:
    if not items:
        return []
    ordered = sorted(items, key=lambda item: (item["start_time"], item["end_time"]))
    merged: List[Dict[str, Any]] = [dict(ordered[0])]
    for item in ordered[1:]:
        previous = merged[-1]
        if item["start_time"] <= previous["end_time"] + max_gap:
            previous["end_time"] = max(previous["end_time"], item["end_time"])
            previous["labels"] = sorted(set(previous.get("labels", []) + item.get("labels", [])))
            previous["confidence"] = max(float(previous.get("confidence", 0.0)), float(item.get("confidence", 0.0)))
        else:
            merged.append(dict(item))
    return merged


def raw_risk_level(result_body: Dict[str, Any]) -> str:
    return str(get_nested(result_body, "data", "riskLevel") or "none").lower()


def raw_action(result_body: Dict[str, Any]) -> str:
    risk_level = raw_risk_level(result_body)
    if risk_level == "none":
        return "PASS"
    if risk_level == "high":
        return "BLOCK"
    return "REVIEW"


def wait_for_result(client: Any, task_id: str, service: str, interval: float, timeout: float) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last_body: Dict[str, Any] = {}
    while True:
        last_body = client.query(task_id, service)
        code = get_nested(last_body, "code")
        data = get_nested(last_body, "data")
        message = str(get_nested(last_body, "message") or "")
        if code == 200 and data:
            return last_body
        if time.time() >= deadline:
            return last_body
        print(json.dumps({"task_id": task_id, "code": code, "message": message}, ensure_ascii=False))
        time.sleep(interval)


def audit_video(
    client: Any,
    video_path: Path,
    service: str,
    poll_interval: float,
    poll_timeout: float,
    include_audio: bool,
) -> Dict[str, Any]:
    submitted = client.submit_local(str(video_path), service)
    task_id = GREEN_MOD.extract_task_id(submitted)
    result = wait_for_result(client, task_id, service, poll_interval, poll_timeout) if task_id else None
    return {
        "task_id": task_id,
        "submitted": GREEN_MOD.scrub_sensitive_values(submitted),
        "result": GREEN_MOD.scrub_sensitive_values(result),
        "decision": GREEN_MOD.summarize_result(result or submitted or {}, include_audio=include_audio),
        "raw_action": raw_action(result or submitted or {}),
        "raw_risk_level": raw_risk_level(result or submitted or {}),
    }


def load_initial_report(path: Path, include_audio: bool) -> Dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    result = report.get("result") or {}
    return {
        "task_id": report.get("task_id"),
        "submitted": GREEN_MOD.scrub_sensitive_values(report.get("submitted")),
        "result": GREEN_MOD.scrub_sensitive_values(result),
        "decision": GREEN_MOD.summarize_result(result, include_audio=include_audio),
        "raw_action": raw_action(result),
        "raw_risk_level": raw_risk_level(result),
    }


def build_provider_redactions(
    result_body: Dict[str, Any],
    scoped_decision: Dict[str, Any],
    visual_window: float,
    include_audio: bool,
) -> List[Dict[str, Any]]:
    frame_result = get_nested(result_body, "data", "frameResult") or {}
    audio_result = (get_nested(result_body, "data", "audioResult") or {}) if include_audio else {}
    frame_intervals: List[Dict[str, Any]] = []
    redactions: List[Dict[str, Any]] = []

    for offset, service, label, description, confidence in GREEN_MOD.iter_frame_hits(frame_result):
        categories = GREEN_MOD.classify_label(label, description)
        category = categories[0] if categories else "provider_risk"
        frame_intervals.append(
            {
                "start_time": max(0.0, float(offset) - visual_window),
                "end_time": float(offset) + visual_window,
                "category": category,
                "labels": [f"{service}:{label}"],
                "confidence": float(confidence),
            }
        )

    for item in merge_intervals(frame_intervals):
        redactions.append(
            {
                "type": "visual_mosaic",
                "category": item["category"],
                "reason": f"Aliyun first review failed; masking provider labels: {', '.join(item['labels'])}",
                "start_time": round(item["start_time"], 3),
                "end_time": round(item["end_time"], 3),
                "region": "full_frame",
                "source": "aliyun_green_rereview_loop",
                "detector_labels": item["labels"],
                "detector_score": round(float(item["confidence"]), 4),
            }
        )

    for start_time, end_time, label, description, text, confidence in GREEN_MOD.iter_audio_hits(audio_result):
        categories = GREEN_MOD.classify_label(label, description)
        category = categories[0] if categories else "provider_risk"
        redactions.append(
            {
                "type": "audio_mute",
                "category": category,
                "reason": f"Aliyun first review failed on audio label={label}; description={description}",
                "start_time": round(max(0.0, start_time), 3),
                "end_time": round(max(start_time, end_time), 3),
                "source": "aliyun_green_rereview_loop",
                "detector_label": label,
                "detector_score": round(float(confidence), 4),
            }
        )
        redactions.append(
            {
                "type": "subtitle_replace",
                "category": category,
                "reason": f"Masking subtitle band for risky audio text label={label}.",
                "start_time": round(max(0.0, start_time), 3),
                "end_time": round(max(start_time, end_time), 3),
                "replacement": "[已处理]",
                "source": "aliyun_green_rereview_loop",
                "detector_label": label,
                "detector_score": round(float(confidence), 4),
            }
        )

    for item in scoped_decision.get("redactions") or []:
        if item.get("type") in {"audio_mute", "subtitle_replace", "visual_mosaic", "visual_blur", "text_mosaic"}:
            redactions.append(dict(item))

    return dedupe_redactions(redactions)


def redaction_identity(item: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        item.get("type"),
        item.get("category"),
        round(float(item.get("start_time", 0.0)), 3),
        round(float(item.get("end_time", 0.0)), 3),
        item.get("region"),
        item.get("detector_label"),
        tuple(item.get("detector_labels") or []),
    )


def dedupe_redactions(redactions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for item in redactions:
        key = redaction_identity(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def visual_redactions(redactions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in redactions if item.get("type") in {"visual_mosaic", "visual_blur", "text_mosaic", "subtitle_replace"}]


def audio_mutes(redactions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in redactions if item.get("type") == "audio_mute"]


def escape_filter_time(value: Any) -> str:
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def build_audio_filter(mutes: List[Dict[str, Any]]) -> Optional[str]:
    filters = []
    for item in mutes:
        start = escape_filter_time(item.get("start_time", 0.0))
        end = escape_filter_time(item.get("end_time", item.get("start_time", 0.0)))
        filters.append(f"volume=enable='between(t,{start},{end})':volume=0")
    return ",".join(filters) if filters else None


def mux_audio(
    ffmpeg: Path,
    visual_video: Path,
    source_video: Path,
    output_video: Path,
    mutes: List[Dict[str, Any]],
) -> None:
    audio_filter = build_audio_filter(mutes)
    output_video.parent.mkdir(parents=True, exist_ok=True)
    if audio_filter:
        args = [
            str(ffmpeg),
            "-y",
            "-i",
            str(visual_video),
            "-i",
            str(source_video),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "20",
            "-preset",
            "veryfast",
            "-af",
            audio_filter,
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_video),
        ]
    else:
        args = [
            str(ffmpeg),
            "-y",
            "-i",
            str(visual_video),
            "-i",
            str(source_video),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "20",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_video),
        ]
    completed = subprocess.run(args, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with exit code {completed.returncode}")


def apply_redactions(
    source_video: Path,
    output_video: Path,
    redactions: List[Dict[str, Any]],
    ffmpeg: Path,
) -> Dict[str, Any]:
    visual_items = visual_redactions(redactions)
    mute_items = audio_mutes(redactions)
    visual_only = output_video.with_name(output_video.stem + "_visual_only.mp4")

    if visual_items:
        visual_report = VISUAL_MOD.render_masked_video(
            source_video,
            visual_only,
            visual_items,
            video_action="REVIEW",
            mask_actions={"REVIEW", "BLOCK"},
        )
    else:
        shutil.copyfile(source_video, visual_only)
        visual_report = {
            "path": str(visual_only),
            "processed_frames": None,
            "masked_frames": 0,
            "audio_preserved": False,
        }

    mux_audio(ffmpeg, visual_only, source_video, output_video, mute_items)
    return {
        "output": str(output_video),
        "visual_only": str(visual_only),
        "visual_report": visual_report,
        "visual_redaction_count": len(visual_items),
        "audio_mute_count": len(mute_items),
    }


def find_ffmpeg() -> Optional[Path]:
    path_value = shutil.which("ffmpeg")
    if path_value:
        return Path(path_value)
    candidates = (
        Path.cwd() / "material_remix_desktop_source" / "bin" / "ffmpeg.exe",
        Path.cwd() / "bin" / "ffmpeg.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--initial-report")
    parser.add_argument("--service", default="videoDetection")
    parser.add_argument("--region-id", default="cn-shanghai")
    parser.add_argument("--endpoint", default="green-cip.cn-shanghai.aliyuncs.com")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--poll-timeout", type=float, default=900.0)
    parser.add_argument("--visual-window", type=float, default=2.0)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--output", required=True)
    parser.add_argument("--is-vpc", action="store_true")
    parser.add_argument(
        "--include-audio",
        action="store_true",
        help="Include Aliyun audio/dialogue hits and emit audio mute/subtitle redactions. Default is visual-only.",
    )
    parser.add_argument(
        "--trigger-on-raw",
        action="store_true",
        help="Process whenever Aliyun raw action is not PASS. Default triggers on scoped decision only.",
    )
    args = parser.parse_args()

    video_path = Path(args.video)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = Path(args.ffmpeg) if args.ffmpeg else find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Pass --ffmpeg or run from the backend repo with material_remix_desktop_source/bin/ffmpeg.exe.")

    if args.initial_report:
        initial = load_initial_report(Path(args.initial_report), args.include_audio)
        initial_source = "reused_report"
        client = None
    else:
        client = GREEN_MOD.AliyunGreenVideoClient(args.region_id, args.endpoint, args.is_vpc)
        initial = audit_video(client, video_path, args.service, args.poll_interval, args.poll_timeout, args.include_audio)
        initial_source = "live_review"

    redactions = []
    processed: Optional[Dict[str, Any]] = None
    rereview: Optional[Dict[str, Any]] = None
    processed_video = output_dir / "processed_for_rereview.mp4"

    should_process = initial["raw_action"] != "PASS" if args.trigger_on_raw else initial["decision"]["action"] != "PASS"
    if should_process:
        redactions = build_provider_redactions(
            initial.get("result") or {},
            initial.get("decision") or {},
            args.visual_window,
            args.include_audio,
        )
        redactions_path = output_dir / "redactions_for_rereview.json"
        redactions_path.write_text(json.dumps({"redactions": redactions}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        processed = apply_redactions(video_path, processed_video, redactions, ffmpeg)
        if client is None:
            client = GREEN_MOD.AliyunGreenVideoClient(args.region_id, args.endpoint, args.is_vpc)
        rereview = audit_video(client, processed_video, args.service, args.poll_interval, args.poll_timeout, args.include_audio)

    report = {
        "flow": "review_mask_rereview",
        "video": str(video_path),
        "audio_policy": "included" if args.include_audio else "ignored_visual_only",
        "trigger_policy": "raw_provider_action" if args.trigger_on_raw else "scoped_business_action",
        "initial_source": initial_source,
        "initial": initial,
        "redactions": redactions,
        "processed": processed,
        "rereview": rereview,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "initial_raw_action": initial["raw_action"],
                "initial_scoped_action": initial["decision"]["action"],
                "redactions": len(redactions),
                "processed_video": processed["output"] if processed else None,
                "rereview_raw_action": rereview["raw_action"] if rereview else None,
                "rereview_scoped_action": rereview["decision"]["action"] if rereview else None,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
