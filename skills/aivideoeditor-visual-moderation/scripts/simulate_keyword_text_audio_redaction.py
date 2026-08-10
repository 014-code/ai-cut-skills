#!/usr/bin/env python3
"""Simulate subtitle masking and synchronized audio muting from a business keyword policy."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


MASK_CHAR = "█"
WORD_TIME_PAD_SECONDS = 0.06

KEYWORD_GROUPS: Dict[str, Sequence[str]] = {
    "色情低俗": (
        "一夜",
        "婚外",
        "偷情",
        "一夜七次",
        "第一次",
        "一夜激情",
        "堕胎",
        "打胎",
        "流产",
        "陪睡",
        "出轨",
        "借种",
        "失身",
        "把持不住",
        "酒醉失身",
        "偷种",
        "一夜怀上",
        "强行受孕",
        "自杀",
        "自尽",
        "殉情",
        "舔狗",
        "小三",
        "童养媳",
        "娃娃亲",
        "你妈逼",
        "妈的",
        "特么",
        "他妈的",
        "我靠",
        "草泥马",
        "玛德",
        "妈蛋",
        "碧池",
        "碧莲",
        "装 13",
        "装13",
        "卧槽",
        "我擦",
        "我草",
        "尼玛",
        "滚粗",
        "叼你老母",
        "浸猪笼",
        "傻逼",
        "王八蛋",
        "畜生",
        "杂种",
        "tmd",
        "nmd",
        "rnm",
        "sb",
        "屌丝",
        "土鳖",
        "狗东西",
        "贱货",
        "小婊砸",
        "绿茶婊",
        "仙人板板",
        "龟儿子",
        "哈批",
        "哈儿",
    ),
    "封建迷信": (
        "改命",
        "改运",
        "借命",
        "借运",
        "算卦",
        "气运",
        "冥婚",
        "冲喜",
        "公鸡拜堂",
        "配阴婚",
        "阴婚",
        "法轮功",
        "驱鬼",
        "招魂",
        "血光之灾",
        "消灾",
        "辟邪",
        "蛇胎",
        "鬼魂索命",
        "亡灵",
        "风水",
        "占卜",
        "旺运",
        "招财消灾",
        "起死回生",
    ),
    "涉军涉政": (
        "警察",
        "警察局",
        "派出所",
        "民政局",
        "团长",
        "营长",
        "旅长",
        "首长",
        "解放军",
        "中国",
        "中华人民共和国",
        "一国两制",
        "台独",
        "港独",
        "疆独",
        "藏独",
        "一中一台",
        "两个中国",
        "军委",
        "中央",
        "部委",
        "省委",
        "市委",
        "省政府",
        "市政府",
        "县政府",
        "区政府",
        "政府",
        "政府机关",
        "特供",
        "专供",
        "国宴专用",
        "RMDHT",
        "GYZY",
        "日本",
        "小日本",
        "日军",
        "抗日",
        "台湾",
        "香港",
        "澳门",
        "美国",
        "韩国",
        "习近平",
        "习大大",
        "毛泽东",
        "毛爷爷",
        "毛主席",
        "周恩来",
        "邓小平",
        "胡锦涛",
        "袁世凯",
    ),
    "竞品及私域导流": (
        "微信",
        "vx",
        "QQ",
        "支付宝",
        "快手",
        "红果",
        "番茄小说",
        "河马",
        "抖音",
        "小红书",
        "腾讯",
        "爱奇艺",
        "优酷",
        "芒果",
    ),
}

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "references" / "feishu_keyword_policy.json"
KEYWORD_POLICY_INFO: Dict[str, Any] = {
    "source_type": "builtin_fallback",
    "version": "builtin",
    "path": None,
}


def _dedupe_keywords(values: Iterable[Any]) -> Tuple[str, ...]:
    output: List[str] = []
    seen = set()
    for value in values:
        keyword = str(value or "").strip()
        if not keyword:
            continue
        normalized = re.sub(r"\s+", "", keyword).casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append(keyword)
    return tuple(output)


def configure_keyword_policy(path: Path | str | None = None) -> Dict[str, Any]:
    """Load a versioned Feishu policy snapshot while keeping built-ins as fallback."""
    global KEYWORD_GROUPS, KEYWORD_POLICY_INFO

    configured = path or os.environ.get("AIVIDEOEDITOR_KEYWORD_POLICY")
    policy_path = Path(configured).expanduser() if configured else DEFAULT_POLICY_PATH
    if not policy_path.is_file():
        KEYWORD_POLICY_INFO = {
            "source_type": "builtin_fallback",
            "version": "builtin",
            "path": None,
            "group_counts": {category: len(values) for category, values in KEYWORD_GROUPS.items()},
        }
        return dict(KEYWORD_POLICY_INFO)

    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    raw_groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(raw_groups, dict) or not raw_groups:
        raise ValueError(f"Keyword policy has no groups: {policy_path}")

    supplements = payload.get("local_supplements") if isinstance(payload, dict) else {}
    supplements = supplements if isinstance(supplements, dict) else {}
    merged: Dict[str, Sequence[str]] = {}
    for category, values in raw_groups.items():
        if not isinstance(values, list):
            raise ValueError(f"Keyword policy group must be a list: {category}")
        extra = supplements.get(category, [])
        if not isinstance(extra, list):
            raise ValueError(f"Keyword policy supplements must be a list: {category}")
        merged[str(category)] = _dedupe_keywords([*values, *extra])

    KEYWORD_GROUPS = merged
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    KEYWORD_POLICY_INFO = {
        "source_type": source.get("type") or "feishu_snapshot",
        "source_url": source.get("url"),
        "source_title": source.get("title"),
        "source_last_modified": source.get("last_modified"),
        "synced_at": payload.get("synced_at"),
        "version": payload.get("version") or "unversioned",
        "path": str(policy_path.resolve()),
        "group_counts": {category: len(values) for category, values in KEYWORD_GROUPS.items()},
    }
    return dict(KEYWORD_POLICY_INFO)


configure_keyword_policy()

@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    keyword: str
    category: str
    actions: Tuple[str, ...]


def normalize_keyword(value: str) -> str:
    return re.sub(r"[\s:：,，;；、.。!！?？\"'“”‘’`·_\-—()（）\[\]【】]+", "", value.casefold())


def normalized_text_with_mapping(value: str) -> Tuple[str, List[int]]:
    chars: List[str] = []
    mapping: List[int] = []
    for index, char in enumerate(value):
        normalized = normalize_keyword(char)
        if not normalized:
            continue
        for normalized_char in normalized:
            chars.append(normalized_char)
            mapping.append(index)
    return "".join(chars), mapping


def find_keyword_matches(text: str, keyword: str) -> List[Tuple[int, int]]:
    normalized_text, mapping = normalized_text_with_mapping(text)
    normalized_keyword = normalize_keyword(keyword)
    if not normalized_text or not normalized_keyword or not mapping:
        return []
    matches: List[Tuple[int, int]] = []
    for match in re.finditer(re.escape(normalized_keyword), normalized_text):
        start = mapping[match.start()]
        end = mapping[match.end() - 1] + 1
        if end > start:
            matches.append((start, end))
    return matches


def normalize_timing_value(value: Any, segment_end: float) -> Optional[float]:
    try:
        timing = float(value)
    except (TypeError, ValueError):
        return None
    if timing < 0:
        return 0.0
    if timing > 1000.0:
        timing /= 1000.0
    elif segment_end <= 60.0 and timing > max(30.0, segment_end * 4.0):
        timing /= 1000.0
    return timing


def extract_word_items(segment: Dict[str, Any], segment_end: float) -> List[Dict[str, Any]]:
    word_items: List[Dict[str, Any]] = []
    source_candidates: List[Tuple[str, Any]] = []

    frontend = segment.get("frontend")
    if isinstance(frontend, dict):
        for key in ("words", "tokens", "word_segments", "word_timestamps"):
            if isinstance(frontend.get(key), list):
                source_candidates.append((f"frontend.{key}", frontend.get(key)))

    for key in ("words", "tokens", "word_segments", "word_timestamps", "token_timestamps"):
        if isinstance(segment.get(key), list):
            source_candidates.append((key, segment.get(key)))

    for source_name, raw_items in source_candidates:
        for index, item in enumerate(raw_items or []):
            if not isinstance(item, dict):
                continue
            word = str(item.get("word") or item.get("text") or item.get("token") or item.get("value") or "").strip()
            if not word:
                continue
            start_raw = (
                item.get("start_time")
                if item.get("start_time") is not None
                else item.get("start")
                if item.get("start") is not None
                else item.get("begin")
                if item.get("begin") is not None
                else item.get("from")
            )
            end_raw = (
                item.get("end_time")
                if item.get("end_time") is not None
                else item.get("end")
                if item.get("end") is not None
                else item.get("to")
            )
            start_time = normalize_timing_value(start_raw, segment_end)
            end_time = normalize_timing_value(end_raw, segment_end)
            if start_time is None or end_time is None:
                continue
            if end_time < start_time:
                start_time, end_time = end_time, start_time
            word_items.append(
                {
                    "index": len(word_items),
                    "word": word,
                    "start_time": round(float(start_time), 3),
                    "end_time": round(float(end_time), 3),
                    "source": source_name,
                    "source_index": index,
                }
            )

    word_items.sort(key=lambda item: (float(item["start_time"]), float(item["end_time"]), int(item["index"])))
    for new_index, item in enumerate(word_items):
        item["index"] = new_index
    return word_items


def build_word_timeline(word_items: Sequence[Dict[str, Any]]) -> Tuple[str, List[int]]:
    normalized_chars: List[str] = []
    char_to_word: List[int] = []
    for word_index, item in enumerate(word_items):
        normalized_word = normalize_keyword(str(item.get("word") or ""))
        if not normalized_word:
            continue
        for normalized_char in normalized_word:
            normalized_chars.append(normalized_char)
            char_to_word.append(word_index)
    return "".join(normalized_chars), char_to_word


def resolve_hit_timing(
    text: str,
    hit: Hit,
    word_items: Sequence[Dict[str, Any]],
    default_start: float,
    default_end: float,
) -> Dict[str, Any]:
    timing_source = "segment_span"
    start_time = round(float(default_start), 3)
    end_time = round(float(default_end), 3)
    word_start_index: Optional[int] = None
    word_end_index: Optional[int] = None

    normalized_keyword = normalize_keyword(hit.keyword)
    timeline, char_to_word = build_word_timeline(word_items)
    if timeline and normalized_keyword and char_to_word:
        matches = list(re.finditer(re.escape(normalized_keyword), timeline))
        if matches:
            target_ratio = hit.start / max(1, len(text or timeline))
            timeline_length = max(1, len(timeline))
            chosen = min(matches, key=lambda match: abs((match.start() / timeline_length) - target_ratio))
            word_start_index = char_to_word[chosen.start()]
            word_end_index = char_to_word[chosen.end() - 1]
            first_word = word_items[word_start_index]
            last_word = word_items[word_end_index]
            start_time = min(float(first_word["start_time"]), float(last_word["start_time"]))
            end_time = max(float(first_word["end_time"]), float(last_word["end_time"]))
            start_time = max(0.0, start_time - WORD_TIME_PAD_SECONDS)
            end_time = end_time + WORD_TIME_PAD_SECONDS
            timing_source = "word_timestamps"

    return {
        "start_time": round(float(start_time), 3),
        "end_time": round(float(end_time), 3),
        "timing_source": timing_source,
        "word_start_index": word_start_index,
        "word_end_index": word_end_index,
    }


def load_segments(path: Path | None) -> List[Dict[str, Any]]:
    if path is None:
        return [
            {"start_time": 0.5, "end_time": 2.0, "text": "今晚他第一次向她表白，说自己一夜都睡不着。"},
            {"start_time": 2.2, "end_time": 3.7, "text": "我靠，你别再提微信和QQ了。"},
            {"start_time": 4.0, "end_time": 5.5, "text": "大师说她能改命改运，还能招财消灾。"},
            {"start_time": 5.8, "end_time": 7.2, "text": "这个故事发生在台湾和香港之间。"},
            {"start_time": 7.5, "end_time": 9.0, "text": "这句是正常对白，可以直接通过。"},
        ]
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("segments", "subtitles", "dialogue", "lines"):
            if isinstance(data.get(key), list):
                return data[key]
    if isinstance(data, list):
        return data
    raise ValueError("Transcript JSON must be a list or contain segments/subtitles/dialogue/lines.")


def find_hits(text: str) -> List[Hit]:
    all_hits: List[Hit] = []
    for category, keywords in KEYWORD_GROUPS.items():
        for keyword in keywords:
            if not keyword:
                continue
            for start, end in find_keyword_matches(text, keyword):
                all_hits.append(Hit(start, end, keyword, category, ("subtitle_mask", "audio_mute")))

    all_hits.sort(key=lambda item: (item.start, -(item.end - item.start), item.keyword))
    selected: List[Hit] = []
    occupied = [False] * max(1, len(text))
    for hit in all_hits:
        if any(occupied[index] for index in range(hit.start, min(hit.end, len(occupied)))):
            continue
        for index in range(hit.start, min(hit.end, len(occupied))):
            occupied[index] = True
        selected.append(hit)
    return sorted(selected, key=lambda item: (item.start, item.end))


def mask_text(text: str, hits: Iterable[Hit]) -> str:
    chars = list(text)
    for hit in hits:
        for index in range(hit.start, min(hit.end, len(chars))):
            if chars[index].isspace():
                continue
            chars[index] = MASK_CHAR
    return "".join(chars)


def merge_intervals(intervals: Iterable[Dict[str, Any]], max_gap: float = 0.15) -> List[Dict[str, Any]]:
    ordered = sorted(intervals, key=lambda item: (float(item["start_time"]), float(item["end_time"])))
    if not ordered:
        return []
    merged: List[Dict[str, Any]] = [dict(ordered[0])]
    for item in ordered[1:]:
        previous = merged[-1]
        if float(item["start_time"]) <= float(previous["end_time"]) + max_gap:
            previous["end_time"] = max(float(previous["end_time"]), float(item["end_time"]))
            previous["keywords"] = sorted(set(previous.get("keywords", []) + item.get("keywords", [])))
        else:
            merged.append(dict(item))
    return merged


def analyze_segments(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    output_segments: List[Dict[str, Any]] = []
    subtitle_redactions: List[Dict[str, Any]] = []
    audio_mute_candidates: List[Dict[str, Any]] = []
    word_timed_hit_count = 0
    fallback_timed_hit_count = 0

    for index, raw in enumerate(segments):
        start_time = float(raw.get("start_time", raw.get("start", 0.0)))
        end_time = float(raw.get("end_time", raw.get("end", start_time + 1.5)))
        text = str(raw.get("text", ""))
        word_items = extract_word_items(raw, end_time)
        hits = find_hits(text)
        masked = mask_text(text, hits)
        hit_payloads: List[Dict[str, Any]] = []
        for hit in hits:
            timing = resolve_hit_timing(text, hit, word_items, start_time, end_time)
            if timing["timing_source"] == "word_timestamps":
                word_timed_hit_count += 1
            else:
                fallback_timed_hit_count += 1
            hit_payloads.append(
                {
                    "keyword": hit.keyword,
                    "category": hit.category,
                    "actions": ["subtitle_mask", "audio_mute"],
                    "char_start": hit.start,
                    "char_end": hit.end,
                    "start_time": timing["start_time"],
                    "end_time": timing["end_time"],
                    "timing_source": timing["timing_source"],
                    "word_start_index": timing["word_start_index"],
                    "word_end_index": timing["word_end_index"],
                }
            )
        if hits:
            subtitle_redactions.append(
                {
                    "type": "subtitle_mask",
                    "start_time": round(min(hit["start_time"] for hit in hit_payloads), 3),
                    "end_time": round(max(hit["end_time"] for hit in hit_payloads), 3),
                    "original_text": text,
                    "masked_text": masked,
                    "hits": hit_payloads,
                }
            )
        mute_hit_payloads = list(hit_payloads)
        mute_keywords = [hit["keyword"] for hit in mute_hit_payloads]
        if mute_keywords:
            audio_mute_candidates.append(
                {
                    "type": "audio_mute",
                    "start_time": round(min(hit["start_time"] for hit in mute_hit_payloads), 3),
                    "end_time": round(max(hit["end_time"] for hit in mute_hit_payloads), 3),
                    "keywords": sorted(set(mute_keywords)),
                    "strategy": "mute_keyword_span_with_word_timestamps" if word_items else "mute_full_subtitle_segment_without_word_timestamps",
                }
            )
        output_segments.append(
            {
                "index": index,
                "start_time": round(start_time, 3),
                "end_time": round(end_time, 3),
                "text": text,
                "masked_text": masked,
                "hits": hit_payloads,
                "word_timestamps_available": bool(word_items),
                "needs_subtitle_mask": bool(hits),
                "needs_audio_mute": bool(mute_keywords),
            }
        )

    return {
        "policy": {
            "subtitle_mask": "all 必杀词 hits",
            "audio_mute": "all subtitle_mask hits",
            "mute_granularity": "word-level when words/tokens are available, otherwise segment-level",
            "keyword_source": dict(KEYWORD_POLICY_INFO),
        },
        "segments": output_segments,
        "subtitle_redactions": subtitle_redactions,
        "audio_mutes": merge_intervals(audio_mute_candidates),
        "timing_summary": {
            "word_timed_hits": word_timed_hit_count,
            "fallback_timed_hits": fallback_timed_hit_count,
            "word_timestamps_available": bool(word_timed_hit_count),
        },
    }


def active_segment(segments: List[Dict[str, Any]], timestamp: float) -> Dict[str, Any] | None:
    for segment in segments:
        if float(segment["start_time"]) <= timestamp <= float(segment["end_time"]):
            return segment
    return None


def wrap_text(draw: Any, text: str, font: Any, max_width: int) -> List[str]:
    lines: List[str] = []
    current = ""
    for char in text:
        candidate = current + char
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if current and width > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def render_preview_video(
    path: Path,
    segments: List[Dict[str, Any]],
    *,
    masked: bool,
    duration: float,
    width: int = 720,
    height: int = 1280,
    fps: int = 24,
) -> None:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    path.parent.mkdir(parents=True, exist_ok=True)
    font_path = Path("C:/Windows/Fonts/msyhbd.ttc")
    subtitle_font = ImageFont.truetype(str(font_path), 42)
    small_font = ImageFont.truetype(str(font_path), 26)
    title_font = ImageFont.truetype(str(font_path), 34)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Failed to open video writer: {path}")

    total_frames = int(math.ceil(duration * fps))
    for frame_index in range(total_frames):
        timestamp = frame_index / fps
        image = Image.new("RGB", (width, height), (22, 26, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width, 120), fill=(34, 41, 55))
        draw.text((36, 36), "处理后预览" if masked else "原始字幕预览", font=title_font, fill=(245, 245, 245))
        draw.text((width - 170, 42), f"{timestamp:04.1f}s", font=small_font, fill=(209, 213, 219))

        segment = active_segment(segments, timestamp)
        subtitle = "（无字幕）"
        muted = False
        hit_count = 0
        if segment:
            subtitle = str(segment["masked_text"] if masked else segment["text"])
            muted = bool(segment.get("needs_audio_mute"))
            hit_count = len(segment.get("hits") or [])

        draw.rounded_rectangle((38, height - 265, width - 38, height - 95), radius=18, fill=(5, 8, 20))
        lines = wrap_text(draw, subtitle, subtitle_font, width - 115)
        y = height - 235
        for line in lines[:3]:
            draw.text((58, y), line, font=subtitle_font, fill=(255, 255, 255))
            y += 54

        if masked and muted:
            draw.rounded_rectangle((56, height - 342, 240, height - 298), radius=12, fill=(185, 28, 28))
            draw.text((76, height - 337), "音频消音中", font=small_font, fill=(255, 255, 255))
        if masked and hit_count:
            draw.rounded_rectangle((56, height - 392, 260, height - 348), radius=12, fill=(30, 64, 175))
            draw.text((76, height - 387), f"字幕打码 {hit_count} 处", font=small_font, fill=(255, 255, 255))

        frame = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        writer.write(frame)
    writer.release()


def write_demo_audio(path: Path, duration: float, mutes: List[Dict[str, Any]], *, sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mute_ranges = [(float(item["start_time"]), float(item["end_time"])) for item in mutes]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for index in range(int(duration * sample_rate)):
            timestamp = index / sample_rate
            muted = any(start <= timestamp <= end for start, end in mute_ranges)
            value = 0 if muted else int(11000 * math.sin(2 * math.pi * 440 * timestamp))
            frames.extend(int(value).to_bytes(2, byteorder="little", signed=True))
        wav.writeframes(bytes(frames))


def write_original_audio(path: Path, duration: float, *, sample_rate: int = 16000) -> None:
    write_demo_audio(path, duration, [], sample_rate=sample_rate)


def write_human_report(path: Path, plan: Dict[str, Any]) -> None:
    timing_summary = plan.get("timing_summary") or {}
    lines = [
        "字幕打码与消音模拟说明",
        "",
        "规则:",
        "- 所有必杀词命中后，对字幕中的命中词做打码。",
        "- 字幕一旦打码，对应台词时间段同步做音频消音。",
        "- 优先使用 words/tokens/frontend.words 词级时间戳；没有词级时间戳时才回退到整句字幕时间段。",
        f"- 词级时间命中: {timing_summary.get('word_timed_hits', 0)}，句级兜底命中: {timing_summary.get('fallback_timed_hits', 0)}。",
        "",
        "命中明细:",
    ]
    for segment in plan["segments"]:
        if not segment["hits"]:
            continue
        actions = []
        if segment["needs_subtitle_mask"]:
            actions.append("字幕打码")
        if segment["needs_audio_mute"]:
            actions.append("音频消音")
        keywords = "、".join(hit["keyword"] for hit in segment["hits"])
        timing_tags = []
        if segment.get("word_timestamps_available"):
            timing_tags.append("词级时间")
        else:
            timing_tags.append("句级兜底")
        hit_windows = "、".join(
            f"{float(hit.get('start_time', segment['start_time'])):.2f}-{float(hit.get('end_time', segment['end_time'])):.2f} 秒"
            for hit in segment["hits"]
        )
        lines.extend(
            [
                f"- {segment['start_time']:.1f}-{segment['end_time']:.1f} 秒 | {' + '.join(actions)} | {'/'.join(timing_tags)} | 命中: {keywords}",
                f"  命中时间: {hit_windows}",
                f"  原字幕: {segment['text']}",
                f"  处理后: {segment['masked_text']}",
            ]
        )
    if not any(segment["hits"] for segment in plan["segments"]):
        lines.append("- 无命中")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate keyword-based subtitle masking and audio muting.")
    parser.add_argument("--transcript", type=Path, help="JSON transcript with start_time/end_time/text segments.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy-json", type=Path, help="Versioned Feishu keyword policy snapshot JSON.")
    parser.add_argument("--render-preview", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    configure_keyword_policy(args.policy_json)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    segments = load_segments(args.transcript)
    plan = analyze_segments(segments)
    duration = max(float(item["end_time"]) for item in plan["segments"]) + 0.8

    plan_path = output_dir / "redaction_plan.json"
    report_path = output_dir / "字幕消音模拟说明.txt"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_human_report(report_path, plan)

    artifacts = {
        "plan": str(plan_path),
        "human_report": str(report_path),
    }
    if args.render_preview:
        before_video = output_dir / "原始字幕预览.mp4"
        after_video = output_dir / "字幕打码后预览.mp4"
        render_preview_video(before_video, plan["segments"], masked=False, duration=duration)
        render_preview_video(after_video, plan["segments"], masked=True, duration=duration)
        original_audio = output_dir / "原始音频示例.wav"
        muted_audio = output_dir / "消音后音频示例.wav"
        write_original_audio(original_audio, duration)
        write_demo_audio(muted_audio, duration, plan["audio_mutes"])
        artifacts.update(
            {
                "before_video": str(before_video),
                "after_video": str(after_video),
                "original_audio": str(original_audio),
                "muted_audio": str(muted_audio),
            }
        )

    print(
        json.dumps(
            {
                "segments": len(plan["segments"]),
                "subtitle_redactions": len(plan["subtitle_redactions"]),
                "audio_mutes": len(plan["audio_mutes"]),
                "artifacts": artifacts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
