from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path


DEFAULT_CUSTOM_TAG_TEMPLATE_NAME = "单曲模板"
DEFAULT_CUSTOM_TAG_TEMPLATE_FIXED_TAGS = (
    "未成年人已授权",
    "影视版权已授权",
    "dxzc",
    "汽水音乐",
    "{月份标签}",
    "{歌曲ID}",
)
DEFAULT_CUSTOM_TAG_TEMPLATE_OPTIONAL_TAGS: tuple[str, ...] = ()
DEFAULT_CUSTOM_TAG_TEMPLATE_TAGS = (
    *DEFAULT_CUSTOM_TAG_TEMPLATE_FIXED_TAGS,
    *DEFAULT_CUSTOM_TAG_TEMPLATE_OPTIONAL_TAGS,
)
DEFAULT_CUSTOM_TAG_TEMPLATES = {
    DEFAULT_CUSTOM_TAG_TEMPLATE_NAME: {
        "fixed_tags": list(DEFAULT_CUSTOM_TAG_TEMPLATE_FIXED_TAGS),
        "optional_tags": list(DEFAULT_CUSTOM_TAG_TEMPLATE_OPTIONAL_TAGS),
    },
}

MONTH_TAG_PLACEHOLDERS = ("{月份标签}", "{month_tag}", "{month}", "{月份}")
SONG_ID_PLACEHOLDERS = ("{歌曲ID}", "{song_id}", "{gqid}", "{gq_id}", "{歌曲id}")


def default_custom_tag_template_tags() -> list[str]:
    """返回默认自定义标签模板，调用方可安全修改返回值。"""
    return list(DEFAULT_CUSTOM_TAG_TEMPLATE_TAGS)


def default_custom_tag_template_fixed_tags() -> list[str]:
    """返回默认固定标签。"""
    return list(DEFAULT_CUSTOM_TAG_TEMPLATE_FIXED_TAGS)


def default_custom_tag_template_optional_tags() -> list[str]:
    """返回默认选填标签。"""
    return list(DEFAULT_CUSTOM_TAG_TEMPLATE_OPTIONAL_TAGS)


def default_custom_tag_templates() -> dict[str, dict[str, list[str]]]:
    """返回内置模板副本。"""
    return {name: normalise_template_payload(payload) for name, payload in DEFAULT_CUSTOM_TAG_TEMPLATES.items()}


def normalise_template_tags(tags: object) -> list[str]:
    """把 UI/JSON 里的模板标签清洗为一行一个标签。"""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw_tags = tags.replace("，", "\n").replace(",", "\n").replace("、", "\n").splitlines()
    elif isinstance(tags, Iterable):
        raw_tags = [str(tag) for tag in tags]
    else:
        raw_tags = [str(tags)]
    return _dedupe_tags(tag.strip() for tag in raw_tags if str(tag).strip())


def normalise_template_payload(value: object) -> dict[str, list[str]]:
    """把新旧模板格式统一成固定/选填两段。"""
    if isinstance(value, dict):
        fixed_tags = normalise_template_tags(
            value.get("fixed_tags", value.get("fixed", value.get("tags")))
        )
        optional_tags = normalise_template_tags(
            value.get("optional_tags", value.get("optional", value.get("extra_tags")))
        )
        return {
            "fixed_tags": fixed_tags,
            "optional_tags": optional_tags,
        }
    return {
        "fixed_tags": normalise_template_tags(value),
        "optional_tags": [],
    }


def template_fixed_tags(value: object) -> list[str]:
    """读取模板中的固定标签。"""
    return normalise_template_payload(value)["fixed_tags"]


def template_optional_tags(value: object) -> list[str]:
    """读取模板中的选填标签。"""
    return normalise_template_payload(value)["optional_tags"]


def combine_template_tags(fixed_tags: object, optional_tags: object = None) -> list[str]:
    """按固定标签在前、选填标签在后的顺序合并模板标签。"""
    return _dedupe_tags([*normalise_template_tags(fixed_tags), *normalise_template_tags(optional_tags)])


def load_usergrowth_tag_templates(path: Path) -> dict[str, dict[str, list[str]]]:
    """读取用户保存的 UserGrowth 自定义标签模板，缺失时使用内置模板。"""
    templates = default_custom_tag_templates()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return templates

    if isinstance(payload, dict):
        source = payload.get("templates", payload)
        if isinstance(source, dict):
            for name, tags in source.items():
                clean_name = str(name or "").strip()
                if clean_name:
                    templates[clean_name] = normalise_template_payload(tags)
    if DEFAULT_CUSTOM_TAG_TEMPLATE_NAME not in templates:
        templates[DEFAULT_CUSTOM_TAG_TEMPLATE_NAME] = normalise_template_payload(default_custom_tag_template_tags())
    return templates


def save_usergrowth_tag_templates(path: Path, templates: dict[str, object]) -> None:
    """保存用户可复用的自定义标签模板。"""
    payload = {
        "templates": {
            str(name).strip(): normalise_template_payload(tags)
            for name, tags in templates.items()
            if str(name).strip()
        }
    }
    if DEFAULT_CUSTOM_TAG_TEMPLATE_NAME not in payload["templates"]:
        payload["templates"][DEFAULT_CUSTOM_TAG_TEMPLATE_NAME] = normalise_template_payload(default_custom_tag_template_tags())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def render_custom_tags_from_template(
        template_tags: object,
        *,
        song_id: str = "",
        month_tag: str = "",
) -> list[str]:
    """用模板生成最终写入平台的自定义标签；歌曲 ID 缺失时跳过 ID 占位标签。"""
    tags: list[str] = []
    actual_month_tag = (month_tag or _default_month_tag()).strip()
    actual_song_id = (song_id or "").strip()
    source_tags = (
        combine_template_tags(template_fixed_tags(template_tags), template_optional_tags(template_tags))
        if isinstance(template_tags, dict)
        else normalise_template_tags(template_tags)
    )
    for raw_tag in source_tags:
        if _contains_any(raw_tag, SONG_ID_PLACEHOLDERS) and not actual_song_id:
            continue
        tag = raw_tag
        for placeholder in MONTH_TAG_PLACEHOLDERS:
            tag = tag.replace(placeholder, actual_month_tag)
        for placeholder in SONG_ID_PLACEHOLDERS:
            tag = tag.replace(placeholder, actual_song_id)
        tag = tag.strip()
        if tag:
            tags.append(tag)
    return _dedupe_tags(tags)


def _default_month_tag() -> str:
    value = datetime.now()
    return f"{value.year % 100}年{value.month}月dxqs"


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)


def _dedupe_tags(tags: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    for tag in tags:
        if tag and tag not in deduped:
            deduped.append(tag)
    return deduped
