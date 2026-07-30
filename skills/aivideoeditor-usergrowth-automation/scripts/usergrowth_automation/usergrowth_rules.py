from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import unicodedata

from .usergrowth_tag_templates import default_custom_tag_template_tags, render_custom_tags_from_template

TEXT_NORMALIZATION_MAP = str.maketrans({
    "—": "-",
    "–": "-",
    "－": "-",
    "‑": "-",
    "‒": "-",
    "―": "-",
    "•": "·",
    "・": "·",
    "･": "·",
    "＆": "&",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
})

MATERIAL_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("金币音乐新high", ("LUNA_金币音乐新high", "LUNA金币音乐新high", "金币音乐新high")),
    ("金币音乐新mid", ("LUNA_金币音乐新mid", "LUNA金币音乐新mid", "金币音乐新mid")),
    ("金币音乐新", ("LUNA_金币音乐新", "LUNA金币音乐新", "金币音乐新")),
    ("金币音乐旧", ("LUNA_金币音乐旧", "LUNA金币音乐旧", "金币音乐旧")),
    ("金币下沉", ("LUNA_金币下沉", "LUNA金币下沉", "金币下沉")),
    ("金币VIP", ("LUNA_金币VIP", "LUNA金币VIP", "金币VIP", "金币兑换VIP")),
    ("金币SVIP", ("LUNA_金币SVIP", "LUNA金币SVIP", "金币SVIP", "金币抵扣开通SVIP")),
]

OPTIONAL_TAG_KEYWORDS = {
    "算法选歌": ("算法",),
    "音综": ("音综",),
    "衍生": ("衍生",),
    "量产": ("量产",),
    "钩子": ("钩子",),
    "抖舞": ("抖舞",),
}

FILE_CUSTOM_TAG_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("金币音乐旧",), ("金币歌单", "金币回捞", "大额到账", "大额连续")),
    (("金币兑换VIP", "金币VIP"), ("金币VIP",)),
    (("金币抵扣开通SVIP", "金币SVIP"), ("金币SVIP",)),
    (("金币音乐",), ("金币歌单",)),
]

CLASSIFICATION_RULES = [
    (
        {
            "音综单曲": (("音综单曲",), ("音综", "单曲")),
            "热歌": (("热歌",),),
            "流行": (("流行",),),
            "单曲": (("单曲",),),
        },
        lambda value: [
            "LUNA_音乐",
            "LUNA_单曲",
            "LUNA_" + (
                "流行" if value == "单曲" else value
            ),
        ],
    ),
    (
        {
            "常规免费听": (("常规免费听",), ("常规", "免费听")),
            "达人免费听": (("达人免费听",), ("达人", "免费听")),
            "免费听": (("免费听",),),
        },
        lambda value: [
            "LUNA_活动",
            "LUNA_免费听",
            "LUNA_" + value,
        ],
    ),
    (
        {
            "金币音乐新high": (("金币音乐新high",), ("金币音乐新", "high")),
            "金币音乐新mid": (("金币音乐新mid",), ("金币音乐新", "mid")),
            "金币音乐新": (("金币音乐新",),),
        },
        lambda value: [
            "LUNA_活动",
            "LUNA_金币",
            "LUNA_金币音乐",
            "LUNA_" + value,
        ],
    ),
    (
        {
            "金币音乐旧": (("金币音乐旧",),),
        },
        lambda value: [
            "LUNA_活动",
            "LUNA_金币",
            "LUNA_金币音乐",
            "LUNA_" + value,
        ],
    ),
]

BASE_CUSTOM_TAGS = ("未成年人已授权", "影视版权已授权", "dxzc", "汽水音乐")


@dataclass(frozen=True)
class CustomTagRule:
    """描述某类素材需要追加哪些固定标签、歌曲标签和歌曲 ID。"""

    fixed_tags: tuple[str, ...] = ()
    song_tags: tuple[str, ...] = ()
    append_song_id: bool = False


