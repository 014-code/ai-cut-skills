#!/usr/bin/env python3
"""Submit/query Aliyun Green CIP video moderation and normalize a compact report.

This local test utility intentionally reads credentials only from environment
variables and never writes them to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


POLICY_VERSION = "visual-moderation-baseline-2026-07-28"
ACTION_RANK = {"PASS": 0, "REVIEW": 1, "BLOCK": 2}
CATEGORY_SCORE_KEYS = ("military", "id_document", "nsfw")

RISK_TO_ACTION = {
    "none": "PASS",
    "low": "REVIEW",
    "medium": "REVIEW",
    "high": "BLOCK",
}

RISK_TO_SCORE = {
    "none": 0.0,
    "low": 0.55,
    "medium": 0.75,
    "high": 0.92,
}

NSFW_KEYWORDS = (
    "sexual",
    "porn",
    "sexy",
    "nudity",
    "nude",
    "breast",
    "cleavage",
    "genital",
    "adult",
    "erotic",
    "色情",
    "性感",
    "裸",
    "暴露",
    "低俗",
)

ID_KEYWORDS = (
    "id",
    "identity",
    "credential",
    "certificate",
    "passport",
    "driver",
    "license",
    "card",
    "身份证",
    "护照",
    "证件",
    "证书",
    "驾驶证",
)

MILITARY_KEYWORDS = (
    "military",
    "army",
    "weapon",
    "gun",
    "rifle",
    "tank",
    "uniform",
    "insignia",
    "soldier",
    "涉军",
    "军",
    "枪",
    "武器",
)


def model_to_dict(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [model_to_dict(item) for item in value]
    if isinstance(value, tuple):
        return [model_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {str(key): model_to_dict(item) for key, item in value.items()}
    if hasattr(value, "to_map"):
        return model_to_dict(value.to_map())
    if hasattr(value, "__dict__"):
        return {
            key: model_to_dict(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and item is not None
        }
    return str(value)


def scrub_sensitive_values(value: Any) -> Any:
    if isinstance(value, list):
        return [scrub_sensitive_values(item) for item in value]
    if isinstance(value, dict):
        scrubbed: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"url", "tempurl"} and isinstance(item, str) and "security-token=" in item.lower():
                scrubbed[key] = "[aliyun-temporary-url-redacted]"
            else:
                scrubbed[key] = scrub_sensitive_values(item)
        return scrubbed
    return value


def get_nested(data: Dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        if key in current:
            current = current[key]
            continue
        alt = key[:1].upper() + key[1:]
        current = current.get(alt)
    return current


def strongest_action(actions: Iterable[str]) -> str:
    result = "PASS"
    for action in actions:
        if ACTION_RANK.get(action, 0) > ACTION_RANK[result]:
            result = action
    return result


def classify_label(label: str, description: str = "") -> List[str]:
    text = f"{label} {description}".lower()
    categories: List[str] = []
    if any(keyword.lower() in text for keyword in NSFW_KEYWORDS):
        categories.append("nsfw")
    if any(keyword.lower() in text for keyword in ID_KEYWORDS):
        categories.append("id_document")
    if any(keyword.lower() in text for keyword in MILITARY_KEYWORDS):
        categories.append("military")
    return categories


def iter_frame_hits(frame_result: Dict[str, Any]) -> Iterable[Tuple[float, str, str, str, float]]:
    frames = get_nested(frame_result, "frames") or []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        offset = float(get_nested(frame, "offset") or 0.0)
        frame_risk = str(get_nested(frame, "riskLevel") or "none")
        results = get_nested(frame, "results") or []
        for result in results:
            if not isinstance(result, dict):
                continue
            service = str(get_nested(result, "service") or "")
            for item in get_nested(result, "result") or []:
                if not isinstance(item, dict):
                    continue
                label = str(get_nested(item, "label") or "")
                description = str(get_nested(item, "description") or "")
                if not label or label == "nonLabel":
                    continue
                confidence = get_nested(item, "confidence")
                try:
                    confidence_value = float(confidence) / 100.0 if confidence is not None and float(confidence) > 1 else float(confidence or 0)
                except Exception:
                    confidence_value = RISK_TO_SCORE.get(frame_risk.lower(), 0.55)
                yield offset, service, label, description, max(confidence_value, RISK_TO_SCORE.get(frame_risk.lower(), 0.55))


def iter_audio_hits(audio_result: Dict[str, Any]) -> Iterable[Tuple[float, float, str, str, str, float]]:
    slice_details = get_nested(audio_result, "sliceDetails") or []
    for detail in slice_details:
        if not isinstance(detail, dict):
            continue
        labels_raw = str(get_nested(detail, "labels") or "")
        if not labels_raw or labels_raw == "nonLabel":
            continue
        description = str(get_nested(detail, "descriptions") or "")
        risk_level = str(get_nested(detail, "riskLevel") or get_nested(audio_result, "riskLevel") or "none").lower()
        start_time = float(get_nested(detail, "startTime") or 0.0)
        end_time = float(get_nested(detail, "endTime") or (start_time + 2.0))
        text = str(get_nested(detail, "text") or "")
        risk_words = str(get_nested(detail, "riskWords") or "")
        score = RISK_TO_SCORE.get(risk_level, 0.55)
        for label in labels_raw.replace(";", ",").split(","):
            label = label.strip()
            if label:
                yield start_time, end_time, label, description, text or risk_words, score


def summarize_result(raw_body: Dict[str, Any], *, include_audio: bool = False) -> Dict[str, Any]:
    data = get_nested(raw_body, "data") or {}
    video_risk = str(get_nested(data, "riskLevel") or "none").lower()
    frame_result = get_nested(data, "frameResult") or {}
    audio_result = (get_nested(data, "audioResult") or {}) if include_audio else {}
    labels: List[str] = []
    dialogue: List[Dict[str, Any]] = []
    redactions: List[Dict[str, Any]] = []
    scores = {key: 0.0 for key in CATEGORY_SCORE_KEYS}
    categories = set()
    actions = ["PASS"]
    unscoped_risks: List[str] = []

    for offset, service, label, description, confidence in iter_frame_hits(frame_result):
        hit_categories = classify_label(label, description)
        labels.append(f"{service}:{label}:{description}".strip(":"))
        if not hit_categories:
            unscoped_risks.append(f"frame:{label}:{description}".strip(":"))
            continue
        hit_action = "BLOCK" if confidence >= 0.85 or video_risk == "high" else "REVIEW"
        actions.append(hit_action)
        for category in hit_categories:
            categories.add(category)
            scores[category] = max(scores[category], confidence)
            redactions.append(
                {
                    "type": "visual_mosaic",
                    "category": category,
                    "reason": f"Aliyun video moderation label={label}; description={description}".strip(),
                    "start_time": max(0.0, offset - 0.5),
                    "end_time": offset + 0.5,
                    "region": "full_frame",
                    "source": "aliyun_green_video",
                    "detector_label": label,
                    "detector_score": round(confidence, 4),
                }
            )

    for start_time, end_time, label, description, text, confidence in iter_audio_hits(audio_result):
        hit_categories = classify_label(label, description)
        labels.append(f"audio:{label}:{description}".strip(":"))
        if not hit_categories:
            unscoped_risks.append(f"audio:{label}:{description}".strip(":"))
            continue
        hit_action = "BLOCK" if confidence >= 0.85 else "REVIEW"
        actions.append(hit_action)
        dialogue.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "text": text[:160],
                "label": label,
                "description": description,
            }
        )
        for category in hit_categories:
            categories.add(category)
            scores[category] = max(scores[category], confidence)
            if category == "nsfw":
                redactions.append(
                    {
                        "type": "audio_mute",
                        "category": category,
                        "reason": f"Aliyun audio moderation label={label}; description={description}".strip(),
                        "start_time": max(0.0, start_time),
                        "end_time": max(start_time, end_time),
                        "source": "aliyun_green_audio",
                        "detector_label": label,
                        "detector_score": round(confidence, 4),
                    }
                )
                redactions.append(
                    {
                        "type": "subtitle_replace",
                        "category": category,
                        "reason": f"Aliyun audio moderation label={label}; description={description}".strip(),
                        "start_time": max(0.0, start_time),
                        "end_time": max(start_time, end_time),
                        "replacement": "[已处理]",
                        "source": "aliyun_green_audio",
                        "detector_label": label,
                        "detector_score": round(confidence, 4),
                    }
                )

    action = strongest_action(actions)
    if categories:
        confidence = max(scores.values())
    else:
        confidence = RISK_TO_SCORE.get(video_risk, 0.0) or (0.74 if action == "PASS" else 0.55)

    reasons = []
    if action == "PASS":
        reasons.append("Aliyun video moderation did not return scoped risk labels.")
    else:
        reasons.append(f"Aliyun video moderation riskLevel={video_risk}.")
    if unscoped_risks:
        reasons.append("Aliyun returned non-scoped risk labels that were kept as evidence but not mapped to military/id_document/nsfw.")

    return {
        "action": action,
        "categories": sorted(categories),
        "confidence": round(min(1.0, max(0.0, confidence)), 4),
        "reasons": reasons,
        "evidence": {
            "scores": {key: round(value, 4) for key, value in scores.items()},
            "labels": labels,
            "ocr": [],
            "dialogue": dialogue,
            "vision": {
                "provider": "aliyun_green_cip",
                "risk_level": video_risk,
                "audio_result_present": bool(audio_result),
                "audio_policy": "included" if include_audio else "ignored_visual_only",
            },
            "policy_hits": ["external.aliyun_green_video"] if action != "PASS" else [],
        },
        "redactions": redactions,
        "policy_version": POLICY_VERSION,
    }


class AliyunGreenVideoClient:
    def __init__(self, region_id: str, endpoint: str, is_vpc: bool = False) -> None:
        from alibabacloud_green20220302.client import Client
        from alibabacloud_tea_openapi.models import Config

        access_key_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
        access_key_secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        if not access_key_id or not access_key_secret:
            raise RuntimeError("Set ALIBABA_CLOUD_ACCESS_KEY_ID and ALIBABA_CLOUD_ACCESS_KEY_SECRET.")

        self.region_id = region_id
        self.endpoint = endpoint
        self.is_vpc = is_vpc
        self.config = Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            connect_timeout=10000,
            read_timeout=10000,
            region_id=region_id,
            endpoint=endpoint,
        )
        self.client = Client(self.config)
        self.upload_token = None
        self.bucket = None

    def submit_url(self, url: str, service: str) -> Dict[str, Any]:
        from alibabacloud_green20220302 import models
        from alibabacloud_tea_util import models as util_models

        request = models.VideoModerationRequest(
            service=service,
            service_parameters=json.dumps({"url": url, "dataId": str(uuid.uuid4())}, ensure_ascii=False),
        )
        response = self.client.video_moderation_with_options(request, util_models.RuntimeOptions())
        return model_to_dict(response.body)

    def submit_local(self, file_path: str, service: str) -> Dict[str, Any]:
        import oss2
        from alibabacloud_green20220302 import models
        from alibabacloud_tea_util import models as util_models

        token_response = self.client.describe_upload_token()
        self.upload_token = token_response.body.data
        auth = oss2.StsAuth(
            self.upload_token.access_key_id,
            self.upload_token.access_key_secret,
            self.upload_token.security_token,
        )
        endpoint = self.upload_token.oss_internal_end_point if self.is_vpc else self.upload_token.oss_internet_end_point
        self.bucket = oss2.Bucket(auth, endpoint, self.upload_token.bucket_name)

        suffix = Path(file_path).suffix.lstrip(".") or "mp4"
        object_name = f"{self.upload_token.file_name_prefix}{uuid.uuid4()}.{suffix}"
        self.bucket.put_object_from_file(object_name, file_path)

        service_parameters = {
            "dataId": str(uuid.uuid4()),
            "ossBucketName": self.upload_token.bucket_name,
            "ossObjectName": object_name,
        }
        request = models.VideoModerationRequest(
            service=service,
            service_parameters=json.dumps(service_parameters, ensure_ascii=False),
        )
        response = self.client.video_moderation_with_options(request, util_models.RuntimeOptions())
        body = model_to_dict(response.body)
        body.setdefault("_uploadedObjectName", object_name)
        body.setdefault("_uploadBucketName", self.upload_token.bucket_name)
        return body

    def query(self, task_id: str, service: str) -> Dict[str, Any]:
        from alibabacloud_green20220302 import models
        from alibabacloud_tea_util import models as util_models

        request = models.VideoModerationResultRequest(
            service=service,
            service_parameters=json.dumps({"taskId": task_id}, ensure_ascii=False),
        )
        response = self.client.video_moderation_result_with_options(request, util_models.RuntimeOptions())
        return model_to_dict(response.body)


def extract_task_id(body: Dict[str, Any]) -> Optional[str]:
    return get_nested(body, "data", "taskId") or get_nested(body, "Data", "TaskId")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video")
    parser.add_argument("--url")
    parser.add_argument("--task-id")
    parser.add_argument("--service", default="videoDetection")
    parser.add_argument("--region-id", default="cn-shanghai")
    parser.add_argument("--endpoint", default="green-cip.cn-shanghai.aliyuncs.com")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--poll-timeout", type=float, default=300.0)
    parser.add_argument("--output", required=True)
    parser.add_argument("--is-vpc", action="store_true")
    parser.add_argument(
        "--include-audio",
        action="store_true",
        help="Include Aliyun audio/dialogue hits in the normalized decision and redactions. Default is visual-only.",
    )
    args = parser.parse_args()

    client = AliyunGreenVideoClient(args.region_id, args.endpoint, args.is_vpc)
    submit_body = None
    task_id = args.task_id
    if args.video:
        submit_body = client.submit_local(args.video, args.service)
        task_id = extract_task_id(submit_body)
    elif args.url:
        submit_body = client.submit_url(args.url, args.service)
        task_id = extract_task_id(submit_body)

    result_body = None
    if task_id:
        if args.poll:
            deadline = time.time() + args.poll_timeout
            while True:
                result_body = client.query(task_id, args.service)
                code = get_nested(result_body, "code")
                message = str(get_nested(result_body, "message") or "")
                data = get_nested(result_body, "data")
                if code == 200 and data:
                    break
                if time.time() >= deadline:
                    break
                print(json.dumps({"task_id": task_id, "code": code, "message": message}, ensure_ascii=False))
                time.sleep(args.poll_interval)
        else:
            result_body = client.query(task_id, args.service)

    report = {
        "provider": "aliyun_green_cip",
        "region_id": args.region_id,
        "endpoint": args.endpoint,
        "service": args.service,
        "audio_policy": "included" if args.include_audio else "ignored_visual_only",
        "task_id": task_id,
        "submitted": scrub_sensitive_values(submit_body),
        "result": scrub_sensitive_values(result_body),
        "decision": summarize_result(result_body or submit_body or {}, include_audio=args.include_audio),
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"task_id": task_id, "action": report["decision"]["action"], "categories": report["decision"]["categories"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
