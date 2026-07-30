#!/usr/bin/env python3
"""Run a minimal visual moderation pipeline for skill testing.

The script is intentionally dependency-light. It can run built-in regression
fixtures with only the Python standard library. If langgraph is installed, the
same nodes can be executed as a LangGraph state machine.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


POLICY_VERSION = "visual-moderation-baseline-2026-07-29"
CATEGORIES = ("military", "political", "nsfw")
ACTION_RANK = {"PASS": 0, "REVIEW": 1, "BLOCK": 2}
VISUAL_MASK_TYPES = {"visual_mosaic", "visual_blur"}
LOCAL_VISUAL_MAX_AREA = 0.38
LOCAL_VISUAL_MAX_WIDTH = 0.94
LOCAL_VISUAL_MAX_HEIGHT = 0.78
NSFW_BODY_PART_MAX_AREA = 0.28
NSFW_BODY_PART_MAX_WIDTH = 0.78
NSFW_BODY_PART_MAX_HEIGHT = 0.62
FULL_FRAME_AREA = 0.82
FULL_FRAME_WIDTH = 0.96
FULL_FRAME_HEIGHT = 0.88

MILITARY_KEYWORDS = (
    "military",
    "army",
    "soldier",
    "uniform",
    "camouflage",
    "insignia",
    "weapon",
    "rifle",
    "gun",
    "tank",
    "missile",
    "navy",
    "air force",
    "军装",
    "军徽",
    "武器",
    "迷彩",
    "士兵",
)

REAL_MILITARY_KEYWORDS = (
    "pla",
    "people's liberation army",
    "liberation army",
    "active duty",
    "classified",
    "military base",
    "operation plan",
    "unit number",
    "recruitment",
    "propaganda",
    "人民解放军",
    "解放军",
    "武警",
    "军区",
    "战区",
    "集团军",
    "部队番号",
    "军事基地",
    "作战计划",
    "军事机密",
    "军演",
    "征兵",
    "军宣",
)

FICTIONAL_CONTEXT_KEYWORDS = (
    "fictional",
    "purely fictional",
    "anime",
    "animated",
    "game",
    "toy",
    "historical",
    "fantasy",
    "web drama",
    "webcomic",
    "costume",
    "period attire",
    "纯属虚构",
    "请勿模仿",
    "动漫",
    "动画",
    "游戏",
    "玩具",
    "古风",
    "古代",
    "玄幻",
    "历史剧",
    "漫剧",
)

POLITICAL_KEYWORDS = (
    "political",
    "politics",
    "government",
    "party",
    "election",
    "president",
    "prime minister",
    "parliament",
    "ministry",
    "official",
    "national emblem",
    "national flag",
    "political slogan",
    "protest",
    "demonstration",
    "涉政",
    "政治",
    "政府",
    "政党",
    "选举",
    "总统",
    "首相",
    "总理",
    "议会",
    "部长",
    "官员",
    "国徽",
    "国旗",
    "党徽",
    "政治口号",
    "游行",
    "示威",
    "抗议",
)

NSFW_KEYWORDS = (
    "nude",
    "nudity",
    "porn",
    "sexual",
    "sex",
    "genitals",
    "breast",
    "nipple",
    "lingerie",
    "underwear",
    "裸",
    "裸体",
    "色情",
    "性感",
    "内衣",
)

SOFT_NSFW_KEYWORDS = (
    "lingerie",
    "underwear",
    "bikini",
    "cleavage",
    "suggestive",
    "性感",
    "内衣",
    "泳装",
    "擦边",
)

MILITARY_UNIT_RE = re.compile(r"(?:第?\d{2,4}(?:集团军|部队|旅|师|团|营)|\d{5,}部队)")
REAL_POLITICAL_KEYWORDS = (
    "communist party",
    "state council",
    "public security bureau",
    "central committee",
    "state leader",
    "political propaganda",
    "separatism",
    "sovereignty",
    "territorial dispute",
    "sensitive political event",
    "中国共产党",
    "共产党",
    "中华人民共和国",
    "国务院",
    "中央委员会",
    "国家主席",
    "总书记",
    "国家领导人",
    "政府机关",
    "公安局",
    "法院",
    "检察院",
    "政治宣传",
    "分裂国家",
    "台独",
    "港独",
    "藏独",
    "疆独",
    "主权争议",
    "领土争议",
    "敏感政治事件",
)
POLITICAL_OFFICE_RE = re.compile(
    r"(?:国家主席|总书记|国务院总理|外交部|公安部|国防部|人民代表大会|政协|政府机关|党委|纪委|法院|检察院)"
)


def clamp_score(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 1.0 and score <= 100.0:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def flatten_text(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        texts: List[str] = []
        for item in value.values():
            texts.extend(flatten_text(item))
        return texts
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        texts = []
        for item in value:
            texts.extend(flatten_text(item))
        return texts
    return [str(value)]


def contains_any(texts: Iterable[str], keywords: Iterable[str]) -> bool:
    joined = " ".join(str(item).lower() for item in texts)
    return any(keyword.lower() in joined for keyword in keywords)


def category_names(value: Any, scores: Optional[Dict[str, Any]] = None) -> List[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [key for key, raw_score in value.items() if key in CATEGORIES and clamp_score(raw_score) >= 0.55]
    if isinstance(value, str):
        return [value] if value in CATEGORIES else []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        names = []
        for item in value:
            if isinstance(item, str) and item in CATEGORIES:
                if not scores or clamp_score(scores.get(item, 0.0)) >= 0.55:
                    names.append(item)
        return names
    return []


def merge_score(scores: Dict[str, float], category: str, value: Any) -> None:
    scores[category] = max(scores.get(category, 0.0), clamp_score(value))


def normalize_dialogue_segments(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"text": value}]
    if isinstance(value, dict):
        if "text" in value:
            return [value]
        segments: List[Dict[str, Any]] = []
        for item in value.values():
            segments.extend(normalize_dialogue_segments(item))
        return segments
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        segments = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("line")
                if text:
                    segment = dict(item)
                    segment["text"] = str(text)
                    segments.append(segment)
            else:
                segments.extend(normalize_dialogue_segments(item))
        return segments
    return [{"text": str(value)}]


def normalize_signals(payload: Dict[str, Any]) -> Dict[str, Any]:
    cv = payload.get("cv") or {}
    vision = payload.get("vision") or {}
    scores = {category: 0.0 for category in CATEGORIES}
    labels: List[str] = []
    ocr: List[str] = []
    dialogue_segments: List[Dict[str, Any]] = []

    for source in (payload, cv, vision):
        labels.extend(flatten_text(source.get("labels")))
        labels.extend(flatten_text(source.get("objects")))
        labels.extend(flatten_text(source.get("visible_objects")))
        ocr.extend(flatten_text(source.get("ocr")))
        ocr.extend(flatten_text(source.get("ocr_text")))
        for dialogue_field in ("dialogue", "subtitles", "subtitle", "transcript", "asr", "lines"):
            dialogue_segments.extend(normalize_dialogue_segments(source.get(dialogue_field)))

    vision_evidence = vision.get("evidence") if isinstance(vision, dict) else None
    if isinstance(vision_evidence, dict):
        labels.extend(flatten_text(vision_evidence.get("visible_objects")))
        labels.extend(flatten_text(vision_evidence.get("objects")))
        ocr.extend(flatten_text(vision_evidence.get("ocr")))
        ocr.extend(flatten_text(vision_evidence.get("ocr_text")))
        for dialogue_field in ("dialogue", "subtitles", "subtitle", "transcript", "asr", "text"):
            dialogue_segments.extend(normalize_dialogue_segments(vision_evidence.get(dialogue_field)))

    score_aliases = {
        "military": ("military", "military_score", "weapon", "gun", "uniform", "insignia"),
        "political": (
            "political",
            "political_score",
            "politics",
            "government",
            "party",
            "election",
            "leader",
            "official",
            "flag",
            "emblem",
            "protest",
        ),
        "nsfw": ("nsfw", "nsfw_score", "nudity", "nude", "porn", "sexual", "adult"),
    }

    vision_scores = vision.get("scores") or {}
    for source in (payload, cv, vision, vision_scores):
        if not isinstance(source, dict):
            continue
        for category, aliases in score_aliases.items():
            for alias in aliases:
                if alias in source:
                    merge_score(scores, category, source.get(alias))

    dialogue = [str(segment.get("text", "")).strip() for segment in dialogue_segments if segment.get("text")]
    all_text = labels + ocr + dialogue
    fictional_context = contains_any(all_text, FICTIONAL_CONTEXT_KEYWORDS)
    real_military_hit = contains_any(all_text, REAL_MILITARY_KEYWORDS) or any(
        MILITARY_UNIT_RE.search(text) for text in all_text
    )
    real_political_hit = contains_any(all_text, REAL_POLITICAL_KEYWORDS) or any(
        POLITICAL_OFFICE_RE.search(text) for text in all_text
    )
    if contains_any(labels, MILITARY_KEYWORDS) and not fictional_context:
        merge_score(scores, "military", 0.62)
    if real_military_hit:
        merge_score(scores, "military", 0.78)
    if contains_any(all_text, POLITICAL_KEYWORDS) and not fictional_context:
        merge_score(scores, "political", 0.62)
    if real_political_hit:
        merge_score(scores, "political", 0.78)
    if contains_any(all_text, NSFW_KEYWORDS):
        merge_score(scores, "nsfw", 0.68)

    risk_level = str(vision.get("risk_level", "")).lower()
    if risk_level == "review":
        for category in category_names(vision.get("categories"), vision_scores):
            if category in scores:
                merge_score(scores, category, vision.get("confidence", 0.62))
    if risk_level == "block":
        for category in category_names(vision.get("categories"), vision_scores):
            if category in scores:
                merge_score(scores, category, vision.get("confidence", 0.9))

    return {
        "scores": scores,
        "labels": dedupe(labels),
        "ocr": dedupe(ocr),
        "dialogue": dedupe(dialogue),
        "dialogue_segments": dialogue_segments,
        "fictional_context": fictional_context,
        "real_military_hit": real_military_hit,
        "real_political_hit": real_political_hit,
        "vision": vision or None,
    }


def dedupe(items: Iterable[Any]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def stronger_action(current: str, candidate: str) -> str:
    return candidate if ACTION_RANK[candidate] > ACTION_RANK[current] else current


def sensitive_text_snippets(texts: Iterable[str], category: str) -> List[str]:
    snippets = []
    for text in texts:
        if category == "political":
            if contains_any([text], POLITICAL_KEYWORDS) or contains_any([text], REAL_POLITICAL_KEYWORDS) or POLITICAL_OFFICE_RE.search(text):
                snippets.append(text)
        elif category == "military":
            if contains_any([text], REAL_MILITARY_KEYWORDS) or MILITARY_UNIT_RE.search(text):
                snippets.append(text)
        elif category == "nsfw":
            if contains_any([text], NSFW_KEYWORDS) or contains_any([text], SOFT_NSFW_KEYWORDS):
                snippets.append(text)
    return dedupe(snippets)


def normalize_bbox(value: Any) -> Optional[List[float]]:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        bbox = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if max(bbox) > 1.0:
        bbox = [item / 1000.0 if item > 1.0 and item <= 1000.0 else item for item in bbox]
    x1, y1, x2, y2 = [max(0.0, min(1.0, item)) for item in bbox]
    if x2 <= x1 or y2 <= y1:
        return None
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def bbox_dimensions(bbox: List[float]) -> Tuple[float, float, float]:
    width = max(0.0, bbox[2] - bbox[0])
    height = max(0.0, bbox[3] - bbox[1])
    return width, height, width * height


def local_visual_bbox_rejection_reason(bbox: Any, category: Optional[str] = None) -> Optional[str]:
    normalized = normalize_bbox(bbox)
    if not normalized:
        return "missing reliable bbox"
    width, height, area = bbox_dimensions(normalized)
    if area >= FULL_FRAME_AREA or (width >= FULL_FRAME_WIDTH and height >= FULL_FRAME_HEIGHT):
        return "bbox is full-frame sized"
    if category == "nsfw":
        if area > NSFW_BODY_PART_MAX_AREA or width > NSFW_BODY_PART_MAX_WIDTH or height > NSFW_BODY_PART_MAX_HEIGHT:
            return "NSFW bbox is too broad; only the exposed/suggestive body part may be masked"
    elif area > LOCAL_VISUAL_MAX_AREA or width > LOCAL_VISUAL_MAX_WIDTH or height > LOCAL_VISUAL_MAX_HEIGHT:
        return "visual bbox is too broad for targeted masking"
    return None


def is_local_visual_bbox(bbox: Any, category: Optional[str] = None) -> bool:
    return local_visual_bbox_rejection_reason(bbox, category) is None


def sanitize_visual_redaction(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(item)
    rtype = normalized.get("type")
    if rtype not in VISUAL_MASK_TYPES:
        return normalized

    category = normalized.get("category")
    if normalized.get("region") == "full_frame":
        normalized["type"] = "visual_localization_required"
        normalized["reason"] = (
            str(normalized.get("reason") or "Visual mask needs localization.")
            + " Full-frame visual masking is prohibited."
        )
        normalized.pop("bbox", None)
        normalized.pop("bbox_keyframes", None)
        normalized.pop("keyframes", None)
        return normalized

    bbox = normalize_bbox(normalized.get("bbox"))
    bbox_reason = local_visual_bbox_rejection_reason(bbox, category) if bbox else "missing reliable bbox"
    if bbox and bbox_reason is None:
        normalized["bbox"] = bbox
        return normalized

    keyframes = []
    rejected_keyframes = 0
    for raw_keyframe in normalized.get("bbox_keyframes") or normalized.get("keyframes") or []:
        if not isinstance(raw_keyframe, dict):
            rejected_keyframes += 1
            continue
        timestamp = raw_keyframe.get("time")
        if timestamp is None:
            timestamp = raw_keyframe.get("timestamp")
        keyframe_bbox = normalize_bbox(raw_keyframe.get("bbox"))
        if timestamp is None or not keyframe_bbox or local_visual_bbox_rejection_reason(keyframe_bbox, category):
            rejected_keyframes += 1
            continue
        keyframes.append({"time": round(float(timestamp), 3), "bbox": keyframe_bbox})
    if keyframes:
        normalized["bbox_keyframes"] = sorted(keyframes, key=lambda keyframe: keyframe["time"])
        normalized.pop("keyframes", None)
        normalized.pop("bbox", None)
        if rejected_keyframes:
            normalized["reason"] = (
                str(normalized.get("reason") or "Visual mask target was localized.")
                + f" Dropped {rejected_keyframes} over-broad keyframe(s)."
            )
        return normalized

    reason = bbox_reason or "bbox_keyframes are missing or too broad"
    normalized["type"] = "visual_localization_required"
    normalized["reason"] = (
        str(normalized.get("reason") or "Visual mask needs localization.")
        + f" {reason}; no visual mask is emitted."
    )
    normalized.pop("bbox", None)
    normalized.pop("bbox_keyframes", None)
    normalized.pop("keyframes", None)
    return normalized


def vision_redaction_targets(vision: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(vision, dict):
        return []
    raw_targets = vision.get("redaction_targets") or vision.get("redactions")
    if not raw_targets and isinstance(vision.get("evidence"), dict):
        raw_targets = vision["evidence"].get("redaction_targets")
    if not raw_targets:
        return []
    if isinstance(raw_targets, dict):
        raw_targets = [raw_targets]
    targets = []
    for target in raw_targets:
        if not isinstance(target, dict):
            continue
        category = target.get("category")
        if category not in CATEGORIES:
            continue
        bbox = normalize_bbox(target.get("bbox"))
        redaction = {
            "type": target.get("type") or "visual_mosaic",
            "category": category,
            "reason": target.get("reason") or "Vision model identified a sensitive region.",
        }
        if bbox:
            redaction["bbox"] = bbox
        if target.get("start_time") is not None:
            redaction["start_time"] = target.get("start_time")
        if target.get("end_time") is not None:
            redaction["end_time"] = target.get("end_time")
        targets.append(sanitize_visual_redaction(redaction))
    return targets


def build_redactions(action: str, categories: List[str], signals: Dict[str, Any], policy_hits: List[str]) -> List[Dict[str, Any]]:
    if action == "PASS":
        return []
    redactions = vision_redaction_targets(signals.get("vision"))
    ocr = signals.get("ocr", [])
    dialogue = signals.get("dialogue", [])
    dialogue_segments = signals.get("dialogue_segments", [])

    for category in categories:
        for snippet in sensitive_text_snippets(ocr, category):
            redactions.append(
                {
                    "type": "text_mosaic",
                    "category": category,
                    "reason": f"OCR text contains {category} evidence.",
                    "text": snippet,
                    "replacement": "[已处理]",
                }
            )
        for segment in dialogue_segments:
            text = str(segment.get("text", ""))
            if text not in sensitive_text_snippets([text], category):
                continue
            redactions.append(
                {
                    "type": "subtitle_replace",
                    "category": category,
                    "reason": f"Dialogue/subtitle contains {category} evidence.",
                    "text": text,
                    "start_time": segment.get("start_time"),
                    "end_time": segment.get("end_time"),
                    "replacement": "[已处理]",
                }
            )
            redactions.append(
                {
                    "type": "audio_mute",
                    "category": category,
                    "reason": f"Dialogue audio contains {category} evidence.",
                    "start_time": segment.get("start_time"),
                    "end_time": segment.get("end_time"),
                }
            )

    has_visual_target = any(
        item.get("type") in VISUAL_MASK_TYPES
        and (
            is_local_visual_bbox(item.get("bbox"), item.get("category"))
            or any(
                is_local_visual_bbox(keyframe.get("bbox"), item.get("category"))
                for keyframe in item.get("bbox_keyframes") or []
                if isinstance(keyframe, dict)
            )
        )
        for item in redactions
    )
    has_visual_or_ocr_evidence = bool(signals.get("labels") or signals.get("ocr") or signals.get("vision"))
    if (
        not has_visual_target
        and has_visual_or_ocr_evidence
        and action == "BLOCK"
        and any(hit.startswith(("nsfw.", "political.", "military.")) for hit in policy_hits)
    ):
        redactions.append(
            {
                "type": "visual_localization_required",
                "category": categories[0] if categories else "unknown",
                "reason": "Confirmed block decision without a reliable bounding box; full-frame masking is prohibited.",
            }
        )
    return redactions


def policy_decision(signals: Dict[str, Any]) -> Dict[str, Any]:
    scores = signals["scores"]
    labels = signals["labels"]
    ocr = signals["ocr"]
    dialogue = signals.get("dialogue", [])
    all_text = labels + ocr + dialogue
    categories: List[str] = []
    reasons: List[str] = []
    policy_hits: List[str] = []
    action = "PASS"
    confidence = 0.74

    real_political_hit = bool(signals.get("real_political_hit"))
    fictional_context = bool(signals.get("fictional_context"))
    visual_political_hit = contains_any(labels, POLITICAL_KEYWORDS)
    political_block_hit = contains_any(
        all_text,
        (
            "political propaganda",
            "separatism",
            "territorial dispute",
            "state secret",
            "政治宣传",
            "分裂国家",
            "主权争议",
            "领土争议",
            "国家机密",
            "煽动",
        ),
    )
    if political_block_hit or (scores["political"] >= 0.85 and real_political_hit):
        action = stronger_action(action, "BLOCK")
        categories.append("political")
        confidence = max(confidence, scores["political"], 0.9)
        reasons.append("Concrete real-world political-sensitive content is high confidence.")
        policy_hits.append("political.block.real_world_sensitive_or_propaganda")
    elif real_political_hit or (scores["political"] >= 0.55 and visual_political_hit and not fictional_context):
        action = stronger_action(action, "REVIEW")
        categories.append("political")
        confidence = max(confidence, scores["political"], 0.7)
        reasons.append("Possible concrete real-world political-sensitive evidence requires review.")
        policy_hits.append("political.review.real_symbol_leader_or_event")

    explicit_nsfw = contains_any(all_text, ("porn", "genitals", "sexual act", "sexual service", "裸体", "色情", "性行为"))
    if scores["nsfw"] >= 0.85 or explicit_nsfw:
        action = stronger_action(action, "BLOCK")
        categories.append("nsfw")
        confidence = max(confidence, scores["nsfw"], 0.9)
        reasons.append("Explicit sexual, nudity, or sexual-dialogue evidence is present.")
        policy_hits.append("nsfw.block.explicit_or_high_confidence")
    elif scores["nsfw"] >= 0.55 or contains_any(all_text, SOFT_NSFW_KEYWORDS):
        action = stronger_action(action, "REVIEW")
        categories.append("nsfw")
        confidence = max(confidence, scores["nsfw"], 0.68)
        reasons.append("Suggestive or ambiguous adult visual evidence requires review.")
        policy_hits.append("nsfw.review.suggestive_or_medium_confidence")

    real_military_hit = bool(signals.get("real_military_hit"))
    visual_military_hit = contains_any(labels, MILITARY_KEYWORDS)
    propaganda_hit = contains_any(all_text, ("recruitment", "propaganda", "combat", "battle", "军事宣传", "征兵", "战斗"))
    if scores["military"] >= 0.85 and (propaganda_hit or real_military_hit):
        action = stronger_action(action, "BLOCK")
        categories.append("military")
        confidence = max(confidence, scores["military"], 0.9)
        reasons.append("Concrete real-world military-sensitive content appears promotional, operational, or combat-related.")
        policy_hits.append("military.block.propaganda_or_combat")
    elif real_military_hit or (scores["military"] >= 0.55 and visual_military_hit and not fictional_context):
        action = stronger_action(action, "REVIEW")
        categories.append("military")
        confidence = max(confidence, scores["military"], 0.7)
        reasons.append("Possible concrete real-world military-sensitive evidence requires review.")
        policy_hits.append("military.review.uniform_weapon_or_insignia")

    categories = [category for category in CATEGORIES if category in set(categories)]
    if action == "PASS":
        reasons = ["No scoped visual safety signals were detected."]
        confidence = 0.74

    return {
        "action": action,
        "categories": categories,
        "confidence": round(clamp_score(confidence), 4),
        "reasons": dedupe(reasons),
        "evidence": {
            "scores": {key: round(value, 4) for key, value in scores.items()},
            "labels": labels,
            "ocr": ocr,
            "dialogue": dialogue,
            "vision": signals.get("vision"),
            "policy_hits": policy_hits,
        },
        "redactions": build_redactions(action, categories, signals, policy_hits),
        "policy_version": POLICY_VERSION,
    }


def sidecar_path_for(image_path: Path) -> Optional[Path]:
    candidates = [
        image_path.with_suffix(".visual_moderation.json"),
        image_path.with_suffix(".moderation.json"),
        image_path.with_suffix(".json"),
    ]
    return next((path for path in candidates if path.exists()), None)


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"JSON fixture must be an object: {path}")
    return data


def mock_payload_from_name(path: Path) -> Dict[str, Any]:
    name = path.name.lower()
    labels: List[str] = []
    cv: Dict[str, Any] = {"labels": labels, "ocr": []}
    if any(token in name for token in ("military", "army", "soldier", "weapon", "jun", "gun")):
        cv["military"] = 0.78
        labels.extend(["military uniform", "weapon"])
    if any(token in name for token in ("political", "politics", "government", "leader", "shezhen", "shezheng")):
        cv["political"] = 0.88
        cv["ocr"] = ["政治宣传", "国家领导人讲话"]
    if any(token in name for token in ("nsfw", "nude", "porn", "sexual")):
        cv["nsfw"] = 0.9
        labels.append("nudity")
    return {"image_path": str(path), "cv": cv, "context": {"provider": "mock_filename"}}


def image_to_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def extract_json_object(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Vision model response JSON must be an object.")
    return parsed


def call_openai_compatible_vision(
    path: Path,
    timeout: int,
    provider: str,
    model_override: Optional[str] = None,
) -> Dict[str, Any]:
    if provider == "dashscope":
        base_url = (
            os.getenv("DASHSCOPE_BASE_URL")
            or os.getenv("VISMOD_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("VISMOD_API_KEY")
        model = model_override or os.getenv("DASHSCOPE_MODEL") or os.getenv("VISMOD_MODEL") or "qwen3-vl-flash"
    else:
        base_url = os.getenv("VISMOD_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("VISMOD_API_KEY") or os.getenv("OPENAI_API_KEY")
        model = model_override or os.getenv("VISMOD_MODEL") or os.getenv("OPENAI_MODEL")
    if not base_url or not api_key or not model:
        raise RuntimeError(
            "Set provider credentials and model. DashScope needs DASHSCOPE_API_KEY; "
            "openai-compatible needs VISMOD_BASE_URL, VISMOD_API_KEY, and VISMOD_MODEL or --model."
        )

    prompt = (
        "Inspect this image only for these scoped risks: "
        "1) concrete real-world military-sensitive content, such as real units, uniforms, insignia, weapons, "
        "vehicles, maps, operations, recruitment, propaganda, bases, or secrets; "
        "2) concrete real-world political-sensitive content, such as real political leaders, party/government "
        "organs, state emblems or flags, political slogans, protests, separatism, sovereignty or territorial "
        "dispute content, propaganda, or sensitive political events; "
        "3) sexual/NSFW content. "
        "Read visible overlay text, OCR text, and subtitles when possible. "
        "Do not treat fictional, historical, game, anime, costume, or generic ancient city-defense scenes as "
        "military or political violations unless real-world identifiers are present. "
        "Return strict JSON with keys: categories, scores, risk_level, confidence, reason, evidence, "
        "redaction_targets. Categories must be only military, political, nsfw. Scores must be 0 to 1. "
        "redaction_targets should include type, category, reason, and normalized bbox [x1,y1,x2,y2] when a "
        "visual or text region needs mosaic. For nsfw redaction targets, never target the face, whole person, "
        "whole chest, belly, covered buttocks, or ordinary low-cut clothing. Only return a visual nsfw target "
        "for explicit exposed intimate parts or an obvious central cleavage groove/gap; for obvious cleavage, "
        "treat visible cleavage, a visible chest gap, or a deep V opening with a clear central groove as REVIEW and "
        "return a narrow strip around the middle groove, not the whole breast area, clothing, money, props, "
        "subtitles, or the body outline. Do not return PASS when the crop or frame clearly shows a cleavage "
        "groove/gap, even if nipples are covered; ordinary low-cut clothing without a clear groove should not "
        "produce an nsfw redaction target. "
        "Do not decide the final PASS/REVIEW/BLOCK business policy."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_to_data_url(path)}},
                ],
            }
        ],
    }

    endpoint = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vision endpoint HTTP {exc.code}: {detail}") from exc

    content = body["choices"][0]["message"]["content"]
    return extract_json_object(content)


def load_input_node(state: Dict[str, Any]) -> Dict[str, Any]:
    path = state.get("input_path")
    if not path:
        return state

    input_path = Path(path)
    if input_path.suffix.lower() == ".json":
        state["payload"] = read_json(input_path)
    else:
        sidecar = sidecar_path_for(input_path)
        if sidecar:
            state["payload"] = read_json(sidecar)
            state["payload"]["image_path"] = str(input_path)
            state["payload"]["sidecar_path"] = str(sidecar)
        elif state.get("provider") == "mock":
            state["payload"] = mock_payload_from_name(input_path)
        else:
            state["payload"] = {"image_path": str(input_path), "cv": {}, "context": {}}
    return state


def cv_node(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = state.get("payload") or {}
    state["signals"] = normalize_signals(payload)
    return state


def vision_node(state: Dict[str, Any]) -> Dict[str, Any]:
    provider = state.get("provider")
    input_path = Path(state["input_path"]) if state.get("input_path") else None
    payload = state.get("payload") or {}
    if provider in {"openai-compatible", "dashscope"} and input_path and input_path.suffix.lower() != ".json":
        payload["vision"] = call_openai_compatible_vision(
            input_path,
            state.get("timeout", 60),
            provider,
            state.get("model"),
        )
        state["payload"] = payload
    state["signals"] = normalize_signals(payload)
    return state


def fusion_node(state: Dict[str, Any]) -> Dict[str, Any]:
    signals = state.get("signals") or normalize_signals(state.get("payload") or {})
    state["signals"] = signals
    state["fusion"] = {
        "scores": signals["scores"],
        "needs_review": any(value >= 0.55 for value in signals["scores"].values()),
    }
    return state


def policy_node(state: Dict[str, Any]) -> Dict[str, Any]:
    state["decision"] = policy_decision(state["signals"])
    return state


def run_sequential(state: Dict[str, Any]) -> Dict[str, Any]:
    for node in (load_input_node, cv_node, vision_node, fusion_node, policy_node):
        state = node(state)
    return state


def run_langgraph(state: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    try:
        from langgraph.graph import END, StateGraph
    except Exception:
        return run_sequential(state), False

    graph = StateGraph(dict)
    graph.add_node("load_input", load_input_node)
    graph.add_node("cv", cv_node)
    graph.add_node("vision", vision_node)
    graph.add_node("fusion", fusion_node)
    graph.add_node("policy", policy_node)
    graph.set_entry_point("load_input")
    graph.add_edge("load_input", "cv")
    graph.add_edge("cv", "vision")
    graph.add_edge("vision", "fusion")
    graph.add_edge("fusion", "policy")
    graph.add_edge("policy", END)
    return graph.compile().invoke(state), True


def run_pipeline(state: Dict[str, Any], engine: str) -> Tuple[Dict[str, Any], str]:
    if engine == "langgraph":
        result, used_graph = run_langgraph(state)
        return result, "langgraph" if used_graph else "sequential-fallback"
    if engine == "auto":
        result, used_graph = run_langgraph(state)
        return result, "langgraph" if used_graph else "sequential"
    return run_sequential(state), "sequential"


def built_in_cases() -> List[Dict[str, Any]]:
    return [
        {
            "name": "military_review",
            "expected_action": "REVIEW",
            "expected_category": "military",
            "payload": {
                "cv": {"military": 0.81, "labels": ["military uniform", "rifle"]},
                "vision": {
                    "categories": ["military"],
                    "scores": {"military": 0.84},
                    "risk_level": "review",
                    "confidence": 0.84,
                    "reason": "Armed uniformed person; context unclear.",
                },
            },
        },
        {
            "name": "political_block",
            "expected_action": "BLOCK",
            "expected_category": "political",
            "payload": {
                "cv": {
                    "political": 0.91,
                    "labels": ["national emblem", "political slogan"],
                    "ocr": ["政治宣传", "国家领导人讲话"],
                }
            },
        },
        {
            "name": "nsfw_block",
            "expected_action": "BLOCK",
            "expected_category": "nsfw",
            "payload": {
                "cv": {"nsfw": 0.93, "labels": ["nudity", "explicit sexual content"]}
            },
        },
        {
            "name": "clean_pass",
            "expected_action": "PASS",
            "expected_category": None,
            "payload": {
                "cv": {"military": 0.05, "political": 0.0, "nsfw": 0.04, "labels": ["city street"]}
            },
        },
        {
            "name": "fictional_ancient_city_pass",
            "expected_action": "PASS",
            "expected_category": None,
            "payload": {
                "cv": {"labels": ["ancient watchtower", "horse", "period attire"], "ocr": ["热门漫剧 纯属虚构 请勿模仿"]},
                "vision": {
                    "categories": ["military", "political", "nsfw"],
                    "scores": {"military": 0.1, "political": 0.0, "nsfw": 0.0},
                    "risk_level": "low",
                    "confidence": 0.98,
                    "reason": "Fictional historical scene without real-world military identifiers.",
                },
            },
        },
        {
            "name": "dialogue_political_block",
            "expected_action": "BLOCK",
            "expected_category": "political",
            "payload": {
                "dialogue": [
                    {
                        "start_time": 10.2,
                        "end_time": 12.6,
                        "text": "这里出现政治宣传和分裂国家相关台词",
                    }
                ]
            },
        },
        {
            "name": "dialogue_nsfw_block",
            "expected_action": "BLOCK",
            "expected_category": "nsfw",
            "payload": {
                "dialogue": [
                    {
                        "start_time": 30.0,
                        "end_time": 31.5,
                        "text": "这里出现色情服务相关台词",
                    }
                ]
            },
        },
        {
            "name": "full_frame_visual_mask_is_rejected",
            "expected_action": "BLOCK",
            "expected_category": "nsfw",
            "expected_redaction_type": "visual_localization_required",
            "forbidden_redaction_type": "visual_mosaic",
            "payload": {
                "cv": {"nsfw": 0.91, "labels": ["nudity"]},
                "vision": {
                    "categories": ["nsfw"],
                    "scores": {"nsfw": 0.91},
                    "risk_level": "high",
                    "confidence": 0.91,
                    "reason": "Unsafe visual content.",
                    "redaction_targets": [
                        {
                            "type": "visual_mosaic",
                            "category": "nsfw",
                            "reason": "Model gave an over-broad target.",
                            "bbox": [0, 0, 1, 1],
                        }
                    ],
                },
            },
        },
        {
            "name": "local_body_part_mask_is_allowed",
            "expected_action": "REVIEW",
            "expected_category": "nsfw",
            "expected_redaction_type": "visual_mosaic",
            "payload": {
                "vision": {
                    "categories": ["nsfw"],
                    "scores": {"nsfw": 0.72},
                    "risk_level": "review",
                    "confidence": 0.72,
                    "reason": "Localized cleavage-heavy region.",
                    "redaction_targets": [
                        {
                            "type": "visual_mosaic",
                            "category": "nsfw",
                            "reason": "Only the exposed chest region needs masking.",
                            "bbox": [0.42, 0.30, 0.58, 0.45],
                        }
                    ],
                },
            },
        },
    ]


def run_self_test(engine: str) -> Dict[str, Any]:
    cases = []
    failed = 0
    used_engine = None
    for case in built_in_cases():
        state = {"payload": case["payload"], "provider": "mock"}
        result_state, actual_engine = run_pipeline(state, engine)
        used_engine = used_engine or actual_engine
        decision = result_state["decision"]
        ok = decision["action"] == case["expected_action"]
        expected_category = case["expected_category"]
        if expected_category and expected_category not in decision["categories"]:
            ok = False
        expected_redaction_type = case.get("expected_redaction_type")
        if expected_redaction_type and not any(item.get("type") == expected_redaction_type for item in decision.get("redactions", [])):
            ok = False
        forbidden_redaction_type = case.get("forbidden_redaction_type")
        if forbidden_redaction_type and any(item.get("type") == forbidden_redaction_type for item in decision.get("redactions", [])):
            ok = False
        if not ok:
            failed += 1
        cases.append(
            {
                "name": case["name"],
                "ok": ok,
                "expected_action": case["expected_action"],
                "actual_action": decision["action"],
                "actual_categories": decision["categories"],
                "decision": decision,
            }
        )
    return {
        "ok": failed == 0,
        "engine": used_engine or engine,
        "summary": {"total": len(cases), "failed": failed},
        "cases": cases,
    }


def write_or_print(result: Any, output: Optional[str]) -> None:
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def pixelate_region(image: Any, box: Tuple[int, int, int, int], blocks: int = 18) -> None:
    x1, y1, x2, y2 = box
    if x2 <= x1 or y2 <= y1:
        return
    region = image.crop(box)
    small_width = max(1, min(blocks, x2 - x1))
    small_height = max(1, min(blocks, y2 - y1))
    region = region.resize((small_width, small_height), resample=0)
    region = region.resize((x2 - x1, y2 - y1), resample=0)
    image.paste(region, box)


def apply_mask_output(input_path: Path, decision: Dict[str, Any], output_dir: Optional[str]) -> Optional[str]:
    if not output_dir or input_path.suffix.lower() == ".json":
        return None

    mask_targets = []
    for redaction in decision.get("redactions", []):
        redaction_type = redaction.get("type")
        if redaction_type not in {"visual_mosaic", "visual_blur", "text_mosaic"}:
            continue
        bbox = redaction.get("bbox")
        if redaction_type in VISUAL_MASK_TYPES and not is_local_visual_bbox(bbox, redaction.get("category")):
            continue
        if bbox:
            mask_targets.append(("bbox", bbox))

    if not mask_targets:
        return None

    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Pillow is required for --mask-output-dir image masking.") from exc

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    with Image.open(input_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        for kind, bbox in mask_targets:
            normalized = normalize_bbox(bbox)
            if not normalized:
                continue
            x1, y1, x2, y2 = normalized
            box = (
                int(round(x1 * width)),
                int(round(y1 * height)),
                int(round(x2 * width)),
                int(round(y2 * height)),
            )
            pixelate_region(image, box)
        final_path = output_path / f"{input_path.stem}_masked.jpg"
        image.save(final_path, quality=92)
    return str(final_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run visual moderation policy checks.")
    parser.add_argument("inputs", nargs="*", help="Image paths or JSON fixture paths.")
    parser.add_argument("--self-test", action="store_true", help="Run built-in regression fixtures.")
    parser.add_argument(
        "--provider",
        choices=("mock", "sidecar", "openai-compatible", "dashscope"),
        default="sidecar",
        help="Evidence provider for image inputs.",
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "sequential", "langgraph"),
        default="auto",
        help="Pipeline engine. LangGraph is optional.",
    )
    parser.add_argument("--timeout", type=int, default=60, help="VLM request timeout in seconds.")
    parser.add_argument("--model", help="Vision model override, for example qwen3-vl-flash or qwen3-vl-plus.")
    parser.add_argument("--output", help="Write JSON result to this path.")
    parser.add_argument("--mask-output-dir", help="Write masked image copies for decisions with redaction bboxes.")
    args = parser.parse_args()

    if args.self_test or not args.inputs:
        result = run_self_test(args.engine)
        write_or_print(result, args.output)
        return 0 if result["ok"] else 1

    results = []
    for raw_path in args.inputs:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(raw_path)
        state = {
            "input_path": str(path),
            "provider": args.provider,
            "timeout": args.timeout,
            "model": args.model,
        }
        result_state, engine = run_pipeline(state, args.engine)
        decision = result_state["decision"]
        masked_output = apply_mask_output(path, decision, args.mask_output_dir)
        results.append(
            {
                "input": str(path),
                "engine": engine,
                "decision": decision,
                "masked_output": masked_output,
            }
        )

    output = results[0] if len(results) == 1 else {"results": results}
    write_or_print(output, args.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