GOLD_NEW_TYPES = ("金币音乐新", "金币音乐新high", "金币音乐新mid")
GOLD_RECALL_TAGS = ("金币回捞", "大额到账", "大额连续")

CUSTOM_TAG_RULES: dict[str, CustomTagRule] = {
    # 金币音乐新系列
    **{
        material_type: CustomTagRule(
            fixed_tags=("金币歌单",),
            append_song_id=True,
        )
        for material_type in GOLD_NEW_TYPES
    },
    # 金币音乐旧
    "金币音乐旧": CustomTagRule(
        fixed_tags=("金币歌单", *GOLD_RECALL_TAGS),
        append_song_id=True,
    ),
    # VIP
    **{
        material_type: CustomTagRule(fixed_tags=("金币VIP",))
        for material_type in ("金币VIP", "金币兑换VIP")
    },
    # SVIP
    **{
        material_type: CustomTagRule(fixed_tags=("金币SVIP",))
        for material_type in ("金币SVIP", "金币抵扣开通SVIP")
    },
}
DEFAULT_CUSTOM_TAG_RULE = CustomTagRule(append_song_id=True)


def default_month_tag(now: datetime | None = None) -> str:
    """按当前年月生成类似“26年7月dxqs”的月份标签。"""
    value = now or datetime.now()
    return f"{value.year % 100}年{value.month}月dxqs"


def normalize_text(value: object) -> str:
    """归一化歌名文本，便于不同 Excel 列格式之间做匹配。"""
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    text = text.translate(TEXT_NORMALIZATION_MAP)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", text)
    text = text.replace("《", "").replace("》", "")
    text = re.sub(r"\s+", "", text)
    text = text.replace("（", "(").replace("）", ")")
    return text.lower()


def normalize_song_id(value: object) -> str:
    """把歌曲 ID 统一成 gd_数字 的格式。"""
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    text = text.translate(TEXT_NORMALIZATION_MAP)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", text)
    if not text:
        return ""
    match = re.search(r"(?:gd|gq)[_-]?(\d{8,})", text, flags=re.IGNORECASE)
    if match:
        return f"gq_{match.group(1)}"
    match = re.search(r"\b(\d{8,})\b", text)
    if match:
        return f"gq_{match.group(1)}"
    return text


def detect_material_type(file_name: str) -> str:
    """从视频文件名中识别金币音乐、金币下沉、VIP 等素材类型。"""
    for material_type, keywords in MATERIAL_KEYWORDS:
        if any(keyword in file_name for keyword in keywords):
            return material_type

    classification_keyword = _best_classification_keyword(file_name)
    if classification_keyword:
        return classification_keyword

    match = re.search(r"LUNA[_-]?([^-\s]+)", Path(file_name).stem)
    if match:
        token = match.group(1)
        token = re.sub(r"\d+$", "", token).strip("_-")
        return token
    return ""


def extract_song_name(file_name: str, material_type: str) -> str:
    """从素材文件名中截取歌曲名，并去掉尾部序号等噪声。"""
    stem = _normalize_filename_fragment(Path(file_name).stem)
    marker_index = -1
    marker_len = 0
    candidates = [material_type, f"LUNA_{material_type}", f"LUNA{material_type}"] if material_type else []
    for marker in candidates:
        index = stem.find(marker)
        if index >= 0:
            marker_index = index
            marker_len = len(marker)
            break

    if marker_index >= 0:
        tail = stem[marker_index + marker_len:]
    else:
        parts = stem.split("-")
        tail = parts[-1] if parts else stem

    tail = tail.strip("-_ ")
    stripped_tail = _strip_leading_batch_marker(tail)
    if stripped_tail != tail:
        tail = stripped_tail
    else:
        stripped_tail = _strip_single_sequence_marker(tail)
        if stripped_tail != tail:
            tail = stripped_tail
        else:
            tail = re.sub(r"^(\d+[-_]){1,3}", "", tail)
    tail = tail.strip("-_ ")
    tail = re.sub(r"[-_]\d{1,3}$", "", tail).strip("-_ ")
    tail = _strip_trailing_file_marker(tail)
    tail = _strip_explicit_book_title_marker(tail)
    return tail or stem


def _strip_leading_batch_marker(value: str) -> str:
    """去掉歌名前的分组/序号前缀，例如：闭环音乐-10-1-Joker -> Joker。"""
    text = _normalize_filename_fragment(value).strip("-_ ")
    patterns = (
        r"^.+?[-_]\s*\d{1,4}\s*[-_]\s*\d{1,4}(?:\s*[-_]\s*\d{1,4})?\s*[-_]\s*(.+)$",
        r"^.+?[-_]?\s*第?\s*\d{1,4}\s*[批组轮次]\s*[-_]?\s*第?\s*\d{1,4}\s*[条个号款]?\s*[-_]\s*(.+)$",
        r"^(?:.+?[-_])?(?:批次|批|组|轮次)\s*\d{1,4}\s*[-_]\s*(?:序号|第)?\s*\d{1,4}\s*[条个号款]?\s*[-_]\s*(.+)$",
        r"^.+?(?:[\[(【]\s*\d{1,4}\s*[\])】]){2,}\s*[-_]\s*(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            song_name = match.group(1).strip("-_ ")
            if song_name:
                return song_name
    return text


def _strip_single_sequence_marker(value: str) -> str:
    """处理 A-1-B 这类单个序号夹在两段文字之间的命名。"""
    text = _normalize_filename_fragment(value).strip("-_ ")
    parts = [part for part in re.split(r"[-_]+", text) if part]
    numeric_indexes = [index for index, part in enumerate(parts) if re.fullmatch(r"\d{1,4}", part)]
    if len(numeric_indexes) != 1:
        return text

    index = numeric_indexes[0]
    left = "-".join(parts[:index]).strip("-_ ")
    right = "-".join(parts[index + 1:]).strip("-_ ")
    if not left or not right:
        return text

    left_score = _song_fragment_score(left)
    right_score = _song_fragment_score(right)
    if left_score == right_score:
        return text
    return left if left_score > right_score else right


def _song_fragment_score(value: str) -> int:
    """用字符信息量给序号两侧片段打分，不依赖具体模板词。"""
    text = normalize_text(value)
    return len(re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text))


def _strip_explicit_book_title_marker(value: str) -> str:
    """如果候选歌名显式写成《歌名》，优先取书名号里的内容。"""
    text = str(value or "").strip("-_ ")
    text = _strip_full_outer_wrapper(text)
    if not text.startswith("《"):
        return value
    title = _extract_leading_book_title(text)
    return title or value


def _strip_full_outer_wrapper(value: str) -> str:
    text = str(value or "").strip()
    wrappers = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"))
    changed = True
    while changed:
        changed = False
        for left, right in wrappers:
            if text.startswith(left) and text.endswith(right):
                text = text[len(left):-len(right)].strip()
                changed = True
                break
    return text


def _extract_leading_book_title(value: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("《"):
        return ""
    depth = 0
    for index, char in enumerate(text):
        if char == "《":
            depth += 1
        elif char == "》":
            depth -= 1
            if depth == 0:
                return text[1:index].strip()
    return ""


def _strip_trailing_file_marker(value: str) -> str:
    """去掉文件复制、导出、成片等非歌名后缀，保留 Live/伴奏版等歌曲版本名。"""
    text = str(value or "").strip("-_ ")
    marker = r"(?:\d{1,3}|副本|copy|拷贝|成片|最终版?|完成版?|已剪辑?|剪辑版|修正版?|导出版?|无水印)"
    text = re.sub(rf"\s*[\(（\[]\s*{marker}\s*[\)）\]]\s*$", "", text, flags=re.IGNORECASE).strip("-_ ")
    text = re.sub(rf"\s*【\s*{marker}\s*】\s*$", "", text, flags=re.IGNORECASE).strip("-_ ")
    text = re.sub(rf"\s*[-_]\s*{marker}\s*$", "", text, flags=re.IGNORECASE).strip("-_ ")
    return text


def _normalize_filename_fragment(value: str) -> str:
    """把文件名中的常见分隔符和全角字符先归一化，便于提取歌名。"""
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    text = text.translate(TEXT_NORMALIZATION_MAP)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s*_\s*", "_", text)
    return text


def optional_tags_for_file(file_name: str) -> list[str]:
    """根据文件名中的关键词生成算法选歌、音综、衍生等选填标签。"""
    tags: list[str] = []
    for tag, keywords in OPTIONAL_TAG_KEYWORDS.items():
        if _best_keyword_match(file_name, keywords):
            tags.append(tag)
    return tags


def file_custom_tags_for_name(file_name: str) -> list[str]:
    """根据素材命名补充金币歌单、金币VIP 等自定义标签。"""
    tags: list[str] = []
    for keywords, extra_tags in FILE_CUSTOM_TAG_RULES:
        if _best_keyword_match(file_name, keywords):
            tags.extend(extra_tags)
    return _dedupe_tags(tags)


def classification_path_for_material(
        file_name: str
) -> list[str]:
    """
    根据素材名称生成级联选择路径
    """

    for keyword_specs, builder in CLASSIFICATION_RULES:
        matched_keyword = _best_keyword_match(file_name, keyword_specs)
        if matched_keyword:
            return builder(matched_keyword)

    return []


def custom_tags_for_material(
        material_type: str,
        song_id: str,
        file_name: str,
        *,
        month_tag: str | None = None,
        template_tags: object = None,
) -> list[str]:
    """按当前模板生成自定义标签；素材类型/文件名规则暂不参与自定义标签匹配。"""
    del material_type, file_name
    tags = default_custom_tag_template_tags() if template_tags is None else template_tags
    return render_custom_tags_from_template(
        tags,
        song_id=song_id,
        month_tag=month_tag or default_month_tag(),
    )


def _best_keyword_match(source_text: str, keyword_specs) -> str:
    """从一组关键词规格里找出当前文件名最具体的命中值。"""
    normalized_source = normalize_text(source_text)
    if isinstance(keyword_specs, dict):
        candidates = keyword_specs.items()
    else:
        candidates = ((keyword, ((keyword,),)) for keyword in keyword_specs)
    matches = [
        keyword
        for keyword, patterns in candidates
        if _keyword_matches_normalized_source(normalized_source, patterns)
    ]
    return max(matches, key=len, default="")


def _best_classification_keyword(file_name: str) -> str:
    """从所有分类规则里挑出最匹配的素材关键词。"""
    best_match = ""
    for keyword_specs, _builder in CLASSIFICATION_RULES:
        matched_keyword = _best_keyword_match(file_name, keyword_specs)
        if len(matched_keyword) > len(best_match):
            best_match = matched_keyword
    return best_match


def _keyword_matches_normalized_source(
        normalized_source: str,
        patterns,
) -> bool:
    """支持直接命中，以及由多个片段覆盖命中的乱序组合规则。"""
    for pattern in patterns:
        normalized_parts = [normalize_text(part) for part in pattern if normalize_text(part)]
        if normalized_parts and all(part in normalized_source for part in normalized_parts):
            return True
    return False


def _dedupe_tags(tags: list[str]) -> list[str]:
    """按原始顺序去掉重复标签。"""
    deduped: list[str] = []
    for tag in tags:
        if tag and tag not in deduped:
            deduped.append(tag)
    return deduped


def display_material_from_label(label: str) -> str:
    """从平台详情中的分类标签文本里提取用于回填的素材类型。"""
    value = (label or "").strip()
    if not value:
        return ""
    leaf = re.split(r"[/>\n]", value)[-1].strip()
    return leaf.removeprefix("LUNA_")
